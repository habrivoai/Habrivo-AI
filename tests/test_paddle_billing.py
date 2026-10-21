"""Signed synthetic HTTP events; every database is a new disposable SQLite fixture.

Network is prohibited by the shared account fixture. No Paddle or live DB calls.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
import time
import unittest
from unittest.mock import MagicMock, patch

import test_customer_account as accounts
import test_public_journey as journey
import paddle_billing as billing

PRICE = 'pri_' + 'p' * 26
PRODUCT = 'pro_' + 'p' * 26
TXN = 'txn_' + 't' * 26
SID = 'sub_' + 's' * 26
CTM = 'ctm_' + 'c' * 26
SECRET = 'synthetic-webhook-secret'


class PaddleWebhookTests(unittest.TestCase):
    setUp = accounts.CustomerAccountTests.setUp
    post = accounts.CustomerAccountTests.post
    get = accounts.CustomerAccountTests.get
    login = accounts.CustomerAccountTests.login
    configured = journey.PublicJourneyTests.configured

    def setup_checkout(self):
        self.uid = self.login()
        self.configured_context = self.configured()
        self.configured_context.__enter__()
        self.addCleanup(self.configured_context.__exit__, None, None, None)
        provider = MagicMock(status_code=201)
        provider.json.return_value = {'data': {'id': TXN, 'checkout': {
            'url': 'https://sandbox-pay.paddle.io/checkout/synthetic'}}}
        with patch.object(self.module.requests, 'post', return_value=provider):
            self.assertEqual(self.post('/api/paddle/checkout', {'plan': 'pro', 'billing_cycle': 'yearly'}).status_code, 200)
        self.start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=360)
        self.end = self.start + timedelta(days=365)
        self.occurred = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)

    def date(self, value):
        return value.isoformat().replace('+00:00', 'Z')

    def item(self):
        return {'quantity': 1, 'price': {'id': PRICE, 'product_id': PRODUCT, 'type': 'standard',
                'billing_cycle': {'interval': 'year', 'frequency': 1}, 'trial_period': None,
                'unit_price': {'amount': '69900', 'currency_code': 'USD'}}}

    def event(self, kind='transaction.completed', number=1, when=None):
        data = {'customer_id': CTM, 'items': [self.item()], 'currency_code': 'USD',
                'collection_mode': 'automatic'}
        period = {'starts_at': self.date(self.start), 'ends_at': self.date(self.end)}
        if kind.startswith('transaction.'):
            data.update(id=TXN, subscription_id=SID, status='completed', origin='api', billing_period=period,
                        payments=[{'status': 'captured', 'amount': '69900'}],
                        details={'totals': {'subtotal': '69900', 'total': '69900', 'tax': '0', 'discount': '0', 'credit': '0'},
                                 'line_items': [{'price_id': PRICE, 'quantity': 1, 'proration': None}]})
        else:
            status = {'canceled': 'canceled', 'paused': 'paused', 'past_due': 'past_due',
                      'trialing': 'trialing'}.get(kind.split('.')[1], 'active')
            data.update(id=SID, status=status, current_billing_period=period)
            if kind == 'subscription.created':
                data['transaction_id'] = TXN
        return {'event_id': 'evt_' + str(number).zfill(26), 'event_type': kind,
                'occurred_at': self.date(when or self.occurred), 'data': data}

    def send(self, event=None, raw=None, timestamp=None, secret=SECRET, header=None):
        raw = json.dumps(event, separators=(',', ':')).encode() if raw is None else raw
        ts = str(int(time.time()) if timestamp is None else timestamp)
        signature = hmac.new(secret.encode(), ts.encode() + b':' + raw, hashlib.sha256).hexdigest()
        return self.client.post('/api/paddle/webhook', data=raw, content_type='application/json',
                                headers={'Paddle-Signature': header or f'ts={ts};h1={signature}'},
                                base_url='https://localhost')

    def rows(self, table):
        self.assertIn(table, ('subscriptions', 'paddle_webhook_events', 'paddle_payments',
                              'paddle_subscription_state', 'paddle_checkout_links', 'paddle_payment_holds', 'paddle_subscription_holds'))
        db = self.module.conn()
        try:
            return [dict(row) for row in db.execute('SELECT * FROM ' + table)]
        finally:
            db.close()

    def active(self):
        return self.get('/api/promo/subscription').get_json()['subscribed']

    def activate(self):
        self.assertEqual(self.send(self.event()).status_code, 200)
        self.assertFalse(self.active())
        self.assertEqual(self.send(self.event('subscription.created', 2)).status_code, 200)
        self.assertTrue(self.active())

    def test_verified_payment_and_creation_activate_in_either_order(self):
        self.setup_checkout()
        self.assertEqual(self.send(self.event('subscription.created', 2)).status_code, 200)
        self.assertFalse(self.active())
        self.assertEqual(self.send(self.event()).status_code, 200)
        self.assertTrue(self.active())
        sub = self.rows('subscriptions')[0]
        self.assertEqual((sub['customer_id'], sub['plan'], sub['billing_cycle'], sub['payment_verified']),
                         (self.uid, 'pro', 'yearly', 1))
        self.assertTrue(self.get('/api/customer/entitlements').get_json()['ai']['paid_entitlement'])

    def test_initial_null_transaction_period_waits_for_created_period(self):
        self.setup_checkout()
        event = self.event()
        event['data']['billing_period'] = None
        self.assertEqual(self.send(event).status_code, 200)
        self.assertEqual(self.rows('subscriptions'), [])
        self.assertEqual(self.send(self.event('subscription.activated', 3)).status_code, 200)
        self.assertFalse(self.active())
        self.assertEqual(self.send(self.event('subscription.created', 2)).status_code, 200)
        self.assertTrue(self.active())

    def test_invalid_unsigned_expired_future_and_raw_body_signatures(self):
        self.setup_checkout()
        event = self.event()
        for arguments in ({'header': 'invalid'}, {'header': 'ts=1;h1=' + '0'*64},
                          {'secret': 'wrong'}, {'timestamp': int(time.time())-6},
                          {'timestamp': int(time.time())+10},
                          {'header': 'ts=1;ts=1;h1=' + '0'*64}):
            with self.subTest(arguments=tuple(arguments)):
                self.assertEqual(self.send(event, **arguments).status_code, 401)
        raw = json.dumps(event).encode()
        ts = str(int(time.time()))
        signature = hmac.new(SECRET.encode(), ts.encode()+b':'+raw, hashlib.sha256).hexdigest()
        self.assertEqual(self.send(raw=raw+b' ', header=f'ts={ts};h1={signature}').status_code,401)
        self.assertEqual(self.client.post('/api/paddle/webhook',data=raw).status_code,401)
        self.assertEqual(self.rows('paddle_webhook_events'), [])
        self.assertEqual(self.rows('subscriptions'), [])

    def test_multiple_signature_values_and_fresh_retry_after_expired_delivery(self):
        self.setup_checkout()
        event = self.event()
        self.assertEqual(self.send(event,timestamp=int(time.time())-30).status_code,401)
        raw = json.dumps(event,separators=(',',':')).encode()
        ts=str(int(time.time()))
        signature=hmac.new(SECRET.encode(),ts.encode()+b':'+raw,hashlib.sha256).hexdigest()
        self.assertEqual(self.send(raw=raw,header=f'ts={ts};h1={"0"*64};h1={signature}').status_code,200)

    def test_durable_idempotency_survives_new_http_client_and_notification_id(self):
        self.setup_checkout()
        self.activate()
        event = self.event()
        event['notification_id']='ntf_'+'n'*26
        response=self.send(event)
        self.assertEqual(response.get_json()['result'],'duplicate')
        self.client=self.module.app.test_client()
        self.assertEqual(self.send(event).get_json()['result'],'duplicate')
        self.assertEqual(len(self.rows('subscriptions')),1)
        self.assertEqual(len(self.rows('paddle_payments')),1)
        self.assertEqual(len(self.rows('paddle_webhook_events')),2)

    def test_same_event_id_with_changed_payload_is_rejected(self):
        self.setup_checkout()
        self.activate()
        event=self.event();event['data']['customer_id']='ctm_'+'x'*26
        self.assertEqual(self.send(event).status_code,409)
        self.assertTrue(self.active())
        self.assertEqual(len(self.rows('paddle_webhook_events')),2)

    def test_transaction_duplicate_with_another_event_id_cannot_double_activate(self):
        self.setup_checkout();self.activate()
        self.assertEqual(self.send(self.event(number=3)).status_code,200)
        self.assertEqual(len(self.rows('subscriptions')),1)
        self.assertEqual(len(self.rows('paddle_payments')),1)

    def test_renewal_extends_same_record_and_late_old_event_cannot_regress(self):
        self.setup_checkout();self.activate()
        before=self.rows('subscriptions')[0]
        event=self.event(number=3,when=self.occurred+timedelta(hours=2))
        event['data'].update(id='txn_'+'r'*26,origin='subscription_recurring',
                            billing_period={'starts_at':self.date(self.end),'ends_at':self.date(self.end+timedelta(days=365))})
        self.assertEqual(self.send(event).status_code,200)
        self.assertTrue(self.active())  # A prepaid next period must not hide the current paid period.
        renewed=self.rows('subscriptions')[0]
        self.assertEqual(renewed['id'],before['id'])
        self.assertEqual(renewed['start_date'],before['start_date'])
        self.assertGreater(renewed['expiry_date'],before['expiry_date'])
        self.assertEqual(self.send(self.event(number=4)).status_code,200)
        self.assertEqual(self.rows('subscriptions')[0],renewed)

    def test_scheduled_cancellation_keeps_access_actual_cancellation_revokes(self):
        self.setup_checkout();self.activate()
        updated=self.event('subscription.updated',3, self.occurred+timedelta(hours=1))
        updated['data']['scheduled_change']={'action':'cancel','effective_at':self.date(self.end)}
        self.assertEqual(self.send(updated).status_code,200)
        self.assertTrue(self.active())
        cancel=self.event('subscription.canceled',4,self.occurred+timedelta(hours=2))
        cancel['data']['current_billing_period']=None
        self.assertEqual(self.send(cancel).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.send(self.event('subscription.updated',5)).status_code,200)
        self.assertFalse(self.active())
        # Even a newer completed payment cannot resurrect a terminal canceled subscription.
        self.assertEqual(self.send(self.event(number=6,when=self.occurred+timedelta(hours=3))).status_code,200)
        self.assertFalse(self.active())

    def test_failed_payment_does_not_activate_or_extend_past_due_revokes(self):
        self.setup_checkout()
        failed=self.event('transaction.payment_failed')
        failed['data']['status']='past_due';failed['data']['payments']=[]
        self.assertEqual(self.send(failed).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('paddle_payments'),[])
        completed=self.event(number=2)
        self.assertEqual(self.send(completed).status_code,200)
        self.assertEqual(self.send(self.event('subscription.created',3)).status_code,200)
        before=self.rows('subscriptions')[0]['expiry_date']
        failed['event_id']='evt_'+'4'.zfill(26)
        self.assertEqual(self.send(failed).status_code,200)
        self.assertEqual(self.rows('subscriptions')[0]['expiry_date'],before)
        self.assertEqual(self.send(self.event('subscription.past_due',5,self.occurred+timedelta(hours=1))).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.send(self.event(number=6)).status_code,200)
        self.assertFalse(self.active())

    def test_paused_and_trialing_never_grant_without_new_active_state(self):
        self.setup_checkout();self.activate()
        self.assertEqual(self.send(self.event('subscription.paused',3,self.occurred+timedelta(hours=1))).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.send(self.event('subscription.resumed',4,self.occurred+timedelta(hours=2))).status_code,200)
        self.assertTrue(self.active())
        self.assertEqual(self.send(self.event('subscription.trialing',5,self.occurred+timedelta(hours=3))).status_code,200)
        self.assertFalse(self.active())

    def test_unpaid_created_paid_ready_and_success_metadata_never_activate(self):
        self.setup_checkout()
        created=self.event('subscription.created')
        self.assertEqual(self.send(created).status_code,200)
        for index,kind in enumerate(('transaction.created','transaction.ready','transaction.paid'),2):
            self.assertEqual(self.send(self.event(kind,index)).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('subscriptions'),[])

    def test_missing_secret_mapping_product_or_persistence_fails_closed(self):
        self.setup_checkout()
        with patch.object(self.module,'PADDLE_WEBHOOK_SECRET',''):
            self.assertEqual(self.send(self.event()).status_code,503)
        with patch.dict(self.module.PADDLE_PRODUCTS,{'pro':None}):
            self.assertEqual(self.send(self.event()).status_code,503)
        with patch.dict(self.module.PADDLE_PRICES['pro'],{'yearly':None}):
            self.assertEqual(self.send(self.event()).status_code,503)
        with patch.object(self.module,'conn',side_effect=RuntimeError('PRIVATE_CONNECTION_VALUE')):
            response=self.send(self.event())
            self.assertEqual(response.status_code,503)
            self.assertNotIn('PRIVATE',response.get_data(as_text=True))
        self.assertEqual(self.rows('paddle_webhook_events'),[])
        self.assertEqual(self.rows('subscriptions'),[])

    def test_no_server_transaction_binding_rejects_forged_customer_custom_data(self):
        self.setup_checkout()
        event=self.event()
        event['data']['id']='txn_'+'x'*26
        event['data']['custom_data']={'customer_id':str(self.uid),'plan':'premium','payment_verified':True}
        self.assertEqual(self.send(event).status_code,503)
        self.assertEqual(self.rows('paddle_subscription_state'),[])
        self.assertEqual(self.rows('paddle_webhook_events'),[])

    def test_browser_identity_metadata_and_cookie_cannot_reassign_customer(self):
        self.setup_checkout()
        self.assertEqual(self.post('/api/register',{'name':'Other','email':'two@example.invalid','password':'Other-password-test'}).status_code,201)
        other=accounts.lookup_customer_id(self.module,'two@example.invalid')
        event=self.event();event['data']['custom_data']={'customer_id':str(other),'plan':'premium'}
        self.assertEqual(self.send(event).status_code,200)
        self.assertEqual(self.send(self.event('subscription.created',2)).status_code,200)
        self.assertEqual(self.rows('subscriptions')[0]['customer_id'],self.uid)
        self.post('/api/customer/logout',{})
        self.assertEqual(self.post('/api/customer/login',{'email':'two@example.invalid','password':'Other-password-test'}).status_code,200)
        self.assertFalse(self.active())
        self.assertFalse(self.get('/api/customer/entitlements').get_json()['ai']['paid_entitlement'])
        forged=self.event(number=3);forged['data']['customer_id']='ctm_'+'x'*26
        self.assertEqual(self.send(forged).status_code,422)

    def test_wrong_catalog_currency_price_quantity_or_unpaid_totals_rejected(self):
        self.setup_checkout()
        variants=[]
        def change(mutator):
            event=self.event();mutator(event['data']);variants.append(event)
        change(lambda d:d['items'][0]['price'].update(id='pri_'+'x'*26))
        change(lambda d:d['items'][0]['price'].update(product_id='pro_'+'x'*26))
        change(lambda d:d['items'][0].update(quantity=2))
        change(lambda d:d.update(currency_code='EUR'))
        change(lambda d:d.update(collection_mode='manual'))
        change(lambda d:d.update(status='paid'))
        change(lambda d:d.update(payments=[{'status':'authorized','amount':'69900'}]))
        change(lambda d:d['details']['totals'].update(discount='100'))
        change(lambda d:d['details']['line_items'][0].update(proration={'rate':'0.5'}))
        change(lambda d:d['items'][0]['price'].update(trial_period={'interval':'day','frequency':7}))
        change(lambda d:d['items'][0]['price'].update(unit_price={'amount':'1','currency_code':'USD'}))
        for event in variants:
            with self.subTest(variant=variants.index(event)):
                self.assertEqual(self.send(event).status_code,422)
                self.assertEqual(self.rows('paddle_payments'),[])
                self.assertEqual(self.rows('paddle_webhook_events'),[])
        self.assertFalse(self.active())

    def test_malformed_duplicate_json_oversize_and_privacy(self):
        self.setup_checkout()
        for raw in (b'{',b'[]',b'{"event_id":"private@example.invalid","event_id":"x"}',b'{"x":NaN}'):
            self.assertEqual(self.send(raw=raw).status_code,400)
        self.assertEqual(self.send(raw=b'x'*(billing.MAX_BODY+1)).status_code,413)
        event=self.event();event['data']['items'][0]['price']['id']='PRIVATE_PASSWORD_HASH'
        out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
            response=self.send(event)
        self.assertNotIn('PRIVATE',out.getvalue()+response.get_data(as_text=True))
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_transactional_receipt_failure_rolls_back_activation_and_is_retryable(self):
        self.setup_checkout()
        self.assertEqual(self.send(self.event('subscription.created',2)).status_code,200)
        original=self.module.conn
        class FailingConnection:
            def __init__(self):self.db=original()
            def __enter__(self):self.db.__enter__();return self
            def __exit__(self,*args):return self.db.__exit__(*args)
            def close(self):self.db.close()
            def execute(self,sql,parameters=()):
                if 'INSERT INTO paddle_webhook_events' in sql:
                    raise RuntimeError('PRIVATE_SQL')
                return self.db.execute(sql,parameters)
        with patch.object(self.module,'conn',side_effect=FailingConnection):
            self.assertEqual(self.send(self.event()).status_code,503)
        self.assertEqual(self.rows('paddle_payments'),[])
        self.assertEqual(self.rows('subscriptions'),[])
        self.assertEqual(len(self.rows('paddle_webhook_events')),1)
        self.assertEqual(self.send(self.event()).status_code,200)
        self.assertTrue(self.active())

    def test_expired_paid_period_does_not_entitle_without_new_payment(self):
        self.setup_checkout()
        self.start-=timedelta(days=10);self.end-=timedelta(days=10)
        self.assertEqual(self.send(self.event()).status_code,200)
        self.assertEqual(self.send(self.event('subscription.created',2)).status_code,200)
        self.assertFalse(self.active())
        self.assertFalse(self.get('/api/customer/entitlements').get_json()['ai']['paid_entitlement'])

    def test_equal_time_conflicting_state_and_nanosecond_ordering(self):
        self.setup_checkout();self.activate()
        conflict=self.event('subscription.canceled',3)
        self.assertEqual(self.send(conflict).status_code,200)
        self.assertFalse(self.active())
        state=self.rows('paddle_subscription_state')[0]
        self.assertEqual(state['lifecycle_status'],'canceled')
        self.assertEqual(state['lifecycle_at'],billing.stamp(conflict['occurred_at']))
        receipt=next(row for row in self.rows('paddle_webhook_events') if row['event_id']==conflict['event_id'])
        self.assertEqual(receipt['payload_checksum'],billing.digest(conflict))
        self.assertEqual(self.send(conflict).get_json()['result'],'duplicate')
        self.assertEqual(len(self.rows('paddle_webhook_events')),3)
        self.assertFalse(self.active())
        earlier=self.event('subscription.canceled',4)
        earlier['occurred_at']=earlier['occurred_at'].replace('Z','.000000001Z')
        self.assertEqual(self.send(earlier).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(billing.stamp('2026-01-01T00:00:00.123456789Z'),'2026-01-01T00:00:00.123456789Z')

    def test_equal_time_cancel_before_creation_and_payment_never_grants(self):
        self.setup_checkout()
        self.assertEqual(self.send(self.event()).status_code,200)
        cancel=self.event('subscription.canceled',3)
        self.assertEqual(self.send(cancel).status_code,200)
        self.assertEqual(self.send(self.event('subscription.created',2)).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('paddle_subscription_state')[0]['lifecycle_status'],'canceled')
        self.assertEqual(len(self.rows('paddle_webhook_events')),3)
        self.assertEqual(self.rows('subscriptions')[0]['payment_verified'],0)

    def test_equal_time_restrictive_precedence_is_order_independent(self):
        # Exercise every pair with in-memory SQL mocks; this ranking is local
        # fail-closed policy, not an assertion about Paddle's status chronology.
        offer={'price_id':PRICE,'product_id':PRODUCT,'billing_cycle':'yearly','amount_usd':699}
        statuses=['active','trialing','past_due','paused','canceled']
        when=billing.stamp('2026-01-01T00:00:00Z')
        for current in statuses:
            for incoming in statuses:
                with self.subTest(current=current,incoming=incoming):
                    db=MagicMock()
                    data={'status':incoming,'items':[self.item()],'currency_code':'USD','collection_mode':'automatic'}
                    state={'lifecycle_at':when,'lifecycle_status':current,'paddle_subscription_id':SID}
                    billing.apply_lifecycle(db,data,'subscription.updated',state,offer,when)
                    selected=(db.execute.call_args.args[1][0] if db.execute.called else current)
                    self.assertEqual(selected,statuses[max(statuses.index(current),statuses.index(incoming))])

    def test_equal_time_cancel_blocks_same_stale_and_newer_reactivation_preserves_usage(self):
        self.setup_checkout();self.activate()
        before=self.rows('subscriptions')[0]
        db=self.module.conn()
        try:
            with db:
                db.execute('INSERT INTO promo_usage (customer_id,subscription_id) VALUES (?,?)',(self.uid,before['id']))
            usage=[tuple(row) for row in db.execute('SELECT * FROM promo_usage ORDER BY id')]
        finally:db.close()
        cancel=self.event('subscription.canceled',3)
        self.assertEqual(self.send(cancel).status_code,200)
        for number,offset in ((4,0),(5,-1),(6,1)):
            event=self.event('subscription.resumed',number,self.occurred+timedelta(seconds=offset))
            self.assertEqual(self.send(event).status_code,200)
            self.assertFalse(self.active())
        after=self.rows('subscriptions')[0]
        self.assertEqual(after['id'],before['id'])
        self.assertEqual(after['expiry_date'],before['expiry_date'])
        db=self.module.conn()
        try:self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM promo_usage ORDER BY id')],usage)
        finally:db.close()
        self.assertEqual(self.rows('paddle_subscription_state')[0]['lifecycle_at'],billing.stamp(cancel['occurred_at']))

    def test_equal_time_cancel_receipt_failure_rolls_back_all_changes_then_retries(self):
        self.setup_checkout();self.activate()
        before={table:self.rows(table) for table in
                ('subscriptions','paddle_subscription_state','paddle_payments','paddle_webhook_events')}
        original=self.module.conn
        class ReceiptFailure:
            def __init__(self):self.db=original()
            def __enter__(self):self.db.__enter__();return self
            def __exit__(self,*args):return self.db.__exit__(*args)
            def close(self):self.db.close()
            def execute(self,sql,parameters=()):
                if 'INSERT INTO paddle_webhook_events' in sql:raise RuntimeError('PRIVATE_RECEIPT_FAILURE')
                return self.db.execute(sql,parameters)
        cancel=self.event('subscription.canceled',3)
        with patch.object(self.module,'conn',side_effect=ReceiptFailure):
            response=self.send(cancel)
        self.assertEqual(response.status_code,503)
        self.assertNotIn('PRIVATE',response.get_data(as_text=True))
        for table,rows in before.items():self.assertEqual(self.rows(table),rows)
        self.assertEqual(self.send(cancel).status_code,200)
        self.assertFalse(self.active())
        receipt=next(row for row in self.rows('paddle_webhook_events') if row['event_id']==cancel['event_id'])
        self.assertEqual(receipt['payload_checksum'],billing.digest(cancel))
        self.assertEqual(receipt['occurred_at'],billing.stamp(cancel['occurred_at']))
        self.assertEqual(self.send(cancel).get_json()['result'],'duplicate')

    def test_environment_or_ambiguous_price_mapping_cannot_activate(self):
        self.setup_checkout()
        with patch.object(self.module,'PADDLE_ENVIRONMENT','live'):
            self.assertEqual(self.send(self.event()).status_code,503)
        with patch.dict(self.module.PADDLE_PRICES['starter'],{'monthly':PRICE}):
            self.assertEqual(self.send(self.event()).status_code,503)
        self.assertFalse(self.active())


    def test_concurrent_duplicate_deliveries_serialize(self):
        from concurrent.futures import ThreadPoolExecutor
        self.setup_checkout()
        self.assertEqual(self.send(self.event('subscription.created',2)).status_code,200)
        raw=json.dumps(self.event(),separators=(',',':')).encode()
        ts=str(int(time.time()))
        signature=hmac.new(SECRET.encode(),ts.encode()+b':'+raw,hashlib.sha256).hexdigest()
        def deliver(_):
            client=self.module.app.test_client()
            return client.post('/api/paddle/webhook',data=raw,content_type='application/json',
                               headers={'Paddle-Signature':f'ts={ts};h1={signature}'},base_url='https://localhost')
        with ThreadPoolExecutor(max_workers=4) as workers:
            responses=list(workers.map(deliver,range(8)))
        self.assertTrue(all(response.status_code==200 for response in responses))
        self.assertEqual(sum(response.get_json()['result']=='processed' for response in responses),1)
        self.assertEqual(len(self.rows('subscriptions')),1)
        self.assertEqual(len(self.rows('paddle_payments')),1)

    def test_cancellation_before_delayed_payment_cannot_activate(self):
        self.setup_checkout()
        self.assertEqual(self.send(self.event('subscription.created',2)).status_code,200)
        self.assertEqual(self.send(self.event('subscription.canceled',3,
            self.occurred+timedelta(hours=1))).status_code,200)
        self.assertEqual(self.send(self.event()).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('subscriptions')[0]['payment_verified'],0)

    def test_recovery_needs_captured_payment_and_active_subscription_state(self):
        self.setup_checkout()
        self.start-=timedelta(days=8);self.end-=timedelta(days=8)
        self.assertEqual(self.send(self.event()).status_code,200)
        self.assertEqual(self.send(self.event('subscription.created',2)).status_code,200)
        self.assertEqual(self.send(self.event('subscription.past_due',3,
            self.occurred+timedelta(hours=1))).status_code,200)
        self.assertFalse(self.active())
        recovery=self.event(number=4,when=self.occurred+timedelta(hours=2))
        recovery['data'].update(id='txn_'+'r'*26,origin='subscription_recurring',billing_period={
            'starts_at':self.date(self.end),'ends_at':self.date(self.end+timedelta(days=365))})
        self.assertEqual(self.send(recovery).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.send(self.event('subscription.updated',5,
            self.occurred+timedelta(hours=3))).status_code,200)
        self.assertTrue(self.active())

    def test_removed_catalog_mapping_does_not_block_known_subscription_revocation(self):
        self.setup_checkout();self.activate()
        with patch.dict(self.module.PADDLE_PRODUCTS,{'pro':None}),patch.dict(self.module.PADDLE_PRICES['pro'],{'yearly':None}):
            self.assertEqual(self.send(self.event('subscription.canceled',3,
                self.occurred+timedelta(hours=1))).status_code,200)
        self.assertFalse(self.active())

    def test_a_paddle_customer_cannot_be_rebound_to_another_account(self):
        self.setup_checkout();self.activate()
        self.post('/api/register',{'name':'Other','email':'two@example.invalid','password':'Other-password-test'})
        self.post('/api/customer/logout',{})
        self.post('/api/customer/login',{'email':'two@example.invalid','password':'Other-password-test'})
        second_txn='txn_'+'x'*26
        provider=MagicMock(status_code=201)
        provider.json.return_value={'data':{'id':second_txn,'checkout':{'url':'https://sandbox-pay.paddle.io/checkout/test'}}}
        with patch.object(self.module.requests,'post',return_value=provider):
            self.assertEqual(self.post('/api/paddle/checkout',{'plan':'pro','billing_cycle':'yearly'}).status_code,200)
        forged=self.event(number=3)
        forged['data'].update(id=second_txn,subscription_id='sub_'+'x'*26)
        self.assertEqual(self.send(forged).status_code,422)
        self.assertEqual(len(self.rows('paddle_subscription_state')),1)
        self.assertFalse(self.active())

    def test_renewal_cannot_bridge_missing_payment_or_extend_on_state_only(self):
        self.setup_checkout();self.activate()
        before=self.rows('subscriptions')[0]
        updated=self.event('subscription.updated',3,self.occurred+timedelta(hours=1))
        updated['data']['current_billing_period']['ends_at']=self.date(self.end+timedelta(days=365))
        self.assertEqual(self.send(updated).status_code,200)
        self.assertEqual(self.rows('subscriptions')[0]['expiry_date'],before['expiry_date'])
        gap=self.event(number=4)
        gap['data'].update(id='txn_'+'r'*26,origin='subscription_recurring',billing_period={
            'starts_at':self.date(self.end+timedelta(days=365)),
            'ends_at':self.date(self.end+timedelta(days=730))})
        self.assertEqual(self.send(gap).status_code,503)
        self.assertEqual(self.rows('subscriptions')[0],before)

    def test_future_event_time_cannot_poison_subscription_ordering(self):
        self.setup_checkout()
        event=self.event(when=datetime.now(timezone.utc)+timedelta(days=2))
        self.assertEqual(self.send(event).status_code,400)
        self.assertEqual(self.rows('paddle_webhook_events'),[])
        self.assertEqual(self.rows('paddle_subscription_state'),[])

    def test_tax_inclusive_and_exclusive_totals_are_reconciled(self):
        self.setup_checkout()
        offer=billing.configured_offer('pro','yearly',**self.module.billing_settings())
        data=self.event()['data']
        data['details']['totals'].update(subtotal='59900',tax='10000',total='69900')
        billing.check_item(data,offer,payment=True)
        data['payments'][0]['amount']='79900'
        data['details']['totals'].update(subtotal='69900',tax='10000',total='79900')
        billing.check_item(data,offer,payment=True)
        data['details']['totals']['tax']='1'
        with self.assertRaises(billing.BillingError):billing.check_item(data,offer,payment=True)

    def test_postgres_lock_is_bounded_and_rollback_boundary_is_preserved_offline(self):
        # Adapter contract only; this does NOT claim execution against PostgreSQL.
        from types import SimpleNamespace
        db=MagicMock()
        with self.assertRaises(billing.BillingError):
            with billing.transaction(lambda:db,SimpleNamespace(backend='postgres')):
                raise billing.BillingError()
        self.assertIn(unittest.mock.call("SET LOCAL lock_timeout = '3s'"),db.execute.call_args_list)
        self.assertIn(unittest.mock.call('SELECT pg_advisory_xact_lock(724182607)'),db.execute.call_args_list)
        self.assertIs(db.__exit__.call_args.args[0],billing.BillingError)
        db.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
