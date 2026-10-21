# Public product, pricing, and authenticated checkout continuation

`/` and `/index.html` now serve the public Habrivo AI Marketing Studio homepage.
It describes the four implemented outputs and customer content library, without
advertising automated publishing, analytics, missing product modules, chatbot
features, customer counts or testimonials. The original admin interface is preserved
at `/admin.html`; its session-restoration request now uses `/api/admin/check-auth`.

Public navigation offers Features, Pricing, Sign Up, Log In, and Get Started.
Features and pricing require no authentication. `/pricing` redirects to the public
pricing section. No remote fonts, stock-image service, third-party UI library or
tracking service was added. Responsive layouts, semantic headings, explicit form
labels, focus styles, skip navigation, reduced-motion support, status announcements,
loading/error states and pricing retry controls are included.

## Catalog and entitlements

`GET /api/promo/plans` remains public and backward-compatible for existing dashboard
fields (`success`, `currency`, `plans`, names, prices and monthly limits). It also
returns billing-period month counts, library allowance, cooldown, shared daily capacity,
provider-configuration availability, checkout availability per plan/cycle, and the
Paddle environment. It exposes neither API credentials nor provider price IDs.

The actual application plan data remains Starter/Pro/Premium with its existing USD
prices, all four existing billing cycles and existing monthly allowances. Nothing
is discounted or repriced. Server validation cross-checks allowances with Marketing
Studio limits and rejects unavailable/malformed catalogs with 503. The frontend
has no fallback price or allowance. It shows the full selected-period amount,
not an invented monthly equivalent or savings percentage.

The page explains that reserved attempts count on provider failures, monthly
allowances are subject to a cooldown and shared daily service capacity, signup is
not a free AI trial, and generation requires a verified active subscription plus
configured provider. Configured-but-unavailable checkout is visibly labeled. Sandbox
checkout is explicitly labeled as test mode, never as a working live purchase.

Application prices and mapped Paddle price IDs must be independently reconciled
before launch. The app does not query Paddle's catalog in this task and cannot prove
the remote price, currency, tax configuration or billing cycle matches its local
display. Do not sell plans until those match and actual capacity/commercial terms
are reviewed. The existing default daily cap can constrain aggregate subscriber use.

## Free account entry

The homepage now also offers a real Free account option through `/start/free`.
It bypasses and clears checkout intent, grants no AI credits, and requires no payment
information. Dashboard access, explicit locked features, unverified email status,
shared auth limits and the enumeration-safe signup response are documented in
[Free accounts](free-accounts.md). Registration now returns the same generic 201 for
new/duplicate emails without an account ID; login still preserves a validated paid
selection when that is the chosen path.

## Plan selection through authentication

1. A plan link goes to `/start?plan=<key>&billing_cycle=<cycle>&mode=signup`.
2. The server validates the identifiers against the configured catalog, permits
   only signup/login mode, and rejects unknown/duplicate query parameters and
   user-provided redirect destinations.
3. Only plan and cycle are stored in the signed Flask session; no client price,
   payment flag or entitlement is accepted. The redirect has a fixed local destination.
4. Signup still creates an account without auto-login. The existing login clears
   the session as before, then restores only the validated plan intent. Logout
   clears both the customer identity and intent to avoid shared-device carryover.
5. `GET /api/checkout/intent` shows the current selection and server price before
   or after login, with `no-store`. It exposes no customer identity or customer
   records. The customer page selects signup mode from the URL and refreshes intent
   after successful authentication or restored authentication.
6. The customer explicitly clicks **Continue to checkout** after authentication and
   only when the selected mapping is configured. No automatic paid-provider request
   occurs just because a visitor browses, signs up, logs in or refreshes the page.

The browser submits plan/cycle only to the existing checkout endpoint. Client price,
user-ID and payment-status claims do not determine prices or access. The server
rejects extra JSON fields, revalidates the catalog and mapping, checks the authenticated
user exists, selects the configured price ID, and persists a server-owned checkout
link before calling Paddle. Only an opaque reference is sent in custom_data; the
returned transaction ID is durably bound before returning checkout. Webhooks use
that stored transaction ID rather than browser/custom_data account claims.
No secret or provider error body is printed.

Returned checkout URLs must use HTTPS, contain no user credentials, and belong to
Paddle or the current application host. The continuation frontend performs a matching
check. Requests use bounded connect/read timeouts and do not follow provider redirects.
Checkout errors preserve the selection and offer retry, without assuming payment.

## Configuration and unfinished billing work

Paddle uses `PADDLE_ENVIRONMENT=sandbox` by default, preserving prior behavior. Explicit
`live` selects `https://api.paddle.com`; invalid environments disable checkout. No
environment variables were changed in this task. Checkout requires `PADDLE_API_KEY`
and the existing per-plan/per-cycle `PADDLE_*` price mapping in `beckend/app.py`,
plus `PADDLE_WEBHOOK_SECRET` and the matching `PADDLE_PRODUCT_<PLAN>` catalog ID.
Checkout remains disabled until these activation prerequisites exist.
The existing Starter quarterly key is `PADDLE_STARTER_QUARTERLY_PRICE_ID`. Secrets
must be provisioned outside the repository through a separately authorized process.

Missing configuration returns safe 503, with no provider call. Account creation
remains available but confers no AI entitlement. Public metadata indicates provider
setup status without revealing its configuration values.

A signed, transaction-safe webhook backend now supports a bounded recurring USD
workflow; see [Paddle billing](paddle-billing.md) for the exact payment proof,
linkage, idempotency, event policy, new migration and unsupported cases. Checkout
initiation and browser redirects never activate access. No sandbox or live payment
has been executed. Refund/dispute handling, checkout recovery, catalog reconciliation,
merchant payment-page configuration and independently reviewed PostgreSQL verification
remain required before accepting money. No Paddle.js integration or successful
end-to-end provider payment is claimed.

AI credentials, durable production storage, reviewed PostgreSQL migrations and backups,
HTTPS/Secure cookies, browser testing and deployment remain separate requirements.
Legacy `/api/chat` and `/api/products` now reject anonymous requests before importing
missing modules; they are not advertised as implemented products. Marketing Studio
and content-library endpoints retain their own subscription/ownership protections.

This routing is verified for the existing Flask same-origin application. A separate
static hosting deployment using `frontend/_redirects` needs its own review of routing
for `/start` and API cookies/proxying; that production-related configuration was not
modified. Opening an HTML file directly is not the supported authenticated journey.

## Offline verification

```sh
python -B -m unittest discover -s tests -q
node --test tests/customer_account_frontend.test.js tests/marketing_studio_frontend.test.js tests/content_library_frontend.test.js tests/public_journey_frontend.test.js
```

Tests use isolated temporary SQLite databases and mocked provider boundaries. They
cover public catalog access, real signup/login continuation, malformed selection,
client-price rejection, absent configuration, server price/buyer binding, no activation,
unsafe checkout URLs, frontend pricing/error/loading behavior, duplicate submission,
and stale authentication-response races. The earlier library/marketing/account tests
remain included. No live/provider request, installation, deployment or production
migration was performed. PostgreSQL tests remain skipped without explicit disposable
target setup, and no browser executable was installed for this task.
