# Paddle Billing webhook (development implementation)

`POST /api/paddle/webhook` is a provider endpoint, not a browser payment-success
endpoint. No real Paddle request, payment, production database, deployment or
configuration change was performed while implementing this work.

## Required configuration before accepting a checkout

Keep sandbox and live credentials, catalog IDs, notification destination secrets,
and databases separate. Never reuse the historical `habrivo-db`.

- `PADDLE_ENVIRONMENT`: `sandbox` (default) or explicitly `live`.
- `PADDLE_API_KEY`: the matching environment's backend API key.
- `PADDLE_WEBHOOK_SECRET`: **notification destination** secret, not an API key,
  client token, application `SECRET_KEY`, or public Paddle.js credential.
- Existing per-plan/per-cycle price environment names in `beckend/app.py`.
  The legacy Starter quarterly name remains `PADDLE_STARTER_QUARTERLY_PRICE_ID`.
- `PADDLE_PRODUCT_STARTER`, `PADDLE_PRODUCT_PRO`, `PADDLE_PRODUCT_PREMIUM`:
  matching catalog product IDs for the plans being offered.

A plan/cycle is offered for checkout only with its unique valid price ID, matching
product ID, positive server-configured USD price, webhook secret and API key.
No configuration values are returned publicly. Price IDs must not be reused for
different offers. Annual prices must use a one-year billing cycle; quarterly and
half-year prices use three and six months respectively. Public app prices must
be reconciled with the actual catalog before any payments. Malformed/unconfigured
selections fail before network access.

## Persistence and trusted customer linkage

PostgreSQL needs explicit reviewed migration `0004_paddle_billing.sql`, following
0001–0003 using the existing migration runner. The additional conservative
reconciliation hold table requires explicit migration `0005_paddle_payment_holds.sql`. Subscription catalog holds additionally require
`0006_paddle_subscription_holds.sql`. The application never migrates
PostgreSQL on import. No PostgreSQL migration was applied in this task. Local
SQLite initialization adds the equivalent tables idempotently; existing records
and original schemas are not rewritten.

New durable tables:

- `paddle_checkout_links`: authenticated local customer, server offer and opaque
  reference, then the actual transaction ID returned by the backend Paddle API.
- `paddle_subscription_state`: immutable provider subscription/customer/checkout
  binding, local subscription ID, lifecycle version and initial paid-period context.
- `paddle_payments`: captured transaction proof digest and paid periods.
- `paddle_payment_holds`: monotonic transaction-scoped reconciliation holds, local
  owner, optional provider customer assertion, first event ID and occurrence time.
  No raw adjustment amounts, action/status strings or payloads are stored here.
- `paddle_webhook_events`: event ID, type, time, canonical payload checksum and
  processing outcome. No raw payload, email, hash, name or address is stored here.

Checkout writes the link before calling Paddle, sends only an opaque reference in
`custom_data`, and binds the returned transaction ID before returning a checkout
URL. Webhooks use that stored transaction ID, **not** custom_data, browser IDs,
prices, cookies or redirect parameters, to identify the local customer. A known
Paddle customer cannot be attached to another local account. Subscription and
payment identifiers cannot be reused across bindings.

An early webhook before transaction binding gets 503 for retry. If Paddle creates
a transaction but the API response/binding cannot be persisted, automatic recovery
is deliberately unavailable: do not repeat payments blindly. Separately authorized
sandbox reconciliation/recovery tooling is still required. Multiple distinct
checkout requests are not deduplicated by this change; only provider events and
completed transactions are idempotent. No local subscription is activated during
checkout initiation.

## Verification and event behavior

The raw request body (maximum 256 KiB) is verified **before** JSON parsing, using
HMAC-SHA256 over `ts:raw_body`, timing-safe signature comparison and a five-second
past/future tolerance. Multiple `h1` values are accepted, one `ts` is required.
Unsigned, malformed, stale, future-dated signatures, duplicate JSON keys and
non-finite JSON constants are rejected. Clock synchronization is operationally
required. A provider retry must carry a freshly signed request; old event occurrence
times themselves are allowed; future occurrence times beyond five seconds are rejected
to avoid poisoning lifecycle ordering. Supported occurrence timestamps are UTC RFC3339 with
up to nine fractional digits, retained exactly for ordering.

Supported paid scope: one standard configured recurring USD catalog item,
quantity one, automatic collection, no trials, country price overrides, discounts,
credits or proration. Tax-inclusive and tax-exclusive totals are checked against
the configured base amount; captured amounts must equal the final positive total.
Unsupported paid shapes are rejected without applying any entitlement.

- `transaction.completed`: requires completed status and captured payment proof,
  matching product/price/amount/currency/cycle and a verified server checkout link
  or existing subscription binding. Initial origins `api`/`web` are accepted;
  subsequent payments must be `subscription_recurring`. No one-time, payment-method
  change or subscription-update/prorated charge activates access.
- `subscription.created`: the initial `transaction_id` must match the stored link.
  Active state alone is insufficient; captured payment proof is also required.
  If the initial transaction has no billing period, the created event supplies it,
  and no access is published until both events arrive. Either delivery order works.
- Activation, update and resume events can permit access only within an already
  independently paid period and with the original configured catalog item.
  Subscription status changes alone cannot extend paid time or upgrade a plan.
- Renewal updates the **same** local subscription ID. Usage history and original
  start remain intact. Paid expiry only advances to the maximum verified paid end;
  an early next-period payment does not hide current paid access. Missing intervening
  payments/gaps return 503 for retry rather than bridging unpaid time.
- Scheduled cancellation in an active `subscription.updated` preserves paid access.
  Actual `subscription.canceled` revokes it; canceled subscriptions are terminal.
- `subscription.paused`, `subscription.past_due`, and `subscription.trialing` revoke
  paid access. No grace period/free trial is granted. A recovery payment alone
  cannot override lifecycle state: a later independently verified active/updated
  event is required, and it still needs a paid period. Payments cannot undo
  pause/cancellation or advance the subscription lifecycle ordering watermark.
- `transaction.payment_failed` never grants or extends access. It is recorded
  without changing an unrelated/currently paid period; actual `subscription.past_due`
  restricts access. `transaction.paid`, ready/created and other event types are also
  recorded as ignored and cannot activate anything.
- Expiration follows existing server UTC start/expiry entitlement checks. No
  invented `subscription.expired` event is required. The subscription status
  endpoint now excludes future start dates, matching Marketing Studio entitlements.

Events, payment proofs, state and local subscription writes commit together or
roll back together. SQLite uses BEGIN IMMEDIATE and a three-second busy timeout;
PostgreSQL uses transaction-scoped advisory lock 724182607 and a three-second
lock timeout. The event receipt is inserted in the same transaction after updates.
Duplicate events survive restarts and different notification IDs; a changed payload
for an existing event ID returns 409. Transactions also have unique proof receipts.
Lifecycle events compare nanosecond occurrence times; older states cannot regress
newer ones. Equal-time states use the deterministic local precedence
`canceled > paused > past_due > trialing > active`, independent of delivery order.
All restrictive states deny paid access; this is a fail-closed tie policy, not
Paddle's status chronology. Same-time active/resume events cannot override a
restriction, and canceled subscriptions remain terminal for later events too.
Tie handling, entitlement changes and event checksums commit or roll back together.
Raw payloads,
SQL errors, credentials and customer data are not printed in handler diagnostics.

Known negative lifecycle events can revoke an already-bound subscription even if
its catalog mapping was retired. Retired mappings can never authorize a new grant.
Keep notification secrets and environment linkage valid throughout cancellation.

## Response handling and operational limits

200 means processed, duplicate or explicitly ignored; it does not necessarily mean
paid access. 400/401/413 reject invalid input; 409 indicates conflicting receipts or
versions; 422 rejects unsupported/unverifiable billing semantics; 503 requests retry
for configuration, missing earlier linkage/payments or storage failures. No raw
payloads are echoed. Monitor these statuses using restricted metadata and alerts;
operator tooling and dead-letter/reconciliation workflows are not implemented.

The extra tables contain account/provider identifiers and must join the backup,
restore and retention policy. The existing source-only SQLite validator/importer
still intentionally supports only its original four-table schema; it is **not** a
backup or migration tool for the expanded SaaS database. Do not drop new tables or
billing receipts to make an old importer accept a database.

Refunds, chargebacks/adjustments, manual invoices, paid trials, discounts, proration,
currency changes, plan changes, paused billing gaps, legacy unlinked subscriptions,
merchant-hosted checkout completion and payment-method changes need separate design,
tests and authorized sandbox verification. Adjustment events now use the bounded
conservative hold policy below; final refund/dispute semantics and automated
reconciliation remain unsupported. This is **not production launch approval**. A live provider misconfiguration fails closed but may leave a paying user
without access; resolve it before accepting money. Free AI remains disabled.

## Verification before any future launch (requires separate authorization)

1. Independently review signature checks, linkage, state policy and migration.
2. Run all six guarded PostgreSQL tests plus dedicated webhook SQL/HTTP tests against
   an independently identified disposable local PostgreSQL target. This step ran
   only offline synthetic SQLite HTTP tests and a mocked PostgreSQL lock contract.
3. In a separately authorized Paddle **sandbox**, reconcile actual catalog IDs,
   cycle, USD prices/tax totals, checkout URL configuration and real payload shapes.
4. Configure the sandbox notification destination for all listed subscription events,
   `transaction.completed` and `transaction.payment_failed`. Exercise creation in
   both delivery orders, fresh retries, duplicate/concurrent delivery, renewal,
   past_due/recovery, scheduled/immediate cancellation and expiration. Confirm the
   five-second signature clock tolerance through the real transport.
5. Add/verify refund and dispute policy, reconciliation/alerting, checkout recovery,
   durable database/storage, backup/restore, deployment controls and rollback.
6. Only after owner authorization consider live credentials, reviewed explicit
   migrations and deployment. Never use an unrelated historical service.

Public provider documentation used for the implementation:

- [Signature verification](https://developer.paddle.com/webhooks/signature-verification)
- [Completed transactions](https://developer.paddle.com/webhooks/transactions/transaction-completed)
- [Created subscriptions and transaction linkage](https://developer.paddle.com/webhooks/subscriptions/subscription-created)
- [Cancellation and scheduled changes](https://developer.paddle.com/webhooks/subscriptions/subscription-canceled)
- [Provisioning and out-of-order events](https://developer.paddle.com/build/subscriptions/provision-access-webhooks)

## Conservative adjustment reconciliation holds (STEP 47)

The repository contains no authoritative Paddle adjustment payload/status contract.
No external provider documentation or Paddle service was accessed to implement this
step. Consequently this implementation does **not** claim to identify final refunds,
partial refunds, dispute outcomes, credits, approval, rejection or reversals of an
adjustment. Synthetic action/status values in tests are deliberately opaque, not
assertions about Paddle's API schema.

After normal raw-body signature verification, **every** `adjustment.*` event uses
only a valid `data.transaction_id` linkage candidate. This field's presence and
actual provider event shapes still need authoritative provider verification. The
transaction must match an existing payment or a server-bound initial checkout in
the current environment. Optional customer/subscription linkage candidates must
match known ownership; mismatches fail without changes. Unknown transactions return
503 for retry rather than being acknowledged as harmless. Missing/malformed
transaction linkage returns 400. Unknown adjustment-family event names receive the
same conservative policy, not silent ignore. A standalone invented `dispute.*`
family has no implemented provider contract and must not be treated as supported.

A correctly linked event creates an irreversible-through-this-API reconciliation
hold on **that transaction only**, regardless of any action, status or amount in
the payload. This intentionally suspends proof even for an ambiguous, partial,
pending, rejected or informational adjustment until separately authorized human
reconciliation can establish the actual outcome. It is a local fail-closed policy,
not a determination that Paddle has finalized a refund or lost a dispute. An
informational event can therefore temporarily deny a legitimate paying customer.
No release endpoint or automatic restoration policy is provided in this step.

Holds, entitlement recomputation and event receipts commit in the existing billing
transaction or all roll back. Duplicate event IDs still require identical canonical
payload checksums. Different/stale/conflicting adjustment events never remove a
hold, nor do active/resumed lifecycle events or replayed completed payments. A known
initial checkout can be held before its paid proof arrives; later proof remains
ineligible. An unknown renewal transaction arriving before its completed payment
cannot yet be linked and returns 503; its later retry must be monitored. This is
not an assertion that event arrival delays can be eliminated.

Entitlement recomputation excludes held payments and merges only contiguous or
overlapping **unheld** paid periods. It chooses the component covering server UTC
now, otherwise a component whose start/expiry checks cannot grant current access.
It never bridges an unpaid hole using the earliest and latest remaining dates.
Holding an old payment therefore does not invalidate an unrelated currently paid
renewal, and holding a future renewal does not consume the current valid period.
Subscription IDs and promo_usage remain intact; the effective start/expiry can
change to reflect only the eligible coverage component. Original payment proofs
remain retained for audit rather than overwritten/deleted.

Migration 0005 is additive; no migration was applied to PostgreSQL here. Local
SQLite initialization creates the table idempotently. The snapshot validator and
source-only importer still do not support this expanded billing schema. Include
holds in backups: losing them could restore invalid proof.

Required later work: independent security/database review, dedicated disposable
PostgreSQL HTTP and rollback tests, authoritative Paddle adjustment event/type/status
and transaction-link verification, full/partial refund and dispute policy, unknown
transaction retry monitoring, and a separately authorized audited reconciliation
and release design. Unsupported plan-change ordering is guarded by the STEP 48
subscription hold policy below; its implementation still requires independent review.
No sandbox or production readiness is claimed.

## Unsupported subscription catalog changes (STEP 48)

Migration `0006_paddle_subscription_holds.sql` adds durable per-subscription
reconciliation holds. Local SQLite initialization is additive and idempotent;
PostgreSQL migration remains explicit and was not applied in this step.

After signature, existing subscription identity, provider customer, local owner,
and environment checks, lifecycle events validate the stored checkout's original
catalog item. Positive events also validate that the server configuration still
matches the original receipt. Unsupported upgrade/downgrade, changed product,
price, amount/cycle/currency/quantity or unverifiable item details latch a hold.
A retired/missing configuration on an already-bound positive lifecycle event also
fails closed through this policy. Initial unknown/unbound subscriptions still
must pass normal checkout-link validation; an unsupported offer cannot establish
a new paid binding. Invalid status/event combinations or ownership do not latch
holds for another account.

The hold stores the owner, first event ID and canonical payload checksum, and a
monotonic ordering watermark. The watermark takes the maximum of the observed
unsupported event, previous lifecycle timestamp and existing hold watermark;
no delayed event can move it backwards. Lifecycle ordering metadata is advanced
atomically too. Unsupported positive states never replace the known lifecycle
status or original plan. Restrictive states still use the existing equal-time
precedence; cancellation remains terminal. Provider state fields therefore remain
separate from the local reconciliation decision.

Every entitlement refresh checks the subscription hold and sets payment_verified
to zero while held. The original plan remains audit metadata, **not paid access**.
Payment receipts, independent transaction adjustment holds, local subscription IDs
and promo_usage are retained. No automatic upgrade, downgrade, proration or credit
calculation occurs. Same-price renewals remain supported for unheld subscriptions;
valid payment proof can still be recorded for a held subscription but cannot release
its hold or grant access.

Duplicate receipt checks, hold creation/watermark updates, lifecycle metadata and
entitlement changes share the existing billing transaction and locking. Supported
active/resumed snapshots, stale events, replays, renewals, cancellations and adjustment
outcomes cannot remove a hold. Even a delayed unsupported snapshot arriving after
a newer supported snapshot conservatively requires reconciliation: this API cannot
establish that a later snapshot explicitly resolved the unsupported commercial
change. No administrative release endpoint exists. This may temporarily deny a
legitimate paying customer; independently verified, audited resolution is future work.

Transaction-only unsupported charges remain rejected under the existing bounded
payment contract; subscription holds are driven by authenticated lifecycle events,
not invented transaction-to-subscription lifecycle ordering. Delivery completeness,
monitoring, authoritative provider payload verification, backup/restore of **both**
hold tables, real disposable PostgreSQL execution, and a reviewed release procedure
remain requirements. Losing holds during restore could restore invalid access.

Focused synthetic HTTP coverage includes upgrade/downgrade, pause/change/stale resume,
delivery-order conflicts, duplicate checksums, same-time cancellation, adjustment
holds, renewals, usage/ID preservation, two linked customers and receipt failure
rollback/retry (including updates to an existing watermark). Independent review is
required; this is not sandbox or production readiness approval.
