"""Synthetic disposable SQLite HTTP tests. SMTP and paid AI are always mocked."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import os
import re
import sqlite3
import threading
import unittest
from unittest.mock import patch
import test_customer_account as accounts
import test_customer_recovery as recovery
import test_marketing_studio as studio_tests
import customer_security as security
import email_verification as verification
import marketing_studio as studio


class EmailTrialTests(unittest.TestCase):
    setUp = accounts.CustomerAccountTests.setUp
    login = accounts.CustomerAccountTests.login
    post = accounts.CustomerAccountTests.post
    get = accounts.CustomerAccountTests.get
    configured = studio_tests.MarketingStudioTests.configured
    provider = studio_tests.MarketingStudioTests.provider
    details = studio_tests.MarketingStudioTests.details
    subscribe = studio_tests.MarketingStudioTests.subscribe

    def query(self, sql, parameters=()):
        db = self.module.conn()
        try:
            with db:
                return [dict(row) for row in db.execute(sql, parameters).fetchall()]
        finally:
            db.close()

    def deliver(self):
        with patch.dict(os.environ, recovery.MAIL), patch.object(security.smtplib, 'SMTP_SSL') as smtp:
            counts = verification.deliver_verification_emails(self.module.conn, self.module.DATABASE_CONFIG, self.module.app.secret_key)
            self.assertEqual(counts, {'sent':1, 'failed':0, 'skipped':0})
            smtp.assert_called_once()
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertIn('https://app.example.invalid/verify-email.html#token=', message.get_content())
            self.assertNotIn('localhost', message.get_content())
            return re.search(r'#token=([A-Za-z0-9_-]{43})', message.get_content())[1]

    def verified(self):
        uid = self.login()
        token = self.deliver()
        # Verification works without an authenticated browser session.
        result = self.post('/api/customer/verify-email', {'token':token}, self.module.app.test_client())
        self.assertEqual(result.status_code, 200)
        return uid, token

    def age(self):
        self.query("UPDATE marketing_trial_usage SET created_at='2000-01-01 00:00:00'")

    def test_new_signup_is_unverified_without_usable_credit_or_payment(self):
        self.login()
        state = self.get('/api/customer/entitlements').get_json()
        self.assertFalse(state['email']['verified'])
        self.assertEqual(state['free_ai']['remaining'], 0)
        self.assertFalse(state['features']['ai_generation'])
        self.assertEqual(self.query('SELECT * FROM marketing_trial_grants'), [])
        self.assertEqual(self.query('SELECT * FROM subscriptions'), [])
        with self.configured(), self.provider() as provider:
            result = self.post('/api/marketing/generate', self.details())
            self.assertEqual(result.status_code, 403)
            self.assertFalse(result.get_json()['attempt_counted'])
            provider.assert_not_called()
        self.assertEqual(self.query('SELECT * FROM marketing_trial_usage'), [])

    def test_verified_single_use_hashed_token_unlocks_exactly_one_grant(self):
        uid, token = self.verified()
        self.assertEqual(self.query('SELECT * FROM email_verification_tokens'), [])
        self.assertEqual(self.query('SELECT used,allowance FROM marketing_trial_grants'), [{'used':0, 'allowance':3}])
        with self.configured():
            access = self.get('/api/customer/entitlements').get_json()
        self.assertTrue(access['email']['verified'])
        self.assertEqual(access['free_ai']['remaining'], 3)
        self.assertTrue(access['features']['ai_generation'])
        replay = self.post('/api/customer/verify-email', {'token':token})
        self.assertEqual(replay.status_code, 400)
        self.assertNotIn(token, replay.get_data(as_text=True))
        with patch.dict(os.environ, recovery.MAIL):
            self.assertEqual(self.post('/api/customer/resend-verification', {}).status_code, 200)
        self.assertEqual(len(self.query('SELECT * FROM marketing_trial_grants WHERE customer_id=?', (uid,))), 1)

    def test_resend_config_queue_rate_limit_and_no_token_disclosure(self):
        self.assertEqual(self.post('/api/customer/resend-verification', {}).status_code, 401)
        self.login()
        result = self.post('/api/customer/resend-verification', {})
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('delivered', result.get_json()['message'])
        with patch.dict(os.environ, recovery.MAIL):
            for _ in range(3):
                result = self.post('/api/customer/resend-verification', {})
                self.assertEqual(result.status_code, 202)
                for private in ('token=', 'one@example.invalid', 'synthetic-secret'):
                    self.assertNotIn(private, result.get_data(as_text=True))
            self.assertEqual(self.post('/api/customer/resend-verification', {}).status_code, 429)
        self.assertEqual(len(self.query("SELECT * FROM email_verification_requests WHERE state='pending'")), 1)
        token = self.deliver()
        rows = self.query('SELECT * FROM email_verification_tokens')
        self.assertEqual(rows[0]['token_hash'], security.token_hash(token))
        self.assertNotIn(token, str(rows))
        self.assertEqual(self.query('SELECT * FROM marketing_trial_grants'), [])

    def test_expiration_malformed_cross_account_and_unknown_tokens_fail(self):
        uid = self.login(); token = self.deliver()
        self.query('UPDATE email_verification_tokens SET expires_at=0')
        for data in ({'token':token}, {'token':'x'*43}, {'token':False}, {'token':token,'customer_id':uid}, []):
            result = self.post('/api/customer/verify-email', data)
            self.assertEqual(result.status_code, 400)
            self.assertNotIn(token, result.get_data(as_text=True))
        self.assertEqual(self.query('SELECT * FROM marketing_trial_grants'), [])

    def test_concurrent_verification_grants_once(self):
        self.login(); token = self.deliver(); barrier = threading.Barrier(2)
        def verify(_):
            client = self.module.app.test_client(); barrier.wait(timeout=5)
            return self.post('/api/customer/verify-email', {'token':token}, client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(verify, range(2)))
        self.assertEqual(sorted(statuses), [200,400])
        self.assertEqual(len(self.query('SELECT * FROM marketing_trial_grants')), 1)

    def test_three_lifetime_attempts_then_fourth_denied_across_months_and_login(self):
        uid, _ = self.verified()
        with self.configured(), self.provider() as provider:
            for attempt in range(1,4):
                result = self.post('/api/marketing/generate', self.details())
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.get_json()['quota'], {'used':attempt,'limit':3,'period':'lifetime'})
                self.assertEqual(self.get('/api/customer/entitlements').get_json()['free_ai']['remaining'], 3-attempt)
                self.age()  # Cross calendar-month and cooldown boundaries, not grant state.
            self.post('/api/customer/logout', {}); self.login()
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 403)
            self.assertEqual(provider.call_count, 3)
        self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':3}])
        self.assertEqual(len(self.query('SELECT * FROM marketing_trial_usage')), 3)
        self.assertEqual(self.query('SELECT * FROM promo_usage'), [])

    def test_concurrent_final_credit_cannot_overuse(self):
        uid, _ = self.verified()
        with self.configured(), self.provider():
            for _ in range(2):
                self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 200); self.age()
        clients = [accounts.authenticated_test_client(self.module,uid) for _ in range(2)]
        barrier = threading.Barrier(2)
        def generate(index):
            barrier.wait(timeout=5)
            return self.post('/api/marketing/generate', self.details(), clients[index]).status_code
        with self.configured(), self.provider() as provider, ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(generate, range(2)))
        self.assertEqual(sorted(statuses), [200,403])
        provider.assert_called_once()
        self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':3}])

    def test_provider_failure_charged_but_missing_config_and_invalid_input_not_charged(self):
        self.verified()
        with self.provider() as provider:
            result = self.post('/api/marketing/generate', self.details())
            self.assertEqual(result.status_code, 503); provider.assert_not_called()
        with self.configured(), self.provider(status=429) as provider:
            self.assertEqual(self.post('/api/marketing/generate', {}).status_code, 400)
            result = self.post('/api/marketing/generate', self.details())
            self.assertEqual(result.status_code, 503)
            self.assertTrue(result.get_json()['attempt_counted'])
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 429)
            provider.assert_called_once()
        self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':1}])

    def test_password_recovery_is_separate_and_does_not_verify_or_regrant(self):
        self.login()
        helper = recovery.CustomerRecoveryTests()
        helper.module, helper.client = self.module, self.client
        helper.post = self.post
        helper.request_link(); token = helper.deliver()
        self.assertEqual(self.post('/api/customer/reset-password', {'token':token,'new_password':self.new_password}).status_code, 200)
        self.assertIsNone(self.query('SELECT verified_at FROM email_verification_accounts')[0]['verified_at'])
        self.assertEqual(self.query('SELECT * FROM marketing_trial_grants'), [])
        self.post('/api/customer/login', {'email':'one@example.invalid','password':self.new_password})
        token = self.deliver()
        self.assertEqual(self.post('/api/customer/verify-email', {'token':token}).status_code, 200)
        with self.configured(), self.provider():
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 200)
        helper.request_link(); reset = helper.deliver()
        self.post('/api/customer/reset-password', {'token':reset,'new_password':self.password})
        self.login()
        self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':1}])

    def test_legacy_verification_never_grants_trial_and_schema_reinit_preserves_history(self):
        from database import init_sqlite_schema
        uid = self.login()
        # Synthetic historical account has no enrollment row.
        self.query('DELETE FROM email_verification_requests')
        self.query('DELETE FROM email_verification_accounts')
        init_sqlite_schema(self.module.DATABASE_CONFIG)
        self.assertEqual(self.query('SELECT * FROM email_verification_accounts'), [])
        with patch.dict(os.environ, recovery.MAIL):
            self.assertEqual(self.post('/api/customer/resend-verification', {}).status_code, 202)
        token = self.deliver()
        self.assertEqual(self.post('/api/customer/verify-email', {'token':token}).status_code, 200)
        self.assertEqual(self.query('SELECT * FROM marketing_trial_grants'), [])
        self.assertFalse(self.get('/api/customer/entitlements').get_json()['free_ai']['eligible'])
        with self.configured(), self.provider() as provider:
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 403)
            provider.assert_not_called()

    def test_paid_upgrade_and_expiry_do_not_reset_or_spend_trial(self):
        uid, _ = self.verified(); self.subscribe(uid)
        with self.configured(), self.provider():
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 200)
            self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':0}])
            self.query("UPDATE subscriptions SET expiry_date='2000-01-01 00:00:00'")
            # Shared cooldown prevents switching ledgers to bypass throttling.
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 429)
            self.query("UPDATE promo_usage SET created_at='2000-01-01 00:00:00'")
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 200)
        self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':1}])

    def test_shared_daily_cap_counts_trial_and_paid_and_library_save_is_free(self):
        self.verified()
        with self.configured(), self.provider() as provider:
            result = self.post('/api/marketing/generate', self.details()).get_json()
            data = {'details':self.details(), 'outputs':result['outputs'], 'save_token':result['save_token']}
            self.assertEqual(self.post('/api/marketing/library', data).status_code, 201)
            self.assertEqual(self.post('/api/marketing/library', data).status_code, 200)
            provider.assert_called_once()
        self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':1}])
        # A different paid customer cannot exceed capacity consumed by the trial.
        self.post('/api/register', {'name':'Other','email':'other@example.invalid','password':self.password})
        self.post('/api/customer/login', {'email':'other@example.invalid','password':self.password})
        other = accounts.lookup_customer_id(self.module,'other@example.invalid'); self.subscribe(other)
        with self.configured(), patch.dict(os.environ,{'MARKETING_AI_DAILY_LIMIT':'1'}), self.provider() as provider:
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 429)
            provider.assert_not_called()
        self.assertFalse(self.get('/api/customer/entitlements').get_json()['email']['verified'])
        self.assertEqual(self.get('/api/marketing/library').get_json()['items'], [])

    def test_mail_failure_has_no_token_grant_or_private_diagnostics(self):
        captured = io.StringIO()
        with patch.dict(os.environ,recovery.MAIL), patch.object(security.smtplib,'SMTP_SSL',side_effect=RuntimeError('PRIVATE_SECRET')), contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            counts = verification.deliver_verification_emails(self.module.conn,self.module.DATABASE_CONFIG,self.module.app.secret_key)
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(captured.getvalue(), '')
        self.assertEqual(self.query('SELECT * FROM email_verification_tokens'), [])
        self.assertEqual(self.query('SELECT * FROM marketing_trial_grants'), [])

    def test_verification_rollback_retry_is_atomic(self):
        self.login(); token = self.deliver()
        self.query("CREATE TRIGGER block_grant BEFORE INSERT ON marketing_trial_grants BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        self.assertEqual(self.post('/api/customer/verify-email', {'token':token}).status_code, 503)
        self.assertIsNone(self.query('SELECT verified_at FROM email_verification_accounts')[0]['verified_at'])
        self.assertEqual(len(self.query('SELECT * FROM email_verification_tokens')), 1)
        self.query('DROP TRIGGER block_grant')
        self.assertEqual(self.post('/api/customer/verify-email', {'token':token}).status_code, 200)

    def test_reservation_rollback_preserves_counter_and_ledger(self):
        self.verified()
        self.query("CREATE TRIGGER block_trial BEFORE INSERT ON marketing_trial_usage BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.configured(), self.provider() as provider:
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 503)
            provider.assert_not_called()
        self.assertEqual(self.query('SELECT used FROM marketing_trial_grants'), [{'used':0}])
        self.assertEqual(self.query('SELECT * FROM marketing_trial_usage'), [])

    def test_forged_host_and_origin_configuration_never_control_mail_links(self):
        self.login()
        with patch.dict(os.environ, recovery.MAIL):
            result = self.client.post('/api/customer/resend-verification', json={},base_url='https://localhost',headers={'X-Forwarded-Host':'evil.invalid'})
            self.assertEqual(result.status_code, 202)
        self.deliver()  # Asserts fixed configured origin.
        with patch.dict(os.environ,{**recovery.MAIL,'PUBLIC_BASE_URL':'http://evil.invalid'}):
            self.assertEqual(self.post('/api/customer/resend-verification', {}).status_code, 503)

    def test_generation_and_verification_rate_limits(self):
        self.login()
        for _ in range(20):
            self.assertEqual(self.post('/api/customer/verify-email', {'token':'x'*43}).status_code, 400)
        self.assertEqual(self.post('/api/customer/verify-email', {'token':'x'*43}).status_code, 429)
        with self.configured(), self.provider() as provider:
            for _ in range(10): self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 403)
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 429)
            provider.assert_not_called()


    def test_duplicate_signup_password_change_and_reinit_never_regrant(self):
        from database import init_sqlite_schema
        uid, token = self.verified()
        with self.configured(), self.provider():
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 200)
        self.assertEqual(self.post('/api/register', {'name':'Other name','email':'one@example.invalid','password':'Different-password'}).status_code, 201)
        self.assertEqual(self.post('/api/customer/password', {'current_password':self.password,'new_password':self.new_password}).status_code, 200)
        init_sqlite_schema(self.module.DATABASE_CONFIG)
        self.assertEqual(self.post('/api/customer/verify-email', {'token':token}).status_code, 400)
        self.assertEqual(self.query('SELECT used,allowance FROM marketing_trial_grants'), [{'used':1,'allowance':3}])
        for sql in ('UPDATE marketing_trial_grants SET used=4', 'UPDATE marketing_trial_grants SET used=-1',
                    'UPDATE marketing_trial_grants SET allowance=999'):
            with self.assertRaises(sqlite3.IntegrityError): self.query(sql)
        with self.assertRaises(sqlite3.IntegrityError):
            self.query('INSERT INTO marketing_trial_grants(customer_id,granted_at) VALUES (?,0)', (uid,))

    def test_bearer_verifies_only_its_account_not_current_browser_identity(self):
        uid = self.login(); token = self.deliver()
        self.post('/api/register', {'name':'Other','email':'other@example.invalid','password':self.password})
        self.post('/api/customer/login', {'email':'other@example.invalid','password':self.password})
        self.assertEqual(self.post('/api/customer/verify-email', {'token':token}).status_code, 200)
        access = self.get('/api/customer/entitlements').get_json()
        self.assertFalse(access['email']['verified'])
        self.assertEqual(access['free_ai']['remaining'], 0)
        self.assertEqual(self.query('SELECT customer_id FROM marketing_trial_grants'), [{'customer_id':uid}])

    def test_signup_enrollment_failure_rolls_back_new_account(self):
        self.query("CREATE TRIGGER block_enrollment BEFORE INSERT ON email_verification_accounts BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        result = self.post('/api/register', {'name':'New','email':'new@example.invalid','password':self.password})
        # Registration intentionally hides all integrity conflicts. There must be
        # no partially enrolled user regardless of the generic response.
        self.assertEqual(self.query("SELECT id FROM users WHERE email='new@example.invalid'"), [])
        self.assertEqual(self.query('SELECT * FROM marketing_trial_grants'), [])


if __name__ == '__main__':
    unittest.main()
