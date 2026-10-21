# Controlled email + real-AI smoke test

Preparation only. No authorization to deploy, connect to any existing database,
send email or incur AI charges is implied by this checklist. Production and the
historical `habrivo-db` must remain separate. A passing offline check does not
verify a provider, database, model or production readiness.

## Configuration-only preflight (no service or database access)

From the repository root, using the project's existing Python environment:

```sh
python -B beckend/smoke_readiness.py
```

This operator-only command reads environment configuration, not `.env` files, and
never imports the Flask app, opens a database, sends mail, calls OpenAI or writes
files. Exit 0 means the configuration syntax and low smoke-test cap are acceptable;
exit 1 means setup is missing/invalid or the AI daily limit exceeds 3. Output is a
fixed JSON report containing statuses, policy constants and explicitly unverified
items. It prints no paths, hostnames, email addresses, model names, credentials,
URLs, connection strings or raw errors. It is not a public HTTP health endpoint.
Do not dump `env`, shell history, `.env` contents, HTTP request bodies or provider
errors to diagnose configuration. Supply secrets privately on the operator's host,
not in CLI arguments, source code or chat.

| Variable | Required value / behavior |
| --- | --- |
| `APP_ENV` | Existing values: `development`, `test`, `production`; default `development`. Use a separately isolated test environment, never infer isolation from this name. |
| `DB_BACKEND` | Explicit `sqlite` or `postgres` for the worker/preflight. |
| `DATA_DIR` | For local SQLite: explicit isolated directory. Worker requires an existing `habrivo.db`; the Flask app initializes local schema. Never point at an existing/shared data directory. |
| `DATABASE_URL` | Required for PostgreSQL; no SQLite fallback. Do not set for SQLite. Syntax validation does not establish ownership, schema or isolation. |
| `SECRET_KEY` | Same private stable random secret for web app and both workers. Preflight requires 32–4096 non-whitespace characters; this checks shape, not randomness. No default. |
| `MARKETING_AI_PROVIDER` | Exactly `openai`; no default/fallback provider. |
| `OPENAI_API_KEY` | Dedicated server-only provider key. No default; visible ASCII, no whitespace/control characters, at most 4096 characters. Credential validity is unverified offline. |
| `MARKETING_AI_MODEL` | Explicit model identifier (1–80 letters/digits/`_.:-`); no default. Must support Chat Completions, strict JSON-schema output with string `minLength`/`maxLength`, and `max_completion_tokens`. A valid identifier does not establish these capabilities. |
| `MARKETING_AI_DAILY_LIMIT` | Set **3** for this one-account journey. Runtime default remains 50, range 1–10000; preflight requires at most 3. Shared UTC-day capacity includes paid and free reservations. |
| `PASSWORD_RESET_EMAIL_PROVIDER` | Exactly `smtp`. Legacy variable name configures both verification and recovery. No implicit email provider. |
| `SMTP_HOST` | Explicit SMTP SSL host, no URL/port/credentials embedded. No default. |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Server-only credentials; nonblank and no control characters. No defaults. |
| `SMTP_FROM` | Explicit sender address accepted by the SMTP provider. No default. |
| `PUBLIC_BASE_URL` | Explicit trusted HTTPS origin, e.g. `https://test.example.invalid` (illustration only). No path/query/fragment/userinfo, whitespace, backslashes or invalid port. Never derive from a request Host or supplied redirect. |

No SMTP port variable exists: the shared transport uses port **465**, verified TLS
and a **10-second socket timeout**. Both queues use the same transport. This is not
a hard total delivery deadline; multiple SMTP operations can each use the timeout.
OpenAI uses the fixed TLS Chat Completions endpoint, connect/read timeouts **5/25s**,
a **35s** elapsed check between chunks, 64 KiB response cap, **900** completion-token
cap and no automatic retry/redirect. A blocking read may extend elapsed time by its
read timeout; this is not a hard process deadline or provider cancellation guarantee.

Before approval, the owner must check the selected model's current official feature
support, availability and pricing. No compatibility lookup or provider handshake is
performed by preflight. A model incompatible with the strict schema fails safely,
but a committed generation reservation still counts. Existing documentation's
`gpt-4.1-mini` example is not a guarantee of current availability or compatibility.

## Human prerequisites before any real smoke test

1. Obtain explicit authorization for the specific disposable environment, one
   owner-controlled test inbox, SMTP sends and at most **three AI attempts**. Verify
   the environment has no real/historical records, shared queues or automatic
   deployment connection to GitHub main. Use synthetic names and product facts.
2. Review the additive migrations through 0008. SQLite testing must use an isolated
   local directory; PostgreSQL needs separately authorized disposable-schema/HTTP
   verification. Preflight does not connect or apply migrations. Never use Render
   production, an existing SQLite database or old `habrivo-db` as a shortcut.
3. Arrange an HTTPS browser origin with trusted TLS and matching `PUBLIC_BASE_URL`.
   Session cookies are Secure/HttpOnly/SameSite=Lax; plain HTTP is not a valid browser
   smoke test. Use the actual existing Flask service entry point in that isolated
   environment, not the preflight command as a web server. Keep debug mode off.
4. Privately configure the variables above in both web and worker processes.
   Preflight intentionally does not load `.env`; the Flask app currently does.
   Ensure the *effective* environments match without printing values. Ensure HTTP,
   proxy/APM and SMTP logging omit request bodies, auth headers, fragments and email
   bodies; never enable SMTP debug output. Use dedicated limited provider credentials.
5. Review selected-model prices and set provider-side budget controls/alerts where
   supported. Use a single new eligible account and global cap 3. There is no exact
   token estimate without the model tokenizer or observed usage. Each attempted pack
   permits at most 900 completion tokens; three permit at most **2700 completion
   tokens**, not a promise of three successful outputs or a dollar ceiling. Input
   includes the bounded brief (2020 field characters plus tone/goal), system text,
   schema and JSON overhead. Measure usage privately in the provider console.
   Cost = `(input_tokens × input_price + output_tokens × output_price) / 1,000,000`
   when prices are quoted per million tokens. Do not assume the token/character ratio.

## One-account journey and expected results

1. Create one new free account through the public website. No payment information
   or Paddle interaction. Signup is generic and queues verification; it does not
   authenticate, verify or grant usable attempts. Sign in to see pending verification,
   **0** usable trial attempts, available settings/library, and locked generation.
   Login before verification is supported; verification does not require login.
2. Explicitly process verification mail in the approved environment:

   ```sh
   python -B beckend/customer_security.py --deliver-verification-email --limit 1
   ```

   This is an actual mail-sending command, **not** a preflight/dry-run. It processes
   the oldest eligible job, so it is safe only in the already verified isolated
   queue. Worker output contains aggregate sent/failed/skipped counts. SMTP acceptance
   is not inbox delivery; verify arrival privately. The worker is a one-shot batch,
   not an automatic daemon. The web service alone does not run it.
3. Open the emailed link (30-minute lifetime after worker issuance) and click Verify
   email. No bearer is displayed in JSON or stored in browser storage. Sign in/return
   to the dashboard and refresh access: **verified, 3 remaining**. Reopening the used
   link must not grant more credits. Do not copy token-bearing URLs into evidence.
4. Generate one short synthetic marketing pack. Expect the four named outputs,
   **2 remaining**, and no unsupported factual claims. Copy an output and save the
   pack. List/reopen it in the library: unchanged remaining count and no extra AI call.
5. Wait at least **60 seconds** between generation attempts. Generate two more packs:
   **1**, then **0** remaining. Attempt four must be rejected **without an OpenAI call**,
   showing Upgrade; settings/library remain usable and no paid subscription activates.
   No monthly rollover, login or reset can renew the three lifetime attempts.
6. Logout, refresh and use browser Back: protected data must require sign-in again.
   Use Forgot Password and submit the test email. Expect generic **queued**, not
   delivered. Process the separate recovery queue explicitly:

   ```sh
   python -B beckend/customer_security.py --deliver-reset-email --limit 1
   ```

   Open that private mail link, reset the password and sign in with the replacement.
   Old password, old sessions and token replay must fail. Email remains verified and
   lifetime trial remains **0**. Recovery must not regrant or verify an unverified
   account. Confirm saved content still belongs to that account.

Missing SMTP configuration returns a safe unavailable response; the worker cannot
send. Missing AI configuration fails before reserving an attempt. Committed attempts
**count even on provider errors, timeouts, refusals and malformed output**. The UI
reports counted/uncertain attempts and cooldown guidance. Do not replenish a grant,
create more accounts or retry uncertain paid requests to force a successful result.
Use deterministic offline mocks for fault scenarios.

## Stop, evidence and rollback

Stop immediately on unexpected account/data/queue contents, nonisolated database,
wrong origin/recipient, secret exposure, missing consent, unexpected provider charges,
wrong counters, cross-account results, or failed generation. Record only aggregate
counts, public error codes, timestamps and non-sensitive pass/fail notes. Stop after
three reserved attempts even if one failed. A default 50/day cap or a model whose
schema support/pricing is unknown must be resolved before starting.

To stop: cease browser requests and stop only the test worker/web processes the
operator started. Disable/revoke only dedicated test credentials if necessary.
Preserve synthetic database, ledger and queue evidence; do not reset credits, truncate
queues or delete/restore databases as an automatic rollback. Any cleanup or corrective
migration requires separate authorization. No production rollback/deployment is part
of this procedure. Inspect `sending` jobs after uncertain mail sends; do not blindly
requeue. Real delivery, model quality/cost, HTTPS/mobile behavior and PostgreSQL
concurrency remain unverified until these separately authorized checks run.
