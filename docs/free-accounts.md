# Free accounts and disabled trial foundation

Visitors can choose **Create free account** from the public homepage or Free account
pricing card. `/start/free` clears any pending paid checkout selection and redirects
to the existing signup form. It accepts no query parameters. There is no payment form,
Paddle request, subscription insertion, trial grant, or automatic entitlement change.
An account begins as Free unless an actual current, verified paid subscription exists.

A signed-in Free customer can use the dashboard, account settings/password changes,
and their saved-content library. A library can naturally be empty for new accounts;
previously saved packs remain accessible after paid access expires. The dashboard
shows locked Marketing Studio access, zero free AI credits and upgrade links. The
old nonfunctional customer Products card now opens the implemented content library.

Choosing a paid plan still records the validated plan/cycle and resumes checkout
after signup/login. Choosing Free does not revoke an existing paid subscription:
`account_entry=free` is UI context only. Actual access is always read from server
records. No client-selected customer ID, email-verification flag, plan, allowance,
payment flag or trial count is accepted in registration. Paid checkout continues to
accept only validated paid plan/cycle identifiers.

## Access and free AI policy

`GET /api/customer/entitlements` requires a live authenticated user, accepts no account
selectors and uses `Cache-Control: no-store`. It reports account tier, available
features, current paid monthly allowance/usage, provider configuration status, and
explicitly unverified email state. No other customer's status can be selected.
Future-start, unverified, pending and expired subscriptions cannot grant generation.

Free AI is HARD DISABLED: `enabled=false`, `monthly_limit=0`. No configurable free
allowance is active or supported. In particular, adding arbitrary `FREE_AI_ENABLED`
or `FREE_AI_MONTHLY_LIMIT` variables does not grant anything. The existing usage
schema requires a subscription ID and its paid plans exclude Free. Reusing a dummy
paid subscription, nullable/magic subscription ID, or browser flag would weaken the
current entitlement and migration rules; this step deliberately does none of those.

A genuinely configurable free allowance needs an explicitly owner-approved allowance,
a dedicated persistent free-usage model independent of paid subscription rows,
transactional monthly reservations, verified email ownership, anti-multi-account
controls, provider budget limits, and review/testing before activation. No numerical
trial allowance or identity-verification policy was guessed. Marketing Studio retains
all existing paid quotas, per-user cooldown and shared daily cap. The legacy chat
route now also requires a real paid entitlement before importing its missing module;
legacy promo checks now honor future start dates. No free AI path was added.

## Email ownership

The repository has no email-verification schema, mail provider integration, trusted
delivery process, or email-ownership proof. The bounded offline portion implemented
here is explicit authenticated verification-status reporting, an honest unverified
UI state, rejection of client verification flags, and denial of free-trial grants.
There is NO working verification-request/delivery/confirmation service. No verification
link/token is returned to the browser or logged, and nothing claims a message was
sent. Payment verification is not email verification. Existing and newly created
accounts are all reported unverified by this foundation.

An eventual mail system must verify delivery and single-use expiring ownership proofs,
keep raw tokens out of storage/logs/responses, bound resend attempts, support legitimate
ownership recovery, and handle historical duplicate/case-normalization issues before
email ownership can gate credits. No real email was sent and no real account reviewed.

## Enumeration and authentication limits

Registration remains a real user INSERT with normalized email and a unique database
constraint. It hashes passwords even for duplicates, never overwrites an existing
password, and returns the SAME 201 status and generic message for a new or existing
email. It no longer returns `user_id` or a duplicate-specific 409. This intentional
security response-contract change removes the straightforward enumeration oracle;
clients should sign in normally rather than infer account creation. The UI no longer
announces that a new account definitely exists. The test fixtures that formerly read
signup IDs now inspect their isolated test databases only.

Login uses the same 401 message for unknown emails and incorrect passwords. An
unknown address follows a current-default dummy-hash verification path. It preserves
existing PBKDF2 compatibility and does not rewrite historical password hashes. This
reduces obvious timing differences, but does NOT promise perfect timing resistance
across legacy hash algorithms, database conditions, or distributed probing. Uniform
errors and rate limits are defense in depth, not proof of bot resistance or ownership.

Both auth endpoints accept only their fixed fields, limit request bodies to 4 KiB,
validate types/lengths/text and handle malformed JSON without raw error messages.
Shared database-backed fixed windows apply across workers:

| Action | Subject | Limit |
| --- | --- | --- |
| Signup | Server-observed IP | 10/hour |
| Signup | Whole service | 200/UTC day |
| Login | IP + supplied email pair | 10/15-minute window |
| Login | Server-observed IP | 30/15-minute window |
| Login | Whole service | 500/hour |

All accepted attempts count, including successes, duplicates, and unknown emails.
Blocked requests return 429 with Retry-After and no existence information. Counter
check/increment is atomic under SQLite write locking or a distinct PostgreSQL
transaction advisory lock. Locks are released before expensive password hashing.
Unavailable limiter storage fails closed with safe 503 rather than bypassing limits.

Only HMAC subject identifiers, action scopes, numeric windows and counters are stored
in `account_action_limits`; no raw IP, email, password or verification token. Old
windows are pruned on accepted auth traffic after 48 hours. Key rotation changes
subject identifiers and can reset effective limits; account for that operationally.

Forwarding headers are deliberately NOT trusted. Behind Render/reverse proxies,
remote_addr may identify a shared proxy, so these conservative limits can affect
multiple users. Trusted-proxy configuration, IP parsing, network limits/CAPTCHA and
capacity review require a separately authorized deployment decision. Fixed-window
boundary bursts, distributed attacks, compromised mailboxes and multiple genuine
addresses still need additional controls before any free AI credit system is enabled.

## Schema and verification scope

Local SQLite initialization adds only `account_action_limits` using IF NOT EXISTS.
`0003_account_action_limits.sql` is an explicit additive PostgreSQL migration, never
run at app import. No PostgreSQL schema migration or connection was executed here.
PostgreSQL runtime concurrency remains NOT VERIFIED. Apply/review/test the migration
only in a separately authorized disposable target before any production rollout;
missing limiter schema makes signup/login unavailable rather than silently unbounded.
Existing sessions, paid tables, library schema and prior migrations are preserved.

The original four-table snapshot validator/importer remains intentionally unchanged
and rejects snapshots containing the added library/auth tables. Inventory/backup and
migration support must be extended separately; do not delete these tables to bypass
validation. No existing data was migrated or discarded.

## Offline tests

```sh
python -B -m unittest discover -s tests -q
node --test tests/customer_account_frontend.test.js tests/marketing_studio_frontend.test.js tests/content_library_frontend.test.js tests/public_journey_frontend.test.js tests/account_access_frontend.test.js
```

Tests use fresh temporary SQLite databases and real HTTP handlers. Provider transport
is mocked only in tests; all unmocked outgoing HTTP is blocked in the account fixture.
Coverage includes Free signup/login, unchanged paid upgrade/generation, zero trial
grants, cross-account restrictions, uniform duplicates, concurrent signup and auth
limits, ignored forwarding-header spoofing, malformed input, and explicit unverified
email status. Browser-state tests cover locked/available features and stale-response
races. No production, Render, historical database, mail provider, paid API, installation,
commit, push, merge or deployment is required or performed.
