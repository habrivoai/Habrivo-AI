"""Source-only offline importer preflight; no target support or data writes."""
import argparse
import json
import re

if __package__:
    from .validate_sqlite_snapshot import guarded_source_session
else:
    from validate_sqlite_snapshot import guarded_source_session


TABLES = ("users", "companies", "subscriptions", "promo_usage")


class ImportDryRunError(Exception):
    """Fixed, privacy-safe failure code. Never attach records or source exceptions."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments, potentially including secrets.
        raise ImportDryRunError("invalid_arguments") from None


def source_only_dry_run(snapshot, *, confirmed_offline=False, uploads=None):
    """Consume one guarded snapshot sequentially and return aggregate metadata.

    Exact tuples are retained privately only for this call, without coercion or
    serialization. They are discarded on success/failure, not returned. This is
    neither secure memory erasure nor a reusable record-transfer API. No target
    connection, output file, import receipt or apply mode exists.
    """
    records = {}
    consumed = {}
    try:
        with guarded_source_session(snapshot, confirmed_offline=confirmed_offline,
                                    uploads=uploads) as source:
            initial = source.report
            if not initial["valid"]:
                raise ImportDryRunError("source_validation_failed")
            for table in TABLES:
                expected = initial["tables"][table]
                if type(expected) is not int or expected < 0:
                    raise ImportDryRunError("source_count_invalid")
                columns = source.columns(table)
                if not columns or columns[0] != "id":
                    raise ImportDryRunError("source_record_contract_invalid")
                rows = records[table] = []
                seen = set()
                last_id = 0
                # Exhaust exactly one reader before creating the next one.
                for values in source.iter_records(table):
                    if type(values) is not tuple or len(values) != len(columns):
                        raise ImportDryRunError("source_record_contract_invalid")
                    record_id = values[0]
                    if (type(record_id) is not int or record_id <= last_id
                            or record_id in seen):
                        raise ImportDryRunError("source_id_enumeration_failed")
                    seen.add(record_id)
                    last_id = record_id
                    rows.append(values)  # No rewriting of hashes, NULLs, or timestamps.
                    if len(rows) > expected:
                        raise ImportDryRunError("source_count_mismatch")
                if len(rows) != expected:
                    raise ImportDryRunError("source_count_mismatch")
                if last_id != initial["sequences"][table]["maximum_existing_id"]:
                    raise ImportDryRunError("source_id_enumeration_failed")
                consumed[table] = len(rows)
        # Do not publish any manifest from inside the pending source context.
        final = source.report
        fingerprint = source.fingerprint
        if (not source.finalized or not final["valid"]
                or not isinstance(fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                or final["source_sha256"] != fingerprint
                or final["tables"] != initial["tables"]
                or final["sequences"] != initial["sequences"]):
            raise ImportDryRunError("source_finalization_failed")
        return {
            "manifest_version": 1,
            "mode": "source_only_dry_run",
            "completed": True,
            "migration_performed": False,
            "source_sha256": fingerprint,
            "tables": {table: {"validated_count": final["tables"][table],
                               "consumed_count": consumed[table]} for table in TABLES},
            "sequences": final["sequences"],
            "warnings": [issue for issue in final["issues"] if issue["severity"] == "warning"],
            "uploads": final["uploads"],
        }
    except ImportDryRunError:
        raise
    except Exception:
        raise ImportDryRunError("source_dry_run_failed") from None
    finally:
        # Drop staged references on every path. Python does not promise zeroization.
        for rows in records.values():
            rows.clear()
        records.clear()


def main(argv=None):
    parser = _Parser(prog="import_sqlite_snapshot", description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Explicit consistent offline SQLite capture")
    parser.add_argument("--confirm-offline-snapshot", action="store_true",
                        help="Attest the source and optional uploads are offline captures")
    parser.add_argument("--uploads", help="Optional separately captured upload directory")
    try:
        args = parser.parse_args(argv)
    except ImportDryRunError:
        print(json.dumps({"completed": False, "error": "invalid_arguments"}))
        return 2
    try:
        manifest = source_only_dry_run(args.snapshot,
                                      confirmed_offline=args.confirm_offline_snapshot,
                                      uploads=args.uploads)
    except BaseException as error:
        # Never render underlying source errors or interrupt messages in the CLI.
        print(json.dumps({"completed": False, "error": "source_dry_run_failed"}))
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
