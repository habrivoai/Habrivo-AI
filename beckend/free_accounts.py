"""Free account policy and bounded authentication attempts; no I/O on import."""
from datetime import datetime, timezone
import hashlib
import hmac
import time

FREE_ACCOUNT = {'name': 'Free', 'amount': 0, 'currency': 'USD', 'payment_required': False,
                'ai_monthly_limit': 0, 'signup_url': '/start/free', 'email_verification_available': True, 'ai_lifetime_limit': 3}


class AuthLimitError(Exception):
    def __init__(self, retry_after):
        super().__init__('Too many authentication attempts. Please try again later.')
        self.retry_after = retry_after


def consume_auth_attempt(db_factory, config, secret, action, address, email):
    """Shared fixed-window limits; no raw IPs, emails, passwords or tokens stored.

    Use remote_addr supplied by Flask, never trust arbitrary forwarding headers.
    Limits apply equally to existing/unknown email addresses and successful attempts.
    """
    now = int(time.time())
    if action == 'signup':
        rules = [('signup-global', 'global', 86400, 200), ('signup-ip', address, 3600, 10)]
    elif action == 'login':
        rules = [('login-global', 'global', 3600, 500), ('login-ip', address, 900, 30),
                 ('login-pair', address + '\n' + email, 900, 10)]
    elif action == 'password':
        rules = [('password-user',email,900,10), ('password-ip',address,900,20)]
    elif action == 'recovery':
        rules = [('recovery-global','global',86400,100), ('recovery-ip',address,3600,10),
                 ('recovery-email',email,3600,3)]
    elif action == 'verification-resend':
        rules = [('verify-mail-global','global',86400,100), ('verify-mail-ip',address,3600,10),
                 ('verify-mail-account',email,3600,3)]
    elif action == 'verification-send':
        rules = [('verify-send-global','global',86400,100), ('verify-send-account',email,3600,3)]
    elif action == 'verification':
        rules = [('verify-global','global',3600,500), ('verify-ip',address,900,20)]
    elif action == 'generation':
        rules = [('generation-ip',address,900,60), ('generation-account',email,900,10)]
    elif action == 'reset':
        rules = [('reset-global','global',3600,200), ('reset-ip',address,900,20)]
    else:
        raise ValueError('Unsupported auth action')
    db = db_factory()
    try:
        with db:
            if config.backend == 'sqlite':
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute("SET LOCAL lock_timeout = '3s'")
                db.execute('SELECT pg_advisory_xact_lock(724182606)')
            db.execute('DELETE FROM account_action_limits WHERE window_start < ?', (now - 172800,))
            counters = []
            for scope, subject, seconds, maximum in rules:
                digest = hmac.new(secret.encode(), ('auth-v1:' + scope + ':' + subject).encode(), hashlib.sha256).hexdigest()
                window = now // seconds * seconds
                row = db.execute('''SELECT attempts FROM account_action_limits
                    WHERE scope = ? AND subject_hash = ? AND window_start = ?''', (scope, digest, window)).fetchone()
                if row and row[0] >= maximum:
                    raise AuthLimitError(window + seconds - now)
                counters.append((scope, digest, window))
            for counter in counters:
                db.execute('''INSERT INTO account_action_limits (scope, subject_hash, window_start, attempts)
                    VALUES (?, ?, ?, 1) ON CONFLICT (scope, subject_hash, window_start)
                    DO UPDATE SET attempts = account_action_limits.attempts + 1''', counter)
    finally:
        db.close()


def account_entitlements(db_factory, customer_id):
    """Derive access from verified server records, never browser account/plan flags."""
    from marketing_studio import PLAN_LIMITS, ProviderConfig, StudioError
    now = datetime.now(timezone.utc)
    db = db_factory()
    try:
        if not db.execute('SELECT id FROM users WHERE id = ?', (customer_id,)).fetchone():
            return None
        subscription = db.execute('''SELECT id,plan,status,payment_verified,start_date,expiry_date,billing_cycle FROM subscriptions
            WHERE customer_id = ? AND status = 'active' AND payment_verified = 1
              AND expiry_date > ? AND (start_date IS NULL OR start_date <= ?)
            ORDER BY expiry_date DESC, id DESC LIMIT 1''',
            (customer_id, now.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
        from email_verification import trial_status
        trial = trial_status(db, customer_id)
        latest = subscription or db.execute('''SELECT id,plan,status,payment_verified,start_date,expiry_date,billing_cycle
            FROM subscriptions WHERE customer_id=? ORDER BY id DESC LIMIT 1''',(customer_id,)).fetchone()
        used = db.execute('SELECT COUNT(*) FROM promo_usage WHERE customer_id = ? AND created_at >= ?',
                          (customer_id, now.strftime('%Y-%m-01 00:00:00'))).fetchone()[0]
    finally:
        db.close()
    paid = bool(subscription and subscription['plan'] in PLAN_LIMITS)
    limit = PLAN_LIMITS[subscription['plan']] if paid else 0
    status = 'none'
    if latest:
        status = latest['status'] if latest['status'] in {'pending','past_due','paused','canceled'} else 'inactive'
        if paid:
            status = 'active'
        elif latest['status']=='active':
            if latest['payment_verified']!=1:
                status = 'unverified'
            elif latest['expiry_date'] and latest['expiry_date'] <= now.strftime('%Y-%m-%d %H:%M:%S'):
                status = 'expired'
            elif latest['start_date'] and latest['start_date'] > now.strftime('%Y-%m-%d %H:%M:%S'):
                status = 'scheduled'
    subscription_info = {'status':status,
                         'plan':latest['plan'] if latest and latest['plan'] in PLAN_LIMITS else None,
                         'billing_cycle':latest['billing_cycle'] if latest else None,
                         'expiry_date':latest['expiry_date'] if latest else None}

    try:
        ProviderConfig.from_env()
        provider_ready = True
    except StudioError:
        provider_ready = False
    return {'success': True, 'account_tier': subscription['plan'] if paid else 'free',
            'account_label': subscription['plan'].title() if paid else 'Free account',
            'subscription': subscription_info,
            'payment_required_for_account': False,
            'email': {'verified': trial['verified'], 'verification_available': True,
                      'status': 'verified' if trial['verified'] else 'unverified'},
            'free_ai': {'enabled': trial['remaining'] > 0, 'monthly_limit': 0,
                        'lifetime_limit': 3 if trial['eligible'] else 0, 'used': trial['used'],
                        'remaining': trial['remaining'], 'eligible': trial['eligible'],
                        'reason': 'Three lifetime attempts after verification; never renewed. Historical accounts receive no automatic grant.'},
            'features': {'dashboard': True, 'account_settings': True, 'content_library': True,
                         'ai_generation': provider_ready and ((paid and used < limit) or (not paid and trial['remaining'] > 0))},
            'ai': {'paid_entitlement': paid, 'monthly_limit': limit, 'used': used,
                   'remaining': max(0, limit - used), 'provider_configured': provider_ready},
            'upgrade_url': '/#pricing'}
