# Database foundation (development branch only)

This change is not a production migration or cutover. It must not be deployed to
the existing Render service as configured: Render requires DATABASE_URL and will
fail configuration validation without it. Do not connect the historical
`habrivo-db` service. No ownership or compatibility is assumed.

## Configuration

- Local default: APP_ENV=development, SQLite at beckend/habrivo.db.
- APP_ENV accepts development, test, production. Unknown values fail validation.
- APP_ENV=production OR RENDER=true requires PostgreSQL and an explicit valid
  postgresql:// or postgres:// DATABASE_URL. RENDER=true overrides a development
  APP_ENV for the SQLite prohibition.
- DB_BACKEND optionally selects sqlite or postgres. A URL selects PostgreSQL when
  backend is omitted. Contradictory settings fail; connection failures never
  retry against SQLite.
- DATABASE_URL is credential-bearing: keep it out of logs and version control.
  Configure TLS according to the chosen provider (including certificate
  verification); the foundation passes the supplied URL to psycopg unchanged.
- DATA_DIR still controls local SQLite and uploads; it does not configure
  PostgreSQL storage. Production uploads remain a separate persistence problem.
- Existing dotenv loading remains in app.py. The standalone migration command
  uses process environment only, not dotenv files.

Outside Render, operators must explicitly set APP_ENV=production. The application
cannot infer every hosting platform. Local SQLite initialization remains automatic
for compatibility. PostgreSQL app import neither connects nor runs schema DDL.
Both database consumers now use database.py for configuration and connections.

## Schema and execution boundary

0001_initial.sql creates users, companies, subscriptions, promo_usage and their
existing indexes. An explicit runner maintains schema_migrations with checksums,
serializes runners with a transaction-scoped advisory lock, and applies all pending
DDL in one transaction. Changed applied files fail validation. Conflicting existing
tables fail rather than being silently adopted. Run only against a deliberately
chosen new SaaS database after separate review.

Future explicit command, NOT executed as part of this change:

    python beckend/migrate.py --apply

No data import, destructive downgrade, database provisioning, historical database
adoption, or automatic production startup migration is included.

Existing text password hashes are stored verbatim; there is no rehash or password
reset. Text timestamps remain UTC YYYY-MM-DD HH:MM:SS, integer payment_verified
remains integer, and amount_usd keeps its existing integer meaning (no conversion
to cents). Nullable fields, defaults, plan checks and missing foreign keys are
preserved. Identity columns use BIGINT; a future ID-preserving data import must
also reseed identity sequences. This migration does not perform that import.

The small PostgreSQL connection adapter supports the current app's question-mark
and named-value DML, row name/index access, and insert IDs for users/companies.
It is not a general SQL dialect converter. Review future SQL against both backends;
do not put literal question marks/colon placeholders in new SQL without extending
that boundary. Missing AI/product modules are untouched.

## Tests

Offline, after project dependencies are available:

    python -B -m unittest discover -s tests -v

The PostgreSQL integration test skips unless HABRIVO_TEST_POSTGRES_URL is explicitly
set. It accepts only a loopback URL with database name habrivo_foundation_test and
no query overrides. It never uses DATABASE_URL from the environment. The operator
must provide a pre-existing disposable LOCAL test database and psycopg; the test
creates and drops only a uniquely named schema, not a database. Do not forward a
production database to that local address.

Offline tests verify configuration, SQLite schema creation/upgrades and hash login,
SQL adapter behavior, migration bookkeeping/checksums, and no PostgreSQL connection
on app import. These do not replace real PostgreSQL schema execution tests.

Before deployment review: run the PostgreSQL integration suite, review TLS and
least-privilege roles, establish backups/restore evidence, design a reviewed data
export/import with stable IDs and session handling, and persist uploaded images.
The current free Render SQLite database is not made durable by this code change.
