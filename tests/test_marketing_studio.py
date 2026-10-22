"""Marketing Studio: real Flask/temporary SQLite, mocked paid-provider boundary only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import test_customer_account as account_tests
import marketing_studio as studio


class MarketingStudioTests(unittest.TestCase):
    setUp = account_tests.CustomerAccountTests.setUp
    post = account_tests.CustomerAccountTests.post
    get = account_tests.CustomerAccountTests.get
    login = account_tests.CustomerAccountTests.login

    def configured(self):
        return patch.dict(os.environ, {'MARKETING_AI_PROVIDER':'openai','MARKETING_AI_MODEL':'gpt-4.1-mini',
                                      'OPENAI_API_KEY':'synthetic-secret-key','MARKETING_AI_DAILY_LIMIT':'50'})

    def details(self):
        return {'product_name':'Synthetic canvas tote','description':'Cotton canvas; zip closure.',
                'audience':'Commuters','tone':'professional','goal':'sales'}

    def subscribe(self, customer_id, verified=1, expiry='2099-01-01 00:00:00', status='active'):
        db=self.module.conn()
        try:
            with db:
                db.execute('''INSERT INTO subscriptions(customer_id,plan,billing_cycle,amount_usd,status,
                    payment_verified,expiry_date) VALUES (?, 'starter', 'monthly', 39, ?, ?, ?)''',
                    (customer_id,status,verified,expiry))
        finally:db.close()

    def count(self):
        db=self.module.conn()
        try:return db.execute('SELECT COUNT(*) FROM promo_usage').fetchone()[0]
        finally:db.close()

    def provider(self, output=None, status=200, finish='stop'):
        if output is None:output={key:'Synthetic test output for '+key for key in studio.OUTPUTS}
        body={'choices':[{'finish_reason':finish,'message':{'content':json.dumps(output)}}]}
        response=MagicMock()
        response.__enter__.return_value=response
        response.status_code=status
        response.iter_content.return_value=[json.dumps(body).encode()]
        return patch.object(studio.requests,'post',return_value=response)

    def test_real_authenticated_pack_quota_and_provider_contract(self):
        uid=self.login(); self.subscribe(uid)
        with self.configured(),self.provider() as provider:
            response=self.post('/api/marketing/generate',self.details())
        self.assertEqual(response.status_code,200)
        result=response.get_json()
        self.assertEqual(set(result['outputs']),set(studio.OUTPUTS))
        self.assertEqual(result['quota'],{'used':1,'limit':100})
        self.assertTrue(result['attempt_counted'])
        self.assertEqual(self.count(),1)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        args,kwargs=provider.call_args
        self.assertEqual(args[0],'https://api.openai.com/v1/chat/completions')
        self.assertEqual(kwargs['json']['max_completion_tokens'],900)
        self.assertEqual(kwargs['timeout'],(5,25))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(json.loads(kwargs['json']['messages'][1]['content']),self.details())

    def test_strict_structured_output_payload_omits_string_length_keywords(self):
        with self.provider() as provider:
            studio.generate(self.details(), studio.ProviderConfig('synthetic-key', 'configured-model', 50))
        response_format = provider.call_args.kwargs['json']['response_format']
        self.assertEqual(response_format['type'], 'json_schema')
        self.assertIs(response_format['json_schema']['strict'], True)
        self.assertEqual(response_format['json_schema']['schema'], {
            'type': 'object',
            'properties': {name: {'type': 'string'} for name in studio.OUTPUTS},
            'required': list(studio.OUTPUTS),
            'additionalProperties': False,
        })

    def test_model_prompt_includes_each_server_output_character_limit(self):
        with self.provider() as provider:
            studio.generate(self.details(), studio.ProviderConfig('synthetic-key', 'configured-model', 50))
        system = provider.call_args.kwargs['json']['messages'][0]
        self.assertEqual(system['role'], 'system')
        self.assertIn('Maximum characters per field:', system['content'])
        for name, maximum in studio.OUTPUTS.items():
            self.assertIn(f'{name}: {maximum}', system['content'])
        self.assertNotIn('schema length limit', system['content'])

    def test_unauthenticated_and_unconfigured_never_call_provider(self):
        with self.provider() as provider:
            self.assertEqual(self.post('/api/marketing/generate',self.details()).status_code,401)
            self.login()
            response=self.post('/api/marketing/generate',self.details())
            self.assertEqual(response.status_code,503)
            provider.assert_not_called()
        self.assertEqual(self.count(),0)

    def test_invalid_input_and_request_size_do_not_reserve_or_call(self):
        uid=self.login(); self.subscribe(uid)
        invalid=[[],{}, {**self.details(),'customer_id':999}, {**self.details(),'product_name':'x'*121},
                 {**self.details(),'description':'\x00'}, {**self.details(),'audience':False},
                 {**self.details(),'tone':'unknown'}, {**self.details(),'goal':['sales']}]
        with self.configured(),self.provider() as provider:
            for data in invalid:
                with self.subTest(data_type=type(data).__name__):
                    self.assertEqual(self.post('/api/marketing/generate',data).status_code,400)
            response=self.client.post('/api/marketing/generate',data='x'*8193,
                                      content_type='application/json',base_url='https://localhost')
            self.assertEqual(response.status_code,413)
            response=self.client.post('/api/marketing/generate',data='{invalid',
                                      content_type='application/json',base_url='https://localhost')
            self.assertEqual(response.status_code,400)
            provider.assert_not_called()
        self.assertEqual(self.count(),0)

    def test_other_accounts_and_unverified_expired_inactive_plans_cannot_grant_access(self):
        uid=self.login()
        other=self.post('/api/register',{'name':'Other','email':'other@example.invalid','password':'Other-password'})
        self.subscribe(account_tests.lookup_customer_id(self.module,'other@example.invalid'))
        with self.configured(),self.provider() as provider:
            self.assertEqual(self.post('/api/marketing/generate',self.details()).status_code,403)
            for verified,expiry,status in ((0,'2099-01-01 00:00:00','active'),
                                           (1,'2000-01-01 00:00:00','active'),
                                           (1,'2099-01-01 00:00:00','pending')):
                self.subscribe(uid,verified,expiry,status)
                self.assertEqual(self.post('/api/marketing/generate',self.details()).status_code,403)
            provider.assert_not_called()
        self.assertEqual(self.count(),0)

    def test_cooldown_and_daily_cap_are_shared_through_database(self):
        uid=self.login(); self.subscribe(uid)
        with self.configured(),self.provider() as provider:
            self.assertEqual(self.post('/api/marketing/generate',self.details()).status_code,200)
            response=self.post('/api/marketing/generate',self.details())
            self.assertEqual(response.status_code,429)
            self.assertEqual(response.headers['Retry-After'],'60')
            other=self.post('/api/register',{'name':'Other','email':'other@example.invalid','password':'Other-password'})
            self.subscribe(account_tests.lookup_customer_id(self.module,'other@example.invalid'))
            self.post('/api/customer/login',{'email':'other@example.invalid','password':'Other-password'})
            with patch.dict(os.environ,{'MARKETING_AI_DAILY_LIMIT':'1'}):
                response=self.post('/api/marketing/generate',self.details())
            self.assertEqual(response.get_json()['code'],'daily_limit')
            provider.assert_called_once()
        self.assertEqual(self.count(),1)

    def test_monthly_limit_counts_all_customer_subscriptions(self):
        uid=self.login(); self.subscribe(uid)
        db=self.module.conn()
        try:
            sub=db.execute('SELECT id FROM subscriptions WHERE customer_id=?',(uid,)).fetchone()[0]
            with db:
                for _ in range(100):
                    db.execute('INSERT INTO promo_usage(customer_id,subscription_id,created_at) VALUES(?,?,?)',
                               (uid,sub,datetime.now(timezone.utc).strftime('%Y-%m-01 00:00:00')))
        finally:db.close()
        self.subscribe(uid)  # A second plan must not reset the monthly allowance.
        with self.configured(),self.provider() as provider:
            response=self.post('/api/marketing/generate',self.details())
            self.assertEqual(response.get_json()['code'],'monthly_limit')
            provider.assert_not_called()
        self.assertEqual(self.count(),100)

    def test_failed_or_timed_out_provider_attempts_remain_counted_and_private(self):
        uid=self.login(); self.subscribe(uid)
        with self.configured(),patch.object(studio.requests,'post',side_effect=studio.requests.Timeout('PRIVATE_PROVIDER_KEY')):
            response=self.post('/api/marketing/generate',self.details())
        self.assertEqual(response.status_code,504)
        self.assertTrue(response.get_json()['attempt_counted'])
        self.assertNotIn('PRIVATE_',response.get_data(as_text=True))
        self.assertEqual(self.count(),1)
        with self.configured(),self.provider() as provider:
            self.assertEqual(self.post('/api/marketing/generate',self.details()).status_code,429)
            provider.assert_not_called()

    def test_provider_output_validation_and_response_size(self):
        config=studio.ProviderConfig('synthetic-secret','gpt-4.1-mini',50)
        cases=[({},'stop'), ({key:'x' for key in studio.OUTPUTS},'length'),
               ({**{key:'x' for key in studio.OUTPUTS},'ad_headline':'x'*121},'stop'),
               ({**{key:'x' for key in studio.OUTPUTS},'social_caption':None},'stop'),
               ({**{key:'x' for key in studio.OUTPUTS},'ad_headline':'bad\x00text'},'stop')]
        for output,finish in cases:
            with self.subTest(finish=finish),self.provider(output=output,finish=finish):
                with self.assertRaises(studio.StudioError):studio.generate(self.details(),config)
        with self.provider(),patch.object(studio.time,'monotonic',side_effect=[0,36]):
            with self.assertRaises(studio.StudioError) as error:studio.generate(self.details(),config)
            self.assertEqual(error.exception.status,504)
        with self.provider(status=429):
            with self.assertRaises(studio.StudioError):studio.generate(self.details(),config)
        response=MagicMock(); response.__enter__.return_value=response
        response.status_code=200; response.iter_content.return_value=[b'x'*65537]
        with patch.object(studio.requests,'post',return_value=response):
            with self.assertRaises(studio.StudioError):studio.generate(self.details(),config)

    def test_concurrent_requests_cannot_both_reserve_or_call_provider(self):
        uid=self.login(); self.subscribe(uid)
        barrier=threading.Barrier(2)
        def submit():
            client=account_tests.authenticated_test_client(self.module,uid)
            barrier.wait(timeout=5)
            return self.post('/api/marketing/generate',self.details(),client=client).status_code
        with self.configured(),self.provider() as provider,ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(lambda _:submit(),range(2)))
        self.assertEqual(sorted(statuses),[200,429])
        provider.assert_called_once()
        self.assertEqual(self.count(),1)

    def test_concurrent_customers_cannot_exceed_global_daily_cap(self):
        first=self.login(); self.subscribe(first)
        other=self.post('/api/register',{'name':'Other','email':'other@example.invalid','password':'Other-password'})
        second=account_tests.lookup_customer_id(self.module,'other@example.invalid'); self.subscribe(second)
        barrier=threading.Barrier(2)
        def submit(uid):
            client=account_tests.authenticated_test_client(self.module,uid)
            barrier.wait(timeout=5)
            return self.post('/api/marketing/generate',self.details(),client=client).status_code
        with self.configured(),patch.dict(os.environ,{'MARKETING_AI_DAILY_LIMIT':'1'}), \
                self.provider() as provider,ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(submit,(first,second)))
        self.assertEqual(sorted(statuses),[200,429])
        self.assertEqual(self.count(),1)
        provider.assert_called_once()

    def test_invalid_configuration_fails_before_reservation(self):
        uid=self.login(); self.subscribe(uid)
        with self.configured(),self.provider() as provider:
            for changes in ({'MARKETING_AI_DAILY_LIMIT':'0'},{'MARKETING_AI_MODEL':''},
                            {'MARKETING_AI_PROVIDER':'unknown'},{'OPENAI_API_KEY':''}):
                with self.subTest(setting=next(iter(changes))),patch.dict(os.environ,changes):
                    self.assertEqual(self.post('/api/marketing/generate',self.details()).status_code,503)
            provider.assert_not_called()
        self.assertEqual(self.count(),0)

    def test_marketing_page_and_script_served_without_breaking_account_settings(self):
        page=self.get('/customer.html')
        try:
            for marker in (b'id="marketingForm"',b'id="passwordForm"',b'/marketing-studio.js'):
                self.assertIn(marker,page.data)
        finally:page.close()
        script=self.get('/marketing-studio.js')
        try:self.assertEqual(script.status_code,200)
        finally:script.close()


    def test_invalid_unicode_is_rejected_before_reserving(self):
        uid = self.login(); self.subscribe(uid)
        with self.configured(), self.provider() as provider:
            result = self.post('/api/marketing/generate', {**self.details(), 'description': '\ud800'})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.count(), 0)
        provider.assert_not_called()

    def test_provider_shapes_duplicate_fields_and_unicode_fail_privately(self):
        valid = {key: 'Synthetic output' for key in studio.OUTPUTS}
        content = json.dumps(valid)
        bodies = [[], {'choices': [None]}, {'choices': [{'message': []}]},
                  {'choices': [{'finish_reason': 'stop', 'message': {'content':
                      content[:-1] + ',"ad_headline":"duplicate"}'}}]},
                  {'choices': [{'finish_reason': 'stop', 'message': {'content':
                      json.dumps({**valid, 'ad_headline': '\ud800'})}}]}]
        for body in bodies:
            with self.subTest(shape=type(body).__name__), self.provider() as provider:
                provider.return_value.iter_content.return_value = [json.dumps(body).encode()]
                with self.assertRaises(studio.StudioError) as failure:
                    studio.generate(self.details(), studio.ProviderConfig('PRIVATE_KEY', 'configured-model', 50))
                self.assertEqual(failure.exception.status, 502)
                self.assertNotIn('PRIVATE_KEY', str(failure.exception))
                self.assertNotIn('Synthetic output', str(failure.exception))

    def test_provider_access_and_capacity_failures_remain_counted(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), self.provider(status=status):
                with self.assertRaises(studio.StudioError) as failure:
                    studio.generate(self.details(), studio.ProviderConfig('PRIVATE_KEY', 'configured-model', 50))
                self.assertEqual(failure.exception.status, 503)
                self.assertNotIn('PRIVATE_KEY', str(failure.exception))
        uid = self.login(); self.subscribe(uid)
        with self.configured(), self.provider(status=429) as provider:
            response = self.post('/api/marketing/generate', self.details())
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.get_json()['attempt_counted'])
            self.assertEqual(self.count(), 1)
            self.assertEqual(self.post('/api/marketing/generate', self.details()).status_code, 429)
            provider.assert_called_once()


if __name__=='__main__':unittest.main()
