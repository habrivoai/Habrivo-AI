"""Customer overview HTTP checks on new disposable SQLite fixtures only."""
import unittest
import test_customer_account as accounts


class CustomerDashboardTests(unittest.TestCase):
    setUp=accounts.CustomerAccountTests.setUp
    post=accounts.CustomerAccountTests.post
    get=accounts.CustomerAccountTests.get
    login=accounts.CustomerAccountTests.login

    def test_free_overview_has_no_invented_credits_and_library_remains_available(self):
        self.login()
        data=self.get('/api/customer/entitlements').get_json()
        self.assertEqual(data['subscription']['status'],'none')
        self.assertEqual(data['account_tier'],'free')
        self.assertEqual(data['ai']['monthly_limit'],0)
        self.assertFalse(data['features']['ai_generation'])
        self.assertEqual(self.get('/api/marketing/library').get_json()['items'],[])

    def test_paid_usage_and_status_are_customer_scoped_and_no_store(self):
        uid=self.login();self.post('/api/register',{'name':'Other','email':'other@example.invalid','password':'Other-password'})
        other=accounts.lookup_customer_id(self.module,'other@example.invalid')
        db=self.module.conn()
        try:
            with db:
                for customer in (uid,other):
                    db.execute("INSERT INTO subscriptions (customer_id,plan,billing_cycle,amount_usd,status,payment_verified,start_date,expiry_date) VALUES (?,'pro','yearly',699,'active',1,'2020-01-01 00:00:00','2099-01-01 00:00:00')",(customer,))
                    sid=db.execute('SELECT id FROM subscriptions WHERE customer_id=?',(customer,)).fetchone()[0]
                    for _ in range(2 if customer==uid else 5):db.execute('INSERT INTO promo_usage (customer_id,subscription_id) VALUES (?,?)',(customer,sid))
        finally:db.close()
        result=self.get('/api/customer/entitlements');data=result.get_json()
        self.assertEqual(data['ai']['used'],2)
        self.assertEqual(data['ai']['remaining'],498)
        self.assertEqual(data['subscription']['status'],'active')
        self.assertEqual(result.headers['Cache-Control'],'no-store')
        self.assertEqual(self.get('/api/promo/subscription').headers['Cache-Control'],'no-store')
        self.assertEqual(self.get('/api/customer/entitlements?customer_id='+str(other)).status_code,400)

    def test_expired_paused_unverified_and_scheduled_states_do_not_enable_ai(self):
        uid=self.login();db=self.module.conn()
        try:
            with db:db.execute("INSERT INTO subscriptions (customer_id,plan,billing_cycle,amount_usd,status,payment_verified,start_date,expiry_date) VALUES (?,'pro','yearly',699,'active',1,'2020-01-01 00:00:00','2021-01-01 00:00:00')",(uid,))
            for status,verified,start,end,expected in (
                ('active',1,'2020-01-01','2021-01-01','expired'),('paused',1,'2020-01-01','2099-01-01','paused'),
                ('active',0,'2020-01-01','2099-01-01','unverified'),('active',1,'2098-01-01','2099-01-01','scheduled')):
                with db:db.execute('UPDATE subscriptions SET status=?,payment_verified=?,start_date=?,expiry_date=?',(status,verified,start,end))
                data=self.get('/api/customer/entitlements').get_json()
                self.assertEqual(data['subscription']['status'],expected)
                self.assertFalse(data['features']['ai_generation'])
                self.assertFalse(data['ai']['paid_entitlement'])
        finally:db.close()

    def test_logout_and_refresh_cannot_return_protected_dashboard_data(self):
        self.login();cookie=self.client.get_cookie('session',domain='localhost').value
        self.assertEqual(self.post('/api/customer/logout',{}).status_code,200)
        replay=self.module.app.test_client();replay.set_cookie('session',cookie,domain='localhost')
        for path in ('/api/customer/entitlements','/api/check-auth','/api/marketing/library','/api/promo/subscription'):
            self.assertEqual(replay.get(path,base_url='https://localhost').status_code,401)
        page=self.get('/customer.html')
        try:
            self.assertEqual(page.headers['Cache-Control'],'no-store')
            self.assertIn(b'customer-dashboard.js',page.data)
            self.assertNotIn(b'one@example.invalid',page.data)
            self.assertNotIn(b'id="chatBox"',page.data)
        finally:page.close()


if __name__=='__main__':unittest.main()
