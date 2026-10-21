# SQLite source-only importer dry-run

This standalone preflight consumes a detached offline SQLite capture. It does not
import Flask, initialize application schemas, load environment credentials, open
PostgreSQL, inspect targets, or write data. It is **not a backup, migration, import
receipt, or authorization to import**. Existing test data remains protected.

## Commands

From the repository root:

```sh
python -B beckend/import_sqlite_snapshot.py \
  --snapshot /path/to/consistent-offline-snapshot.sqlite \
  --confirm-offline-snapshot
```

Optional separately captured uploads:

```sh
python -B -m beckend.import_sqlite_snapshot \
  --snapshot /path/to/consistent-offline-snapshot.sqlite \
  --confirm-offline-snapshot \
  --uploads /path/to/offline-upload-snapshot
```

There is no inferred source, target argument, apply flag, or additional mode. Never
point it at an existing service database or prepare a capture by deleting sidecars.
Use only an independently obtained consistent offline snapshot. See
[the source validator prerequisites](sqlite-snapshot-validation.md).

Exit status is 0 for a completed source dry-run, 1 for failure, 2 for invalid CLI
arguments, and 130 for KeyboardInterrupt during the dry-run. Failure JSON contains
`completed: false` and a fixed error code, without a fingerprint or manifest.
`--help` prints static usage. Unknown arguments are rejected without echoing their
possibly sensitive contents. Neither command creates an output file; redirecting
stdout would itself write a file and requires a separate decision.

## What is checked

Exactly one `guarded_source_session()` is opened. The importer does not independently
open or hash the SQLite file. It uses only the public session API and exhausts one
reader before starting another, in this order:

1. users
2. companies
3. subscriptions
4. promo_usage

Each reader supplies all explicit columns in primary-key order. The consumer checks
row width, tuple shape, positive strictly increasing IDs, duplicate IDs, record
counts against the validated counts, and the maximum ID against validated sequence
metadata. Missing records, excess records, duplicate IDs, unsorted IDs, or interrupted
consumption fail closed. This trusts the guarded reader's value fidelity; it is not
a second independent SQL read or proof of capture completeness.

Rows are retained as exact tuples in local memory during consumption: hashes,
IDs, NULLs, timestamps, payment references, verification flags, company fields,
and file references are not rewritten. These private rows are never returned,
printed, or persisted. Staging lists are cleared on success or failure; Python does
not guarantee secure memory erasure. Do not collect debug dumps or tracebacks with
locals, or log consumer exceptions containing private values.

The manifest is constructed only AFTER source-context exit and cleanup succeed,
`finalized` is true, the final report is valid, and its fingerprint and baseline
counts/sequences agree. A read fault caught inside the source consumer still
invalidates the guarded session and prevents completion. Failed cleanup or source
mutation also prevents completion. The original validator CLI is unchanged.

## Manifest and limitations

Successful aggregate JSON contains:

- Version, `mode: source_only_dry_run`, `completed: true`, and `migration_performed: false`.
- Authoritative whole-source SHA-256 after successful finalization.
- Validated and consumed counts for each table.
- Existing maximum IDs, recorded SQLite sequence high-water marks, and minimum safe
  next IDs, including deleted-ID history where it exists.
- Validator warnings and upload-dependency summary, without filenames or rows.

Warnings are not automatically resolved. An omitted upload snapshot can leave
unchecked-logo warnings; missing referenced files in a supplied snapshot block
validation. Upload inspection does not pin, fingerprint, transfer, or durably store
file contents. Preserve references and plan file handling independently.

Use one session at a time in a trusted, sequential process. Do not independently
open/close the same inode, use overlapping/nested sessions, share readers across
threads, or run source-writing helpers. POSIX locks and sequential metadata checks
cannot establish capture provenance or defeat every hostile/transient filesystem
change. Ordinary reads may update access times. Source files must already be
consistent detached captures on a filesystem with reliable POSIX locks.

All retained rows increase memory use. This step is intended for bounded synthetic
or development snapshots, not unbounded datasets. It creates no target schema,
sequence changes, receipt, repeat-run ledger, rollback process, or migration plan.
Any future importer must independently guard its disposable target, reconcile exact
records, and keep its transaction uncommitted until source finalization succeeds.
A separate technical review is required before adding target support.

## Offline tests

```sh
python -B -m unittest discover -s tests -q
```

Tests create only temporary synthetic snapshots and upload fixtures. They check
source non-mutation, sequential complete consumption, count/ID reconciliation,
privacy, CLI behavior, and failed-read/finalization rejection. A fault-injection
test intentionally mutates its own synthetic fixture; the tool does not do so.
