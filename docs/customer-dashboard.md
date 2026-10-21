# Customer dashboard experience

The existing customer.html workspace now uses one tested authentication controller
instead of inline login/session code. It reuses Marketing Studio, the server-backed
library, account/password settings, recovery links and the public pricing journey.
The inaccessible legacy chat panel and duplicate dashboard checkout/pricing widget
were removed from the customer UI; their backend routes were not changed.

## Customer workflow

- Successful login or restored server authentication opens the welcome/workspace.
  Signup still requires a separate login and never invents an authenticated account.
- Navigation opens Studio, saved content and account settings; Upgrade/Pricing uses
  the existing public catalog. Logout navigation clicks the existing backend logout
  control. Paid selection still uses the existing server checkout-intent flow.
- The account overview displays server-derived account tier, subscription state and
  monthly usage. Remaining attempts are calculated from the returned used/limit;
  no free allowance is fabricated. API errors clear old metrics and lock generation.
- Free accounts retain dashboard, settings and library access. Studio stays locked
  without verified paid access or an email-verified eligible lifetime trial, available provider configuration and remaining quota.
- Empty library messaging acknowledges that generation needs an eligible plan.
  Existing save/list/copy/delete behavior is preserved. Account/library sections
  scroll into view; navigation, summaries and content cards wrap for small screens.
- Initial session checks, errors/retries, duplicate login prevention and generic
  sign-in errors have explicit states. User-provided names/content use textContent
  or textareas, never innerHTML. No customer content is persisted in browser storage.

`account_entitlements()` adds a backward-compatible subscription metadata object.
It selects the currently entitled subscription first, otherwise the latest owned
record. Status distinguishes no paid subscription, pending, unverified, scheduled,
expired, paused, past_due and canceled. "Unverified" includes records requiring
payment verification/reconciliation; the UI does not claim a refund outcome or
identify why a hold exists. Previous plan metadata does not grant paid access.
Existing quota calculations, ownership filters and billing mutations are unchanged.

Authentication mode is now owned solely by customer-dashboard.js. The duplicated
signup toggle was removed from checkout-journey.js to avoid a script-loading race
that could switch a selected signup page back to login. Checkout API calls,
validation, intent preservation and billing state transitions were not changed.

## Logout, expiry and stale responses

Existing revocable server sessions remain authoritative. Successful logout and
password-change sign-out clear client workspace data before redirecting to login.
A 401 from account access, Studio, library or settings ends the displayed session.
Generation/library/settings responses from an earlier displayed session cannot
repopulate private panels. Generation failures also refresh quota information;
server reservation policy still determines whether an attempt counted.

Pagehide clears private DOM and component references; a back-forward-cache restore
rechecks backend authentication before showing the workspace. Prompts, outputs,
account profile, login values and library cards are cleared. These are UI privacy
controls, not secure erasure of browser process memory or an OS screenshot guarantee.
Customer HTML and customer-specific overview/subscription API responses use no-store.
Static dashboard markup contains no customer records. Backend authentication rejects
copied pre-logout cookies, including after refresh. No URL parameter, including
_ptxn, supplies customer identity or subscription access.

## Verification and remaining browser work

Offline HTTP checks use disposable SQLite and cover free access/library, real owned
subscription metadata and usage, cross-account selectors, expired/unverified/paused/
scheduled states, no-store and copied-cookie rejection after logout. Frontend tests
cover login/signup/restore, navigation, failed session checks, late responses,
private-data clearing, cached-page restoration and server-derived overview rendering.
No paid AI, SMTP, Paddle, Render or existing database was accessed.

Later separately authorized browser smoke checks, using isolated HTTPS and synthetic
accounts only:

1. Load /customer.html logged out: check sign-in loading, signup mode and visible
   recovery navigation. Login must reveal the workspace; refresh must restore only
   an actually valid session. Exercise a failed API request and Retry.
2. Use an unverified free account: verify zero usable AI credits and disabled generation, usable library
   empty state/settings, and working public pricing link. Navigation must not call AI.
3. Use an authorized disposable paid fixture: compare status/usage with fixture data,
   test quota exhaustion and provider-unavailable UI without making a paid API call.
4. Exercise library list/copy/delete and settings, keyboard focus and 320px/mobile/
   tablet/desktop layouts. Confirm untrusted text is not interpreted as markup.
5. Logout, refresh and use browser Back. No old profile, outputs or saved cards should
   reappear. Test expired sessions and delayed Studio/library responses during logout.
6. Verify paid-plan signup/login selection still appears through the existing checkout
   continuation UI, without initiating any payment in this smoke test.

Real-browser rendering, clipboard permissions, browser-specific history caching,
assistive-technology behavior and guarded PostgreSQL HTTP execution remain unverified.
No migration or billing-policy change was introduced. Existing payment/provider and
email-worker readiness limitations remain. No deployment readiness is claimed.
