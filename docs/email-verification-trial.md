# Email verification and three lifetime Marketing Studio attempts

This is development functionality, not production or payment-launch approval.
No live email, AI request, database migration or deployment was performed.

## Policy and customer journey

A successful **new** signup atomically creates an unverified eligibility marker
and one pending verification mail job alongside the existing user. It grants no
usable AI attempts. Duplicate signup cannot overwrite an account or enroll a
historical account. The generic registration response does not reveal existence.
Sign in, open the dashboard verification status and use **Send / resend verification
email** if needed. The dashboard is the verification-pending screen; account
settings and saved content remain available. The background worker must be operated
separately before a queued message can arrive. SMTP configuration errors are shown
truthfully; the HTTP response never claims delivery.

The mail link opens `/verify-email.html#token=…`. That page removes the fragment
from history, keeps the bearer only in memory, and requires an explicit **Verify
email** click (GET/link scanning does not consume a token). It has no third-party
assets or analytics, and uses no-referrer, no-store and a restrictive CSP. Do not
share links, email bodies, request bodies or tokens in logs, analytics or chat.

A valid token verifies only the account/email it was issued for, regardless of the
browser's current customer. It does not sign anyone in. A single transaction marks
the address verified, creates at most one grant of exactly three attempts, consumes
all verification tokens for that account and expires pending mail jobs. Replays
return a generic invalid/expired-link error and never create more credits. Expired
links require sign-in and resend. Tokens are 32 random bytes (43 URL-safe characters),
SHA-256 hashed in storage and expire 30 minutes after worker issuance. The stored
email digest binds the token/grant to the existing normalized address. There is no
email-change UI; an unexpected address change fails closed and needs support.

**Historical account policy:** migration 0008 does not backfill eligibility or
verification. Accounts without a new-signup enrollment can request verification;
they receive `eligible=0` and **no free grant**, even after verification. Existing
paid entitlements continue to work as before. Any future legacy trial grant policy
requires separate approval and implementation; do not manually change flags.

Password reset, password change, login, duplicate signup and paid subscription
changes do not grant, replenish or clear lifetime attempts. Recovery remains a
separate flow: it does not set `verified_at` or create a trial grant. Verification
mail is not a password-reset link. There is no automatic monthly renewal or
operator/API hold-release/credit-reset route.

## Charging, transactions and persistence

- Active verified paid subscriptions keep their existing monthly `promo_usage`
  accounting. Trial attempts are used only when there is no eligible active paid
  subscription; payment/entitlement checks are not altered.
- Otherwise the customer must have a current verified email, new-signup eligibility
  and an unused lifetime grant. The server checks the immutable allowance (3), used
  counter and ledger count, then increments the counter and inserts one unique
  `marketing_trial_usage` reservation **in one transaction**. Any inconsistency fails
  closed. SQLite uses `BEGIN IMMEDIATE`; PostgreSQL uses the existing shared Studio
  advisory transaction lock and bounded lock timeout. The conditional update guards
  the final credit. No SQL transaction waits on the AI provider.
- The reservation commits **before** calling the real provider. Timeouts, refusals,
  HTTP errors, unusable responses, or process interruption after reservation consume
  the attempt. This bounds uncertain provider charges. No automatic retry/refund.
- Invalid input, failed authentication, unverified email, exhausted grant, missing
  provider configuration, rate/cooldown/service-cap rejection and rolled-back
  reservations do not consume a generation attempt. A failed response's
  `attempt_counted` indicates whether reservation completed.
- The one-minute cooldown and shared UTC-day service cap count **both** trial and
  paid ledgers. Changing plans cannot bypass the cooldown. The lifetime three-attempt
  maximum also caps trial attempts at three per account in any UTC day. The existing
  paid monthly allowance is neither increased nor debited by historical free usage.
- The dashboard reports 3/2/1/0 remaining from durable records. Once exhausted,
  Upgrade opens existing public pricing; it never activates Paddle or a paid plan.
- Saving/copying generated content uses the existing signed pack and owner-scoped
  library, with no provider call and no additional generation reservation.

Migration `0008_email_verification_trial.sql` is additive and follows 0007. It adds
verification accounts, hashed tokens, bounded mail queue metadata, a primary-keyed
one-grant-per-customer record with allowance/used checks, and a unique-attempt ledger
with owner/date indexes. SQLite local initialization reuses the same SQL with
idempotent CREATE statements. PostgreSQL requires the existing explicit migration
runner; no import-time PostgreSQL DDL or fallback was added. Real PostgreSQL HTTP,
constraints and concurrent behavior have **not** been executed for this change.
SQLite foreign-key enforcement remains a pre-existing parity limitation; application
ownership checks and transaction/CAS protections remain required.

## Abuse boundaries and email operation

Existing signup limits remain 10/IP/hour and 200/service/day. Authenticated resend
accepts no email/account/redirect field: 3/account/hour, 10/IP/hour, 100/service/day.
Each resend expires any older pending job. Actual worker sends (including initial
signup jobs) have an additional durable 3/account/hour and 100/service/day budget.
Token verification is limited to 20/IP/15 minutes and 500/service/hour; invalid and
expired well-shaped bearer attempts count. Studio requests are additionally limited
to 10/account/15 minutes and 60/IP/15 minutes. These request limits do not grant credits.
The existing one-minute reservation cooldown and configured global daily cap remain.
Counters use server-keyed HMAC subjects; raw IPs and supplied tokens are not retained
in rate-limit rows. Work batches are bounded to 1–100 jobs, default 20.

No disposable-domain reputation service, invasive fingerprinting or personal
identity collection was added. Email verification proves mailbox access at that
time, **not one person per email or one trial per person**. Aliases, multiple inboxes,
shared IPs, automated verified signups and proxy configuration remain operational
abuse concerns. Provider budgets/alerts and monitoring are still necessary.

The existing SMTP settings are shared (the provider variable retains its legacy
name): `PASSWORD_RESET_EMAIL_PROVIDER=smtp`, `SMTP_HOST`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_FROM`, and a fixed trusted `PUBLIC_BASE_URL=https://…`
with no path/query/fragment/credentials. No request Host or redirect controls links.
Server credentials never reach the frontend. The shared SMTP transport uses TLS
port 465 with certificate verification and a 10-second timeout.

For a separately authorized **nonproduction** environment, after reviewing and
applying migrations there, the explicit worker command is:

```sh
python beckend/customer_security.py --deliver-verification-email --limit 20
```

As with recovery, select an explicitly authorized `DB_BACKEND`/database; SQLite
requires an explicit `DATA_DIR` containing an existing database. Do not run this
command against production or historical services under this task. The worker
outputs aggregate counts only. The web request never sends mail. Pending work
expires after one hour; failed or uncertain sends are not automatically retried.
A crash after claiming a job leaves `sending` for operator inspection; a resend can
create a new bounded job. A new worker token invalidates older tokens. Successful
SMTP submission is not proof of inbox delivery. Operate/schedule and monitor this
worker explicitly; web service deployment alone does not run it.

## Review and later smoke-test checklist

1. Independently review migration 0008, enrollment/verification transactions and both
   reservation ledgers. Exercise migrations and HTTP concurrency in a separately
   authorized disposable PostgreSQL target; do not substitute old `habrivo-db`.
2. Preserve all existing user IDs, credentials, subscriptions, usage, grants, tokens,
   queue state and uploaded files in future backup/import designs. The existing
   source-only SQLite validator/importer does not migrate these new security/trial
   tables; it must not be represented as covering them. Do not discard test data.
3. With separate real-email authorization, configure a trusted HTTPS origin, sender,
   nonproduction SMTP credentials and delivery worker. Verify one synthetic new
   account transitions from unverified/0 to verified/3. Confirm expired/resend/replay,
   no secret leakage, inbox delivery and HTTPS mobile/browser navigation.
4. With separate paid-provider authorization, configure the documented
   `MARKETING_AI_PROVIDER`, `OPENAI_API_KEY`, explicit compatible `MARKETING_AI_MODEL`
   and a low `MARKETING_AI_DAILY_LIMIT`; set provider budget controls. Use synthetic
   briefs, measure actual tokens/cost and verify 3/2/1/0, fourth denied, and save with
   no extra charge. Do not spend money to exercise failure cases; use offline mocks.
5. Review rate limits behind the intended trusted proxy, queue housekeeping,
   monitoring of blocked requests/failed mail and provider capacity. Do not log
   bearer tokens, private prompts, addresses or raw errors. Test normal paid usage
   and disabled/expired paid access without modifying Paddle protections.

Offline tests use only newly created temporary SQLite databases, mocked SMTP and
deterministic mocked AI responses. They cannot establish deliverability, real model
quality, production persistence or PostgreSQL race behavior. This is not production
readiness approval.
