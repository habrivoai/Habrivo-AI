"""Single-use email verification and lifetime trial grants; no I/O on import.

Separate from password recovery. Existing customers can verify but are ineligible
unless their account was atomically enrolled by the new signup path.
"""
import hmac
import re
import secrets
import time
from email.message import EmailMessage
from flask import Blueprint, jsonify, request, session
from customer_security import SecurityError, locked, mail_config, token_hash, send_security_email
from free_accounts import consume_auth_attempt, AuthLimitError

VERIFICATION_SECONDS = 30 * 60


def email_digest(email):
    return token_hash(email.strip().lower())


def enqueue(db, customer_id, digest):
    now = int(time.time())
    db.execute("UPDATE email_verification_requests SET state='expired' WHERE customer_id=? AND state='pending'",
               (customer_id,))
    db.execute("INSERT INTO email_verification_requests (request_id,customer_id,email_hash,queued_at,state) VALUES (?,?,?,?,'pending')",
               (secrets.token_hex(16), customer_id, digest, now))


def register_new_account(db, customer_id, email):
    """Must run in the SAME transaction as a successful new users insert."""
    digest = email_digest(email)
    db.execute('INSERT INTO email_verification_accounts (customer_id,eligible,email_hash) VALUES (?,1,?)',
               (customer_id, digest))
    enqueue(db, customer_id, digest)


def trial_status(db, customer_id):
    row = db.execute('''SELECT v.eligible,v.email_hash,v.verified_at,u.email,g.used,g.allowance
        FROM users u LEFT JOIN email_verification_accounts v ON v.customer_id=u.id
        LEFT JOIN marketing_trial_grants g ON g.customer_id=u.id WHERE u.id=?''', (customer_id,)).fetchone()
    verified = bool(row and row['verified_at'] is not None and
                    hmac.compare_digest(row['email_hash'], email_digest(row['email'])))
    eligible = bool(row and row['eligible'] == 1)
    used = row['used'] if row and row['used'] is not None else 0
    remaining = max(0, 3-used) if verified and eligible and row['allowance'] == 3 else 0
    return {'verified': verified, 'eligible': eligible, 'used': used, 'remaining': remaining}


def deliver_verification_emails(factory, config, secret, limit=20):
    """Same explicit SMTP queue pattern as recovery; no tokens in queue or output.

    Workers claim under the security lock, send outside the transaction, and do not
    retry uncertain sends. Resend is authenticated and bounded; stale tokens cannot
    produce a second grant. Never invoke from a web request.
    """
    if not secret or type(limit) is not int or not 1 <= limit <= 100:
        raise SecurityError()
    settings = mail_config()
    counts = {'sent': 0, 'failed': 0, 'skipped': 0}
    for _ in range(limit):
        raw = secrets.token_urlsafe(32)
        digest = token_hash(raw)
        with locked(factory, config) as db:
            now = int(time.time())
            db.execute("UPDATE email_verification_requests SET state='expired' WHERE state='pending' AND queued_at<=?", (now-3600,))
            job = db.execute("SELECT * FROM email_verification_requests WHERE state='pending' ORDER BY queued_at,request_id LIMIT 1").fetchone()
            if not job:
                break
            user = db.execute('''SELECT u.email,v.email_hash,v.verified_at FROM users u
                JOIN email_verification_accounts v ON u.id=v.customer_id WHERE u.id=?''', (job['customer_id'],)).fetchone()
            if (not user or user['verified_at'] is not None or
                    job['email_hash'] != email_digest(user['email']) or job['email_hash'] != user['email_hash']):
                db.execute("UPDATE email_verification_requests SET state='expired' WHERE request_id=?", (job['request_id'],))
                counts['skipped'] += 1
                continue
            # Includes automatically queued signup mail. Durable global send budget.
            # It is consumed outside this lock below to avoid nested transactions.
            db.execute("UPDATE email_verification_requests SET state='sending' WHERE request_id=?", (job['request_id'],))
            db.execute('DELETE FROM email_verification_tokens WHERE expires_at<=?', (now,))
            db.execute('DELETE FROM email_verification_tokens WHERE customer_id=?', (job['customer_id'],))
            db.execute('INSERT INTO email_verification_tokens (token_hash,customer_id,email_hash,expires_at) VALUES (?,?,?,?)',
                       (digest, job['customer_id'], job['email_hash'], now+VERIFICATION_SECONDS))
        try:
            consume_auth_attempt(factory, config, secret, 'verification-send', 'worker', str(job['customer_id']))
            message = EmailMessage()
            message['From'], message['To'], message['Subject'] = settings[3], user['email'], 'Verify your Habrivo AI email'
            message.set_content('Verify your email within 30 minutes:\n' + settings[4] + '/verify-email.html#token=' + raw +
                                '\nIf you did not create this account, ignore this email. No payment is required.')
            send_security_email(message, settings)
        except Exception:
            with locked(factory, config) as db:
                db.execute('DELETE FROM email_verification_tokens WHERE token_hash=?', (digest,))
                db.execute("UPDATE email_verification_requests SET state='failed' WHERE request_id=?", (job['request_id'],))
            counts['failed'] += 1
        else:
            with locked(factory, config) as db:
                db.execute("UPDATE email_verification_requests SET state='sent' WHERE request_id=?", (job['request_id'],))
            counts['sent'] += 1
    return counts


def create_blueprint(factory, config, secret, read_json):
    blueprint = Blueprint('email_verification', __name__)

    @blueprint.post('/api/customer/resend-verification')
    def resend():
        customer_id = session.get('customer_id')
        if not customer_id:
            return jsonify(success=False, message='Please sign in first.'), 401
        try:
            data, invalid = read_json()
            if invalid or data != {}:
                raise SecurityError('Send an empty verification request.', invalid or 400)
            try:
                mail_config()
            except SecurityError:
                raise SecurityError('Verification email is not configured. Please contact support.') from None
            consume_auth_attempt(factory, config, secret, 'verification-resend', request.remote_addr or 'unknown', str(customer_id))
            with locked(factory, config) as db:
                user = db.execute('SELECT email FROM users WHERE id=?', (customer_id,)).fetchone()
                if not user:
                    raise SecurityError('Please sign in again.', 401)
                digest = email_digest(user['email'])
                row = db.execute('SELECT verified_at,email_hash FROM email_verification_accounts WHERE customer_id=?', (customer_id,)).fetchone()
                if not row:
                    # Explicit legacy policy: verification is allowed; a trial is not.
                    db.execute('INSERT INTO email_verification_accounts (customer_id,eligible,email_hash) VALUES (?,0,?)', (customer_id, digest))
                elif row['verified_at'] is not None and row['email_hash'] == digest:
                    return jsonify(success=True, message='Email is already verified. No additional trial attempts are granted.')
                elif row['email_hash'] != digest:
                    # No email-edit workflow exists. Never silently rebind identity.
                    raise SecurityError('Email verification requires account support.', 409)
                enqueue(db, customer_id, digest)
            return jsonify(success=True, message='Verification instructions are queued, not yet delivered. Check your inbox after delivery.'), 202
        except AuthLimitError as error:
            response = jsonify(success=False, message=str(error)); response.headers['Retry-After'] = str(error.retry_after)
            return response, 429
        except SecurityError as error:
            return jsonify(success=False, message=str(error)), error.status
        except Exception:
            return jsonify(success=False, message='Verification is temporarily unavailable.'), 503

    @blueprint.post('/api/customer/verify-email')
    def verify():
        try:
            data, invalid = read_json()
            if (invalid or not isinstance(data, dict) or set(data) != {'token'} or
                    not isinstance(data['token'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', data['token'])):
                raise SecurityError('Verification link is invalid or expired. Sign in to request another.', invalid or 400)
            consume_auth_attempt(factory, config, secret, 'verification', request.remote_addr or 'unknown', '')
            with locked(factory, config) as db:
                row = db.execute('''SELECT t.customer_id,t.email_hash,t.expires_at,v.eligible,v.verified_at,u.email
                    FROM email_verification_tokens t JOIN email_verification_accounts v ON v.customer_id=t.customer_id
                    JOIN users u ON u.id=t.customer_id WHERE t.token_hash=? AND v.email_hash=t.email_hash''',
                                 (token_hash(data['token']),)).fetchone()
                if (not row or row['expires_at'] <= int(time.time()) or row['verified_at'] is not None or
                        not hmac.compare_digest(row['email_hash'], email_digest(row['email']))):
                    raise SecurityError('Verification link is invalid or expired. Sign in to request another.', 400)
                db.execute('UPDATE email_verification_accounts SET verified_at=? WHERE customer_id=? AND verified_at IS NULL',
                           (int(time.time()), row['customer_id']))
                if row['eligible'] == 1:
                    db.execute('''INSERT INTO marketing_trial_grants (customer_id,granted_at) VALUES (?,?)
                        ON CONFLICT (customer_id) DO NOTHING''', (row['customer_id'], int(time.time())))
                db.execute('DELETE FROM email_verification_tokens WHERE customer_id=?', (row['customer_id'],))
                db.execute("UPDATE email_verification_requests SET state='expired' WHERE customer_id=? AND state='pending'", (row['customer_id'],))
            return jsonify(success=True, message='Email verified. Sign in to view your account and any remaining lifetime attempts.')
        except AuthLimitError as error:
            response = jsonify(success=False, message=str(error)); response.headers['Retry-After'] = str(error.retry_after)
            return response, 429
        except SecurityError as error:
            return jsonify(success=False, message=str(error)), error.status
        except Exception:
            return jsonify(success=False, message='Verification is temporarily unavailable.'), 503

    return blueprint
