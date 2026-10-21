"""Public discovery and checkout continuation on isolated temporary SQLite."""
import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch
import test_customer_account as account_tests


class PublicJourneyTests(unittest.TestCase):
    setUp = account_tests.CustomerAccountTests.setUp
    post = account_tests.CustomerAccountTests.post
    get = account_tests.CustomerAccountTests.get
    login = account_tests.CustomerAccountTests.login

    def configured(self):
        stack=contextlib.ExitStack()
        stack.enter_context(patch.object(self.module,'PADDLE_API_KEY','synthetic-private-key'))
        stack.enter_context(patch.dict(self.module.PADDLE_PRICES['pro'],{'yearly':'pri_pppppppppppppppppppppppppp'}))
        stack.enter_context(patch.object(self.module,'PADDLE_WEBHOOK_SECRET','synthetic-webhook-secret'))
        stack.enter_context(patch.dict(self.module.PADDLE_PRODUCTS,{'pro':'pro_'+'p'*26}))
        return stack

    def test_home_features_pricing_are_public_and_do_not_query_customer_storage(self):
        with patch.object(self.module,'conn',side_effect=AssertionError('Public pages must not query storage')):
            home=self.get('/')
            try:
                self.assertEqual(home.status_code,200)
                for text in (b'Habrivo AI Marketing Studio',b'id="features"',b'id="pricing"',b'/public-pricing.js'):
                    self.assertIn(text,home.data)
                self.assertNotIn(b'Habrivo Admin Login',home.data)
            finally:home.close()
            result=self.get('/api/promo/plans')
            self.assertEqual(result.status_code,200)
            data=result.get_json()
            self.assertEqual(data['currency'],'USD')
            for key,plan in self.module.PROMO_PLANS.items():
                self.assertEqual(data['plans'][key]['prices'],plan['prices'])
                self.assertEqual(data['plans'][key]['monthly_limit'],plan['monthly_limit'])
            self.assertEqual(data['entitlements']['saved_packs'],200)
            self.assertEqual(self.get('/pricing').location,'/#pricing')
        admin=self.get('/admin.html')
        try:
            self.assertIn(b'Habrivo Admin Login',admin.data)
            self.assertIn(b'/api/admin/check-auth',admin.data)
        finally:admin.close()

    def test_plan_selection_survives_signup_and_login_without_activation(self):
        response=self.get('/start?plan=pro&billing_cycle=yearly&mode=signup')
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.location,'/customer.html?mode=signup')
        selected=self.get('/api/checkout/intent').get_json()
        self.assertEqual(selected['selection']['amount'],699)
        self.assertFalse(selected['authenticated'])
        payload={'name':'New Seller','email':'seller@example.invalid','password':'Synthetic-password'}
        self.assertEqual(self.post('/api/register',payload).status_code,201)
        self.assertEqual(self.post('/api/customer/login',{'email':payload['email'],'password':payload['password']}).status_code,200)
        selected=self.get('/api/checkout/intent').get_json()
        self.assertTrue(selected['authenticated'])
        self.assertEqual(selected['selection']['plan'],'pro')
        self.assertEqual(selected['selection']['billing_cycle'],'yearly')
        self.assertEqual(selected['selection']['environment'],'sandbox')
        with self.client.session_transaction(base_url='https://localhost') as session:
            self.assertEqual(session['checkout_intent'],{'plan':'pro','billing_cycle':'yearly'})
        db=self.module.conn()
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0],0)
        finally:db.close()
        self.assertEqual(self.get('/api/checkout/intent').headers['Cache-Control'],'no-store')
        self.post('/api/customer/logout',{})
        self.assertIsNone(self.get('/api/checkout/intent').get_json()['selection'])

    def test_invalid_selection_and_redirect_parameters_are_rejected(self):
        for query in ('plan=unknown&billing_cycle=monthly','plan=pro&billing_cycle=weekly',
                      'plan=pro&billing_cycle=yearly&next=https://untrusted.invalid',
                      'plan=pro&billing_cycle=yearly&mode=untrusted',
                      'plan=pro&plan=starter&billing_cycle=yearly',
                      'plan=pro&billing_cycle=yearly&price=1'):
            with self.subTest(query=query):
                self.assertEqual(self.get('/start?'+query).status_code,400)
        self.assertIsNone(self.get('/api/checkout/intent').get_json()['selection'])

    def test_pricing_configuration_failure_has_no_fallback_offers(self):
        bad={**self.module.PROMO_PLANS['starter'],'monthly_limit':999}
        with patch.dict(self.module.PROMO_PLANS,{'starter':bad}):
            result=self.get('/api/promo/plans')
            self.assertEqual(result.status_code,503)
            self.assertNotIn('plans',result.get_json())
            self.assertEqual(self.get('/start?plan=pro&billing_cycle=yearly').status_code,503)

    def test_missing_checkout_configuration_fails_without_external_calls(self):
        self.login()
        self.get('/start?plan=pro&billing_cycle=yearly')
        with patch.object(self.module.requests,'post') as provider:
            result=self.post('/api/paddle/checkout',{'plan':'pro','billing_cycle':'yearly'})
            self.assertEqual(result.status_code,503)
            provider.assert_not_called()
        self.assertEqual(self.get('/api/checkout/intent').get_json()['selection']['plan'],'pro')

    def test_checkout_uses_server_price_and_buyer_metadata_without_activating(self):
        uid=self.login()
        self.get('/start?plan=pro&billing_cycle=yearly')
        provider=MagicMock(status_code=201)
        provider.json.return_value={'data':{'id':'txn_tttttttttttttttttttttttttt','checkout':{'url':'https://sandbox-pay.paddle.io/checkout/synthetic'}}}
        with self.configured(),patch.object(self.module.requests,'post',return_value=provider) as api:
            public=self.get('/api/promo/plans').get_json()
            self.assertTrue(public['plans']['pro']['checkout_available']['yearly'])
            self.assertNotIn('synthetic-private-key',str(public))
            self.assertNotIn('pri_synthetic',str(public))
            result=self.post('/api/paddle/checkout',{'plan':'pro','billing_cycle':'yearly'})
            self.assertEqual(result.status_code,200)
            request=api.call_args.kwargs['json']
            self.assertEqual(request['items'],[{'price_id':'pri_pppppppppppppppppppppppppp','quantity':1}])
            self.assertEqual(set(request['custom_data']), {'habrivo_checkout_ref'})
            db=self.module.conn()
            try:
                link=db.execute('SELECT * FROM paddle_checkout_links WHERE checkout_ref=?',
                                (request['custom_data']['habrivo_checkout_ref'],)).fetchone()
                self.assertEqual(link['customer_id'],uid)
                self.assertEqual(link['transaction_id'],'txn_'+'t'*26)
            finally:db.close()
            self.assertFalse(api.call_args.kwargs['allow_redirects'])
            self.assertEqual(result.get_json()['environment'],'sandbox')
        db=self.module.conn()
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0],0)
        finally:db.close()
        self.assertFalse(self.get('/api/promo/subscription').get_json()['subscribed'])

    def test_client_prices_entitlements_and_malformed_checkout_are_rejected(self):
        self.login()
        with self.configured(),patch.object(self.module.requests,'post') as api:
            for data in ([],{}, {'plan':['pro'],'billing_cycle':'yearly'},
                         {'plan':'pro','billing_cycle':'yearly','price':1},
                         {'plan':'pro','billing_cycle':'yearly','payment_verified':True}):
                with self.subTest(kind=type(data).__name__):
                    self.assertEqual(self.post('/api/paddle/checkout',data).status_code,400)
            api.assert_not_called()

    def test_protected_endpoints_require_auth_before_missing_modules_or_provider(self):
        with patch.object(self.module.requests,'post') as api:
            for path in ('/api/marketing/generate','/api/paddle/checkout','/api/chat','/api/promo/generate'):
                self.assertEqual(self.post(path,{}).status_code,401)
            for path in ('/api/marketing/library','/api/promo/subscription','/api/products','/api/companies'):
                self.assertEqual(self.get(path).status_code,401)
            api.assert_not_called()

    def test_untrusted_checkout_urls_and_provider_failure_do_not_leak(self):
        self.login()
        with self.configured():
            for url in ('https://untrusted.invalid/pay','javascript:alert(1)', 'https://user:secret@paddle.com/pay'):
                provider=MagicMock(status_code=201)
                provider.json.return_value={'data':{'id':'txn_tttttttttttttttttttttttttt','checkout':{'url':url}}}
                with patch.object(self.module.requests,'post',return_value=provider):
                    self.assertEqual(self.post('/api/paddle/checkout',{'plan':'pro','billing_cycle':'yearly'}).status_code,502)
            provider=MagicMock(status_code=400,text='PRIVATE_PADDLE_RESPONSE')
            provider.json.return_value={'error':'PRIVATE_PADDLE_RESPONSE'}
            output=io.StringIO()
            with patch.object(self.module.requests,'post',return_value=provider),contextlib.redirect_stdout(output):
                result=self.post('/api/paddle/checkout',{'plan':'pro','billing_cycle':'yearly'})
            self.assertEqual(result.status_code,502)
            self.assertNotIn('PRIVATE_',result.get_data(as_text=True)+output.getvalue())


if __name__=='__main__':unittest.main()
