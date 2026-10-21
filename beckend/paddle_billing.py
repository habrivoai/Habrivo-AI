"""Paddle Billing ingestion. No network, storage or schema work at import time.

Narrow contract: server-created checkouts, one configured USD recurring catalog
price, quantity one, no trials/discounts/proration. Unsupported payments fail closed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import time
from uuid import uuid4

from flask import Blueprint, jsonify, request

MAX_BODY = 256 * 1024
CYCLES = {'monthly': ('month', 1), 'quarterly': ('month', 3),
          'half_yearly': ('month', 6), 'yearly': ('year', 1)}
STATES = {'active', 'trialing', 'past_due', 'paused', 'canceled'}
# Local fail-closed tie policy, not Paddle status chronology. Every state above
# active denies paid access; cancellation remains terminal at any later timestamp.
LIFECYCLE_PRECEDENCE = {'active': 0, 'trialing': 1, 'past_due': 2, 'paused': 3, 'canceled': 4}
LIFECYCLE = {'subscription.' + name for name in
             ('created', 'activated', 'updated', 'trialing', 'past_due', 'paused', 'resumed', 'canceled')}


class BillingError(Exception):
    def __init__(self, status=422):
        super().__init__('Billing event could not be verified.')
        self.status = status


def identifier(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r'_[a-z0-9]{26}', value) is not None


def stamp(value):
    """Canonical nanosecond UTC strings retain Paddle event ordering precision."""
    if not isinstance(value, str):
        raise BillingError(400)
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z', value)
    if not match:
        raise BillingError(400)
    try:
        datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        raise BillingError(400) from None
    return match[1] + '.' + (match[2] or '').ljust(9, '0') + 'Z'


def sql_stamp(value):
    return value[:19].replace('T', ' ')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


def verify_signature(raw, header, secret):
    if not isinstance(secret, str) or not secret.strip():
        raise BillingError(503)
    if not isinstance(header, str) or len(header) > 1024:
        raise BillingError(401)
    parts = header.split(';')
    timestamps, signatures = [], []
    for part in parts:
        key, sep, value = part.strip().partition('=')
        if not sep:
            raise BillingError(401)
        if key == 'ts':
            timestamps.append(value)
        elif key == 'h1' and re.fullmatch(r'[a-f0-9]{64}', value):
            signatures.append(value)
        else:
            raise BillingError(401)
    if (len(timestamps) != 1 or not re.fullmatch(r'\d{1,12}', timestamps[0])
            or not signatures or abs(time.time() - int(timestamps[0])) > 5):
        raise BillingError(401)
    expected = hmac.new(secret.encode(), timestamps[0].encode() + b':' + raw, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, signature) for signature in signatures):
        raise BillingError(401)


@contextmanager
def transaction(factory, config):
    db = factory()
    try:
        with db:
            if config.backend == 'sqlite':
                db.execute('PRAGMA busy_timeout = 3000')
                db.execute('BEGIN IMMEDIATE')
            else:
                db.execute("SET LOCAL lock_timeout = '3s'")
                db.execute('SELECT pg_advisory_xact_lock(724182607)')
            yield db
    finally:
        db.close()


def configured_offer(plan, cycle, plans, prices, products, environment):
    try:
        price = prices[plan][cycle]
        product = products[plan]
        amount = plans[plan]['prices'][cycle]
        if (environment not in {'sandbox', 'live'} or not identifier(price, 'pri')
                or not identifier(product, 'pro') or cycle not in CYCLES
                or type(amount) is not int or amount <= 0
                or sum(value == price for cycles in prices.values() for value in cycles.values()) != 1):
            raise BillingError(503)
        return {'plan': plan, 'billing_cycle': cycle, 'price_id': price,
                'product_id': product, 'amount_usd': amount, 'environment': environment}
    except (KeyError, TypeError):
        raise BillingError(503) from None


def create_checkout_link(factory, config, customer_id, offer):
    reference = uuid4().hex
    with transaction(factory, config) as db:
        if not db.execute('SELECT id FROM users WHERE id=?', (customer_id,)).fetchone():
            raise BillingError(401)
        db.execute('''INSERT INTO paddle_checkout_links
            (checkout_ref, customer_id, plan, billing_cycle, price_id, product_id, amount_usd, environment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                   (reference, customer_id, offer['plan'], offer['billing_cycle'], offer['price_id'],
                    offer['product_id'], offer['amount_usd'], offer['environment']))
    return reference


def bind_checkout(factory, config, reference, transaction_id):
    if not identifier(transaction_id, 'txn'):
        raise BillingError(502)
    with transaction(factory, config) as db:
        result = db.execute('''UPDATE paddle_checkout_links SET transaction_id=?
            WHERE checkout_ref=? AND transaction_id IS NULL''', (transaction_id, reference))
        if result.rowcount != 1:
            raise BillingError(503)


def receipt_offer(receipt, settings):
    offer = configured_offer(receipt['plan'], receipt['billing_cycle'], **settings)
    if any(receipt[key] != value for key, value in offer.items()):
        raise BillingError(503)
    return offer


def check_item(data, offer, payment=False):
    items = data.get('items')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise BillingError()
    item = items[0]
    price = item.get('price')
    if (not isinstance(price, dict) or type(item.get('quantity')) is not int or item['quantity'] != 1
            or price.get('id') != offer['price_id'] or price.get('product_id') != offer['product_id']
            or price.get('type') != 'standard' or price.get('trial_period') is not None
            or price.get('unit_price_overrides')
            or price.get('billing_cycle') != dict(zip(('interval', 'frequency'), CYCLES[offer['billing_cycle']]))
            or price.get('unit_price') != {'amount': str(offer['amount_usd'] * 100), 'currency_code': 'USD'}
            or data.get('currency_code') != 'USD' or data.get('collection_mode') != 'automatic'):
        raise BillingError()
    if payment:
        details = data.get('details')
        if not isinstance(details, dict):
            raise BillingError()
        totals, lines = details.get('totals'), details.get('line_items')
        if (not isinstance(totals, dict)
                or any(not isinstance(totals.get(key), str) or not re.fullmatch(r'[0-9]{1,12}', totals[key])
                       for key in ('subtotal', 'tax', 'total'))
                or not (int(totals['subtotal']) == offer['amount_usd'] * 100
                        or int(totals['total']) == offer['amount_usd'] * 100)
                or int(totals['subtotal']) + int(totals['tax']) != int(totals['total'])
                or totals.get('discount') != '0' or totals.get('credit') != '0'
                or not isinstance(totals.get('total'), str) or not re.fullmatch(r'[0-9]{1,12}', totals['total'])
                or int(totals['total']) < offer['amount_usd'] * 100
                or not isinstance(lines, list) or len(lines) != 1 or not isinstance(lines[0], dict)
                or lines[0].get('price_id') != offer['price_id']
                or type(lines[0].get('quantity')) is not int or lines[0]['quantity'] != 1
                or lines[0].get('proration') is not None or data.get('discount_id') is not None):
            raise BillingError()
        payments = data.get('payments')
        if not isinstance(payments, list) or len(payments) > 100:
            raise BillingError()
        captured = 0
        for attempt in payments:
            if not isinstance(attempt, dict):
                raise BillingError()
            if attempt.get('status') == 'captured':
                amount = attempt.get('amount')
                if not isinstance(amount, str) or not re.fullmatch(r'[0-9]{1,12}', amount):
                    raise BillingError()
                captured += int(amount)
        if captured <= 0 or captured != int(totals['total']):
            raise BillingError()


def period(value, cycle):
    if not isinstance(value, dict):
        raise BillingError()
    start, end = stamp(value.get('starts_at')), stamp(value.get('ends_at'))
    days = (datetime.fromisoformat(end.replace('Z', '+00:00')) -
            datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds() / 86400
    months = {'monthly': 1, 'quarterly': 3, 'half_yearly': 6, 'yearly': 12}[cycle]
    if not 27 * months <= days <= 31 * months + 1:
        raise BillingError()
    return start, end


def get_state(db, subscription_id):
    return db.execute('SELECT * FROM paddle_subscription_state WHERE paddle_subscription_id=?',
                      (subscription_id,)).fetchone()


def resolve_state(db, data, kind, settings):
    sid = data.get('subscription_id') if kind.startswith('transaction.') else data.get('id')
    if not identifier(sid, 'sub') or not identifier(data.get('customer_id'), 'ctm'):
        raise BillingError()
    state = get_state(db, sid)
    initial = data.get('id') if kind == 'transaction.completed' else data.get('transaction_id')
    if not state:
        if not identifier(initial, 'txn'):
            raise BillingError(503)  # Earlier subscription.created may still be in flight.
        receipt = db.execute('SELECT * FROM paddle_checkout_links WHERE transaction_id=?', (initial,)).fetchone()
        if not receipt:
            raise BillingError(503)  # Retry; never trust custom_data as a substitute.
        offer = receipt_offer(receipt, settings)
        check_item(data, offer, kind == 'transaction.completed')
        if kind == 'transaction.completed' and data.get('origin') not in {'api', 'web'}:
            raise BillingError()
        if not db.execute('SELECT id FROM users WHERE id=?', (receipt['customer_id'],)).fetchone():
            raise BillingError()
        # A Paddle customer cannot be rebound to another Habrivo account.
        other = db.execute('''SELECT customer_id FROM paddle_subscription_state
            WHERE paddle_customer_id=? AND customer_id<>? LIMIT 1''',
                           (data['customer_id'], receipt['customer_id'])).fetchone()
        if other:
            raise BillingError()
        db.execute('''INSERT INTO paddle_subscription_state
            (paddle_subscription_id, customer_id, paddle_customer_id, initial_transaction_id,
             checkout_ref, lifecycle_status) VALUES (?, ?, ?, ?, ?, 'pending')''',
                   (sid, receipt['customer_id'], data['customer_id'], initial, receipt['checkout_ref']))
        state = get_state(db, sid)
    if state['paddle_customer_id'] != data['customer_id']:
        raise BillingError()
    receipt = db.execute('SELECT * FROM paddle_checkout_links WHERE checkout_ref=?',
                         (state['checkout_ref'],)).fetchone()
    if not receipt or receipt['customer_id'] != state['customer_id']:
        raise BillingError()
    if not db.execute('SELECT id FROM users WHERE id=?', (state['customer_id'],)).fetchone():
        raise BillingError()
    if kind in LIFECYCLE:
        # Resolve ownership from stored linkage before validating catalog changes.
        # Positive lifecycle events validate current configuration or latch a hold.
        if receipt['environment'] != settings['environment']:
            raise BillingError(503)
        offer = {key: receipt[key] for key in
                 ('plan', 'billing_cycle', 'price_id', 'product_id', 'amount_usd', 'environment')}
    else:
        offer = receipt_offer(receipt, settings)
    return state, offer


def refresh(db, sid, offer):
    """Publish only intersecting lifecycle permission and independently paid time."""
    state = get_state(db, sid)
    # Removing one proof must not bridge an unpaid gap with MIN/MAX dates.
    periods = db.execute('''SELECT p.starts_at, p.ends_at FROM paddle_payments p
        WHERE p.paddle_subscription_id=? AND p.starts_at IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM paddle_payment_holds h WHERE h.transaction_id=p.transaction_id)
        ORDER BY p.starts_at, p.ends_at''', (sid,)).fetchall()
    windows = []
    for row in periods:
        start, end = row['starts_at'], row['ends_at']
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + '000Z'
    selected = next((window for window in windows if window[0] <= now < window[1]),
                    windows[-1] if windows else None)
    first = selected[0] if selected else None
    paid = {'ends_at': selected[1]} if selected else None
    held = db.execute('SELECT customer_id FROM paddle_subscription_holds WHERE paddle_subscription_id=?',
                      (sid,)).fetchone()
    if held and held['customer_id'] != state['customer_id']:
        raise BillingError()
    active = bool(state['lifecycle_status'] == 'active' and paid and not held)
    if not state['local_subscription_id'] and not paid:
        return
    if not state['local_subscription_id']:
        # Existing subscriptions remain untouched; this ID is owned by this provider binding.
        db.execute('''INSERT INTO subscriptions
            (customer_id, plan, billing_cycle, currency, amount_usd, status, payment_reference,
             start_date, expiry_date, payment_verified) VALUES (?, ?, ?, 'USD', ?, ?, ?, ?, ?, ?)''',
                   (state['customer_id'], offer['plan'], offer['billing_cycle'], offer['amount_usd'],
                    'active' if active else state['lifecycle_status'], state['initial_transaction_id'],
                    sql_stamp(first), sql_stamp(paid['ends_at']), int(active)))
        rows = db.execute('SELECT id FROM subscriptions WHERE customer_id=? AND payment_reference=?',
                          (state['customer_id'], state['initial_transaction_id'])).fetchall()
        if len(rows) != 1:
            raise BillingError()
        db.execute('UPDATE paddle_subscription_state SET local_subscription_id=? WHERE paddle_subscription_id=?',
                   (rows[0]['id'], sid))
        state = get_state(db, sid)
    updated = db.execute('''UPDATE subscriptions SET plan=?, billing_cycle=?, amount_usd=?, status=?,
        payment_verified=?, start_date=?, expiry_date=? WHERE id=? AND customer_id=?''',
               (offer['plan'], offer['billing_cycle'], offer['amount_usd'],
                'active' if active else state['lifecycle_status'], int(active),
                sql_stamp(first) if paid else None,
                sql_stamp(paid['ends_at']) if paid else None,
                state['local_subscription_id'], state['customer_id']))
    if updated.rowcount != 1:
        raise BillingError(503)


def apply_payment(db, data, state, offer):
    check_item(data, offer, payment=True)
    if data.get('status') != 'completed' or not identifier(data.get('id'), 'txn'):
        raise BillingError()
    hold = db.execute('SELECT * FROM paddle_payment_holds WHERE transaction_id=?', (data['id'],)).fetchone()
    if hold and (hold['customer_id'] != state['customer_id'] or
                 (hold['paddle_customer_id'] and hold['paddle_customer_id'] != data['customer_id'])):
        raise BillingError()
    initial = data['id'] == state['initial_transaction_id']
    if (initial and data.get('origin') not in {'api', 'web'}) or (
            not initial and data.get('origin') != 'subscription_recurring'):
        raise BillingError()
    start, end = (None, None)
    if data.get('billing_period') is not None:
        start, end = period(data['billing_period'], offer['billing_cycle'])
    elif not initial:
        raise BillingError()
    proof = digest([data['id'], state['paddle_subscription_id'], data['customer_id'],
                    offer, start, end, data['details']['totals']['total']])
    existing = db.execute('SELECT * FROM paddle_payments WHERE transaction_id=?', (data['id'],)).fetchone()
    if existing:
        if existing['proof_checksum'] != proof:
            raise BillingError(409)
        return
    if initial and state['initial_start']:
        if start and (start != state['initial_start'] or end != state['initial_end']):
            raise BillingError()
        start, end = state['initial_start'], state['initial_end']
    if not initial:
        latest = db.execute('''SELECT ends_at FROM paddle_payments
            WHERE paddle_subscription_id=? AND ends_at IS NOT NULL ORDER BY ends_at DESC LIMIT 1''',
                            (state['paddle_subscription_id'],)).fetchone()
        if not latest or start > latest['ends_at']:
            raise BillingError(503)  # Missing earlier payment; retry, never bridge unpaid gaps.
    db.execute('''INSERT INTO paddle_payments
        (transaction_id, paddle_subscription_id, starts_at, ends_at, proof_checksum)
        VALUES (?, ?, ?, ?, ?)''', (data['id'], state['paddle_subscription_id'], start, end, proof))
    # Payment proofs never override subscription lifecycle state. Recovery needs
    # an independently verified active/updated subscription event as well.


def advance_lifecycle(db, state, status, occurred):
    previous = state['lifecycle_at']
    if previous and occurred < previous:
        return
    if (previous == occurred
            and LIFECYCLE_PRECEDENCE[status] < LIFECYCLE_PRECEDENCE[state['lifecycle_status']]):
        return
    if state['lifecycle_status'] == 'canceled' and status != 'canceled':
        return
    db.execute('UPDATE paddle_subscription_state SET lifecycle_status=?, lifecycle_at=? WHERE paddle_subscription_id=?',
               (status, occurred, state['paddle_subscription_id']))


def hold_subscription(db, state, status, occurred, event):
    sid = state['paddle_subscription_id']
    old = db.execute('SELECT * FROM paddle_subscription_holds WHERE paddle_subscription_id=?', (sid,)).fetchone()
    if old and old['customer_id'] != state['customer_id']:
        raise BillingError()
    watermark = max(occurred, state['lifecycle_at'] or occurred, old['watermark'] if old else occurred)
    if not old:
        checksum = digest({key: event[key] for key in ('event_id','event_type','occurred_at','data')})
        db.execute('''INSERT INTO paddle_subscription_holds
            (paddle_subscription_id, customer_id, first_event_id, first_payload_checksum, watermark, reason)
            VALUES (?, ?, ?, ?, ?, 'unsupported_catalog_requires_reconciliation')''',
                   (sid, state['customer_id'], event['event_id'], checksum, watermark))
    elif watermark > old['watermark']:
        db.execute('UPDATE paddle_subscription_holds SET watermark=? WHERE paddle_subscription_id=?', (watermark, sid))
    # Unsupported positive states never replace the known lifecycle status.
    # Restrictive states still participate in the existing cancellation precedence.
    if status != 'active':
        advance_lifecycle(db, state, status, occurred)
    db.execute('''UPDATE paddle_subscription_state SET lifecycle_at=?
        WHERE paddle_subscription_id=? AND (lifecycle_at IS NULL OR lifecycle_at < ?)''',
               (watermark, sid, watermark))


def apply_lifecycle(db, data, kind, state, offer, occurred, event=None, settings=None):
    status = data.get('status')
    expected = {'subscription.activated': 'active', 'subscription.resumed': 'active',
                'subscription.trialing': 'trialing', 'subscription.canceled': 'canceled',
                'subscription.paused': 'paused', 'subscription.past_due': 'past_due'}
    if status not in STATES or (kind in expected and expected[kind] != status):
        raise BillingError()
    if kind == 'subscription.created' and data.get('transaction_id') != state['initial_transaction_id']:
        raise BillingError()
    try:
        if status in {'active', 'trialing'} or data.get('items') is not None:
            check_item(data, offer)
        if settings is not None and status == 'active':
            receipt = db.execute('SELECT * FROM paddle_checkout_links WHERE checkout_ref=?',
                                 (state['checkout_ref'],)).fetchone()
            receipt_offer(receipt, settings)
    except BillingError:
        if event is None:
            raise
        hold_subscription(db, state, status, occurred, event)
        return
    if kind == 'subscription.created':
        check_item(data, offer)
        if data.get('transaction_id') != state['initial_transaction_id']:
            raise BillingError()
        if status == 'active':
            start, end = period(data.get('current_billing_period'), offer['billing_cycle'])
            if state['initial_start'] and (state['initial_start'], state['initial_end']) != (start, end):
                raise BillingError(409)
            paid = db.execute('SELECT * FROM paddle_payments WHERE transaction_id=?',
                              (state['initial_transaction_id'],)).fetchone()
            if paid and paid['starts_at'] and (paid['starts_at'], paid['ends_at']) != (start, end):
                raise BillingError()
            db.execute('''UPDATE paddle_subscription_state SET initial_start=?, initial_end=?
                WHERE paddle_subscription_id=?''', (start, end, state['paddle_subscription_id']))
            db.execute('''UPDATE paddle_payments SET starts_at=?, ends_at=?
                WHERE transaction_id=? AND starts_at IS NULL''', (start, end, state['initial_transaction_id']))
    elif status in {'active', 'trialing'}:
        check_item(data, offer)
    advance_lifecycle(db, state, status, occurred)


def apply_adjustment_hold(db, data, event, settings):
    """Do not interpret action/status/amount as verified provider semantics.

    Recognize only a signed adjustment-family envelope and a known transaction
    identifier. All such events require reconciliation; none automatically release
    a hold. No provider fields beyond the linkage candidates authorize a grant.
    """
    txn = data.get('transaction_id')
    if not identifier(txn, 'txn'):
        raise BillingError(400)
    payment = db.execute('SELECT * FROM paddle_payments WHERE transaction_id=?', (txn,)).fetchone()
    state = get_state(db, payment['paddle_subscription_id']) if payment else None
    receipt = db.execute('SELECT * FROM paddle_checkout_links WHERE ' +
                         ('checkout_ref=?' if state else 'transaction_id=?'),
                         (state['checkout_ref'] if state else txn,)).fetchone()
    if not receipt or receipt['environment'] != settings['environment']:
        raise BillingError(503)
    if state and state['customer_id'] != receipt['customer_id']:
        raise BillingError()
    ctm = data.get('customer_id')
    if 'customer_id' in data:
        if not identifier(ctm, 'ctm') or (state and ctm != state['paddle_customer_id']):
            raise BillingError()
        other = db.execute('SELECT customer_id FROM paddle_subscription_state WHERE paddle_customer_id=? AND customer_id<>? LIMIT 1',
                           (ctm, receipt['customer_id'])).fetchone()
        if other:
            raise BillingError()
    if 'subscription_id' in data and (not state or data['subscription_id'] != state['paddle_subscription_id']):
        raise BillingError(503 if not state else 422)
    old = db.execute('SELECT * FROM paddle_payment_holds WHERE transaction_id=?', (txn,)).fetchone()
    if old:
        if (old['customer_id'] != receipt['customer_id'] or
                (ctm and old['paddle_customer_id'] and ctm != old['paddle_customer_id'])):
            raise BillingError(409)
    else:
        db.execute('''INSERT INTO paddle_payment_holds
            (transaction_id, customer_id, paddle_customer_id, first_event_id, occurred_at, reason)
            VALUES (?, ?, ?, ?, ?, 'adjustment_requires_reconciliation')''',
                   (txn, receipt['customer_id'], ctm, event['event_id'], stamp(event['occurred_at'])))
    if state:
        offer = {key: receipt[key] for key in
                 ('plan','billing_cycle','price_id','product_id','amount_usd','environment')}
        refresh(db, state['paddle_subscription_id'], offer)


def process(factory, config, event, settings):
    kind, data = event['event_type'], event['data']
    occurred = stamp(event['occurred_at'])
    event_hash = digest({key: event[key] for key in ('event_id', 'event_type', 'occurred_at', 'data')})
    with transaction(factory, config) as db:
        old = db.execute('SELECT payload_checksum FROM paddle_webhook_events WHERE event_id=?',
                         (event['event_id'],)).fetchone()
        if old:
            if old['payload_checksum'] != event_hash:
                raise BillingError(409)
            return 'duplicate'
        outcome = 'ignored'
        if kind.startswith('adjustment.'):
            apply_adjustment_hold(db, data, event, settings)
            outcome = 'processed'
        elif kind == 'transaction.completed' or kind in LIFECYCLE:
            state, offer = resolve_state(db, data, kind, settings)
            if kind == 'transaction.completed':
                apply_payment(db, data, state, offer)
            else:
                apply_lifecycle(db, data, kind, state, offer, occurred, event=event, settings=settings)
            revoked_status = data.get('status') in {'canceled', 'paused', 'past_due', 'trialing'}
            # A stale negative event may be recorded, but its retired mapping must
            # never republish active access. Only current configured offers grant.
            subscription_held = db.execute('SELECT customer_id FROM paddle_subscription_holds WHERE paddle_subscription_id=?',
                                           (state['paddle_subscription_id'],)).fetchone()
            if subscription_held or not (kind in LIFECYCLE and revoked_status
                    and get_state(db, state['paddle_subscription_id'])['lifecycle_status'] == 'active'):
                refresh(db, state['paddle_subscription_id'], offer)
            outcome = 'processed'
        # Payment failures, transaction.paid, initiated checkouts and unknown events
        # never grant or extend access. Actual past_due/paused/canceled lifecycle
        # events restrict access; a failed unrelated charge must not revoke it.
        db.execute('''INSERT INTO paddle_webhook_events
            (event_id, event_type, occurred_at, payload_checksum, outcome) VALUES (?, ?, ?, ?, ?)''',
                   (event['event_id'], kind, occurred, event_hash, outcome))
    return outcome


def create_blueprint(factory, config, secret_getter, settings_getter):
    blueprint = Blueprint('paddle_billing', __name__)

    @blueprint.post('/api/paddle/webhook')
    def webhook():
        try:
            if request.content_length is not None and request.content_length > MAX_BODY:
                raise BillingError(413)
            raw = request.stream.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise BillingError(413)
            verify_signature(raw, request.headers.get('Paddle-Signature'), secret_getter())
            if request.mimetype != 'application/json':
                raise BillingError(400)
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise BillingError(400)
                    result[key] = value
                return result
            event = json.loads(raw, object_pairs_hook=unique_object,
                               parse_constant=lambda value: (_ for _ in ()).throw(BillingError(400)))
            if (not isinstance(event, dict) or not identifier(event.get('event_id'), 'evt')
                    or not isinstance(event.get('event_type'), str)
                    or not re.fullmatch(r'[a-z_]+\.[a-z_]+', event['event_type'])
                    or len(event['event_type']) > 80 or not isinstance(event.get('data'), dict)):
                raise BillingError(400)
            occurred = stamp(event.get('occurred_at'))
            if datetime.fromisoformat(occurred.replace('Z', '+00:00')).timestamp() > time.time() + 5:
                raise BillingError(400)
            result = process(factory, config, event, settings_getter())
            return jsonify(success=True, result=result)
        except BillingError as error:
            return jsonify(success=False, error='Billing event could not be verified.'), error.status
        except (ValueError, UnicodeError, RecursionError, KeyError, TypeError):
            return jsonify(success=False, error='Invalid billing event.'), 400
        except Exception:
            # No payload, SQL, credentials, identifiers or provider error bodies logged.
            return jsonify(success=False, error='Billing persistence is unavailable. Retry later.'), 503

    @blueprint.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    return blueprint
