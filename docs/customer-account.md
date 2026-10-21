# Customer account settings

The customer portal at `/customer.html` now supports **Account Settings** and
server-backed **Logout**. Open Account Settings to view the signed-in customer's
name and email and change their password. Current password, a new password of
8–128 characters, and matching confirmation are required in the form. Successful
password change returns the customer to sign-in; the old password stops working.
Network/server failures keep controls available for retry and never display hashes
or password values. Logout waits for the server's response rather than reloading
an authenticated session.

`POST /api/customer/password` accepts JSON with `current_password` and
`new_password`. It requires a customer session, verifies the current stored hash,
and updates only that session's customer ID. Client-supplied IDs/emails cannot select
another account. The update compares the verified original hash to avoid overwriting
a competing change. Existing Werkzeug PBKDF2 hashes are supported; the new hash uses
Werkzeug's current password-hashing default. No schema or migration changes are
needed. Customer authentication responses are marked `Cache-Control: no-store`.

Responses: 200 success; 400 invalid input/reused password; 401 no valid customer
session; 403 incorrect current password; 409 competing account change; 503 storage
failure. Passwords, database URLs and hashes are not included in error responses
or logged by this handler. Password requests must use HTTPS, as do the application's
existing Secure session cookies.

This is a signed-in password change, **not forgotten-password recovery**. It needs
no email provider or paid API. Clearing the current Flask cookie signs out this
browser; it does not revoke previously issued cookies on other devices. Global
session revocation and authentication rate limiting remain separate security work.
No live services or existing customer records are needed to run the tests.

## Offline checks

```sh
python -B -m unittest discover -s tests -q
node --test tests/customer_account_frontend.test.js
```

Backend tests use real Flask HTTP handlers and freshly created temporary SQLite
storage, block outgoing HTTP calls, and suppress dotenv loading. JavaScript tests
use Node's built-in runner with mocked DOM/network boundaries; a real browser and
PostgreSQL HTTP integration have not been exercised for this feature.

AI assistant/promo routes still require the missing `ai_agent` module and a reviewed
provider integration. Product routes still require `products_db`. Paddle checkout
still needs valid configured prices/credentials; payment verification/webhooks and
entitlement activation are not completed by account settings. Nothing in this
change authorizes deployment or makes those integrations complete.
