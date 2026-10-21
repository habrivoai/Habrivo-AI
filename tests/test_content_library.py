"""Real HTTP persistence tests using fresh disposable SQLite only."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_customer_account as account_tests
import test_marketing_studio as studio_tests
from content_library import save_token
from database import init_sqlite_schema


class ContentLibraryTests(unittest.TestCase):
    setUp = account_tests.CustomerAccountTests.setUp
    post = account_tests.CustomerAccountTests.post
    get = account_tests.CustomerAccountTests.get
    login = account_tests.CustomerAccountTests.login
    configured = studio_tests.MarketingStudioTests.configured
    provider = studio_tests.MarketingStudioTests.provider
    subscribe = studio_tests.MarketingStudioTests.subscribe
    details = studio_tests.MarketingStudioTests.details

    def payload(self, uid, suffix=''):
        details=self.details(); details['product_name']+=suffix
        outputs={'promotional_copy':'<img src=x onerror=alert(1)>',
                 'product_description':'Exact product description.\nSecond line.',
                 'social_caption':'A caption', 'ad_headline':'A headline'}
        return {'details':details,'outputs':outputs,
                'save_token':save_token(self.module.app.secret_key,uid,details,outputs)}

    def delete(self,item_id,client=None):
        return (client or self.client).delete('/api/marketing/library/'+str(item_id),base_url='https://localhost')

    def test_generated_pack_save_list_detail_delete_uses_no_extra_ai_attempt(self):
        uid=self.login(); self.subscribe(uid)
        with self.configured(),self.provider() as provider:
            generated=self.post('/api/marketing/generate',self.details()).get_json()
            payload={'details':self.details(),'outputs':generated['outputs'],'save_token':generated['save_token']}
            response=self.post('/api/marketing/library',payload)
            self.assertEqual(response.status_code,201)
            item=response.get_json()['item']
            self.assertEqual(self.get('/api/marketing/library').get_json()['items'],[item])
            self.assertEqual(self.get('/api/marketing/library/'+str(item['id'])).get_json()['item'],item)
            self.assertEqual(self.delete(item['id']).status_code,200)
            self.assertEqual(self.get('/api/marketing/library').get_json()['items'],[])
            provider.assert_called_once()
        db=self.module.conn()
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM promo_usage').fetchone()[0],1)
        finally:db.close()

    def test_persistence_exact_values_and_idempotency(self):
        uid=self.login(); payload=self.payload(uid)
        first=self.post('/api/marketing/library',payload)
        duplicate=self.post('/api/marketing/library',payload)
        self.assertEqual((first.status_code,duplicate.status_code),(201,200))
        self.assertEqual(first.get_json()['item'],duplicate.get_json()['item'])
        self.assertEqual(first.get_json()['item']['outputs'],payload['outputs'])
        # Reinitialization must not drop older users or saved records.
        init_sqlite_schema(self.module.DATABASE_CONFIG)
        other_browser=self.module.app.test_client()
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.password},other_browser).status_code,200)
        result=other_browser.get('/api/marketing/library',base_url='https://localhost')
        self.assertEqual(result.get_json()['items'][0]['id'],first.get_json()['item']['id'])
        self.assertEqual(result.headers['Cache-Control'],'no-store')

    def test_cross_account_proof_read_write_delete_isolation(self):
        first_id=self.login(); first_payload=self.payload(first_id)
        first=self.post('/api/marketing/library',first_payload).get_json()['item']
        self.post('/api/register',{'name':'Second','email':'second@example.invalid','password':'Second-password'})
        second=account_tests.lookup_customer_id(self.module,'second@example.invalid')
        self.post('/api/customer/login',{'email':'second@example.invalid','password':'Second-password'})
        self.assertEqual(self.get('/api/marketing/library').get_json()['items'],[])
        self.assertEqual(self.get('/api/marketing/library/'+str(first['id'])).status_code,404)
        self.assertEqual(self.delete(first['id']).status_code,404)
        self.assertEqual(self.post('/api/marketing/library',first_payload).status_code,400)
        tampered={**self.payload(second),'customer_id':first_id}
        self.assertEqual(self.post('/api/marketing/library',tampered).status_code,400)
        own=self.post('/api/marketing/library',self.payload(second)).get_json()['item']
        self.assertNotEqual(first['id'],own['id'])
        self.post('/api/customer/login',{'email':'one@example.invalid','password':self.password})
        self.assertEqual(self.get('/api/marketing/library').get_json()['items'],[first])

    def test_unauthenticated_requests_do_not_connect(self):
        with patch.object(self.module,'connect',side_effect=AssertionError('Must not access storage')):
            self.assertEqual(self.get('/api/marketing/library').status_code,401)
            self.assertEqual(self.post('/api/marketing/library',{}).status_code,401)
            self.assertEqual(self.get('/api/marketing/library/1').status_code,401)
            self.assertEqual(self.delete(1).status_code,401)

    def test_malformed_input_size_and_modified_outputs_are_rejected(self):
        uid=self.login(); payload=self.payload(uid)
        cases=[[],{}, {**payload,'save_token':False}, {**payload,'save_token':'invalid'},
               {**payload,'outputs':{}}, {**payload,'details':[]},
               {**payload,'outputs':{**payload['outputs'],'ad_headline':'tampered'}},
               {**payload,'outputs':{**payload['outputs'],'ad_headline':'x'*121}},
               {**payload,'outputs':{**payload['outputs'],'social_caption':'bad\x00'}},
               {**payload,'details':{**payload['details'],'audience':'x'*301}},
               {**payload,'outputs':{**payload['outputs'],'ad_headline':'\ud800'}}]
        for data in cases:
            with self.subTest(kind=type(data).__name__):
                self.assertEqual(self.post('/api/marketing/library',data).status_code,400)
        for raw,expected in (('x'*32769,413),('{bad',400)):
            response=self.client.post('/api/marketing/library',data=raw,content_type='application/json',base_url='https://localhost')
            self.assertEqual(response.status_code,expected)
        self.assertEqual(self.get('/api/marketing/library').get_json()['items'],[])

    def test_expired_proof_denied_without_regeneration(self):
        uid=self.login()
        with patch('itsdangerous.timed.TimestampSigner.get_timestamp',return_value=int(time.time())-90000):
            payload=self.payload(uid)
        response=self.post('/api/marketing/library',payload)
        self.assertEqual(response.status_code,400)
        self.assertEqual(response.get_json()['code'],'invalid_proof')
        self.assertNotIn(payload['save_token'],response.get_data(as_text=True))

    def test_pagination_is_ordered_bounded_and_owner_scoped(self):
        uid=self.login()
        ids=[]
        for n in range(23):ids.append(self.post('/api/marketing/library',self.payload(uid,str(n))).get_json()['item']['id'])
        first=self.get('/api/marketing/library').get_json()
        self.assertEqual([item['id'] for item in first['items']],list(reversed(ids))[:20])
        second=self.get('/api/marketing/library?before='+str(first['next_cursor'])).get_json()
        self.assertEqual([item['id'] for item in second['items']],list(reversed(ids))[20:])
        self.assertIsNone(second['next_cursor'])
        for query in ('?before=-1','?before=0','?before=bad','?before='+('9'*5000),
                      '?customer_id=1','?before=1&before=2'):
            self.assertEqual(self.get('/api/marketing/library'+query).status_code,400)

    def test_failed_storage_never_exposes_database_details_or_deletes_data(self):
        uid=self.login(); payload=self.payload(uid)
        item=self.post('/api/marketing/library',payload).get_json()['item']
        with patch.object(self.module,'connect',side_effect=RuntimeError('PRIVATE_DB_SECRET')):
            for result in (self.get('/api/marketing/library'),self.delete(item['id']),self.post('/api/marketing/library',payload)):
                self.assertEqual(result.status_code,503)
                self.assertNotIn('PRIVATE_',result.get_data(as_text=True))
        self.assertEqual(len(self.get('/api/marketing/library').get_json()['items']),1)
        self.assertEqual(self.delete(item['id']).status_code,200)
        self.assertEqual(self.delete(item['id']).status_code,404)

    def test_concurrent_duplicate_saves_create_one_record(self):
        uid=self.login(); payload=self.payload(uid); barrier=threading.Barrier(2)
        def save():
            client=account_tests.authenticated_test_client(self.module,uid)
            barrier.wait(timeout=5)
            response=self.post('/api/marketing/library',payload,client)
            return response.status_code,response.get_json()['item']['id']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:save(),range(2)))
        self.assertEqual(sorted(status for status,_ in results),[200,201])
        self.assertEqual(results[0][1],results[1][1])
        self.assertEqual(len(self.get('/api/marketing/library').get_json()['items']),1)

    def test_capacity_blocks_new_pack_but_allows_idempotent_retry(self):
        uid=self.login(); payload=self.payload(uid)
        first=self.post('/api/marketing/library',payload).get_json()['item']
        db=self.module.conn()
        try:
            with db:
                for n in range(199):
                    db.execute('''INSERT INTO marketing_content(customer_id,content_digest,product_name,details_json,outputs_json,created_at)
                        VALUES(?,?,?,?,?,?)''',(uid,'synthetic-'+str(n),'Synthetic',json.dumps(payload['details']),
                                              json.dumps(payload['outputs']),'2026-01-01 00:00:00'))
        finally:db.close()
        self.assertEqual(self.post('/api/marketing/library',self.payload(uid,'different')).status_code,409)
        duplicate=self.post('/api/marketing/library',payload)
        self.assertEqual(duplicate.status_code,200)
        self.assertEqual(duplicate.get_json()['item']['id'],first['id'])

    def test_deleted_account_cannot_access_library(self):
        uid=self.login(); self.post('/api/marketing/library',self.payload(uid))
        db=self.module.conn()
        try:
            with db:db.execute('DELETE FROM users WHERE id=?',(uid,))
        finally:db.close()
        self.assertEqual(self.get('/api/marketing/library').status_code,401)
        self.assertEqual(self.delete(1).status_code,401)


if __name__=='__main__':unittest.main()
