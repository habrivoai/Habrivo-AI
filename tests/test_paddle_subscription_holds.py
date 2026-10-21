"""Signed HTTP regression cases on disposable SQLite only; network prohibited."""
from datetime import timedelta
import unittest
from unittest.mock import patch
import test_paddle_billing as base
import test_paddle_adjustment_holds as adjustments


class SubscriptionHoldTests(unittest.TestCase):
    setUp = base.PaddleWebhookTests.setUp
    post = base.PaddleWebhookTests.post
    get = base.PaddleWebhookTests.get
    login = base.PaddleWebhookTests.login
    configured = base.PaddleWebhookTests.configured
    setup_checkout = base.PaddleWebhookTests.setup_checkout
    date = base.PaddleWebhookTests.date
    item = base.PaddleWebhookTests.item
    event = base.PaddleWebhookTests.event
    send = base.PaddleWebhookTests.send
    rows = base.PaddleWebhookTests.rows
    active = base.PaddleWebhookTests.active
    activate = base.PaddleWebhookTests.activate
    adjustment = adjustments.AdjustmentHoldTests.adjustment

    def change(self, number=10, offset=2, letter='x'):
        event=self.event('subscription.updated',number,self.occurred+timedelta(hours=offset))
        event['data']['items'][0]['price'].update(id='pri_'+letter*26,product_id='pro_'+letter*26)
        return event

    def test_upgrade_downgrade_and_repeated_changes_hold_original_plan(self):
        self.setup_checkout();self.activate()
        original=self.rows('subscriptions')[0]
        for number,plan,letter in ((10,'starter','a'),(11,'premium','b')):
            with patch.dict(self.module.PADDLE_PRICES[plan],{'yearly':'pri_'+letter*26}),patch.dict(self.module.PADDLE_PRODUCTS,{plan:'pro_'+letter*26}):
                event=self.change(number,number,letter)
                self.assertEqual(self.send(event).status_code,200)
                self.assertFalse(self.active())
                self.assertEqual(self.rows('subscriptions')[0]['plan'],original['plan'])
        hold=self.rows('paddle_subscription_holds')[0]
        self.assertEqual(hold['watermark'],base.billing.stamp(event['occurred_at']))
        self.assertEqual(hold['first_payload_checksum'],base.billing.digest(self.change(10,10,'a')))
        self.assertEqual(self.rows('subscriptions')[0]['id'],original['id'])

    def test_pause_then_change_blocks_stale_and_newer_active_events(self):
        self.setup_checkout();self.activate()
        self.assertEqual(self.send(self.event('subscription.paused',3,self.occurred+timedelta(hours=1))).status_code,200)
        changed=self.change()
        self.assertEqual(self.send(changed).status_code,200)
        watermark=base.billing.stamp(changed['occurred_at'])
        self.assertEqual(self.rows('paddle_subscription_state')[0]['lifecycle_at'],watermark)
        for number,offset in ((11,1.5),(12,2),(13,3)):
            self.assertEqual(self.send(self.event('subscription.resumed',number,self.occurred+timedelta(hours=offset))).status_code,200)
            self.assertFalse(self.active())
        self.assertEqual(len(self.rows('paddle_subscription_holds')),1)
        self.assertEqual(self.send(changed).get_json()['result'],'duplicate')

    def test_conflicting_delivery_order_never_releases_hold(self):
        self.setup_checkout();self.activate()
        # A newer supported snapshot arriving first does not implicitly authorize
        # automatic resolution of a subsequently delivered unsupported change.
        newer=self.event('subscription.updated',3,self.occurred+timedelta(hours=4))
        self.assertEqual(self.send(newer).status_code,200)
        changed=self.change()
        self.assertEqual(self.send(changed).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('paddle_subscription_holds')[0]['watermark'],base.billing.stamp(newer['occurred_at']))
        self.assertEqual(self.send(self.change(11,1)).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('paddle_subscription_state')[0]['lifecycle_at'],base.billing.stamp(newer['occurred_at']))
        conflicting=self.change();conflicting['data']['items'][0]['quantity']=2
        self.assertEqual(self.send(conflicting).status_code,409)

    def test_same_time_cancellation_and_reversal_holds_remain_effective(self):
        self.setup_checkout();self.activate()
        changed=self.change()
        self.assertEqual(self.send(changed).status_code,200)
        self.assertEqual(self.send(self.event('subscription.canceled',11,self.occurred+timedelta(hours=2))).status_code,200)
        self.assertEqual(self.send(self.adjustment(12)).status_code,200)
        self.assertEqual(self.send(self.event(number=13)).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('paddle_subscription_state')[0]['lifecycle_status'],'canceled')
        self.assertEqual(len(self.rows('paddle_payment_holds')),1)
        self.assertEqual(len(self.rows('paddle_subscription_holds')),1)

    def test_same_price_renewal_and_usage_stay_intact_but_cannot_release_hold(self):
        self.setup_checkout();self.activate()
        original=self.rows('subscriptions')[0]
        db=self.module.conn()
        try:
            with db:db.execute('INSERT INTO promo_usage (customer_id,subscription_id) VALUES (?,?)',(self.uid,original['id']))
        finally:db.close()
        renewal=self.event(number=3)
        renewal['data'].update(id='txn_'+'r'*26,origin='subscription_recurring',billing_period={
            'starts_at':self.date(self.end),'ends_at':self.date(self.end+timedelta(days=365))})
        self.assertEqual(self.send(renewal).status_code,200)
        self.assertTrue(self.active())
        self.assertEqual(self.send(self.change()).status_code,200)
        self.assertEqual(self.send(self.event(number=11)).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('subscriptions')[0]['id'],original['id'])
        self.assertEqual(len(self.rows('paddle_payments')),2)
        db=self.module.conn()
        try:self.assertEqual(db.execute('SELECT COUNT(*) FROM promo_usage WHERE subscription_id=?',(original['id'],)).fetchone()[0],1)
        finally:db.close()

    def test_receipt_failure_rolls_back_hold_watermark_and_entitlement(self):
        self.setup_checkout();self.activate()
        before={name:self.rows(name) for name in ('subscriptions','paddle_subscription_state','paddle_webhook_events')}
        original=self.module.conn
        class Failure:
            def __init__(self):self.db=original()
            def __enter__(self):self.db.__enter__();return self
            def __exit__(self,*args):return self.db.__exit__(*args)
            def close(self):self.db.close()
            def execute(self,sql,parameters=()):
                if 'INSERT INTO paddle_webhook_events' in sql:raise RuntimeError('PRIVATE_SQL')
                return self.db.execute(sql,parameters)
        with patch.object(self.module,'conn',side_effect=Failure):
            response=self.send(self.change())
        self.assertEqual(response.status_code,503)
        self.assertNotIn('PRIVATE',response.get_data(as_text=True))
        self.assertEqual(self.rows('paddle_subscription_holds'),[])
        for name,rows in before.items():self.assertEqual(self.rows(name),rows)
        self.assertEqual(self.send(self.change()).status_code,200)
        self.assertFalse(self.active())
        after={name:self.rows(name) for name in (*before, 'paddle_subscription_holds')}
        with patch.object(self.module,'conn',side_effect=Failure):
            self.assertEqual(self.send(self.change(11,4)).status_code,503)
        for name,rows in after.items():self.assertEqual(self.rows(name),rows)
        self.assertEqual(self.send(self.change(11,4)).status_code,200)
        self.assertEqual(self.rows('paddle_subscription_holds')[0]['watermark'],
                         base.billing.stamp(self.change(11,4)['occurred_at']))

    def test_mismatched_owner_and_unlinked_subscription_cannot_latch_hold(self):
        self.setup_checkout();self.activate()
        event=self.change();event['data']['customer_id']='ctm_'+'x'*26
        self.assertEqual(self.send(event).status_code,422)
        event=self.change();event['data']['id']='sub_'+'x'*26
        self.assertEqual(self.send(event).status_code,503)
        self.assertEqual(self.rows('paddle_subscription_holds'),[])
        self.assertTrue(self.active())

    def test_another_linked_customer_remains_paid_and_unchanged(self):
        self.setup_checkout();self.activate()
        self.post('/api/register',{'name':'Other','email':'other@example.invalid','password':'Other-password-test'})
        self.post('/api/customer/logout',{})
        self.assertEqual(self.post('/api/customer/login',{'email':'other@example.invalid','password':'Other-password-test'}).status_code,200)
        txn='txn_'+'o'*26;sid='sub_'+'o'*26;ctm='ctm_'+'o'*26
        provider=base.MagicMock(status_code=201)
        provider.json.return_value={'data':{'id':txn,'checkout':{'url':'https://sandbox-pay.paddle.io/checkout/other'}}}
        with patch.object(self.module.requests,'post',return_value=provider):
            self.assertEqual(self.post('/api/paddle/checkout',{'plan':'pro','billing_cycle':'yearly'}).status_code,200)
        paid=self.event(number=30);paid['data'].update(id=txn,subscription_id=sid,customer_id=ctm)
        created=self.event('subscription.created',31);created['data'].update(id=sid,transaction_id=txn,customer_id=ctm)
        self.assertEqual(self.send(paid).status_code,200)
        self.assertEqual(self.send(created).status_code,200)
        before=next(row for row in self.rows('paddle_subscription_state') if row['paddle_subscription_id']==sid)
        sub=next(row for row in self.rows('subscriptions') if row['id']==before['local_subscription_id'])
        self.assertEqual(self.send(self.change()).status_code,200)
        self.assertTrue(self.active())
        self.assertEqual(next(row for row in self.rows('paddle_subscription_state') if row['paddle_subscription_id']==sid),before)
        self.assertEqual(next(row for row in self.rows('subscriptions') if row['id']==sub['id']),sub)
        self.assertEqual(self.rows('paddle_subscription_holds')[0]['paddle_subscription_id'],base.SID)

    def test_catalog_retirement_and_changed_negative_event_fail_closed(self):
        self.setup_checkout();self.activate()
        with patch.dict(self.module.PADDLE_PRICES['pro'],{'yearly':None}):
            self.assertEqual(self.send(self.event('subscription.updated',10)).status_code,200)
        self.assertFalse(self.active())
        # This stale negative event must not bypass refresh when a hold exists.
        event=self.change(11,-1);event['data']['status']='past_due'
        self.assertEqual(self.send(event).status_code,200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('paddle_subscription_state')[0]['lifecycle_status'],'active')


if __name__=='__main__':unittest.main()
