"""Customer session/recovery boundary. Importing performs no I/O.

Reset requests queue metadata only. A separately operated worker generates tokens
in memory and sends real SMTP-over-TLS mail. HTTP never claims delivery occurred.
"""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

SESSION_SECONDS = 8 * 3600
RESET_SECONDS = 30 * 60
EMAIL = re.compile(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+')


class SecurityError(Exception):
    def __init__(self, message='Authentication is temporarily unavailable.', status=503):
        super().__init__(message)
        self.status = status


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def version(secret, password_hash):
    return hmac.new(secret.encode(), ('credential-v1:' + password_hash).encode(), hashlib.sha256).hexdigest()


@contextmanager
def locked(factory, config):
    db = factory()
    try:
        with db:
            if config.backend == 'sqlite':
                db.execute('PRAGMA busy_timeout=3000')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute("SET LOCAL lock_timeout='3s'")
                db.execute('SELECT pg_advisory_xact_lock(724182608)')
            yield db
    finally:
        db.close()


def issue_session(factory, config, secret, user):
    raw = secrets.token_urlsafe(32)
    with locked(factory, config) as db:
        current = db.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
        if not current or current['password_hash'] != user['password_hash']:
            raise SecurityError('Please sign in again.', 401)
        db.execute('DELETE FROM customer_sessions WHERE expires_at<=?', (int(time.time()),))
        db.execute('INSERT INTO customer_sessions (token_hash,customer_id,credential_version,expires_at) VALUES (?,?,?,?)',
                   (token_hash(raw), user['id'], version(secret,current['password_hash']), int(time.time())+SESSION_SECONDS))
    return raw


def valid_session(factory, secret, customer_id, raw):
    if not isinstance(raw,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',raw):
        return False
    db = factory()
    try:
        row = db.execute('''SELECT s.credential_version,s.expires_at,u.password_hash
            FROM customer_sessions s JOIN users u ON u.id=s.customer_id
            WHERE s.token_hash=? AND s.customer_id=?''', (token_hash(raw),customer_id)).fetchone()
        return bool(row and row['expires_at'] > int(time.time()) and
                    hmac.compare_digest(row['credential_version'],version(secret,row['password_hash'])))
    finally:
        db.close()


def revoke_session(factory, config, raw):
    if isinstance(raw,str):
        with locked(factory, config) as db:
            db.execute('DELETE FROM customer_sessions WHERE token_hash=?',(token_hash(raw),))


def invalidate_account(db, customer_id):
    db.execute('DELETE FROM customer_sessions WHERE customer_id=?',(customer_id,))
    db.execute('DELETE FROM password_reset_tokens WHERE customer_id=?',(customer_id,))
    db.execute('DELETE FROM password_reset_requests WHERE customer_id=?',(customer_id,))


def mail_config():
    """Explicit fixed origin and authenticated SMTP SSL; no Host-header links."""
    host = os.getenv('SMTP_HOST','')
    username,password,sender = (os.getenv(key,'') for key in ('SMTP_USERNAME','SMTP_PASSWORD','SMTP_FROM'))
    origin = os.getenv('PUBLIC_BASE_URL','').rstrip('/')
    try:
        url = urlsplit(origin)
        good = (not re.search(r'[\s\x00-\x1f\x7f\\]', origin) and url.scheme=='https' and url.hostname and url.path=='' and not url.query
                and not url.fragment and not url.username and not url.password)
        if url.port is not None and not 1 <= url.port <= 65535:
            good = False
    except ValueError:
        good = False
    if (os.getenv('PASSWORD_RESET_EMAIL_PROVIDER')!='smtp' or not good
            or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',host)
            or not username.strip() or not password.strip() or len(username)>320 or len(password)>4096
            or re.search(r'[\x00-\x1f\x7f]', username+password)
            or not EMAIL.fullmatch(sender) or len(sender)>254):
        raise SecurityError('Password recovery is not configured. Please contact support.')
    return host,username,password,sender,origin


def send_security_email(message, settings):
    """Shared server-only SMTP transport; never log message content or credentials."""
    host, username, password, _sender, _origin = settings
    with smtplib.SMTP_SSL(host,465,timeout=10,context=ssl.create_default_context()) as smtp:
        smtp.login(username,password)
        smtp.send_message(message)


def deliver_reset_emails(factory, config, secret, limit=20):
    """Explicit worker entry; no raw tokens/addresses returned or logged.

    No automatic retry of uncertain sends. A crash leaves 'sending' for operator
    review; a failed SMTP call invalidates that attempt's token. Request again to retry.
    """
    if not secret or type(limit) is not int or not 1<=limit<=100:
        raise SecurityError()
    host,username,password,sender,origin = mail_config()
    counts = {'sent':0,'failed':0,'skipped':0}
    for _ in range(limit):
        raw = secrets.token_urlsafe(32)
        digest = token_hash(raw)
        with locked(factory,config) as db:
            now = int(time.time())
            db.execute("UPDATE password_reset_requests SET state='expired' WHERE state='pending' AND queued_at<=?",(now-3600,))
            job = db.execute("SELECT * FROM password_reset_requests WHERE state='pending' ORDER BY queued_at,request_id LIMIT 1").fetchone()
            if not job:
                break
            user = db.execute('SELECT id,email,password_hash FROM users WHERE id=?',(job['customer_id'],)).fetchone() if job['customer_id'] else None
            if not user or job['credential_version'] != version(secret,user['password_hash']):
                db.execute("UPDATE password_reset_requests SET state='expired' WHERE request_id=?",(job['request_id'],))
                counts['skipped']+=1
                continue
            db.execute("UPDATE password_reset_requests SET state='sending' WHERE request_id=?",(job['request_id'],))
            db.execute('DELETE FROM password_reset_tokens WHERE expires_at<=?',(now,))
            db.execute('INSERT INTO password_reset_tokens (token_hash,customer_id,credential_version,expires_at) VALUES (?,?,?,?)',
                       (digest,user['id'],job['credential_version'],now+RESET_SECONDS))
        try:
            message = EmailMessage()
            message['From'],message['To'],message['Subject'] = sender,user['email'],'Reset your Habrivo AI password'
            message.set_content('Use this link within 30 minutes to reset your password:\n'+origin+
                                '/reset-password.html#token='+raw+'\nIf you did not request this, ignore this email.')
            send_security_email(message, (host,username,password,sender,origin))
        except Exception:
            with locked(factory,config) as db:
                db.execute('DELETE FROM password_reset_tokens WHERE token_hash=?',(digest,))
                db.execute("UPDATE password_reset_requests SET state='failed' WHERE request_id=?",(job['request_id'],))
            counts['failed']+=1
        else:
            with locked(factory,config) as db:
                db.execute("UPDATE password_reset_requests SET state='sent' WHERE request_id=?",(job['request_id'],))
            counts['sent']+=1
    return counts


def create_blueprint(factory, config, secret, read_json):
    blueprint = Blueprint('customer_recovery',__name__)

    @blueprint.post('/api/customer/forgot-password')
    def forgot():
        from free_accounts import consume_auth_attempt,AuthLimitError
        try:
            data,invalid = read_json()
            if invalid:
                raise SecurityError('Provide a valid email address.',invalid)
            if (not isinstance(data,dict) or set(data)!={'email'} or not isinstance(data['email'],str)
                    or len(data['email'])>254 or not EMAIL.fullmatch(data['email'].strip())):
                raise SecurityError('Provide a valid email address.',400)
            email = data['email'].strip().lower()
            email.encode('utf-8')
            mail_config()  # Uniform configuration error for known/unknown accounts.
            consume_auth_attempt(factory,config,secret,'recovery',request.remote_addr or 'unknown',email)
            with locked(factory,config) as db:
                user = db.execute('SELECT id,password_hash FROM users WHERE email=?',(email,)).fetchone()
                now = int(time.time())
                db.execute('DELETE FROM password_reset_requests WHERE queued_at<? AND state<>?',(now-172800,'sending'))
                # Queue indistinguishable bounded work for unknown addresses too.
                db.execute("INSERT INTO password_reset_requests (request_id,customer_id,credential_version,queued_at,state) VALUES (?,?,?,?,'pending')",
                           (secrets.token_hex(16),user['id'] if user else None,
                            version(secret,user['password_hash']) if user else None,now))
            return jsonify(success=True,message='If an eligible account exists, recovery instructions are queued for delivery. If none arrives, contact support.'),202
        except AuthLimitError as error:
            response = jsonify(success=False,message=str(error));response.headers['Retry-After']=str(error.retry_after)
            return response,429
        except SecurityError as error:
            return jsonify(success=False,message=str(error)),error.status
        except Exception:
            return jsonify(success=False,message='Password recovery is temporarily unavailable.'),503

    @blueprint.post('/api/customer/reset-password')
    def reset():
        from free_accounts import consume_auth_attempt,AuthLimitError
        try:
            data,invalid = read_json()
            if invalid:
                raise SecurityError('Invalid reset request.',invalid)
            if (not isinstance(data,dict) or set(data)!={'token','new_password'}
                    or not isinstance(data['token'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',data['token'])
                    or not isinstance(data['new_password'],str) or not 8<=len(data['new_password'])<=128):
                raise SecurityError('Invalid reset request.',400)
            data['new_password'].encode('utf-8')
            consume_auth_attempt(factory,config,secret,'reset',request.remote_addr or 'unknown',token_hash(data['token']))
            replacement = generate_password_hash(data['new_password'])
            with locked(factory,config) as db:
                row = db.execute('''SELECT t.customer_id,t.credential_version,t.expires_at,u.password_hash
                    FROM password_reset_tokens t JOIN users u ON u.id=t.customer_id WHERE token_hash=?''',
                                 (token_hash(data['token']),)).fetchone()
                if (not row or row['expires_at']<=int(time.time()) or
                        not hmac.compare_digest(row['credential_version'],version(secret,row['password_hash']))):
                    raise SecurityError('Reset link is invalid or expired. Request a new link.',400)
                updated = db.execute('UPDATE users SET password_hash=? WHERE id=? AND password_hash=?',
                                     (replacement,row['customer_id'],row['password_hash']))
                if updated.rowcount!=1:
                    raise SecurityError('Reset link is invalid or expired. Request a new link.',400)
                invalidate_account(db,row['customer_id'])
            return jsonify(success=True,message='Password reset. All customer sessions have ended. Please sign in again.')
        except AuthLimitError as error:
            response = jsonify(success=False,message=str(error));response.headers['Retry-After']=str(error.retry_after)
            return response,429
        except SecurityError as error:
            return jsonify(success=False,message=str(error)),error.status
        except Exception:
            return jsonify(success=False,message='Password reset is temporarily unavailable.'),503

    return blueprint


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Explicit queued verification/password reset SMTP worker; never run against an unauthorized database.')
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--deliver-reset-email',action='store_true')
    mode.add_argument('--deliver-verification-email',action='store_true')
    parser.add_argument('--limit',type=int,default=20)
    args=parser.parse_args()
    try:
        from database import DatabaseConfig,connect
        if os.getenv('DB_BACKEND') not in {'sqlite','postgres'}:
            raise SecurityError()
        config=DatabaseConfig.from_env()
        if config.backend=='sqlite' and (not os.getenv('DATA_DIR') or not (config.data_dir/'habrivo.db').is_file()):
            raise SecurityError()
        if args.deliver_verification_email:
            from email_verification import deliver_verification_emails
            worker = deliver_verification_emails
        else:
            worker = deliver_reset_emails
        counts=worker(lambda:connect(config),config,os.getenv('SECRET_KEY',''),args.limit)
        print('Security mail worker: sent={sent} failed={failed} skipped={skipped}'.format(**counts))
        raise SystemExit(1 if counts['failed'] else 0)
    except Exception:
        print('Security mail worker unavailable; check authorized configuration and storage.')
        raise SystemExit(1)
