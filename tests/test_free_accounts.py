"""Real Free account/auth policy HTTP tests on disposable SQLite; no mail or AI I/O."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
from unittest.mock import patch
import test_customer_account as account_tests
import test_marketing_studio as studio_tests
from free_accounts import consume_auth_attempt


class FreeAccountTests(unittest.TestCase):
    setUp = account_tests.CustomerAccountTests.setUp
    post = account_tests.CustomerAccountTests.post
    get = account_tests.CustomerAccountTests.get
    login = account_tests.CustomerAccountTests.login
    configured = studio_tests.MarketingStudioTests.configured
    provider = studio_tests.MarketingStudioTests.provider
    subscribe = studio_tests.MarketingStudioTests.subscribe
    details = studio_tests.MarketingStudioTests.details

    def counts(self):
        db=self.module.conn()
        try:return tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('users','subscriptions','promo_usage'))
        finally:db.close()

    def test_free_entry_clears_checkout_but_creates_no_subscription_or_credits(self):
        self.get('/start?plan=pro&billing_cycle=yearly')
        with patch.object(self.module.requests,'post') as provider:
            response=self.get('/start/free')
            self.assertEqual(response.location,'/customer.html?mode=signup&account=free')
            self.assertIsNone(self.get('/api/checkout/intent').get_json()['selection'])
            self.assertEqual(self.post('/api/register',{'name':'New Free Seller','email':'free@example.invalid','password':'Synthetic-password'}).status_code,201)
            self.assertEqual(self.post('/api/customer/login',{'email':'free@example.invalid','password':'Synthetic-password'}).status_code,200)
            access=self.get('/api/customer/entitlements').get_json()
            provider.assert_not_called()
        self.assertEqual(access['account_tier'],'free')
        self.assertEqual(access['free_ai']['monthly_limit'],0)
        self.assertFalse(access['free_ai']['enabled'])
        self.assertEqual(access['features'],{'dashboard':True,'account_settings':True,'content_library':True,'ai_generation':False})
        self.assertEqual(self.get('/api/marketing/library').get_json()['items'],[])
        self.assertEqual(self.counts(),(2,0,0))

    def test_free_entitlement_cannot_be_changed_by_browser_or_environment_flags(self):
        self.login()
        with self.configured(),patch.dict(os.environ,{'FREE_AI_ENABLED':'true','FREE_AI_MONTHLY_LIMIT':'9999'}),self.provider() as api:
            access=self.get('/api/customer/entitlements').get_json()
            self.assertFalse(access['features']['ai_generation'])
            self.assertEqual(access['free_ai']['monthly_limit'],0)
            for path in ('/api/marketing/generate','/api/promo/generate','/api/chat'):
                self.assertEqual(self.post(path,self.details()).status_code,403)
            self.assertEqual(self.post('/api/paddle/checkout',{'plan':'free','billing_cycle':'monthly'}).status_code,400)
            api.assert_not_called()
        self.assertEqual(self.counts(),(1,0,0))
        for key,value in (('plan','free'),('payment_verified',True),('email_verified',True),('ai_credits',1000),('customer_id',99)):
            data={'name':'Synthetic','email':'new@example.invalid','password':'Synthetic-password',key:value}
            self.assertEqual(self.post('/api/register',data).status_code,400)
        self.assertEqual(self.counts(),(1,0,0))

    def test_signup_is_uniform_and_duplicate_does_not_reset_credentials(self):
        payload={'name':'Private Synthetic','email':'new@example.invalid','password':'Synthetic-original'}
        first=self.post('/api/register',payload)
        duplicate=self.post('/api/register',{**payload,'password':'Synthetic-other'})
        self.assertEqual(first.status_code,duplicate.status_code)
        self.assertEqual(first.get_json(),duplicate.get_json())
        self.assertNotIn('user_id',first.get_json())
        self.assertNotIn(payload['email'],first.get_data(as_text=True))
        self.assertEqual(self.post('/api/customer/login',{'email':payload['email'],'password':payload['password']}).status_code,200)
        self.assertEqual(self.post('/api/customer/login',{'email':payload['email'],'password':'Synthetic-other'}).status_code,401)
        unknown=self.post('/api/customer/login',{'email':'unknown@example.invalid','password':'Synthetic-other'})
        wrong=self.post('/api/customer/login',{'email':payload['email'],'password':'Synthetic-other'})
        self.assertEqual((unknown.status_code,unknown.get_json()),(wrong.status_code,wrong.get_json()))
        self.assertEqual(self.counts(),(2,0,0))

    def test_concurrent_duplicate_signup_returns_same_response_and_one_account(self):
        payload={'name':'Synthetic','email':'race@example.invalid','password':'Synthetic-password'}
        barrier=threading.Barrier(2)
        def register():
            client=self.module.app.test_client()
            barrier.wait(timeout=5)
            response=self.post('/api/register',payload,client)
            return response.status_code,response.get_json()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:register(),range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(results[0][0],201)
        self.assertEqual(self.counts(),(2,0,0))

    def test_signup_rate_limit_cannot_be_bypassed_by_forwarded_headers(self):
        payload={'name':'Synthetic','email':'one@example.invalid','password':self.password}
        for _ in range(9):self.assertEqual(self.post('/api/register',payload).status_code,201)
        response=self.client.post('/api/register',json=payload,base_url='https://localhost',headers={'X-Forwarded-For':'203.0.113.99'})
        self.assertEqual(response.status_code,429)
        self.assertGreater(int(response.headers['Retry-After']),0)
        self.assertEqual(self.counts(),(1,0,0))

    def test_login_limits_apply_to_existing_and_unknown_addresses(self):
        for email in ('one@example.invalid','unknown@example.invalid'):
            for _ in range(10):
                self.assertEqual(self.post('/api/customer/login',{'email':email,'password':'wrong-password'}).status_code,401)
            blocked=self.post('/api/customer/login',{'email':email,'password':'wrong-password'})
            self.assertEqual(blocked.status_code,429)
            self.assertNotIn(email,blocked.get_data(as_text=True))

    def test_limiter_retains_no_plain_ip_email_or_credentials_and_prunes_old_rows(self):
        self.login()
        db=self.module.conn()
        try:
            rows=db.execute('SELECT scope, subject_hash, window_start, attempts FROM account_action_limits').fetchall()
            rendered=str([tuple(row) for row in rows])
            for value in ('127.0.0.1','one@example.invalid',self.password):self.assertNotIn(value,rendered)
            self.assertTrue(all(len(row[1])==64 for row in rows))
            with db:db.execute('INSERT INTO account_action_limits VALUES(?,?,?,?)',('old','synthetic',0,1))
        finally:db.close()
        consume_auth_attempt(self.module.conn,self.module.DATABASE_CONFIG,self.module.app.secret_key,'login','synthetic-ip','synthetic@example.invalid')
        db=self.module.conn()
        try:self.assertIsNone(db.execute("SELECT 1 FROM account_action_limits WHERE scope='old'").fetchone())
        finally:db.close()

    def test_email_ownership_is_never_claimed_and_access_is_owner_scoped(self):
        self.assertEqual(self.get('/api/customer/entitlements').status_code,401)
        uid=self.login(); self.subscribe(uid)
        paid=self.get('/api/customer/entitlements').get_json()
        self.assertEqual(paid['email'],{'verified':False,'verification_available':True,'status':'unverified'})
        self.assertEqual(self.get('/api/customer/entitlements?customer_id='+str(uid)).status_code,400)
        self.post('/api/register',{'name':'Other','email':'other@example.invalid','password':'Other-password'})
        self.post('/api/customer/login',{'email':'other@example.invalid','password':'Other-password'})
        self.assertEqual(self.get('/api/customer/entitlements').get_json()['account_tier'],'free')

    def test_free_customer_can_upgrade_without_grants_or_changing_paid_quota(self):
        uid=self.login()
        self.get('/start?plan=pro&billing_cycle=yearly')
        self.assertEqual(self.get('/api/checkout/intent').get_json()['selection']['plan'],'pro')
        self.assertEqual(self.counts(),(1,0,0))
        self.subscribe(uid)  # Synthetic verified paid entitlement only, never done by signup.
        with self.configured(),self.provider() as provider:
            access=self.get('/api/customer/entitlements').get_json()
            self.assertEqual(access['account_tier'],'starter')
            self.assertEqual(access['ai']['monthly_limit'],100)
            self.assertTrue(access['features']['ai_generation'])
            self.assertEqual(self.post('/api/marketing/generate',self.details()).status_code,200)
            self.assertEqual(self.get('/api/customer/entitlements').get_json()['ai']['remaining'],99)
            self.get('/start/free')
            self.assertEqual(self.get('/api/customer/entitlements').get_json()['account_tier'],'starter')
            provider.assert_called_once()

    def test_malformed_auth_and_unknown_free_parameters_fail_closed(self):
        for path in ('/api/register','/api/customer/login'):
            for data in ([],False,{'password':False}):
                self.assertEqual(self.post(path,data).status_code,400)
            result=self.client.post(path,data='x'*4097,content_type='application/json',base_url='https://localhost')
            self.assertEqual(result.status_code,413)
            result=self.client.post(path,data='['*1500+']'*1500,content_type='application/json',base_url='https://localhost')
            self.assertEqual(result.status_code,400)
        self.assertEqual(self.get('/start/free?ai_credits=999').status_code,400)
        self.assertEqual(self.get('/start/free?next=https://untrusted.invalid').status_code,400)
        self.assertEqual(self.counts(),(1,0,0))

    def test_concurrent_login_attempts_cannot_bypass_the_shared_limit(self):
        for _ in range(9):
            consume_auth_attempt(self.module.conn,self.module.DATABASE_CONFIG,self.module.app.secret_key,
                                 'login','127.0.0.1','one@example.invalid')
        barrier=threading.Barrier(2)
        def attempt():
            client=self.module.app.test_client()
            barrier.wait(timeout=5)
            return self.post('/api/customer/login',{'email':'one@example.invalid','password':'wrong-password'},client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:attempt(),range(2)))
        self.assertEqual(sorted(statuses),[401,429])
        self.assertEqual(self.counts(),(1,0,0))

    def test_future_paid_start_date_cannot_bypass_free_locks(self):
        uid=self.login(); self.subscribe(uid)
        db=self.module.conn()
        try:
            with db:db.execute('UPDATE subscriptions SET start_date = ? WHERE customer_id = ?',('2098-01-01 00:00:00',uid))
        finally:db.close()
        self.assertEqual(self.get('/api/customer/entitlements').get_json()['account_tier'],'free')
        with self.configured(),self.provider() as provider:
            for route in ('/api/marketing/generate','/api/promo/generate','/api/chat'):
                self.assertEqual(self.post(route,self.details()).status_code,403)
            provider.assert_not_called()

    def test_storage_failure_is_safe_and_does_not_bypass_auth_controls(self):
        with patch.object(self.module,'connect',side_effect=RuntimeError('PRIVATE_DATABASE_URL')):
            for path,data in (('/api/register',{'name':'Synthetic','email':'new@example.invalid','password':'Synthetic-password'}),
                              ('/api/customer/login',{'email':'one@example.invalid','password':self.password})):
                response=self.post(path,data)
                self.assertEqual(response.status_code,503)
                self.assertNotIn('PRIVATE_',response.get_data(as_text=True))
        self.assertEqual(self.counts(),(1,0,0))


if __name__=='__main__':unittest.main()
