"""Real HTTP/disposable SQLite. SMTP is mocked; no email account is accessed."""
import contextlib
import io
import os
import re
import unittest
from unittest.mock import patch
import test_customer_account as accounts
import customer_security as security

MAIL={'PASSWORD_RESET_EMAIL_PROVIDER':'smtp','SMTP_HOST':'smtp.example.invalid',
      'SMTP_USERNAME':'synthetic-user','SMTP_PASSWORD':'synthetic-secret',
      'SMTP_FROM':'support@example.invalid','PUBLIC_BASE_URL':'https://app.example.invalid'}


class CustomerRecoveryTests(unittest.TestCase):
    setUp=accounts.CustomerAccountTests.setUp
    post=accounts.CustomerAccountTests.post
    get=accounts.CustomerAccountTests.get
    login=accounts.CustomerAccountTests.login

    def request_link(self,email='one@example.invalid'):
        with patch.dict(os.environ,MAIL):return self.post('/api/customer/forgot-password',{'email':email})

    def deliver(self):
        with patch.dict(os.environ,MAIL),patch.object(security.smtplib,'SMTP_SSL') as smtp:
            result=security.deliver_reset_emails(self.module.conn,self.module.DATABASE_CONFIG,self.module.app.secret_key)
            message=smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            token=re.search(r'#token=([A-Za-z0-9_-]{43})',message.get_content())[1]
            self.assertEqual(result,{'sent':1,'failed':0,'skipped':0})
            smtp.assert_called_once()
            self.assertEqual(smtp.call_args.args,('smtp.example.invalid',465))
            return token

    def test_known_and_unknown_generic_queue_response_no_token_or_delivery_claim(self):
        known=self.request_link();unknown=self.request_link('unknown@example.invalid')
        self.assertEqual((known.status_code,unknown.status_code),(202,202))
        self.assertEqual(known.get_json(),unknown.get_json())
        self.assertIn('queued',known.get_json()['message'])
        for secret in ('synthetic-secret','one@example.invalid','token='):
            self.assertNotIn(secret,known.get_data(as_text=True))
        self.assertEqual(known.headers['Cache-Control'],'no-store')

    def test_unconfigured_email_fails_uniformly_and_does_not_queue(self):
        for email in ('one@example.invalid','unknown@example.invalid'):
            self.assertEqual(self.post('/api/customer/forgot-password',{'email':email}).status_code,503)
        db=self.module.conn()
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM password_reset_requests').fetchone()[0],0)
        finally:db.close()

    def test_reset_consumes_hashed_token_invalidates_all_sessions_and_other_tokens(self):
        uid=self.login();other=accounts.authenticated_test_client(self.module,uid)
        self.assertEqual(self.request_link().status_code,202)
        older=self.deliver()
        self.assertEqual(self.request_link().status_code,202)
        token=self.deliver()
        db=self.module.conn()
        try:
            row=db.execute('SELECT * FROM password_reset_tokens WHERE token_hash=?',(security.token_hash(token),)).fetchone()
            self.assertEqual(row['token_hash'],security.token_hash(token))
            self.assertNotIn(token,str(dict(row)))
        finally:db.close()
        result=self.post('/api/customer/reset-password',{'token':token,'new_password':self.new_password})
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.get('/api/check-auth').status_code,401)
        self.assertEqual(other.get('/api/check-auth',base_url='https://localhost').status_code,401)
        self.assertEqual(self.post('/api/customer/reset-password',{'token':token,'new_password':'Another-password'}).status_code,400)
        self.assertEqual(self.post('/api/customer/reset-password',{'token':older,'new_password':'Another-password'}).status_code,400)
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.password}).status_code,401)
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.new_password}).status_code,200)

    def test_expired_invalid_and_cross_account_selectors_rejected(self):
        self.request_link();token=self.deliver()
        db=self.module.conn()
        try:
            with db:db.execute('UPDATE password_reset_tokens SET expires_at=0')
        finally:db.close()
        for data in ({'token':token,'new_password':self.new_password},
                     {'token':'x'*43,'new_password':self.new_password},
                     {'token':token,'new_password':self.new_password,'customer_id':999},
                     {'token':token,'new_password':'short'}):
            response=self.post('/api/customer/reset-password',data)
            self.assertEqual(response.status_code,400)
            self.assertNotIn(token,response.get_data(as_text=True))
        self.login()

    def test_cookie_replay_after_logout_and_absolute_session_expiry(self):
        uid=self.login()
        cookie=self.client.get_cookie('session',domain='localhost').value
        self.assertEqual(self.post('/api/customer/logout',{}).status_code,200)
        replay=self.module.app.test_client();replay.set_cookie('session',cookie,domain='localhost')
        for path in ('/api/check-auth','/api/customer/entitlements','/api/marketing/library','/api/promo/subscription'):
            self.assertEqual(replay.get(path,base_url='https://localhost').status_code,401)
        self.login()
        db=self.module.conn()
        try:
            with db:db.execute('UPDATE customer_sessions SET expires_at=0 WHERE customer_id=?',(uid,))
        finally:db.close()
        self.assertEqual(self.get('/api/check-auth').status_code,401)

    def test_reset_rate_limits_and_shared_email_limit(self):
        for _ in range(3):self.assertEqual(self.request_link().status_code,202)
        response=self.request_link()
        self.assertEqual(response.status_code,429)
        self.assertIn('Retry-After',response.headers)
        for _ in range(20):self.assertEqual(self.post('/api/customer/reset-password',{'token':'x'*43,'new_password':self.new_password}).status_code,400)
        self.assertEqual(self.post('/api/customer/reset-password',{'token':'y'*43,'new_password':self.new_password}).status_code,429)

    def test_mail_failure_invalidates_token_without_private_logs(self):
        self.request_link();output=io.StringIO()
        with patch.dict(os.environ,MAIL),patch.object(security.smtplib,'SMTP_SSL',side_effect=RuntimeError('PRIVATE_PROVIDER_ERROR')),contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
            counts=security.deliver_reset_emails(self.module.conn,self.module.DATABASE_CONFIG,self.module.app.secret_key)
        self.assertEqual(counts['failed'],1)
        self.assertEqual(output.getvalue(),'')
        db=self.module.conn()
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM password_reset_tokens').fetchone()[0],0)
        finally:db.close()

    def test_password_change_revokes_other_sessions_and_pending_links(self):
        uid=self.login();other=accounts.authenticated_test_client(self.module,uid)
        self.request_link();token=self.deliver()
        self.assertEqual(self.post('/api/customer/password',{'current_password':self.password,'new_password':self.new_password}).status_code,200)
        self.assertEqual(other.get('/api/check-auth',base_url='https://localhost').status_code,401)
        self.assertEqual(self.post('/api/customer/reset-password',{'token':token,'new_password':'Other-password'}).status_code,400)

    def test_public_pages_reject_query_authentication_and_protect_reset_fragment(self):
        for path in ('/forgot-password.html','/reset-password.html'):
            response=self.get(path+'?_ptxn=untrusted&customer_id=1')
            try:
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
                self.assertEqual(response.headers['Cache-Control'],'no-store')
                self.assertIn(b'password-recovery.js',response.data)
            finally:response.close()
        self.assertEqual(self.get('/api/check-auth?_ptxn=untrusted').status_code,401)
        home=self.get('/customer.html')
        try:self.assertIn(b'Forgot Password?',home.data)
        finally:home.close()

    def test_reset_token_cannot_change_another_customer(self):
        self.post('/api/register',{'name':'Other','email':'other@example.invalid','password':'Other-password'})
        self.request_link();token=self.deliver()
        self.assertEqual(self.post('/api/customer/reset-password',{'token':token,'new_password':self.new_password}).status_code,200)
        self.assertEqual(self.post('/api/customer/login',{'email':'other@example.invalid','password':'Other-password'}).status_code,200)

    def test_concurrent_reset_is_single_use_and_password_change_is_rate_limited(self):
        from concurrent.futures import ThreadPoolExecutor
        self.request_link();token=self.deliver()
        def reset(_):
            return self.post('/api/customer/reset-password',{'token':token,'new_password':self.new_password},
                             self.module.app.test_client()).status_code
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(reset,range(2))),[200,400])
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.new_password}).status_code,200)
        for _ in range(10):
            self.assertEqual(self.post('/api/customer/password',{'current_password':'incorrect','new_password':'Another-password'}).status_code,403)
        self.assertEqual(self.post('/api/customer/password',{'current_password':'incorrect','new_password':'Another-password'}).status_code,429)

    def test_mail_origin_validation_and_unknown_requests_send_nothing(self):
        self.request_link('unknown@example.invalid')
        with patch.dict(os.environ,MAIL),patch.object(security.smtplib,'SMTP_SSL') as smtp:
            counts=security.deliver_reset_emails(self.module.conn,self.module.DATABASE_CONFIG,self.module.app.secret_key)
            self.assertEqual(counts['skipped'],1);smtp.assert_not_called()
        for origin in ('http://app.example.invalid','https://user:secret@example.invalid','https://app.example.invalid/path','https://app.example.invalid?token=bad'):
            with patch.dict(os.environ,{**MAIL,'PUBLIC_BASE_URL':origin}):
                self.assertEqual(self.post('/api/customer/forgot-password',{'email':'one@example.invalid'}).status_code,503)


if __name__=='__main__':unittest.main()
