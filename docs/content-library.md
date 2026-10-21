# Customer marketing content library

The Marketing Studio now has **Save Marketing Pack** and **My Content Library**.
After successful generation, Save persists the exact four outputs and normalized
product brief in the application database. Open the library to view saved packs,
copy individual outputs, paginate, or permanently delete an item. Empty/loading/error
states and retry controls are included. No localStorage is used. Saving does not
regenerate content, call a provider, consume another attempt, or change entitlement.

## Ownership and save proof

Generation now returns an additional `save_token`, signed with a dedicated salt and
the existing application secret. The token contains only the customer ID and a
canonical pack digest, not the brief/outputs. Save verifies the signature, digest,
customer ownership, and a 24-hour age limit. Tampered output, another customer's
proof, missing proof or expired proof is rejected. The API never automatically
regenerates an expired pack; its displayed content remains manually copyable.
Secret rotation invalidates pending save proofs but does not remove saved records.
Treat tokens as sensitive capabilities; do not log them or business content.

Every operation derives the customer from the authenticated session and verifies
that the user still exists. Client customer IDs are rejected/never used as selectors.
Detail and delete query both item ID AND owner ID; foreign and nonexistent items
return the same 404. Listing is owner-scoped. Saved content remains accessible if a
subscription expires; there is no new subscription charge or AI call for library
access. The save proof originated from the existing subscription-protected generation
endpoint, so arbitrary client-written packs cannot be submitted without that proof.

## HTTP API

- `POST /api/marketing/library`: JSON `{details, outputs, save_token}`. Returns 201
  for a new item or 200 for an existing identical pack. Unknown fields are rejected.
- `GET /api/marketing/library`: newest-first page of at most 20 complete packs.
  Optional `?before=<positive ID>` uses keyset pagination. `next_cursor: null` marks
  the end. No customer filter, arbitrary sorting or client-selected page size exists.
- `GET /api/marketing/library/<id>`: one owned pack.
- `DELETE /api/marketing/library/<id>`: permanently deletes only an owned pack.

All library responses use `Cache-Control: no-store`. Errors are fixed safe messages,
not SQL, credentials, proof values or rows. Bodies are bounded to 32 KiB; product
and output fields retain existing Marketing Studio character limits. UI fields use
textContent/textarea.value, never HTML interpretation. HTML-looking text is stored
and copied literally. The UI confirms deletion, suppresses repeated in-flight
save/delete actions, and supports clipboard failure through manual text selection.

## Persistence and limits

The new `marketing_content` table stores ID, customer ID, canonical content digest,
product name, JSON brief, JSON outputs, and a UTC text timestamp. A unique constraint
on `(customer_id, content_digest)` makes retries and concurrent duplicate saves
idempotent. Different accounts can each own identical content. Re-saving a deleted
item with a still-valid proof creates a new record.

A customer may retain at most 200 packs. Duplicate retries still succeed at capacity;
delete existing content before saving another unique pack. Save transactions serialize
with SQLite `BEGIN IMMEDIATE` or a separate PostgreSQL transaction advisory lock with
a 3-second lock timeout. No transaction waits on a paid AI call. Authentication and
payment tables are not rewritten. Existing data-directory/connection policy remains
unchanged; no fallback from PostgreSQL to SQLite is added.

Local SQLite initialization adds only the new table/index with `IF NOT EXISTS`.
It is safe to repeat and preserves prior users, subscriptions and content. All tests
use freshly created disposable SQLite databases, never repository/runtime data.

PostgreSQL has an additive migration:
`beckend/migrations/postgres/0002_marketing_content.sql`. The original migration's
contents/checksum are unchanged. It is picked up by the existing explicit migration
runner; app import does not create PostgreSQL tables. No migration was executed in
this task. **PostgreSQL persistence/concurrency is implemented but NOT runtime
verified**; do not enable it for real customers until independently reviewed and
tested against a separately authorized disposable PostgreSQL instance. A missing
migration fails safely with library-unavailable responses, without creating schema
or silently switching databases.

The offline migration-runner mock test now checks the checksum for each migration
filename instead of reusing 0001's checksum for every version. PostgreSQL integration
safety guards are unchanged and those tests remain skipped without explicit setup.

## Data-safety limitations

Saved packs contain business content in plain database text. Protect database files,
access permissions and backups. The library uses the same existing persistence
location; it does not make ephemeral Render storage durable. Deletion removes the
active row; backup retention and restore are separate operational policies. A future
account-deletion flow must explicitly purge this new table; no account-deletion
endpoint or new cascade guarantee is introduced here.

The earlier offline SQLite snapshot validator/importer intentionally supports ONLY
the four original tables. It will reject a new snapshot containing `marketing_content`
as an unexpected table. Its safeguards are unchanged: do not drop/ignore library
records to make a snapshot pass. Extending the data inventory, validation, sequence
handling and importer for saved content is separate work required before migrating
such snapshots. Existing four-table offline fixtures remain supported.

No live database, Render service, paid API, credentials or deployment was used.
Real-browser testing, PostgreSQL HTTP testing, explicit production migrations,
durable storage, backups and payment activation remain separately reviewed work.

## Offline checks

```sh
python -B -m unittest discover -s tests -q
node --test tests/customer_account_frontend.test.js tests/marketing_studio_frontend.test.js tests/content_library_frontend.test.js
```

HTTP tests exercise real SQLite writes, persistence across browser sessions and
idempotent schema initialization, ownership isolation, malformed/oversized input,
proof expiry/tampering, capacity, pagination, concurrent duplicates and storage
failures. The generated-pack→save→list→delete test mocks only the paid provider and
confirms generation usage and provider calls do not increase during library actions.
