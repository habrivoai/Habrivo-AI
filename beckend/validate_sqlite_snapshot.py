"""Offline SQLite source preflight. No application imports or target connections."""
import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import urlsplit
from types import MappingProxyType

TABLES = {
    "users": {
        "id": "INTEGER", "name": "TEXT", "email": "TEXT",
        "password_hash": "TEXT", "created_at": "TEXT",
    },
    "companies": {
        "id": "INTEGER", **dict.fromkeys(("company", "owner", "whatsapp", "email",
        "category", "logo", "address", "website", "created_at"), "TEXT"),
    },
    "subscriptions": {
        "id": "INTEGER", "customer_id": "INTEGER", "plan": "TEXT",
        "billing_cycle": "TEXT", "currency": "TEXT", "amount_usd": "INTEGER",
        "status": "TEXT", "payment_reference": "TEXT", "start_date": "TEXT",
        "expiry_date": "TEXT", "created_at": "TEXT", "payment_verified": "INTEGER",
    },
    "promo_usage": {"id": "INTEGER", "customer_id": "INTEGER",
                    "subscription_id": "INTEGER", "created_at": "TEXT"},
}
REQUIRED = {
    "users": {"name", "email", "password_hash"},
    "companies": set(),
    "subscriptions": {"customer_id", "plan", "billing_cycle", "currency",
                      "amount_usd", "status", "created_at", "payment_verified"},
    "promo_usage": {"customer_id", "subscription_id", "created_at"},
}
PLANS = {"starter", "pro", "premium"}
CYCLES = {"monthly", "quarterly", "half_yearly", "yearly"}
# Review vocabulary, not a new database constraint or automatic status mapping.
STATUSES = {"pending", "active", "expired", "canceled", "cancelled",
            "paused", "past_due", "trialing"}
MAX_ID = (1 << 63) - 1
SIDECARS = ("-wal", "-shm", "-journal")


class UnsafeSnapshot(Exception):
    def __init__(self, code):
        self.code = code


class Report:
    def __init__(self):
        self.issues = Counter()
        self.counts = {table: None for table in TABLES}
        self.sequences = {}
        self.fingerprint = None
        self.uploads = {"checked": False}

    def add(self, severity, category, code, table=None, field=None, count=1):
        # All labels are internal constants; never pass source names or values.
        self.issues[(severity, category, code, table, field)] += count

    def error(self, category, code, table=None, field=None, count=1):
        self.add("blocking_error", category, code, table, field, count)

    def warn(self, category, code, table=None, field=None, count=1):
        self.add("warning", category, code, table, field, count)

    def result(self):
        issues = [{"severity": level, "category": category, "code": code,
                   **({"table": table} if table else {}),
                   **({"field": field} if field else {}), "count": count}
                  for (level, category, code, table, field), count in sorted(
                      self.issues.items(), key=lambda item: tuple(part or "" for part in item[0])) if count]
        return {"report_version": 1, "mode": "source_only_dry_run",
                "valid": not any(i["severity"] == "blocking_error" for i in issues),
                "migration_performed": False, "source_sha256": self.fingerprint,
                "tables": self.counts, "sequences": self.sequences,
                "uploads": self.uploads, "issues": issues,
                "summary": {level: sum(i["count"] for i in issues if i["severity"] == level)
                            for level in ("blocking_error", "warning")}}


def _record_cleanup_failure(report, code):
    report.fingerprint = None
    report.error("cleanup", code)


def _has_cleanup_failure(report):
    return any(category == "cleanup" and count
               for (_, category, _, _, _), count in report.issues.items())


def _annotate_cleanup(error, report):
    """Preserve a propagating exception; attach only fixed-code safe diagnostics."""
    safe = deepcopy(report.result())
    BaseException.__setattr__(error, "source_cleanup_report", safe)
    if isinstance(error, SourceSessionError):
        error._safe_report = deepcopy(safe)
    codes = sorted({issue["code"] for issue in safe["issues"] if issue["category"] == "cleanup"})
    note = "Habrivo source cleanup failed: " + ", ".join(codes)
    # Python 3.10 keeps the structured attribute; 3.11+ also shows a safe note in
    # ordinary tracebacks. Diagnostics contain no raw cleanup errors/paths/rows.
    if hasattr(BaseException, "add_note") and note not in getattr(error, "__notes__", ()):
        BaseException.add_note(error, note)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _explicit_path(value):
    path = Path(os.path.abspath(os.fspath(value)))
    # Refuse symlinks in any component; never resolve one to an unknown source.
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise UnsafeSnapshot("symlink_path_rejected")
    return path


def _reject_sidecars(path):
    if any(os.path.lexists(str(path) + suffix) for suffix in SIDECARS):
        raise UnsafeSnapshot("sqlite_sidecar_present")


def _digest(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


_LOCK_PLATFORM = os.name


def _acquire_source_lock(fd):
    """POSIX-only nonblocking shared SQLite lock; unsupported means rejection.

    No Windows fallback. This boundary can be mocked by portable resource tests,
    independently of the host's ability to import fcntl.
    """
    if _LOCK_PLATFORM != "posix":
        raise UnsafeSnapshot("read_lock_unavailable")
    try:
        import fcntl
        fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, 514, 0x40000000)
    except ImportError:
        raise UnsafeSnapshot("read_lock_unavailable") from None
    except OSError:
        raise UnsafeSnapshot("active_writer_or_read_lock_unavailable") from None


def _release_source_lock(fd):
    """Close the owned descriptor once, releasing its kernel locks.

    Deliberately no separate LOCK_UN: preserve the existing descriptor-close
    release and error behavior. The caller must attempt this even if acquisition
    or SQLite/cursor cleanup failed. Do not retry close after failure.
    """
    os.close(fd)


@contextmanager
def _snapshot(path_value, confirmed, report):
    if not confirmed:
        raise UnsafeSnapshot("offline_snapshot_confirmation_required")
    path = _explicit_path(path_value)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise UnsafeSnapshot("snapshot_missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UnsafeSnapshot("snapshot_must_be_detached_regular_file")
    _reject_sidecars(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    db = None
    original_error = None
    try:
        baseline = os.fstat(fd)
        if _signature(baseline) != _signature(info):
            raise UnsafeSnapshot("snapshot_changed")
        header = os.read(fd, 100)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
            raise UnsafeSnapshot("invalid_sqlite_header")
        if header[18:20] != b"\x01\x01":
            raise UnsafeSnapshot("wal_or_unsupported_snapshot_header")
        _acquire_source_lock(fd)
        before = _digest(fd)
        _reject_sidecars(path)
        # Immutable is safe ONLY after the detached/offline checks above. It
        # prevents SQLite from creating journal/shared-memory files. Never use
        # it as a way to ignore the WAL of an active database.
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        if _signature(path.lstat()) != _signature(baseline):
            raise UnsafeSnapshot("snapshot_changed")
        yield db
        try:
            def verify_final_source():
                # Compare both names for the source against the opening baseline.
                # Repeat after hashing: a matching digest alone cannot establish
                # that the path/descriptor still identify the captured file.
                _explicit_path(path)
                _reject_sidecars(path)
                if (_signature(os.fstat(fd)) != _signature(baseline)
                        or _signature(path.lstat()) != _signature(baseline)):
                    raise UnsafeSnapshot("snapshot_changed")

            verify_final_source()
            final_digest = _digest(fd)
            verify_final_source()
            if final_digest != before:
                raise UnsafeSnapshot("snapshot_changed")
        except (UnsafeSnapshot, OSError, ValueError, TypeError):
            raise
        except Exception:
            # Unexpected hashing/verification errors must not expose raw text.
            # Control-flow BaseExceptions still propagate through cleanup.
            raise UnsafeSnapshot("source_final_verification_failed") from None
        except BaseException:
            report.error("snapshot", "source_final_verification_interrupted")
            raise
        report.fingerprint = before
    except BaseException as error:
        original_error = error
        raise
    finally:
        # Each release is attempted once, even for unexpected close exceptions.
        # Do not retry a failed os.close(): descriptor reuse can make that unsafe.
        if db is not None:
            try:
                db.close()
            except BaseException:
                _record_cleanup_failure(report, "source_sqlite_close_failed")
        try:
            _release_source_lock(fd)
        except BaseException:
            _record_cleanup_failure(report, "source_descriptor_close_failed")
        if _has_cleanup_failure(report):
            report.fingerprint = None
            if original_error is not None:
                _annotate_cleanup(original_error, report)
            else:
                raise UnsafeSnapshot("source_cleanup_failed") from None


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _schema(db, report):
    rows = db.execute("SELECT name,type,sql FROM sqlite_schema").fetchall()
    tables = {row["name"]: row for row in rows if row["type"] == "table"}
    unexpected = sum(not name.startswith("sqlite_") and name not in TABLES for name in tables)
    if unexpected:
        report.error("schema", "unexpected_tables_require_review", count=unexpected)
    objects = sum(row["type"] in {"view", "trigger"} for row in rows)
    if objects:
        report.error("schema", "views_or_triggers_require_review", count=objects)
    usable = set()
    for table, expected in TABLES.items():
        if table not in tables:
            report.error("schema", "missing_table", table)
            continue
        columns = {r["name"]: r for r in db.execute("PRAGMA table_xinfo(" + _quote(table) + ")")}
        missing = set(expected) - columns.keys()
        extra = columns.keys() - expected.keys()
        if missing:
            report.error("schema", "historical_or_incomplete_columns", table, count=len(missing))
        if extra:
            report.error("schema", "unexpected_columns_require_review", table, count=len(extra))
        for field in expected.keys() & columns.keys():
            col = columns[field]
            if col["type"].upper() != expected[field] or col["hidden"]:
                report.error("schema", "declared_type_or_generated_column_differs", table, field)
            if field in REQUIRED[table] and not col["notnull"]:
                report.error("schema", "required_not_null_constraint_missing", table, field)
        defaults = {"created_at": "CURRENT_TIMESTAMP"}
        if table == "subscriptions":
            defaults.update(currency="'USD'", status="'PENDING'", payment_verified="0")
        for field, default in defaults.items():
            if field in columns:
                actual = columns[field]["dflt_value"]
                if actual is None or actual.strip("() ").upper() != default:
                    report.warn("schema", "column_default_differs_requires_review", table, field)
        primary = [name for name, col in columns.items() if col["pk"]]
        if primary != ["id"]:
            report.error("schema", "id_primary_key_differs", table)
        # SQL is metadata only and is never included in the report.
        sql = tables[table]["sql"] or ""
        if "AUTOINCREMENT" not in sql.upper():
            report.warn("sequences", "deleted_id_history_may_be_unavailable", table)
        if table == "users":
            unique_email = False
            for index in db.execute('PRAGMA index_list("users")'):
                if index["unique"] and not index["partial"]:
                    keys = [r for r in db.execute("PRAGMA index_xinfo(" + _quote(index["name"]) + ")") if r["key"]]
                    if len(keys) == 1 and keys[0]["name"] == "email":
                        unique_email = True
                        if keys[0]["coll"] != "BINARY":
                            report.error("schema", "email_uniqueness_collation_differs", table, "email")
            if not unique_email:
                report.error("schema", "email_unique_constraint_missing", table, "email")
        if table == "subscriptions":
            # Conservative recognition of the current schema, not a SQL parser.
            # Equivalent alternative CHECK syntax is flagged for manual review.
            compact = re.sub(r"\s+", "", sql).lower()
            for field, values in (("plan", ("starter", "pro", "premium")),
                                  ("billing_cycle", ("monthly", "quarterly", "half_yearly", "yearly"))):
                expected_check = "check(" + field + "in(" + ",".join("'"+v+"'" for v in values) + "))"
                if expected_check not in compact:
                    report.error("schema", "check_constraint_missing_or_requires_review", table, field)
        report.counts[table] = db.execute("SELECT COUNT(*) FROM " + _quote(table)).fetchone()[0]
        if not missing:
            usable.add(table)
    if all(count == 0 for count in report.counts.values()):
        report.warn("records", "empty_application_snapshot")
    return usable


def _positive_id(value):
    return type(value) is int and 0 < value <= MAX_ID


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return True
    except ValueError:
        return False


def _records(db, usable, report):
    ids = {}; subscriptions = {}; logos = set(); active = Counter()
    for table in usable:
        ids[table] = {r[0] for r in db.execute('SELECT id FROM '+_quote(table)) if _positive_id(r[0])}
    raw_emails = set(); normalized_emails = set(); references = set()
    for table in TABLES:
        if table not in usable:
            continue
        for row in db.execute("SELECT " + ",".join(_quote(f) for f in TABLES[table]) + " FROM " + _quote(table)):
            for field, kind in TABLES[table].items():
                value = row[field]
                if value is None:
                    if field == "id" or field in REQUIRED[table]:
                        report.error("records", "required_value_null", table, field)
                    continue
                if (kind == "INTEGER" and type(value) is not int) or (kind == "TEXT" and not isinstance(value, str)):
                    report.error("records", "stored_value_type_mismatch", table, field)
                if isinstance(value, str):
                    try:
                        value.encode("utf-8")
                        if "\x00" in value:
                            raise UnicodeError()
                    except UnicodeError:
                        report.error("records", "text_not_postgres_compatible", table, field)
                if field == "created_at" or field in {"start_date", "expiry_date"}:
                    if not _timestamp(value):
                        report.error("timestamps", "invalid_utc_text_timestamp", table, field)
            if not _positive_id(row["id"]):
                report.error("records", "invalid_positive_id", table, "id")
            if row["created_at"] is None and "created_at" not in REQUIRED[table]:
                report.warn("timestamps", "nullable_creation_timestamp_missing", table, "created_at")
            if table == "users":
                email = row["email"]
                if isinstance(email, str):
                    normalized = email.strip().lower()
                    if email in raw_emails:
                        report.error("records", "duplicate_email", table, "email")
                    if normalized in normalized_emails:
                        report.error("records", "normalized_email_collision", table, "email")
                    raw_emails.add(email); normalized_emails.add(normalized)
                    if email != normalized:
                        report.warn("records", "email_not_login_normalized", table, "email")
                    if len(email) > 254 or email.count("@") != 1 or any(c.isspace() for c in email) or email.startswith("@") or email.endswith("@"):
                        report.warn("records", "email_format_requires_review", table, "email")
                if not isinstance(row["name"], str) or not row["name"].strip() or len(row["name"]) > 100:
                    report.warn("records", "customer_name_requires_review", table, "name")
                password_hash = row["password_hash"]
                if not isinstance(password_hash, str) or not password_hash:
                    report.error("records", "password_hash_missing", table, "password_hash")
                elif not re.fullmatch(r"(?:pbkdf2:[a-z0-9]+:[0-9]+|scrypt:[0-9]+:[0-9]+:[0-9]+)\$[^$]+\$[0-9a-fA-F]+", password_hash):
                    report.warn("records", "password_hash_format_requires_review", table, "password_hash")
            if table in {"subscriptions", "promo_usage"}:
                customer = row["customer_id"]
                if not _positive_id(customer):
                    report.error("references", "invalid_customer_reference", table, "customer_id")
                elif "users" in ids and customer not in ids["users"]:
                    report.error("references", "orphan_customer_reference", table, "customer_id")
            if table == "subscriptions":
                if row["plan"] not in PLANS:
                    report.error("records", "invalid_plan", table, "plan")
                if row["billing_cycle"] not in CYCLES:
                    report.error("records", "invalid_billing_cycle", table, "billing_cycle")
                if row["status"] not in STATUSES:
                    report.warn("records", "status_requires_review", table, "status")
                if row["currency"] != "USD":
                    report.warn("records", "currency_requires_review", table, "currency")
                if type(row["amount_usd"]) is not int or row["amount_usd"] < 0:
                    report.error("records", "invalid_amount_integer", table, "amount_usd")
                if type(row["payment_verified"]) is not int or row["payment_verified"] not in (0, 1):
                    report.error("records", "invalid_payment_flag", table, "payment_verified")
                reference = row["payment_reference"]
                if isinstance(reference, str) and reference.strip():
                    if reference.strip() in references:
                        report.warn("records", "duplicate_payment_reference_requires_review", table, "payment_reference")
                    references.add(reference.strip())
                elif row["payment_verified"] == 1:
                    report.warn("records", "verified_payment_reference_missing", table, "payment_reference")
                if row["status"] == "active":
                    if _positive_id(row["customer_id"]):
                        active[row["customer_id"]] += 1
                    if not row["start_date"] or not row["expiry_date"] or row["payment_verified"] != 1:
                        report.warn("records", "active_subscription_entitlement_requires_review", table)
                if _timestamp(row["start_date"]) and _timestamp(row["expiry_date"]) and row["start_date"] >= row["expiry_date"]:
                    report.warn("timestamps", "subscription_date_order_requires_review", table)
                if _positive_id(row["id"]):
                    subscriptions[row["id"]] = row["customer_id"]
            if table == "promo_usage":
                subscription = row["subscription_id"]
                if not _positive_id(subscription):
                    report.error("references", "invalid_subscription_reference", table, "subscription_id")
                elif "subscriptions" in usable:
                    if subscription not in subscriptions:
                        report.error("references", "orphan_subscription_reference", table, "subscription_id")
                    elif subscriptions[subscription] != row["customer_id"]:
                        report.error("references", "usage_customer_subscription_mismatch", table)
            if table == "companies":
                if not isinstance(row["company"], str) or not row["company"].strip():
                    report.warn("records", "company_name_requires_review", table, "company")
                if isinstance(row["website"], str) and row["website"]:
                    try:
                        valid_website = urlsplit(row["website"]).scheme in {"http", "https"}
                    except ValueError:
                        valid_website = False
                    if not valid_website:
                        report.warn("records", "website_requires_review", table, "website")
                if isinstance(row["logo"], str) and row["logo"]:
                    logos.add(row["logo"])
    if any(count > 1 for count in active.values()):
        report.warn("records", "multiple_active_subscriptions_require_review", "subscriptions",
                    count=sum(count > 1 for count in active.values()))
    for table in usable:
        for field, parent in (("customer_id", "users"), ("subscription_id", "subscriptions")):
            if field in TABLES[table] and parent not in usable:
                report.warn("references", "reference_validation_incomplete", table, field)
    return ids, logos


def _sequences(db, ids, report):
    has_sequence = db.execute("SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence' AND type='table'").fetchone()
    entries = {}
    if has_sequence:
        for name, seq in db.execute("SELECT name,seq FROM sqlite_sequence"):
            if name not in TABLES:
                report.warn("sequences", "unexpected_sequence_entry_requires_review")
            elif name in entries or type(seq) is not int or seq < 0:
                report.error("sequences", "invalid_or_duplicate_sequence", name)
            else:
                entries[name] = seq
    for table, values in ids.items():
        maximum = max(values, default=0)
        recorded = entries.get(table)
        high = max(maximum, recorded or 0)
        if recorded is not None and recorded < maximum:
            report.warn("sequences", "sequence_below_existing_max_id", table)
        if high >= MAX_ID:
            report.error("sequences", "sequence_exhausted", table)
        report.sequences[table] = {"maximum_existing_id": maximum,
            "recorded_sqlite_high_water": recorded,
            "minimum_next_id": high + 1 if high < MAX_ID else None}


def _uploads(logo_refs, upload_value, report):
    names = set()
    for reference in logo_refs:
        if not re.fullmatch(r"/uploads/[A-Za-z0-9_][A-Za-z0-9_.-]*", reference):
            report.error("files", "unsafe_or_nonlocal_logo_reference", "companies", "logo")
        else:
            names.add(reference[len("/uploads/"):])
    if upload_value is None:
        if names:
            report.warn("files", "referenced_logos_not_checked", count=len(names))
        return
    directory = _explicit_path(upload_value)
    if not directory.is_dir():
        report.error("files", "upload_snapshot_missing_or_not_directory")
        return
    files = set(); total_bytes = 0
    for current, directories, filenames in os.walk(directory, followlinks=False):
        safe_dirs = []
        for name in directories:
            if (Path(current)/name).is_symlink():
                report.error("files", "upload_symlink_rejected")
            else:
                safe_dirs.append(name)
        directories[:] = safe_dirs
        for name in filenames:
            path = Path(current)/name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                report.error("files", "upload_not_detached_regular_file")
                continue
            relative = path.relative_to(directory).as_posix()
            files.add(relative); total_bytes += info.st_size
            if relative in names and info.st_size == 0:
                report.error("files", "referenced_logo_empty", "companies", "logo")
    report.uploads = {"checked": True, "regular_files": len(files), "total_bytes": total_bytes,
                      "unique_referenced_files": len(names), "missing_referenced_files": len(names-files),
                      "unreferenced_files": len(files-names)}
    if names-files:
        report.error("files", "referenced_logo_missing", "companies", "logo", count=len(names-files))
    if files-names:
        report.warn("files", "unreferenced_uploads_preserve_for_review", count=len(files-names))


def _validate_open_database(db, report, uploads):
    """One validation implementation shared by the CLI and guarded sessions."""
    integrity = db.execute("PRAGMA integrity_check(100)").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        report.error("integrity", "sqlite_integrity_check_failed")
    else:
        usable = _schema(db, report)
        ids, logos = _records(db, usable, report)
        _sequences(db, ids, report)
        _uploads(logos, uploads, report)


def _record_inspection_error(report, error):
    if isinstance(error, UnsafeSnapshot):
        report.error("snapshot", error.code)
        return error.code
    if isinstance(error, sqlite3.DatabaseError):
        report.error("integrity", "sqlite_read_or_schema_validation_failed")
        return "sqlite_read_or_schema_validation_failed"
    report.error("snapshot", "snapshot_or_upload_inspection_failed")
    return "snapshot_or_upload_inspection_failed"


def validate_snapshot(snapshot, *, confirmed_offline=False, uploads=None):
    """Return only aggregate metadata. Never log paths, rows, SQL errors or secrets."""
    report = Report()
    try:
        with _snapshot(snapshot, confirmed_offline, report) as db:
            _validate_open_database(db, report, uploads)
    except (UnsafeSnapshot, sqlite3.DatabaseError, OSError, ValueError, TypeError) as error:
        _record_inspection_error(report, error)
    return report.result()


# Freeze the record-reading contract independently of caller-supplied identifiers.
_READ_COLUMNS = MappingProxyType({table: tuple(columns) for table, columns in TABLES.items()})
_INSPECTION_ERRORS = (UnsafeSnapshot, sqlite3.DatabaseError, OSError, ValueError, TypeError)


class SourceSessionError(Exception):
    """Fixed error code plus a detached, privacy-safe aggregate report."""
    def __init__(self, code, report):
        super().__init__(code)
        self.code = code
        self._safe_report = deepcopy(report)

    @property
    def report(self):
        return deepcopy(self._safe_report)


class _RecordReader:
    def __init__(self, session, cursor):
        self._session = session
        self._cursor = cursor

    def __iter__(self):
        return self

    def __next__(self):
        self._session._require_open()
        if self._cursor is None:
            raise StopIteration
        try:
            row = self._cursor.fetchone()
            # Conversion is part of the guarded read, including custom row errors.
            if row is not None:
                return tuple(row)
        except BaseException as error:
            self._session._read_failed(error)
        # Only SQLite's explicit end-of-results sentinel is normal exhaustion.
        self._close()
        raise StopIteration

    def _close(self):
        if self._cursor is not None:
            cursor, self._cursor = self._cursor, None
            self._session._readers.discard(self)
            try:
                cursor.close()
            except BaseException as error:
                self._session._cleanup_failure("source_cursor_close_failed")
                if not isinstance(error, Exception):
                    # Exhaustion runs in the consumer's next() call: keep the
                    # original interrupt after permanently invalidating reads.
                    raise
                raise SourceSessionError("source_cursor_close_failed", self._session.report) from None


class _SourceSession:
    """Only the public methods/properties below form the supported interface."""
    def __init__(self, db, report):
        self._db = db
        self._report = report
        self._readers = set()
        self._open = True
        self._finalized = False
        self._failure_code = None

    @property
    def report(self):
        result = deepcopy(self._report.result())
        if not self._finalized:
            result["source_sha256"] = None
        return result

    @property
    def finalized(self):
        return self._finalized

    @property
    def fingerprint(self):
        if not self._finalized:
            raise SourceSessionError("source_not_finalized", self.report)
        return self._report.fingerprint

    def _failure(self, code):
        if self._open and self._failure_code is None:
            self._failure_code = code
            self._report.error("session", code)
        return SourceSessionError(code, self.report)

    def _require_open(self):
        if not self._open:
            raise SourceSessionError("source_session_not_open", self.report)
        if self._failure_code is not None:
            raise SourceSessionError("source_session_invalidated", self.report)

    def columns(self, table):
        """Fixed column tuple for one exact allowlisted table; no SQL inputs."""
        self._require_open()
        if type(table) is not str or table not in _READ_COLUMNS:
            raise self._failure("source_table_not_allowed") from None
        return _READ_COLUMNS[table]

    def iter_records(self, table):
        """Yield sensitive value tuples in columns(table) order, ordered by id.

        Readers are valid only inside this session. Do not log or serialize rows.
        """
        columns = self.columns(table)
        cursor = reader = None
        try:
            cursor = self._db.execute("SELECT " + ",".join(_quote(c) for c in columns)
                                      + " FROM " + _quote(table) + ' ORDER BY "id" ASC')
            reader = _RecordReader(self, cursor)
            self._readers.add(reader)
            return reader
        except BaseException as error:
            # Latch before allocating diagnostics or attempting cleanup. An
            # acquired cursor must also be released if reader creation fails.
            self._latch_read_failure()
            if cursor is not None:
                try:
                    if reader is not None:
                        reader._close()
                    else:
                        cursor.close()
                except BaseException:
                    if reader is None:
                        self._cleanup_failure("source_cursor_close_failed")
            self._read_failed(error)

    def _latch_read_failure(self):
        if self._failure_code is None:
            self._failure_code = "source_record_read_failed"
            self._report.fingerprint = None
            self._report.error("session", "source_record_read_failed")

    def _read_failed(self, error):
        """Never turn a failed read into exhaustion or a valid finalization."""
        self._latch_read_failure()
        if not isinstance(error, Exception):
            # Preserve control-flow exceptions (including custom BaseException
            # subclasses), with only safe cleanup diagnostics when applicable.
            if _has_cleanup_failure(self._report):
                _annotate_cleanup(error, self._report)
            raise error
        raise SourceSessionError("source_record_read_failed", self.report) from None

    def _cleanup_failure(self, code):
        _record_cleanup_failure(self._report, code)
        if self._failure_code is None:
            self._failure_code = "source_cleanup_failed"

    def _close_readers(self):
        try:
            for reader in tuple(self._readers):
                key = ("blocking_error", "cleanup", "source_cursor_close_failed", None, None)
                recorded = self._report.issues[key]
                try:
                    reader._close()
                except BaseException:
                    # Count a normal cursor-close failure only once. Also record
                    # unexpected reader failures without abandoning peer readers.
                    if self._report.issues[key] == recorded:
                        self._cleanup_failure("source_reader_close_failed")
                    if self._failure_code is None:
                        self._failure_code = "source_cleanup_failed"
        finally:
            self._readers.clear()
            self._open = False
            self._db = None


class _GuardedSourceContext:
    def __init__(self, snapshot, confirmed_offline, uploads):
        self._snapshot = snapshot
        self._confirmed = confirmed_offline
        self._uploads = uploads
        self._report = Report()
        self._entered = False
        self._manager = None
        self._session = None

    def _error(self, error):
        self._report.fingerprint = None
        if isinstance(error, SourceSessionError):
            return SourceSessionError(error.code, self._report.result())
        code = _record_inspection_error(self._report, error)
        return SourceSessionError(code, self._report.result())

    def __enter__(self):
        if self._entered:
            raise SourceSessionError("source_context_not_reusable", self._report.result())
        self._entered = True
        self._manager = _snapshot(self._snapshot, self._confirmed, self._report)
        opened = False
        try:
            db = self._manager.__enter__()
            opened = True
            _validate_open_database(db, self._report, self._uploads)
            if not self._report.result()["valid"]:
                raise SourceSessionError("source_validation_failed", self._report.result())
            self._session = _SourceSession(db, self._report)
            return self._session
        except BaseException as error:
            if opened:
                try:
                    self._manager.__exit__(type(error), error, error.__traceback__)
                except _INSPECTION_ERRORS as cleanup_error:
                    raise self._error(cleanup_error) from None
            if isinstance(error, (*_INSPECTION_ERRORS, SourceSessionError)):
                raise self._error(error) from None
            raise

    def __exit__(self, kind, value, traceback):
        session = self._session
        try:
            session._close_readers()
        except BaseException:
            # Last-resort containment: snapshot cleanup must still be attempted.
            session._open = False
            session._db = None
            session._cleanup_failure("source_reader_cleanup_failed")
        if kind is not None:
            self._report.fingerprint = None
            self._report.error("session", "source_consumption_aborted")
            try:
                self._manager.__exit__(kind, value, traceback)
            except BaseException:
                _record_cleanup_failure(self._report, "source_snapshot_context_cleanup_failed")
            if _has_cleanup_failure(self._report):
                _annotate_cleanup(value, self._report)
            return False  # Preserve the original consumer exception, with diagnostics.
        if session._failure_code is not None:
            error = SourceSessionError(session._failure_code, session.report)
            try:
                self._manager.__exit__(type(error), error, None)
            except BaseException:
                _record_cleanup_failure(self._report, "source_snapshot_context_cleanup_failed")
            # Refresh after low-level cleanup so simultaneous failures are all visible.
            raise SourceSessionError(error.code, session.report) from None
        try:
            self._manager.__exit__(None, None, None)
        except _INSPECTION_ERRORS as error:
            raise self._error(error) from None
        if _has_cleanup_failure(self._report):
            self._report.fingerprint = None
            raise SourceSessionError("source_cleanup_failed", session.report) from None
        session._finalized = True
        return False


def guarded_source_session(snapshot, *, confirmed_offline=False, uploads=None):
    """Public single-use context for validated, read-only offline source records.

    Initial blocking findings raise SourceSessionError before exposing a session.
    Inside the context use only columns(table), iter_records(table), report and
    finalized. No SQL, column selection, connection or cursor is public. Warnings
    remain visible and are not migration authorization. Value tuples are private
    data: callers must not log them. Readers close on any context exit, even when
    only partly consumed. A caught read fault invalidates the session.

    fingerprint/report['source_sha256'] is authoritative ONLY after successful
    context exit and finalized=True. Source-change/finalization failures raise;
    propagate them to any future target transaction rather than committing early.
    """
    return _GuardedSourceContext(snapshot, confirmed_offline, uploads)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Explicit existing detached SQLite snapshot")
    parser.add_argument("--confirm-offline-snapshot", action="store_true",
                        help="Attest this is a consistent offline capture, not an active service database")
    parser.add_argument("--uploads", help="Optional explicit separately captured offline upload directory")
    args = parser.parse_args(argv)
    report = validate_snapshot(args.snapshot, confirmed_offline=args.confirm_offline_snapshot, uploads=args.uploads)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
