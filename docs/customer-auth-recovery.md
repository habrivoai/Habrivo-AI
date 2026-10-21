# Customer authentication and password recovery

Implemented on the development branch; no production settings/data, email account,
Paddle service, deployment or historical database was accessed. No real mail was
sent. Signup, login, account settings and the existing password-change UI were
already implemented and are preserved. New public forgot/reset pages complete the
missing recovery journey. `_ptxn`, customer IDs, plan identifiers and redirects
never authenticate a customer or grant paid access.

## Customer sessions

Successful login issues an opaque random 256-bit session identifier, stored as
SHA-256 in `customer_sessions`; the HttpOnly signed Flask cookie carries the raw
identifier and customer ID. Every customer-authenticated API request validates the
server record, eight-hour absolute expiry and HMAC credential version derived from
the current password hash. Browser cookie expiration does not extend server expiry.
Secure, HttpOnly, SameSite=Lax remain the defaults. Exact CORS configuration can
select SameSite=None as before; HTTPS and a reviewed origin policy are required.

Customer logout deletes the server session before clearing the browser identity.
A copied pre-logout cookie cannot restore that session. Failed storage access denies
protected requests; logout failure leaves a retry rather than pretending success.
Password change/reset invalidates all customer sessions and all outstanding reset
links/jobs for that account. Credential-version checking also invalidates sessions
when a password hash changes outside those flows. Old customer cookies without a
server session are rejected: deployment will intentionally require reauthentication.
Admin authentication has not been converted to this customer session architecture.

## Recovery API and privacy

`POST /api/customer/forgot-password` accepts only an email and returns the same
202 message for known/unknown addresses when configuration/storage are available:
instructions are **queued if eligible**, not claimed delivered. Both cases queue
bounded metadata jobs; unknown addresses store no email and never receive mail.
Unconfigured delivery returns a uniform 503 for known/unknown addresses. HTTP does
not attempt SMTP, avoiding synchronous delivery/existence timing or error oracles.

`POST /api/customer/reset-password` accepts only a token and new password (8–128
characters). Tokens use 32 random bytes, are stored only as SHA-256, expire 30 minutes
after worker issuance, and are single-use. Reset runs under a bounded database lock
and conditional password update; success consumes all reset tokens for the owner,
invalidates customer sessions and does not log in automatically. Multiple outstanding
unexpired links may exist, bounded by recovery limits; using any one invalidates all.
Invalid, expired and reused links receive the same generic reset-link error.

Shared database-backed limits apply to successful and failed attempts:

- Recovery: 3 per normalized email/hour, 10 per source IP/hour, 100 globally/day.
- Reset submissions: 20 per source IP/15 minutes, 200 globally/hour.
- Current-password changes: 10 per account/15 minutes, 20 per source IP/15 minutes.
- Existing signup/login limits are preserved. Arbitrary forwarding headers are not
  trusted for IP identity. A trusted-proxy deployment policy remains necessary.

No reset token, password, provider credential or raw provider error is returned in
HTTP JSON or worker logs. Reset links put the token in the URL fragment; the page
immediately removes it from browser history and keeps it only in memory until submit.
It is never stored in localStorage. Recovery pages use no-referrer, no-store and a
restrictive same-origin CSP with no third-party assets. The bearer must necessarily
be delivered privately to the recipient; never paste reset URLs into logs or chat.
Password recovery does not verify email ownership or grant trial attempts. The separate [email verification flow](email-verification-trial.md) controls that state.

## Explicit SMTP configuration and worker

Delivery uses Python's real SMTP-over-TLS implementation, verified TLS context,
authentication, port 465, and a ten-second socket timeout. No package was added.
Required server-only environment names (never place values in frontend or chat):

- `PASSWORD_RESET_EMAIL_PROVIDER=smtp`
- `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`
- `PUBLIC_BASE_URL`: fixed HTTPS origin only, no path, credentials, query or fragment.
- Existing `SECRET_KEY` and explicit database configuration.

Links use this fixed origin, never an inbound Host or forwarding header. STARTTLS
on port 587 and provider-specific HTTP email APIs are not implemented.

An explicitly operated worker must run with **the same database and SECRET_KEY**
as the authorized application environment. It never imports Flask or initializes
schemas. For later authorized isolated testing only:

```sh
python -B beckend/customer_security.py --deliver-reset-email --limit 20
```

The CLI requires explicit DB_BACKEND; SQLite additionally requires an explicit
DATA_DIR containing an existing habrivo.db. Never run it with inherited production,
Render or historical database credentials. Configuration must be supplied privately
outside the repository. This command sends real email if configured; it was NOT run
against a real provider in this task.

The worker claims at most 100 jobs per invocation, drops requests older than one
hour, and checks the account's credential version before issuance. Tokens exist
only in memory, the intended outgoing message, and hashed database records. SMTP
failures mark the job failed and invalidate that attempt's token; exceptions are not
printed. Crashes/uncertain delivery can leave `sending` jobs for operator review;
there is deliberately no blind retry. User can request a new link within limits.
An already-requested email may race a password change and deliver an invalid link;
it cannot authorize access after credential-version invalidation.

Scheduling, failure monitoring, sender/domain verification and real inbox delivery
remain deployment prerequisites. A 202 without a running worker is only queued work.
Do not claim reliable email delivery or production readiness until these are tested.

## Schema, compatibility and later browser smoke test

Migration `0007_customer_security.sql` adds sessions, hashed reset tokens and queue
metadata; it does not rewrite users, IDs, original hashes, payments, usage or holds.
Local SQLite initialization is additive; PostgreSQL requires explicit migration after
0001–0006. No PostgreSQL migration was applied here. Existing legacy PBKDF2 login
continues to use Werkzeug verification. Existing PostgreSQL HTTP/constraint execution
is still unverified. SQLite foreign-key enforcement remains a pre-existing parity gap.
The expanded schema is outside the original four-table source-only importer contract.
Back up auth state consistently and preserve SECRET_KEY; losing/reverting security
state can affect revocation. Never migrate/drop existing test data without approval.

Later **separately authorized** browser smoke procedure:

1. Use an isolated HTTPS application, disposable database and synthetic accounts.
   Review/apply migrations there only; preserve existing data. Secure cookies require
   HTTPS; do not disable Secure as a workaround or use production infrastructure.
2. Confirm public signup/login/forgot/reset navigation. Add `_ptxn=untrusted` to a
   URL: it must not sign in or activate a plan. Signup grants no paid entitlement.
3. Sign in in two browser profiles. Inspect Secure/HttpOnly/SameSite and eight-hour
   expiry without copying cookie values. Log out one profile; its protected requests
   and copied-cookie replay in a controlled test must fail, while the other remains.
4. First leave mail unconfigured: known/unknown recovery requests must show the same
   unavailable response. Then separately authorize a test SMTP destination and set
   server-only configuration privately. Known/unknown requests must share the queued
   response and reveal no account existence.
5. Explicitly run the authorized worker. Open the link only in the recipient's test
   inbox. Confirm fragment disappears, passwords must match, and no token is logged.
6. Reset the password. Both old sessions must fail; old password and reused/expired
   links must fail. New password must sign in to the same account ID. Verify the other
   customer's credentials, saved content, subscriptions and usage are unchanged.
7. Exercise delivery outage, rate limits, network retry and password-change invalidation.
   Inspect only sanitized aggregate worker results and metadata. Verify no secrets or
   private messages enter browser console, application logs or support chat.

Independent security review, guarded PostgreSQL auth integration, actual browser
execution, email transport/deliverability and a reliable monitored worker remain
required before calling this production-ready. This step does not authorize them.
