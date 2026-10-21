# Offline SQLite snapshot validation

This is Phase 1 source preflight, not a backup utility or a data importer. It uses
only Python's standard library. It does not import Flask, read .env, initialize
schemas, read DATABASE_URL, contact PostgreSQL, or access Render.

## Source prerequisites

Provide an existing **separately captured, consistent, detached offline snapshot**.
Do not point this tool at an application database, including a running local one.
The operator must know how the snapshot was obtained. Existing test records are
not disposable without explicit owner authorization.

The validator cannot prove capture provenance, recover missing records, or detect
all logically incomplete copies. A clean integrity check is not proof that the
capture included every committed transaction. Obtain a consistent snapshot through
a separately reviewed/authorized process first. Do not simply copy a live database
or delete its WAL/SHM/journal to make the checks pass.

For conservative safety it rejects:

- Missing files, zero-length files, malformed headers and nonregular files.
- Symlinks in the source path and hardlinked source files.
- Any adjacent `-wal`, `-shm`, or `-journal`, even empty files.
- WAL-mode database headers, even after a checkpoint removed the sidecars.
- Unavailable POSIX read locking or a cooperating writer holding SQLite's
  pending/reserved/shared lock bytes.
- Source content, identity, size, mtime or ctime changes detected during validation.

A valid self-contained backup that still has a WAL-mode header is deliberately
rejected; it needs a separately prepared standalone rollback-journal-mode snapshot,
not manual header editing or sidecar removal. Snapshot preparation is outside this
tool. Sources on filesystems without reliable POSIX locks are unsupported.

After the checks, SQLite opens with `mode=ro&immutable=1`, `query_only=ON`, and
`trusted_schema=OFF`. Immutable mode avoids creating sidecars, but must never be
used to bypass an active source's WAL. Read locks and before/after checks add
protection; they do not defeat a malicious writer ignoring locks, same-process
writers, or an untrustworthy filesystem. Ordinary reads may update filesystem
access times. No source content or metadata is intentionally written.

## Usage

From the repository root, with Python 3.10+ and SQLite supporting table_xinfo and
trusted_schema, on a supported POSIX filesystem:

```sh
python -B beckend/validate_sqlite_snapshot.py \
  --snapshot /path/to/offline-capture/habrivo.db \
  --confirm-offline-snapshot
```

Optionally supply a separately captured upload directory:

```sh
python -B beckend/validate_sqlite_snapshot.py \
  --snapshot /path/to/offline-capture/habrivo.db \
  --confirm-offline-snapshot \
  --uploads /path/to/offline-capture/uploads
```

There is no inferred default path or automatic source creation. The confirmation
attests that both supplied inputs are offline captures. No output files are
created: aggregate JSON is written to stdout. Store reports only through a
separately authorized process; shell redirection itself creates a file.

Exit status: 0 = no blocking findings (warnings can remain); 1 = validation blocked;
2 = command usage error. A zero exit status does **not** authorize migration.

## Checks and reporting

- SQLite integrity; the four expected application tables; columns, declared types,
  ID primary keys, required NOT NULL constraints, unique email index/collation,
  expected plan/cycle checks and differences in column defaults.
- Unexpected tables, columns, views and triggers block automatic acceptance. Names
  from the source schema are never printed. Equivalent alternative CHECK syntax
  may be flagged for review: this is not a general SQLite DDL parser.
- Actual stored value types, positive IDs, required values, PostgreSQL-incompatible
  NUL text, exact/normalized email collisions, and password-hash format.
- Customer/subscription references and cross-account usage mismatches; missing
  parent tables make reference validation incomplete and block acceptance.
- Plan/cycle membership, integer amounts and verification flags, timestamp format
  and date order, and status/currency/payment-reference anomalies.
- Creation timestamps are UTC-style `YYYY-MM-DD HH:MM:SS`. The text cannot prove
  the historical timezone. Nullable creation dates and entitlement/date anomalies
  are reported without changing values.
- Subscription status vocabulary is a review aid (pending, active, expired,
  canceled/cancelled, paused, past_due, trialing), not a new enforced constraint.
  Unknown statuses, repeated payment references, active subscription ambiguity,
  and unknown hash formats are warnings requiring human interpretation. No money
  units, hashes, statuses or emails are converted.
- For each usable table: current maximum ID, recorded sqlite_sequence high-water
  mark when available, and minimum safe next ID. Deleted-row history is preserved
  where the source sequence records it. Missing history cannot be reconstructed.
- Local `/uploads/<safe-basename>` references. With --uploads, aggregate existence,
  nonempty referenced files, regular-file/symlink checks and unreferenced files.
  Missing referenced files block acceptance; unreferenced files are retained for
  review. No filenames, image content or file bytes appear in the report.

The report contains only fixed issue codes/categories, counts, known table/column
labels, ID high-water metadata and the whole-source SHA-256 after a stable read.
No paths, raw schema SQL, unexpected object names, rows, emails, password hashes,
payment references or exception text are emitted. Counts and source fingerprints
are still operational metadata: handle them appropriately.

## Public guarded source-session API

`guarded_source_session(snapshot, confirmed_offline=True, uploads=None)` is a
single-use context manager for trusted offline consumers. It uses the SAME
snapshot guard and validation implementation as `validate_snapshot()` and the CLI;
it does not reopen a previously validated path with weaker checks. The CLI's JSON
format, exit statuses and warning/blocking-error behavior remain unchanged.

```python
from beckend.validate_sqlite_snapshot import guarded_source_session

with guarded_source_session(
    snapshot_path,                 # Explicit, already captured offline file
    confirmed_offline=True,
    uploads=upload_snapshot_path,   # Optional, separately captured directory
) as source:
    columns = source.columns("users")
    for values in source.iter_records("users"):
        # Values are SENSITIVE. Consume privately; never print/log/serialize them.
        # This example intentionally performs no writes or target connections.
        pass
    # source.finalized is False here; report["source_sha256"] is None.

# Only a successful context exit grants an authoritative fingerprint.
assert source.finalized
fingerprint = source.fingerprint
safe_report = source.report
```

No session or readable records are exposed until initial integrity, schema,
record/reference, sequence and optional upload validation passes with no blocking
errors. Warnings remain visible: readable records are not migration approval.
A historical/incomplete schema is not repaired. If uploads are omitted, the
existing unchecked-reference warnings remain; supplying an incomplete upload
snapshot remains blocking.

The supported session interface is:

- `columns(table)`: immutable fixed tuple of column names.
- `iter_records(table)`: iterator of value tuples in that column order, sorted
  by positive primary key `id ASC`. Every table uses all its explicitly listed
  columns, including IDs, NULLs, timestamps and unchanged password hashes.
- `report`: detached aggregate report, safe to inspect without revealing rows.
  Mutating the returned dictionary cannot change validation state. Previously
  retrieved report copies do not update when the context finalizes.
- `finalized`: true only after successful consumption-context exit and final
  source checks/cleanup. It is false after consumer errors or validation failures.
- `fingerprint`: raises `SourceSessionError("source_not_finalized", ...)` until
  successful finalization. The aggregate report likewise withholds its fingerprint
  while pending or failed.

`table` must exactly select `users`, `companies`, `subscriptions`, or `promo_usage`.
There is no public SQL, custom column/projection, connection or cursor interface.
Unknown tables are rejected without echoing the supplied value. Private Python
attributes are implementation details, not a sandbox against malicious callers.

All readers close at context exit, including unstarted/partially consumed readers.
Reading after exit fails; full consumption is not required because initial
validation already checked the whole snapshot. A read or allowlist fault latches
session failure: catching it inside the body does not make exit succeed.
`SourceSessionError` exposes only a fixed `code` and detached privacy-safe `report`.
Source/SQLite/OS error messages are not echoed. Exceptions raised by the consumer
itself propagate unchanged; consumers must not put private records in their own
exception messages or logs.

### Cleanup failure behavior

Every registered reader is given one close attempt. A cursor is detached before
its close attempt so failure at end-of-iteration is not retried at context exit.
One reader's failure cannot skip peer readers. SQLite connection closure and the
separate source-descriptor closure are both attempted even if earlier releases
raise unexpected exceptions, including interruption exceptions. A descriptor-close
failure is never automatically retried: its descriptor number might already have
been released/reused. This is a release-attempt guarantee, not proof that an OS or
SQLite resource whose close operation failed is actually closed.

Any cleanup failure clears the fingerprint, adds blocking `cleanup` findings and
prevents `finalized=True`. Context-exit cleanup failures without a propagating
consumer exception raise `SourceSessionError` with a fixed code and the complete
safe aggregate report. At iterator exhaustion, ordinary cursor-close errors also
use this safe error; non-`Exception` interrupts (`KeyboardInterrupt`, `SystemExit`,
`GeneratorExit`, or custom `BaseException` subclasses) propagate as the ORIGINAL
exception object after invalidation. Catching that interrupt inside the context
cannot restore validity. Peer readers and underlying resources still receive
cleanup attempts at exit. Simultaneous failures are recorded individually by
category/count. Reports and translated errors omit original close messages;
preserved interrupts can carry private text, which callers must not log.

When a consumer exception is already propagating, that SAME exception is preserved
rather than replaced. Cleanup diagnostics are attached as its
`source_cleanup_report` dictionary. On Python 3.11+ a fixed-code exception note also
makes cleanup failure visible in ordinary tracebacks. Python 3.10 callers must
inspect the structured attribute (also available on later versions). For an API
`SourceSessionError`, its `report` is refreshed with all cleanup findings as well.
The retained session's `report` also contains the failures. Caller-originated
exception text remains the caller's responsibility. A consumer exception with
cleanup diagnostics is still failure: never commit a future target transaction or
continue source processing after catching it.

The standalone validator retains its JSON/exit-status interface. Cleanup failures
now produce a blocking report, exit status 1 and no source fingerprint, including
when both SQLite and descriptor closure fail. Successful validation behavior is
unchanged. Resources acquired before an initial-validation failure receive the
same release attempts; no session is exposed in that case.

Guarded record execution, fetch, tuple conversion and reader creation/registration
now latch any escaping `BaseException` before reporting or cleanup. Catching the
error inside the context does not restore validity: subsequent reads and successful
finalization are denied, and no authoritative fingerprint is released. Ordinary
exceptions (including `MemoryError` and unexpected `StopIteration`) become fixed-code
`SourceSessionError` diagnostics without underlying exception text. Control-flow
exceptions such as `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and custom
non-`Exception` subclasses propagate as the original exception object. Callers must
not log their potentially sensitive original messages. Consumer exceptions outside
supported read operations retain the existing propagation behavior.

Only `fetchone()` returning `None` or advancing an already exhausted reader counts
as normal exhaustion. Breaking iteration, leaving a reader unstarted, and ordinary
early context exit remain valid; the context owns and closes all readers. Failure
while creating/registering a reader releases the acquired cursor as well as peers.
Fault tests use synthetic in-memory exceptions, including allocation failures;
actual process termination or memory exhaustion that prevents Python cleanup or
error-report allocation cannot be made recoverable by this API.

Final SHA-256 computation is now bracketed by no-symlink path, SQLite sidecar,
source-descriptor identity/metadata and source-path identity/metadata checks, both
before and after hashing, against the opening baseline. Only a matching digest
AND successful checks on both sides permit fingerprint publication. Matching
content does not excuse a replaced path, changed descriptor or changed metadata.
Unexpected ordinary final-verification errors produce fixed-code diagnostics;
interrupts propagate unchanged and leave a blocking report. Every failure still
attempts SQLite and descriptor cleanup, including simultaneous cleanup failures.
The same final bracket applies to the standalone validator CLI, preserving its
JSON and exit-status interface.

This closes the reviewed missing-post-hash-check window, but does not prove capture
provenance, completeness of the original capture, or safety against every hostile
or continuously mutating filesystem. Checks are sequential observations, not an
atomic filesystem snapshot; changes after a check or transient changes hidden
between observations may evade detection. Continue requiring a trusted, detached,
consistent offline capture and cooperating locking. Upload trees are not pinned
by these checks. Independent post-fix review is required before designing importer
writes; these tests do not establish importer readiness. In-memory fault tests
cover release calls and state transitions, not every filesystem or OS close failure.

Successful exit reruns no-symlink path, sidecar, identity, size, mtime, ctime and
whole-content checks while the guard remains held. Source replacement, mutation
or finalization I/O failure raises and denies an authoritative fingerprint. The
validator never repairs or restores a changed source. Callers can catch exceptions
outside the context, but must not treat that as success: always propagate failure
to any future enclosing target transaction. Never commit a future import from
inside this source context; finalization has not yet happened there.

Use one guarded source session at a time in a trusted process. Do not reopen/close
the same inode independently, run concurrent/nested sessions on it, alter private
attributes or share readers between threads: POSIX locks are process-wide and
closing another descriptor can release them. Filesystem/provenance and concurrent
writer limitations described above still apply. Upload validation remains initial
reference/metadata inspection; it does not pin or fingerprint an upload tree for
future file transfer. This API neither authorizes live-data access nor implements
an importer, target connection, receipt, file copy or sequence restart.

## Limitations and future phases

No PostgreSQL target is opened, no records transferred, no sequence reseeded, no
files uploaded/deleted and no schema repaired. A missing historical column blocks
acceptance rather than being filled with a guessed value. File inspection checks
references/metadata, not image validity, checksums, capture timing or durable
storage. It does not validate missing products_db/ai_agent modules.

Validation holds ID/email/reference sets in memory and hashes/reads the whole
SQLite snapshot; it is intended for development/test captures, not an unbounded
hostile database service. Run only against trusted offline captures.

Future migration work must independently validate the target, preserve every ID
and exact password-hash string, reconcile all values, restart identity sequences
beyond historical high-water marks, and establish repeat-run and recovery rules.
Do not infer backup or migration readiness from the 32 foundation tests alone.

## Offline tests

```sh
python -B -m unittest discover -s tests -v
```

New tests generate synthetic snapshots and upload trees, check unchanged contents,
mtime/mode and sidecar inventories, exercise WAL and another process's SQLite
reserved lock, and verify privacy-safe reporting. Existing real PostgreSQL tests
remain guarded and skip when no explicit disposable test target is supplied.

## Narrow platform-test compatibility (Step 57Q)

Native Windows source validation/import remains **unsupported and fail-closed**.
`_acquire_source_lock()` is the internal POSIX-only boundary: it uses the same
nonblocking shared `fcntl.lockf()` lock over 514 bytes at `0x40000000`. Unsupported
platforms/missing `fcntl` produce `read_lock_unavailable`; OS lock failures produce
`active_writer_or_read_lock_unavailable`. There is no unlocked fallback. The
`_release_source_lock()` boundary closes the owned descriptor once, as before;
there is no new early `LOCK_UN` or retry of a failed close. Snapshot cleanup still
attempts it even after acquisition, reader or SQLite-close failure. Existing
fingerprint-denial and original-exception protections remain unchanged.

Portable in-memory resource tests mock acquisition at this internal boundary,
not `fcntl.lockf`. They still exercise descriptor release/close failures and all
reader/SQLite cleanup. Boundary tests inject a synthetic POSIX module and explicitly
verify unsupported-platform denial without depending on a real host `fcntl`.
Only the two real POSIX subprocess-lock tests are explicitly POSIX-only; no whole
validator/importer class is skipped. Consequently the **full native Windows suite
is not expected to pass yet**: real snapshot success tests need a supported runtime.
Passing the mock tests does not establish native Windows locking support.

To run just the portable resource/boundary tests on Windows, from the project root:

```powershell
Push-Location tests
try {
    python -B -m unittest test_sqlite_snapshot_validator.CleanupTests test_sqlite_snapshot_validator.PlatformLockBoundaryTests test_snapshot_metadata_probe -v
} finally {
    Pop-Location
}
```

Investigate `snapshot_changed` independently with:

```powershell
python -B tests/snapshot_metadata_probe.py
```

This probe creates and removes one new synthetic binary temporary file. It opens
no SQLite database, takes no source path and accesses no customer data. It prints
only platform and equality booleans for path/descriptor device, inode, size, mtime
and ctime before/after reading, plus content equality. It never prints paths, raw
metadata identifiers or file contents. It does not use or alter validator checks.
A false comparison is diagnostic evidence, **not** permission to discard that
identity/metadata field. All-true comparisons cannot rule out later SQLite-related
or filesystem-specific behavior; capture representative safe Windows test error
codes and failing test names as well. On Linux the probe returned all true; this
does not establish the cause on Windows Python 3.13/SQLite 3.50.

Metadata comparisons still occur before lock acquisition; therefore an unsupported
Windows source can fail with `snapshot_changed` before reaching
`read_lock_unavailable`. No ordering, identity, sidecar, read-only, schema or digest
checks were relaxed. A future native Windows locking/identity backend, reparse-point
handling and actual Windows filesystem tests require separate implementation/review.
