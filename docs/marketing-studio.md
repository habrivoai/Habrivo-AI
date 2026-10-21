# Habrivo AI Marketing Studio

The customer portal offers a single English-first marketing-pack workflow:
product name, factual description, target audience, tone, and marketing goal →
promotional copy, product description, social caption, and ad headline. Each output
has its own copy button and manual-selection fallback. Results are plain text,
not HTML. Loading prevents duplicate clicks; errors hide stale results and allow
retry. Review claims before publication; model instructions cannot guarantee accuracy.

`POST /api/marketing/generate` requires an existing signed-in customer and an active,
payment-verified, nonexpired subscription belonging to that customer, or an email-verified eligible lifetime trial with remaining attempts (see [trial policy](email-verification-trial.md)). A future
start date cannot grant access. The endpoint never accepts a client account ID or
entitlement flag. The dashboard links its main tool card to this workflow. Existing
account settings, payment APIs, and legacy API URLs remain intact.

## Explicit provider opt-in (configuration instructions only)

No configuration was applied by this development step. Before any authorized test
with a provider, the owner must provision credentials and explicitly set:

- `MARKETING_AI_PROVIDER=openai`
- `OPENAI_API_KEY`: secret configured outside the repository, never pasted into chat
- `MARKETING_AI_MODEL`: a model supporting Chat Completions JSON-schema structured
  output and `max_completion_tokens` (for example `gpt-4.1-mini`; verify current
  availability, supported schema constraints, and pricing with the provider)
- `MARKETING_AI_DAILY_LIMIT`: optional positive integer, default 50, maximum 10000

The adapter uses the fixed TLS endpoint `https://api.openai.com/v1/chat/completions`
through the existing requests dependency. There is no arbitrary provider URL,
provider fallback, generated sample text, automatic retry, or redirect following.
Missing/invalid provider configuration returns a safe 503 before reserving usage.
Prompt content includes product facts only, not account email, credentials, or hashes.
Product details are sent to OpenAI once configured; disclose provider processing and
review privacy/retention terms before accepting real business data.

## Limits and charging semantics

- Maximum request: 8192 bytes; product name 120 characters, description 1600,
  audience 300. All required. Only fixed tone/goal selections are accepted.
- Output token cap: 900 completion tokens for the entire pack. Character caps:
  promotional copy 900, description 1200, caption 600, headline 120. Truncated,
  refused, malformed, oversized or incomplete responses are rejected, not presented
  as a completed pack.
- Provider connect/read timeouts: 5/25 seconds. Streaming checks also enforce a
  35-second elapsed budget between reads; a blocking read can extend this by its
  read-timeout interval. Response buffering is capped at 64 KiB. This is not a hard
  process-level deadline or provider-side cancellation guarantee.
- One attempt per customer per 60 seconds. UTC calendar-month allowances are
  Starter 100 / Pro 500 / Premium 2000, counted across that customer's subscriptions.
- A shared UTC-day service cap defaults to 50. Existing promo usage is included in
  these counts. This can temporarily constrain subscribers before monthly limits;
  disclose actual availability and commercial terms before selling plans.

An attempt is inserted into existing `promo_usage` and committed BEFORE the paid
HTTP request. SQLite reservations use `BEGIN IMMEDIATE`; PostgreSQL reservations
use a transaction-scoped advisory lock and a 3-second lock timeout. This serializes
new Studio reservations across app workers, including the global capacity check.
No SQL transaction remains open while waiting for the provider. Lock/storage errors
fail closed without starting a provider request.

Reserved attempts remain counted on timeouts, refusals, malformed responses, HTTP
errors, or process interruption. The provider might charge despite no usable result;
retaining reservations prevents an unlimited failure/retry spending loophole.
This extends the shared usage table's semantics: new Studio rows represent reserved
attempts, while historical legacy rows represented successful generations. There is
no attempt-status column, refund mechanism, or claim that every row is a successful
pack. Failed provider responses include `attempt_counted: true`; successes include
the customer's used/limit counts. No subscription is automatically created or marked
paid. Generation reservations retain their original schema; the separate content
library adds its own table/migration without changing database configuration or
payment routes.

These caps bound new Studio attempts, not total spend on a provider account shared
with other applications or legacy endpoints. Configure separate provider credentials
and provider-side budget/usage alerts. Audit any other provider-enabled routes before
launch; the legacy AI routes still reference missing `ai_agent`/`products_db` modules.

## Cost planning (illustration, not a current price quote)

Per-pack token cost is `(input_tokens × input_price_per_million +
output_tokens × output_price_per_million) / 1,000,000`. For an illustrative 2000
input tokens, 900 output tokens and hypothetical USD $0.40/$1.60 per million, that is
about $0.00224 per attempt or $3.36 for 1500 attempts (50/day for 30 days). Actual
model prices, input tokenization, failures, taxes, foreign-exchange fees, hosting,
and payment costs differ. Set a provider-side budget rather than relying on this
illustration or subscription revenue projections.

## Persistence and launch dependencies

Generation itself returns outputs to the browser and stores no brief/output text.
Customers can now explicitly save a successful pack through the
[customer content library](content-library.md), which has its own additive table and
owner-scoped API. Save before leaving; unsaved results remain transient. Library
saving does not consume another generation attempt or call the provider. Existing
`promo_usage` still stores only account/subscription IDs and timestamps.

Before real customers: independently review this implementation, run an explicitly
authorized provider smoke test, verify PostgreSQL HTTP/concurrency behavior in an
isolated target, and perform real-browser testing. Only SQLite reservation concurrency
has been exercised here. Review and sandbox-verify the bounded [Paddle webhook implementation](paddle-billing.md)
and entitlement activation; current checkout alone is not proof of payment. Review plan availability,
refund/attempt policy, privacy wording, HTTPS, persistent production database and
backups. This implementation is not a deployment or production-readiness approval.

## Offline verification

```sh
python -B -m unittest discover -s tests -q
node --test tests/customer_account_frontend.test.js tests/marketing_studio_frontend.test.js
node --check frontend/marketing-studio.js
```

Tests use fresh temporary SQLite databases and real Flask handlers, with provider
HTTP mocked only in tests. Existing account tests block all unmocked outgoing HTTP.
Concurrency tests exercise both per-user cooldown and global-cap reservation races.
No paid provider calls, software installation, production access or deployment is
required. Existing guarded PostgreSQL integration checks remain skipped without
an explicitly supplied independent disposable test target.

## Real-provider readiness safeguards (Step 53)

Input and output text must be valid UTF-8, in addition to the existing character
and control-character limits. Invalid input is rejected before any reservation.
Provider JSON must contain one completion with a complete, non-refused message;
duplicate JSON fields, malformed shapes and invalid output Unicode fail safely.
Provider authentication failures ask customers to contact support; provider
capacity failures advise against repeated retries. Neither exposes provider bodies.
Reserved failures still consume an attempt and the normal cooldown still applies.
There is no automatic retry, even for capacity errors.

The browser validates all four output limits, quota integers and the signed save
proof before displaying results or enabling library saving. Incomplete responses
cannot publish a partially usable pack. Saving an existing successful pack still
uses the separate library endpoint and never invokes generation again.

### Separately authorized smoke test

1. Independently review the changes and use a dedicated nonproduction environment
   with a synthetic customer, isolated persistent database and deliberately verified
   test entitlement. Never activate a subscription through browser flags.
2. Configure the four provider variables above on the server only, choosing a model
   confirmed to support this exact strict schema. Set a low daily cap (for example
   1), dedicated provider credentials and provider-side budget alerts/limits where
   available. Check the model's current prices and data processing terms first.
3. With separate authorization to incur a provider charge, submit one synthetic
   brief. Verify all four distinct factual outputs, latency, one usage reservation,
   copy, save, list and delete. Saving must leave usage unchanged. Check the immediate
   retry is blocked. Do not paste credentials or brief/output bodies into diagnostics.
4. Exercise missing configuration and mocked timeout/error cases separately; do not
   spend money deliberately retrying failures. Verify mobile layout and HTTPS
   clipboard behavior in a real browser.

No tokenizer is installed or called, so an exact input-token estimate is unavailable.
Input has at most 2,020 user-field characters plus tone/goal, JSON escaping, the fixed
system prompt and schema overhead; character counts are not token counts (especially
for Unicode). Output is capped at 900 completion tokens, including any model-specific
reasoning usage. Use the selected model's tokenizer/usage reporting during the
separately authorized test to measure actual input usage and price the formula above.
Models that exhaust that budget return an error rather than fabricated/truncated
content. Service caps bound attempts, not guaranteed dollar spend. No real-provider,
PostgreSQL, production or browser smoke test was performed for this offline change.
