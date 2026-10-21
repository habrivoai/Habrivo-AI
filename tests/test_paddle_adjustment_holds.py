"""Conservative linkage/hold policy, NOT fixtures claiming Paddle refund semantics."""
from datetime import timedelta
import unittest
from unittest.mock import patch
import test_paddle_billing as base


class AdjustmentHoldTests(unittest.TestCase):
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

    def adjustment(self, number=10, **fields):
        event = self.event(number=number)
        event.update(event_type='adjustment.created', data={'transaction_id': base.TXN, 'customer_id': base.CTM})
        event['data'].update(fields)
        return event

    def test_linked_full_partial_and_ambiguous_requests_all_require_reconciliation(self):
        self.setup_checkout(); self.activate()
        before = self.rows('subscriptions')[0]
        for number, action in enumerate(('refund', 'partial_refund', 'dispute', 'unknown_action'), 10):
            # Deliberately opaque fields: none is treated as verified finality.
            event = self.adjustment(number, action=action, status='unverified_status', amount='1')
            self.assertEqual(self.send(event).status_code, 200)
            self.assertFalse(self.active())
        self.assertEqual(len(self.rows('paddle_payment_holds')), 1)
        self.assertEqual(self.rows('subscriptions')[0]['id'], before['id'])
        self.assertEqual(len(self.rows('paddle_payments')), 1)

    def test_duplicates_stale_updates_and_payment_replays_cannot_clear_hold(self):
        self.setup_checkout(); self.activate()
        event = self.adjustment()
        self.assertEqual(self.send(event).status_code, 200)
        hold = self.rows('paddle_payment_holds')
        self.assertEqual(self.send(event).get_json()['result'], 'duplicate')
        for number, status in enumerate(('pending', 'approved', 'rejected', 'reversed'), 11):
            update = self.adjustment(number, status=status)
            update['event_type'] = 'adjustment.updated'
            self.assertEqual(self.send(update).status_code, 200)
        self.assertEqual(self.send(self.event(number=20)).status_code, 200)
        self.assertEqual(self.send(self.event('subscription.updated', 21, self.occurred+timedelta(hours=1))).status_code, 200)
        self.assertEqual(self.rows('paddle_payment_holds'), hold)
        self.assertFalse(self.active())
        conflict = self.adjustment(); conflict['data']['status'] = 'changed'
        self.assertEqual(self.send(conflict).status_code, 409)
        self.assertFalse(self.active())

    def test_hold_before_initial_paid_proof_prevents_delayed_activation(self):
        self.setup_checkout()
        self.assertEqual(self.send(self.adjustment()).status_code, 200)
        self.assertEqual(self.rows('paddle_payments'), [])
        self.assertEqual(self.send(self.event('subscription.created', 2)).status_code, 200)
        self.assertEqual(self.send(self.event()).status_code, 200)
        self.assertFalse(self.active())
        self.assertEqual(self.rows('subscriptions'), [])
        self.assertEqual(len(self.rows('paddle_payment_holds')), 1)

    def test_unknown_family_member_is_held_not_silently_ignored(self):
        self.setup_checkout(); self.activate()
        event = self.adjustment(); event['event_type'] = 'adjustment.unrecognized_event'
        self.assertEqual(self.send(event).get_json()['result'], 'processed')
        self.assertFalse(self.active())

    def test_unknown_transaction_malformed_or_mismatched_linkage_changes_nothing(self):
        self.setup_checkout(); self.activate()
        for fields, expected in (({'transaction_id': 'txn_'+'x'*26}, 503),
                                 ({'transaction_id': []}, 400),
                                 ({'customer_id': 'ctm_'+'x'*26}, 422),
                                 ({'subscription_id': 'sub_'+'x'*26}, 422)):
            with self.subTest(fields=tuple(fields)):
                self.assertEqual(self.send(self.adjustment(**fields)).status_code, expected)
                self.assertEqual(self.rows('paddle_payment_holds'), [])
                self.assertTrue(self.active())
        empty = self.adjustment(); empty['data'] = {}
        self.assertEqual(self.send(empty).status_code, 400)

    def test_failed_receipt_rolls_back_hold_and_entitlement_then_retry_commits(self):
        self.setup_checkout(); self.activate()
        before = self.rows('subscriptions')
        original = self.module.conn
        class Failure:
            def __init__(self): self.db = original()
            def __enter__(self): self.db.__enter__(); return self
            def __exit__(self, *args): return self.db.__exit__(*args)
            def close(self): self.db.close()
            def execute(self, sql, parameters=()):
                if 'INSERT INTO paddle_webhook_events' in sql: raise RuntimeError('PRIVATE_FAILURE')
                return self.db.execute(sql, parameters)
        with patch.object(self.module, 'conn', side_effect=Failure):
            response = self.send(self.adjustment())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('PRIVATE', response.get_data(as_text=True))
        self.assertEqual(self.rows('paddle_payment_holds'), [])
        self.assertEqual(self.rows('subscriptions'), before)
        self.assertEqual(self.send(self.adjustment()).status_code, 200)
        self.assertFalse(self.active())
        receipt = next(row for row in self.rows('paddle_webhook_events') if row['event_id']==self.adjustment()['event_id'])
        self.assertEqual(receipt['payload_checksum'], base.billing.digest(self.adjustment()))

    def test_unrelated_valid_renewal_keeps_access_and_usage_after_old_payment_hold(self):
        self.setup_checkout()
        self.start -= timedelta(days=10); self.end -= timedelta(days=10)
        self.assertEqual(self.send(self.event()).status_code, 200)
        self.assertEqual(self.send(self.event('subscription.created', 2)).status_code, 200)
        before = self.rows('subscriptions')[0]
        db = self.module.conn()
        try:
            with db: db.execute('INSERT INTO promo_usage (customer_id,subscription_id) VALUES (?,?)', (self.uid, before['id']))
        finally: db.close()
        renewal = self.event(number=3)
        renewal['data'].update(id='txn_'+'r'*26, origin='subscription_recurring', billing_period={
            'starts_at':self.date(self.end), 'ends_at':self.date(self.end+timedelta(days=365))})
        self.assertEqual(self.send(renewal).status_code, 200)
        self.assertTrue(self.active())
        self.assertEqual(self.send(self.adjustment()).status_code, 200)
        self.assertTrue(self.active())
        self.assertEqual(self.rows('subscriptions')[0]['id'], before['id'])
        db = self.module.conn()
        try: self.assertEqual(db.execute('SELECT COUNT(*) FROM promo_usage WHERE subscription_id=?', (before['id'],)).fetchone()[0], 1)
        finally: db.close()
        held = self.adjustment(11, transaction_id='txn_'+'r'*26)
        self.assertEqual(self.send(held).status_code, 200)
        self.assertFalse(self.active())

    def test_holding_middle_period_does_not_bridge_past_and_future_proof(self):
        self.setup_checkout()
        self.start -= timedelta(days=10); self.end -= timedelta(days=10)
        self.assertEqual(self.send(self.event()).status_code, 200)
        self.assertEqual(self.send(self.event('subscription.created', 2)).status_code, 200)
        for number, letter, offset in ((3, 'r', 0), (4, 's', 365)):
            renewal = self.event(number=number)
            renewal['data'].update(id='txn_'+letter*26, origin='subscription_recurring', billing_period={
                'starts_at': self.date(self.end+timedelta(days=offset)),
                'ends_at': self.date(self.end+timedelta(days=offset+365))})
            self.assertEqual(self.send(renewal).status_code, 200)
        self.assertTrue(self.active())
        self.assertEqual(self.send(self.adjustment(transaction_id='txn_'+'r'*26)).status_code, 200)
        self.assertFalse(self.active())
        self.assertEqual(len(self.rows('paddle_payments')), 3)
        self.assertEqual(self.send(self.event('subscription.updated', 20, self.occurred+timedelta(hours=1))).status_code, 200)
        self.assertFalse(self.active())

    def test_signature_guard_and_retired_catalog_hold_remain_fail_closed(self):
        self.setup_checkout(); self.activate()
        self.assertEqual(self.send(self.adjustment(), secret='incorrect').status_code, 401)
        self.assertEqual(self.rows('paddle_payment_holds'), [])
        with patch.dict(self.module.PADDLE_PRICES['pro'], {'yearly': None}):
            self.assertEqual(self.send(self.adjustment()).status_code, 200)
        self.assertFalse(self.active())

    def test_different_customer_subscription_is_untouched(self):
        self.setup_checkout(); self.activate()
        self.post('/api/register', {'name':'Other', 'email':'other@example.invalid', 'password':'Other-password-test'})
        import test_customer_account as accounts
        other = accounts.lookup_customer_id(self.module, 'other@example.invalid')
        db = self.module.conn()
        try:
            with db: db.execute("INSERT INTO subscriptions (customer_id,plan,billing_cycle,amount_usd,status,payment_verified,expiry_date) VALUES (?,'starter','monthly',39,'active',1,'2099-01-01 00:00:00')", (other,))
            before = dict(db.execute('SELECT * FROM subscriptions WHERE customer_id=?', (other,)).fetchone())
        finally: db.close()
        event = self.adjustment(customer_id='ctm_'+'x'*26)
        self.assertEqual(self.send(event).status_code, 422)
        self.assertEqual(self.send(self.adjustment()).status_code, 200)
        db = self.module.conn()
        try: self.assertEqual(dict(db.execute('SELECT * FROM subscriptions WHERE customer_id=?', (other,)).fetchone()), before)
        finally: db.close()


if __name__ == '__main__': unittest.main()
