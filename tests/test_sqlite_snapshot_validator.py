"""Synthetic, offline source snapshots only. Never import the SaaS application."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1]/'beckend'/'validate_sqlite_snapshot.py'
spec = importlib.util.spec_from_file_location('snapshot_validator', MODULE)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

# Deliberately synthetic schema/data; no production input or app initialization.
SCHEMA = '''
CREATE TABLE users (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
 email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE companies (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT, owner TEXT,
 whatsapp TEXT, email TEXT, category TEXT, logo TEXT, address TEXT,
 website TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE subscriptions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
 plan TEXT NOT NULL CHECK(plan IN ('starter','pro','premium')),
 billing_cycle TEXT NOT NULL CHECK(billing_cycle IN ('monthly','quarterly','half_yearly','yearly')),
 currency TEXT NOT NULL DEFAULT 'USD', amount_usd INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', payment_reference TEXT,
 start_date TEXT, expiry_date TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 payment_verified INTEGER NOT NULL DEFAULT 0);
CREATE INDEX idx_subscriptions_customer ON subscriptions(customer_id);
CREATE TABLE promo_usage (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
 subscription_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX idx_promo_usage_customer_date ON promo_usage(customer_id,created_at);
'''
HASH = 'pbkdf2:sha256:260000$private-fixture-salt$'+'a'*64


def fixture(path, populated=True, schema=SCHEMA):
    db = sqlite3.connect(path)
    try:
        db.executescript(schema)
        if populated:
            db.execute('INSERT INTO users (id,name,email,password_hash) VALUES (?,?,?,?)',
                       (7,'PRIVATE_CUSTOMER','private-customer@example.invalid',HASH))
            db.execute("INSERT INTO companies (id,company,logo,website) VALUES (11,'PRIVATE_COMPANY','/uploads/logo.png','https://example.invalid')")
            db.execute("""INSERT INTO subscriptions
                (id,customer_id,plan,billing_cycle,amount_usd,status,payment_reference,start_date,expiry_date,payment_verified)
                VALUES (13,7,'starter','monthly',39,'active','PRIVATE_PAYMENT_TOKEN','2026-01-01 00:00:00','2099-01-01 00:00:00',1)""")
            db.execute('INSERT INTO promo_usage (id,customer_id,subscription_id) VALUES (17,7,13)')
        db.commit()
    finally:
        db.close()


REAL_REJECT_SIDECARS = validator._reject_sidecars


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.source = self.directory/'snapshot.sqlite'
        self.uploads = self.directory/'uploads'
        self.uploads.mkdir()
        (self.uploads/'logo.png').write_bytes(b'synthetic-logo-bytes')

    def inventory(self):
        result = {}
        for p in self.directory.rglob('*'):
            info = p.lstat()
            # Reads can affect atime; content, mtime, mode and sidecar inventory
            # must be unchanged. No test pretends atime is under SQLite control.
            digest = None
            if p.is_file() and not p.is_symlink():
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            result[str(p.relative_to(self.directory))] = (info.st_size if not p.is_dir() else None,
                info.st_mtime_ns, info.st_mode, digest)
        return result

    def validate(self, source=None, **kwargs):
        before = self.inventory()
        report = validator.validate_snapshot(source or self.source, confirmed_offline=True, **kwargs)
        self.assertEqual(self.inventory(), before, 'Source snapshot content/metadata or sidecars changed')
        return report

    def codes(self, report):
        return {issue['code'] for issue in report['issues']}

    def update(self, sql, parameters=()):
        db = sqlite3.connect(self.source)
        try:
            db.execute(sql,parameters)
            db.commit()
        finally:
            db.close()

    def test_valid_snapshot_full_inventory_and_privacy(self):
        fixture(self.source)
        report = self.validate(uploads=self.uploads)
        self.assertTrue(report['valid'], report['issues'])
        self.assertEqual(report['tables'], dict(users=1,companies=1,subscriptions=1,promo_usage=1))
        self.assertEqual(report['summary']['blocking_error'], 0)
        self.assertEqual(report['uploads']['missing_referenced_files'], 0)
        self.assertEqual(report['sequences']['users']['minimum_next_id'],8)
        self.assertEqual(report['source_sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        output = json.dumps(report)
        for private in ('PRIVATE_CUSTOMER','private-customer@example.invalid',HASH,
                        'PRIVATE_PAYMENT_TOKEN','PRIVATE_COMPANY','logo.png',str(self.source)):
            self.assertNotIn(private,output)

    def test_existing_empty_schema_is_valid_but_warned(self):
        fixture(self.source, populated=False)
        report = self.validate()
        self.assertTrue(report['valid'])
        self.assertIn('empty_application_snapshot',self.codes(report))
        self.assertEqual(report['tables']['users'],0)
        self.assertEqual(report['sequences']['users']['minimum_next_id'],1)

    def test_missing_zero_length_and_corrupt_files_do_not_create_database(self):
        report = self.validate()
        self.assertIn('snapshot_missing',self.codes(report))
        self.assertFalse(self.source.exists())
        for data in (b'',b'not a SQLite snapshot',b'SQLite format 3\0'+b'\0'*200):
            self.source.write_bytes(data)
            report = self.validate()
            self.assertFalse(report['valid'])

    def test_integrity_failure_is_blocking_and_sanitized(self):
        fixture(self.source)
        # Valid header, deliberately damaged b-tree root page.
        data = bytearray(self.source.read_bytes())
        page_size = int.from_bytes(data[16:18],'big')
        data[page_size] = 255
        self.source.write_bytes(data)
        report = self.validate()
        self.assertFalse(report['valid'])
        self.assertTrue({'sqlite_read_or_schema_validation_failed','sqlite_integrity_check_failed'} & self.codes(report))
        self.assertNotIn('PRIVATE_', json.dumps(report))

    def test_explicit_path_and_offline_confirmation_required(self):
        fixture(self.source)
        before=self.inventory()
        result=validator.validate_snapshot(self.source)
        self.assertIn('offline_snapshot_confirmation_required',self.codes(result))
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            status=validator.main(['--snapshot',str(self.source)])
        self.assertEqual(status,1)
        self.assertEqual(self.inventory(),before)
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            validator.main([])

    def test_read_only_uri_and_no_application_or_target_imports(self):
        fixture(self.source)
        real = sqlite3.connect
        def read_only(source_uri, **kwargs):
            self.assertIn('mode=ro&immutable=1',source_uri)
            self.assertTrue(kwargs['uri'])
            return real(source_uri,**kwargs)
        with patch.object(validator.sqlite3,'connect',side_effect=read_only):
            self.assertTrue(self.validate()['valid'])
        # Static dependency boundary: no SaaS or PostgreSQL module import.
        import ast
        modules=[]
        for node in ast.walk(ast.parse(MODULE.read_text())):
            if isinstance(node,ast.Import):modules.extend(a.name for a in node.names)
            if isinstance(node,ast.ImportFrom):modules.append(node.module)
        self.assertFalse({'app','database','subscriptions_db','psycopg'} & set(modules))

    def test_connection_refuses_writes_and_sanitizes_sqlite_errors(self):
        fixture(self.source)
        before=self.inventory()
        with validator._snapshot(self.source,True,validator.Report()) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM users")
        self.assertEqual(self.inventory(),before)
        with patch.object(validator.sqlite3,'connect',side_effect=sqlite3.OperationalError('PRIVATE_ERROR_VALUE')):
            report=self.validate()
        self.assertFalse(report['valid'])
        self.assertNotIn('PRIVATE_ERROR_VALUE',json.dumps(report))

    def test_historical_missing_columns_and_unexpected_schema_are_blocking(self):
        old = SCHEMA.replace(' payment_verified INTEGER NOT NULL DEFAULT 0',' legacy_payment_flag INTEGER NOT NULL DEFAULT 0')
        fixture(self.source,populated=False,schema=old)
        self.update('CREATE TABLE "PRIVATE_UNEXPECTED_TABLE" (secret TEXT)')
        report=self.validate()
        self.assertIn('historical_or_incomplete_columns',self.codes(report))
        self.assertIn('unexpected_tables_require_review',self.codes(report))
        self.assertNotIn('PRIVATE_UNEXPECTED_TABLE',json.dumps(report))
        self.update('DROP TABLE promo_usage')
        self.assertIn('missing_table',self.codes(self.validate()))

    def test_incompatible_constraints_declared_and_stored_types(self):
        bad = SCHEMA.replace('email TEXT NOT NULL UNIQUE','email TEXT').replace('amount_usd INTEGER NOT NULL','amount_usd TEXT NOT NULL')
        fixture(self.source,schema=bad)
        report=self.validate()
        self.assertFalse(report['valid'])
        self.assertIn('email_unique_constraint_missing',self.codes(report))
        self.assertIn('required_not_null_constraint_missing',self.codes(report))
        self.assertIn('declared_type_or_generated_column_differs',self.codes(report))
        self.assertIn('stored_value_type_mismatch',self.codes(report))

    def test_duplicate_email_normalization_and_null_validation(self):
        schema=SCHEMA.replace('email TEXT NOT NULL UNIQUE','email TEXT')
        fixture(self.source,schema=schema)
        self.update('INSERT INTO users(name,email,password_hash) VALUES (?,?,?)',('Other','PRIVATE-CUSTOMER@EXAMPLE.INVALID',HASH))
        self.assertIn('normalized_email_collision',self.codes(self.validate()))
        self.update('INSERT INTO users(name,email,password_hash) VALUES (?,?,?)',('Other','private-customer@example.invalid',HASH))
        self.assertIn('duplicate_email',self.codes(self.validate()))
        self.update('INSERT INTO users(name,email,password_hash) VALUES (?,?,?)',('Other',None,HASH))
        self.assertIn('required_value_null',self.codes(self.validate()))

    def test_orphaned_and_cross_account_references(self):
        fixture(self.source)
        self.update('UPDATE subscriptions SET customer_id=999')
        self.update('UPDATE promo_usage SET customer_id=888')
        report=self.validate()
        self.assertIn('orphan_customer_reference',self.codes(report))
        self.assertIn('usage_customer_subscription_mismatch',self.codes(report))
        self.update('UPDATE promo_usage SET subscription_id=999')
        self.assertIn('orphan_subscription_reference',self.codes(self.validate()))

    def test_bad_ids_business_values_and_postgres_text(self):
        fixture(self.source)
        self.update('UPDATE users SET id=-7')
        self.update('UPDATE subscriptions SET amount_usd=-1,payment_verified=2,status=?',('PRIVATE_UNKNOWN_STATUS',))
        self.update('UPDATE companies SET company=?',('private\0text',))
        self.update('UPDATE users SET password_hash=?',('PRIVATE_UNRECOGNIZED_HASH',))
        report=self.validate()
        self.assertFalse(report['valid'])
        for code in ('invalid_positive_id','invalid_amount_integer','invalid_payment_flag',
                     'status_requires_review','text_not_postgres_compatible','password_hash_format_requires_review'):
            self.assertIn(code,self.codes(report))
        self.assertNotIn('PRIVATE_',json.dumps(report))

    def test_plan_cycle_checks_and_invalid_values(self):
        no_checks=SCHEMA.replace(" CHECK(plan IN ('starter','pro','premium'))",'').replace(" CHECK(billing_cycle IN ('monthly','quarterly','half_yearly','yearly'))",'')
        fixture(self.source,schema=no_checks)
        self.update("UPDATE subscriptions SET plan='invalid',billing_cycle='invalid'")
        report=self.validate()
        for code in ('check_constraint_missing_or_requires_review','invalid_plan','invalid_billing_cycle'):
            self.assertIn(code,self.codes(report))

    def test_timestamps_payment_references_and_multiple_subscriptions(self):
        fixture(self.source)
        self.update("UPDATE subscriptions SET start_date='2026-99-01 00:00:00',created_at='private timestamp'")
        self.update("""INSERT INTO subscriptions(customer_id,plan,billing_cycle,amount_usd,status,payment_reference)
                    VALUES (7,'pro','monthly',79,'active','PRIVATE_PAYMENT_TOKEN')""")
        report=self.validate()
        for code in ('invalid_utc_text_timestamp','duplicate_payment_reference_requires_review',
                     'multiple_active_subscriptions_require_review','active_subscription_entitlement_requires_review'):
            self.assertIn(code,self.codes(report))
        self.assertNotIn('private timestamp',json.dumps(report))

    def test_deleted_id_high_water_and_exhaustion(self):
        fixture(self.source)
        self.update('INSERT INTO users(id,name,email,password_hash) VALUES (?,?,?,?)',(500,'Deleted','deleted@example.invalid',HASH))
        self.update('DELETE FROM users WHERE id=500')
        sequence=self.validate()['sequences']['users']
        self.assertEqual(sequence,{'maximum_existing_id':7,'recorded_sqlite_high_water':500,'minimum_next_id':501})
        self.update("UPDATE sqlite_sequence SET seq=? WHERE name='users'",(validator.MAX_ID,))
        report=self.validate()
        self.assertIn('sequence_exhausted',self.codes(report))
        self.assertIsNone(report['sequences']['users']['minimum_next_id'])

    def test_optional_missing_partial_and_unsafe_uploads(self):
        fixture(self.source)
        self.assertIn('referenced_logos_not_checked',self.codes(self.validate()))
        (self.uploads/'logo.png').unlink()
        self.assertIn('referenced_logo_missing',self.codes(self.validate(uploads=self.uploads)))
        self.assertIn('upload_snapshot_missing_or_not_directory',self.codes(self.validate(uploads=self.directory/'absent')))
        self.update('UPDATE companies SET logo=?',('/uploads/../private-token',))
        report=self.validate(uploads=self.uploads)
        self.assertIn('unsafe_or_nonlocal_logo_reference',self.codes(report))
        self.assertNotIn('private-token',json.dumps(report))

    def test_unreferenced_files_are_preserved_and_empty_logo_rejected(self):
        fixture(self.source)
        (self.uploads/'extra-private-file').write_bytes(b'private bytes')
        (self.uploads/'logo.png').write_bytes(b'')
        report=self.validate(uploads=self.uploads)
        self.assertIn('unreferenced_uploads_preserve_for_review',self.codes(report))
        self.assertIn('referenced_logo_empty',self.codes(report))
        self.assertNotIn('extra-private-file',json.dumps(report))

    def test_symlinks_and_hardlinks_rejected_without_following(self):
        fixture(self.source)
        link=self.directory/'linked.sqlite'
        link.symlink_to(self.source)
        self.assertIn('symlink_path_rejected',self.codes(self.validate(source=link)))
        link.unlink()
        os.link(self.source,link)
        self.assertIn('snapshot_must_be_detached_regular_file',self.codes(self.validate()))
        link.unlink()
        (self.uploads/'link').symlink_to(self.source)
        self.assertIn('upload_not_detached_regular_file',self.codes(self.validate(uploads=self.uploads)))

    def test_all_sidecars_are_rejected_even_empty_and_unchanged(self):
        fixture(self.source)
        for suffix in validator.SIDECARS:
            sidecar=Path(str(self.source)+suffix)
            sidecar.write_bytes(b'')
            report=self.validate()
            self.assertIn('sqlite_sidecar_present',self.codes(report))
            sidecar.unlink()

    def test_wal_database_and_wal_header_without_sidecars_rejected(self):
        fixture(self.source)
        db=sqlite3.connect(self.source)
        try:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
            db.execute('INSERT INTO users(name,email,password_hash) VALUES (?,?,?)',('WAL','wal@example.invalid',HASH))
            db.commit()
            report=self.validate()
            self.assertIn('sqlite_sidecar_present',self.codes(report))
        finally:
            db.close()
        self.assertFalse(Path(str(self.source)+'-wal').exists())
        report=self.validate()
        self.assertIn('wal_or_unsupported_snapshot_header',self.codes(report))

    @unittest.skipUnless(os.name == 'posix', 'POSIX kernel-lock integration; native Windows snapshots unsupported')
    def test_external_sqlite_reserved_lock_rejected(self):
        fixture(self.source)
        # A reserved lock without journal writes proves lock detection separately
        # from the sidecar checks. Use another process: POSIX locks are per process.
        child=subprocess.Popen([sys.executable,'-B','-c',
            "import fcntl,sys; f=open(sys.argv[1],'rb+'); fcntl.lockf(f,fcntl.LOCK_EX,1,0x40000001); print('locked',flush=True); sys.stdin.read()",
            str(self.source)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'locked')
            self.assertIn('active_writer_or_read_lock_unavailable',self.codes(self.validate()))
        finally:
            child.communicate(input='',timeout=5)

    def test_source_change_during_validation_is_detected(self):
        fixture(self.source)
        original=validator._schema
        def replaced(db,report):
            result=original(db,report)
            info=self.source.stat()
            os.utime(self.source,ns=(info.st_atime_ns,info.st_mtime_ns+1000000))
            return result
        # Simulate a non-cooperating external change; the validator must not
        # repair it or claim a stable snapshot fingerprint.
        with patch.object(validator,'_schema',side_effect=replaced):
            report=validator.validate_snapshot(self.source,confirmed_offline=True)
        self.assertIn('snapshot_changed',self.codes(report))
        self.assertIsNone(report['source_sha256'])

    def test_cli_json_no_private_values_or_path_echo_and_exit_status(self):
        fixture(self.source)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            status=validator.main(['--snapshot',str(self.source),'--confirm-offline-snapshot','--uploads',str(self.uploads)])
        self.assertEqual(status,0)
        self.assertTrue(json.loads(output.getvalue())['valid'])
        self.assertNotIn(str(self.directory),output.getvalue())
        self.assertNotIn('PRIVATE_',output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(validator.main(['--snapshot',str(self.directory/'missing'),'--confirm-offline-snapshot']),1)


    def test_guarded_session_validates_before_reads_and_finalizes(self):
        fixture(self.source)
        self.update('INSERT INTO users(id,name,email,password_hash) VALUES (?,?,?,?)',
                    (3,'Earlier','earlier@example.invalid',HASH))
        before=self.inventory()
        context=validator.guarded_source_session(self.source,confirmed_offline=True,uploads=self.uploads)
        with context as source:
            self.assertFalse(source.finalized)
            self.assertIsNone(source.report['source_sha256'])
            with self.assertRaises(validator.SourceSessionError) as early:
                _=source.fingerprint
            self.assertEqual(early.exception.code,'source_not_finalized')
            for table, columns in validator.TABLES.items():
                self.assertEqual(source.columns(table),tuple(columns))
                rows=list(source.iter_records(table))
                self.assertTrue(all(type(row) is tuple and len(row)==len(columns) for row in rows))
                self.assertEqual([row[0] for row in rows],sorted(row[0] for row in rows))
            users=list(source.iter_records('users'))
            self.assertEqual([row[0] for row in users],[3,7])
            self.assertEqual(users[-1][source.columns('users').index('password_hash')],HASH)
            detached=source.report
            detached['tables']['users']=999
            self.assertEqual(source.report['tables']['users'],2)
        self.assertTrue(source.finalized)
        self.assertEqual(source.fingerprint,hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertIsNone(detached['source_sha256'])
        self.assertEqual(source.report,validator.validate_snapshot(self.source,confirmed_offline=True,uploads=self.uploads))
        self.assertEqual(self.inventory(),before)
        with self.assertRaises(validator.SourceSessionError):
            with context:
                self.fail('A session context must not be reusable')

    def test_guarded_session_missing_invalid_and_unconfirmed_never_yield(self):
        for data in (None,b'',b'PRIVATE_CORRUPT_BYTES'):
            if data is not None:
                self.source.write_bytes(data)
            before=self.inventory()
            with self.assertRaises(validator.SourceSessionError) as error:
                with validator.guarded_source_session(self.source,confirmed_offline=True):
                    self.fail('Invalid source exposed a session')
            self.assertFalse(error.exception.report['valid'])
            self.assertNotIn(str(self.directory),str(error.exception))
            self.assertNotIn('PRIVATE_',json.dumps(error.exception.report))
            self.assertEqual(self.inventory(),before)
        self.source.unlink()
        fixture(self.source)
        with self.assertRaises(validator.SourceSessionError) as error:
            with validator.guarded_source_session(self.source):
                self.fail('Unconfirmed source exposed a session')
        self.assertEqual(error.exception.code,'offline_snapshot_confirmation_required')

    def test_guarded_session_blocks_schema_references_and_missing_uploads(self):
        fixture(self.source)
        for prepare in (
            lambda:self.update('UPDATE promo_usage SET customer_id=99'),
            lambda:self.update('ALTER TABLE users ADD COLUMN extra TEXT'),
        ):
            prepare()
            before=self.inventory()
            with self.assertRaises(validator.SourceSessionError) as error:
                with validator.guarded_source_session(self.source,confirmed_offline=True):
                    self.fail('Blocking validation findings exposed records')
            self.assertEqual(error.exception.code,'source_validation_failed')
            self.assertIsNone(error.exception.report['source_sha256'])
            self.assertEqual(self.inventory(),before)
        self.source.unlink()
        fixture(self.source)
        (self.uploads/'logo.png').unlink()
        with self.assertRaises(validator.SourceSessionError) as error:
            with validator.guarded_source_session(self.source,confirmed_offline=True,uploads=self.uploads):
                self.fail('Missing supplied upload snapshot was accepted')
        self.assertIn('referenced_logo_missing',self.codes(error.exception.report))

    def test_guarded_session_reuses_wal_sidecar_and_symlink_rejection(self):
        fixture(self.source)
        for suffix in validator.SIDECARS:
            sidecar=Path(str(self.source)+suffix)
            sidecar.write_bytes(b'')
            before=self.inventory()
            with self.assertRaises(validator.SourceSessionError) as error:
                with validator.guarded_source_session(self.source,confirmed_offline=True):
                    self.fail('Sidecar-dependent source exposed records')
            self.assertEqual(error.exception.code,'sqlite_sidecar_present')
            self.assertEqual(self.inventory(),before)
            sidecar.unlink()
        linked=self.directory/'link.sqlite'
        linked.symlink_to(self.source)
        with self.assertRaises(validator.SourceSessionError) as error:
            with validator.guarded_source_session(linked,confirmed_offline=True):
                self.fail('Symlink source exposed records')
        self.assertEqual(error.exception.code,'symlink_path_rejected')
        linked.unlink()
        db=sqlite3.connect(self.source)
        try:
            db.execute('PRAGMA journal_mode=WAL')
        finally:
            db.close()
        with self.assertRaises(validator.SourceSessionError) as error:
            with validator.guarded_source_session(self.source,confirmed_offline=True):
                self.fail('WAL header exposed records')
        self.assertEqual(error.exception.code,'wal_or_unsupported_snapshot_header')

    def test_guarded_session_fixed_allowlist_and_caught_errors_invalidate(self):
        fixture(self.source)
        before=self.inventory()
        for table in ('sqlite_sequence','users; PRIVATE_SQL_TOKEN',None,['users']):
            with self.assertRaises(validator.SourceSessionError) as final_error:
                with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
                    with self.assertRaises(validator.SourceSessionError) as inner_error:
                        source.iter_records(table)
                    self.assertEqual(inner_error.exception.code,'source_table_not_allowed')
                    with self.assertRaises(validator.SourceSessionError):
                        source.iter_records('users')
            self.assertFalse(source.finalized)
            self.assertEqual(final_error.exception.code,'source_table_not_allowed')
            self.assertNotIn('PRIVATE_SQL_TOKEN',str(final_error.exception))
            self.assertNotIn('PRIVATE_SQL_TOKEN',json.dumps(final_error.exception.report))
            with self.assertRaises(validator.SourceSessionError):
                _=source.fingerprint
        self.assertEqual(self.inventory(),before)

    def test_guarded_session_partial_and_unstarted_readers_close_on_early_exit(self):
        fixture(self.source)
        before=self.inventory()
        with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
            reader=source.iter_records('users')
            unstarted=source.iter_records('companies')
            self.assertEqual(next(reader)[0],7)
            self.assertFalse(hasattr(reader,'execute'))
            self.assertFalse(hasattr(source,'execute'))
        self.assertTrue(source.finalized)
        for iterator in (reader,unstarted):
            with self.assertRaises(validator.SourceSessionError) as error:
                next(iterator)
            self.assertEqual(error.exception.code,'source_session_not_open')
        with self.assertRaises(validator.SourceSessionError):
            source.iter_records('users')
        self.assertEqual(self.inventory(),before)

    def test_guarded_session_propagates_consumer_exception_without_success(self):
        fixture(self.source)
        before=self.inventory()
        expected=RuntimeError('synthetic consumer abort')
        with self.assertRaises(RuntimeError) as error:
            with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
                next(source.iter_records('users'))
                raise expected
        self.assertIs(error.exception,expected)
        self.assertFalse(source.finalized)
        self.assertIsNone(source.report['source_sha256'])
        self.assertIn('source_consumption_aborted',self.codes(source.report))
        self.assertEqual(self.inventory(),before)

    def test_guarded_session_replacement_during_consumption_fails(self):
        fixture(self.source)
        replacement=self.directory/'replacement.sqlite'
        fixture(replacement,populated=False)
        expected=replacement.read_bytes()
        with self.assertRaises(validator.SourceSessionError) as error:
            with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
                next(source.iter_records('users'))
                os.replace(replacement,self.source)  # Simulated external replacement.
        self.assertEqual(error.exception.code,'snapshot_changed')
        self.assertFalse(source.finalized)
        self.assertIsNone(source.report['source_sha256'])
        self.assertEqual(self.source.read_bytes(),expected)  # No repair by validator.

    def test_guarded_session_modification_during_consumption_fails(self):
        fixture(self.source)
        with self.assertRaises(validator.SourceSessionError) as error:
            with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
                next(source.iter_records('users'))
                info=self.source.stat()
                os.utime(self.source,ns=(info.st_atime_ns,info.st_mtime_ns+1000000))
                expected=self.source.stat().st_mtime_ns
        self.assertEqual(error.exception.code,'snapshot_changed')
        self.assertFalse(source.finalized)
        self.assertEqual(self.source.stat().st_mtime_ns,expected)
        self.assertIsNone(error.exception.report['source_sha256'])

    def test_guarded_session_content_change_and_new_sidecars_fail(self):
        fixture(self.source)
        original=self.source.read_bytes()
        for action in ('content','sidecar'):
            with self.subTest(action=action),self.assertRaises(validator.SourceSessionError):
                with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
                    if action=='content':
                        # Non-cooperating synthetic writer, never the validator.
                        with self.source.open('r+b') as writer:
                            writer.seek(72)
                            writer.write(b'\x01')
                    else:
                        Path(str(self.source)+'-journal').write_bytes(b'')
            self.assertFalse(source.finalized)
            self.assertIsNone(source.report['source_sha256'])
            self.assertEqual(self.source.read_bytes(),original[:72]+b'\x01'+original[73:] if action=='content' else original)
            if action=='content':
                self.source.write_bytes(original)  # Reset only synthetic test fixture.
            else:
                Path(str(self.source)+'-journal').unlink()

    def test_guarded_session_read_failure_cannot_be_caught_into_success(self):
        from unittest.mock import MagicMock
        fixture(self.source)
        before=self.inventory()
        with self.assertRaises(validator.SourceSessionError) as final_error:
            with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
                fake=MagicMock()
                fake.execute.return_value.fetchone.side_effect=sqlite3.OperationalError('PRIVATE_DATABASE_ERROR')
                with patch.object(source,'_db',fake):
                    reader=source.iter_records('users')
                    with self.assertRaises(validator.SourceSessionError) as inner:
                        next(reader)
                    self.assertEqual(inner.exception.code,'source_record_read_failed')
        self.assertEqual(final_error.exception.code,'source_record_read_failed')
        self.assertNotIn('PRIVATE_DATABASE_ERROR',str(final_error.exception))
        self.assertNotIn('PRIVATE_DATABASE_ERROR',json.dumps(final_error.exception.report))
        self.assertFalse(source.finalized)
        self.assertEqual(self.inventory(),before)

    def test_guarded_session_finalization_io_error_is_safe_and_authority_denied(self):
        fixture(self.source)
        before=self.inventory()
        real_digest=validator._digest
        calls=0
        def failing_digest(fd):
            nonlocal calls
            calls+=1
            if calls>1:
                raise OSError('PRIVATE_PATH_OR_CREDENTIAL')
            return real_digest(fd)
        with patch.object(validator,'_digest',side_effect=failing_digest):
            with self.assertRaises(validator.SourceSessionError) as error:
                with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
                    list(source.iter_records('users'))
        self.assertFalse(source.finalized)
        self.assertIsNone(source.report['source_sha256'])
        self.assertNotIn('PRIVATE_PATH_OR_CREDENTIAL',str(error.exception))
        self.assertNotIn('PRIVATE_PATH_OR_CREDENTIAL',json.dumps(error.exception.report))
        self.assertEqual(self.inventory(),before)

    @unittest.skipUnless(os.name == 'posix', 'POSIX kernel-lock integration; native Windows snapshots unsupported')
    def test_guarded_session_holds_writer_lock_during_record_consumption(self):
        fixture(self.source)
        before=self.inventory()
        with validator.guarded_source_session(self.source,confirmed_offline=True) as source:
            next(source.iter_records('users'))
            script=("import fcntl,sys; f=open(sys.argv[1],'rb+');\n"
                    "try: fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB,1,0x40000001)\n"
                    "except BlockingIOError: print('blocked')\n"
                    "else: print('available')")
            result=subprocess.run([sys.executable,'-B','-c',script,str(self.source)],
                                  capture_output=True,text=True,timeout=5,check=True)
            self.assertEqual(result.stdout.strip(),'blocked')
        released = subprocess.run([sys.executable,'-B','-c',script,str(self.source)],
                                  capture_output=True,text=True,timeout=5,check=True)
        self.assertEqual(released.stdout.strip(), 'available')
        self.assertTrue(source.finalized)
        self.assertEqual(self.inventory(),before)


class CleanupTests(unittest.TestCase):
    """All resources are in-memory mocks: no DB connection or file opens."""
    @contextlib.contextmanager
    def resources(self, failures=()):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        calls=[]
        info=SimpleNamespace(st_dev=1,st_ino=2,st_size=4096,st_mtime_ns=1,
                             st_ctime_ns=1,st_mode=0o100600,st_nlink=1)
        path=MagicMock()
        path.lstat.return_value=info
        path.as_uri.return_value='file:///PRIVATE_SNAPSHOT_PATH'
        header=bytearray(100)
        header[:16]=b'SQLite format 3\0'
        header[18:20]=b'\1\1'
        db=MagicMock()
        cursors=[MagicMock(),MagicMock()]
        def release(label):
            def attempt():
                calls.append(label)
                if label in failures:
                    # Unexpected RuntimeError deliberately falls outside the old
                    # cleanup handler. Its private message must never escape.
                    raise RuntimeError('PRIVATE_CLOSE_VALUE_'+label)
            return attempt
        for index,cursor in enumerate(cursors):
            cursor.close.side_effect=release('cursor'+str(index))
            cursor.fetchone.return_value=(7,)
        db.close.side_effect=release('sqlite')
        fd_close=MagicMock(side_effect=lambda fd:release('descriptor')())
        cursor_index=0
        def execute(query):
            nonlocal cursor_index
            if query.startswith('SELECT'):
                cursor=cursors[cursor_index]
                cursor_index+=1
                return cursor
            return MagicMock()  # Connection-local read-only PRAGMAs.
        db.execute.side_effect=execute
        with contextlib.ExitStack() as stack:
            for target,options in (
                ('_require_supported_lock_platform',{'return_value':None}),
                ('_explicit_path',{'return_value':path}),
                ('_reject_sidecars',{'return_value':None}),
                ('_digest',{'return_value':'a'*64}),
                ('_validate_open_database',{'return_value':None}),
            ):
                stack.enter_context(patch.object(validator,target,**options))
            stack.enter_context(patch.object(validator.os,'open',return_value=12345))
            stack.enter_context(patch.object(validator.os,'fstat',return_value=info))
            stack.enter_context(patch.object(validator.os,'read',return_value=bytes(header)))
            stack.enter_context(patch.object(validator.os,'close',fd_close))
            stack.enter_context(patch.object(validator.sqlite3,'connect',return_value=db))
            stack.enter_context(patch.object(validator, '_acquire_source_lock'))
            yield SimpleNamespace(db=db,cursors=cursors,fd_close=fd_close,calls=calls,
                                  path=path,info=info)

    def test_lock_denial_closes_descriptor_without_opening_sqlite(self):
        cases = [(code, failures) for code in ('read_lock_unavailable', 'active_writer_or_read_lock_unavailable')
                 for failures in ((), ('descriptor',))]
        for code, failures in cases:
            with self.subTest(code=code, failures=failures), self.resources(failures) as mocks, patch.object(
                    validator, '_acquire_source_lock', side_effect=validator.UnsafeSnapshot(code)):
                with self.assertRaises(validator.SourceSessionError) as caught:
                    with validator.guarded_source_session('PRIVATE_PATH', confirmed_offline=True):
                        self.fail('Denied source exposed a session')
                self.assertEqual(caught.exception.code, code)
                self.assertIsNone(caught.exception.report['source_sha256'])
                validator.sqlite3.connect.assert_not_called()
                mocks.db.close.assert_not_called()
                mocks.fd_close.assert_called_once_with(12345)
                self.assertNotIn('PRIVATE_', str(caught.exception))
                if failures:
                    self.assertIn('source_descriptor_close_failed', {i['code'] for i in caught.exception.report['issues']})

    def test_unsupported_platform_denial_is_not_bypassed_by_portable_mocks(self):
        require = validator._require_supported_lock_platform
        with self.resources() as mocks, patch.object(validator, '_LOCK_PLATFORM', 'nt'), patch.object(
                validator, '_require_supported_lock_platform', side_effect=require):
            with self.assertRaises(validator.SourceSessionError) as caught:
                with validator.guarded_source_session('PRIVATE_PATH', confirmed_offline=True):
                    self.fail('Unsupported platform exposed records')
            self.assertEqual(caught.exception.code, 'read_lock_unavailable')
            self.assertIsNone(caught.exception.report['source_sha256'])
            validator.sqlite3.connect.assert_not_called()
            validator._explicit_path.assert_not_called()
            validator.os.open.assert_not_called()
            validator._digest.assert_not_called()
            mocks.fd_close.assert_not_called()

    def test_posix_initial_ctime_only_mismatch_denies_before_locking(self):
        from types import SimpleNamespace
        with self.resources() as mocks, patch.object(validator, '_LOCK_PLATFORM', 'posix'):
            changed = SimpleNamespace(**{**vars(mocks.info), 'st_ctime_ns':2})
            validator.os.fstat.return_value = changed
            with self.assertRaises(validator.SourceSessionError) as caught:
                with validator.guarded_source_session('PRIVATE_PATH', confirmed_offline=True):
                    self.fail('Changed source exposed records')
            self.assertEqual(caught.exception.code, 'snapshot_changed')
            self.assertIsNone(caught.exception.report['source_sha256'])
            validator._acquire_source_lock.assert_not_called()
            validator._digest.assert_not_called()
            validator.sqlite3.connect.assert_not_called()
            mocks.fd_close.assert_called_once_with(12345)

    def open_readers(self,source):
        return [source.iter_records('users'),source.iter_records('companies')]

    def assert_releases(self,resources):
        for cursor in resources.cursors:
            cursor.close.assert_called_once_with()
        resources.db.close.assert_called_once_with()
        resources.fd_close.assert_called_once_with(12345)
        self.assertEqual(set(resources.calls),{'cursor0','cursor1','sqlite','descriptor'})
        self.assertEqual(resources.calls[-2:],['sqlite','descriptor'])

    def cleanup_codes(self,report):
        return {issue['code']:issue['count'] for issue in report['issues'] if issue['category']=='cleanup'}

    def assert_denied(self,source,report):
        self.assertFalse(source.finalized)
        self.assertFalse(report['valid'])
        self.assertIsNone(source.report['source_sha256'])
        self.assertIsNone(report['source_sha256'])
        with self.assertRaises(validator.SourceSessionError):
            _=source.fingerprint
        self.assertNotIn('PRIVATE_',json.dumps(report))

    def test_each_cleanup_failure_attempts_every_release_once(self):
        cases=[('cursor0','source_cursor_close_failed'),
               ('sqlite','source_sqlite_close_failed'),
               ('descriptor','source_descriptor_close_failed')]
        for resource,code in cases:
            with self.subTest(resource=resource),self.resources({resource}) as mocks:
                with self.assertRaises(validator.SourceSessionError) as error:
                    with validator.guarded_source_session('PRIVATE_SNAPSHOT_PATH',confirmed_offline=True) as source:
                        readers=self.open_readers(source)
                self.assert_releases(mocks)
                self.assert_denied(source,error.exception.report)
                self.assertEqual(self.cleanup_codes(error.exception.report),{code:1})
                self.assertNotIn('PRIVATE_',str(error.exception))
                for reader in readers:
                    with self.assertRaises(validator.SourceSessionError):next(reader)
                self.assert_releases(mocks)  # No second release on later misuse.

    def test_simultaneous_failures_are_all_recorded_once(self):
        with self.resources({'cursor0','cursor1','sqlite','descriptor'}) as mocks:
            with self.assertRaises(validator.SourceSessionError) as error:
                with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                    self.open_readers(source)
            self.assert_releases(mocks)
            self.assert_denied(source,error.exception.report)
            self.assertEqual(self.cleanup_codes(error.exception.report),{
                'source_cursor_close_failed':2,'source_sqlite_close_failed':1,
                'source_descriptor_close_failed':1})

    def test_consumer_exception_preserved_and_cleanup_diagnostics_observable(self):
        for primary in (RuntimeError('synthetic consumer exception'),KeyboardInterrupt(),SystemExit(3)):
            with self.subTest(primary=type(primary).__name__),self.resources({'cursor0','cursor1','sqlite','descriptor'}) as mocks:
                with self.assertRaises(type(primary)) as caught:
                    with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                        self.open_readers(source)
                        raise primary
                self.assertIs(caught.exception,primary)
                self.assert_releases(mocks)
                self.assert_denied(source,primary.source_cleanup_report)
                self.assertEqual(self.cleanup_codes(primary.source_cleanup_report),{
                    'source_cursor_close_failed':2,'source_sqlite_close_failed':1,
                    'source_descriptor_close_failed':1})
                self.assertNotIn('PRIVATE_',str(primary))
                notes=getattr(primary,'__notes__',[])
                if hasattr(BaseException,'add_note'):
                    self.assertTrue(any('source_descriptor_close_failed' in note for note in notes))
                self.assertNotIn('PRIVATE_',json.dumps(notes))

    def test_close_at_iterator_exhaustion_latches_and_is_not_retried(self):
        with self.resources({'cursor0'}) as mocks:
            mocks.cursors[0].fetchone.return_value=None
            with self.assertRaises(validator.SourceSessionError) as final_error:
                with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                    reader,other=self.open_readers(source)
                    with self.assertRaises(validator.SourceSessionError) as inner:
                        next(reader)
                    self.assertEqual(inner.exception.code,'source_cursor_close_failed')
            self.assert_releases(mocks)
            self.assert_denied(source,final_error.exception.report)
            self.assertEqual(self.cleanup_codes(final_error.exception.report),{'source_cursor_close_failed':1})

    def test_exhaustion_close_interrupt_identity_invalidation_and_cleanup(self):
        class UnusualInterrupt(BaseException):
            pass
        for kind in (KeyboardInterrupt,SystemExit,GeneratorExit,UnusualInterrupt):
            for caught_inside in (False,True):
                for peer_failures in (False,True):
                    with self.subTest(kind=kind.__name__,caught_inside=caught_inside,
                                      peer_failures=peer_failures),self.resources(
                            {'cursor1','sqlite','descriptor'} if peer_failures else ()) as mocks:
                        original=kind('PRIVATE_CLOSE_INTERRUPT')
                        mocks.cursors[0].fetchone.return_value=None
                        def close():
                            mocks.calls.append('cursor0')
                            raise original
                        mocks.cursors[0].close.side_effect=close
                        expected=validator.SourceSessionError if caught_inside else kind
                        with self.assertRaises(expected) as final:
                            with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                                reader,other=self.open_readers(source)
                                if caught_inside:
                                    with self.assertRaises(kind) as caught:next(reader)
                                    self.assertIs(caught.exception,original)
                                    self.assert_denied(source,source.report)
                                    with self.assertRaises(validator.SourceSessionError):next(other)
                                else:next(reader)
                        if not caught_inside:
                            self.assertIs(final.exception,original)
                            self.assertNotIn('PRIVATE_',json.dumps(original.source_cleanup_report))
                        else:self.assertNotIn('PRIVATE_',str(final.exception))
                        self.assert_releases(mocks)
                        self.assert_denied(source,source.report)
                        codes={'source_cursor_close_failed':2 if peer_failures else 1}
                        if peer_failures:
                            codes.update(source_sqlite_close_failed=1,source_descriptor_close_failed=1)
                        self.assertEqual(self.cleanup_codes(source.report),codes)

    def test_consumer_exception_after_caught_exhaustion_interrupt_is_preserved(self):
        with self.resources({'cursor1','sqlite','descriptor'}) as mocks:
            interrupt=KeyboardInterrupt()
            consumer=RuntimeError('synthetic consumer failure')
            mocks.cursors[0].fetchone.return_value=None
            def close():
                mocks.calls.append('cursor0')
                raise interrupt
            mocks.cursors[0].close.side_effect=close
            with self.assertRaises(RuntimeError) as final:
                with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                    reader,other=self.open_readers(source)
                    with self.assertRaises(KeyboardInterrupt) as caught:next(reader)
                    self.assertIs(caught.exception,interrupt)
                    raise consumer
            self.assertIs(final.exception,consumer)
            self.assert_releases(mocks)
            self.assert_denied(source,consumer.source_cleanup_report)

    def test_reader_creation_registration_interrupt_identity_and_cleanup(self):
        class UnusualInterrupt(BaseException):
            pass
        for stage in ('creation','registration'):
            for kind in (KeyboardInterrupt,SystemExit,GeneratorExit,UnusualInterrupt):
                for caught_inside in (False,True):
                    with self.subTest(stage=stage,kind=kind.__name__,caught_inside=caught_inside), \
                            self.resources({'cursor1','sqlite','descriptor'}) as mocks:
                        original=kind('PRIVATE_CONSTRUCTION_INTERRUPT')
                        class BrokenRegistry(set):
                            def add(self,reader):
                                super().add(reader)
                                raise original
                        expected=validator.SourceSessionError if caught_inside else kind
                        with self.assertRaises(expected) as final:
                            with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                                source.iter_records('users')
                                with contextlib.ExitStack() as patches:
                                    if stage=='creation':
                                        patches.enter_context(patch.object(validator,'_RecordReader',side_effect=original))
                                    else:source._readers=BrokenRegistry(source._readers)
                                    if caught_inside:
                                        with self.assertRaises(kind) as caught:source.iter_records('companies')
                                        self.assertIs(caught.exception,original)
                                        self.assert_denied(source,source.report)
                                    else:source.iter_records('companies')
                        if not caught_inside:self.assertIs(final.exception,original)
                        self.assert_releases(mocks)
                        self.assert_denied(source,source.report)
                        self.assertEqual(self.cleanup_codes(source.report),{
                            'source_cursor_close_failed':1,'source_sqlite_close_failed':1,
                            'source_descriptor_close_failed':1})

    def test_cleanup_during_initial_validation_failure_keeps_all_diagnostics(self):
        with self.resources({'sqlite','descriptor'}) as mocks:
            with patch.object(validator,'_validate_open_database',side_effect=sqlite3.OperationalError('PRIVATE_VALIDATION_ERROR')):
                with self.assertRaises(validator.SourceSessionError) as error:
                    with validator.guarded_source_session('synthetic',confirmed_offline=True):
                        self.fail('Invalid source yielded')
            mocks.db.close.assert_called_once_with()
            mocks.fd_close.assert_called_once_with(12345)
            for cursor in mocks.cursors:cursor.close.assert_not_called()
            self.assertEqual(self.cleanup_codes(error.exception.report),{
                'source_sqlite_close_failed':1,'source_descriptor_close_failed':1})
            self.assertNotIn('PRIVATE_',str(error.exception))
            self.assertNotIn('PRIVATE_',json.dumps(error.exception.report))

    def test_cli_cleanup_failure_is_sanitized_and_fingerprint_cleared(self):
        with self.resources({'sqlite','descriptor'}) as mocks:
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                status=validator.main(['--snapshot','PRIVATE_SNAPSHOT_PATH','--confirm-offline-snapshot'])
            report=json.loads(output.getvalue())
            self.assertEqual(status,1)
            self.assertFalse(report['valid'])
            self.assertIsNone(report['source_sha256'])
            self.assertNotIn('PRIVATE_',output.getvalue())
            self.assertEqual(self.cleanup_codes(report),{
                'source_sqlite_close_failed':1,'source_descriptor_close_failed':1})
            mocks.db.close.assert_called_once_with()
            mocks.fd_close.assert_called_once_with(12345)

    def test_unexpected_reader_failure_does_not_abandon_other_readers(self):
        from unittest.mock import MagicMock
        with self.resources() as mocks:
            bad_reader=MagicMock()
            bad_reader._close.side_effect=SystemExit('PRIVATE_READER_ERROR')
            with self.assertRaises(validator.SourceSessionError) as error:
                with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                    self.open_readers(source)
                    source._readers.add(bad_reader)
            bad_reader._close.assert_called_once_with()
            self.assert_releases(mocks)
            self.assert_denied(source,error.exception.report)
            self.assertEqual(self.cleanup_codes(error.exception.report),{'source_reader_close_failed':1})

    def test_read_failures_latch_even_when_caught_inside_context(self):
        class UnusualInterrupt(BaseException):
            pass
        class BrokenRow:
            def __init__(self,error):self.error=error
            def __iter__(self):raise self.error
        classes=(RuntimeError,MemoryError,OverflowError,StopIteration,
                 KeyboardInterrupt,SystemExit,GeneratorExit,UnusualInterrupt)
        for stage in ('execute','fetch','conversion'):
            for kind in classes:
                with self.subTest(stage=stage,kind=kind.__name__),self.resources() as mocks:
                    original=kind('PRIVATE_READ_VALUE')
                    expected=validator.SourceSessionError if isinstance(original,Exception) else kind
                    with self.assertRaises(validator.SourceSessionError) as final:
                        with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                            if stage=='execute':
                                source.iter_records('companies')
                                mocks.db.execute.side_effect=original
                                operation=lambda:source.iter_records('users')
                            else:
                                reader,other=self.open_readers(source)
                                if stage=='fetch':mocks.cursors[0].fetchone.side_effect=original
                                else:mocks.cursors[0].fetchone.return_value=BrokenRow(original)
                                operation=lambda:next(reader)
                            with self.assertRaises(expected) as caught:operation()
                            if not isinstance(original,Exception):self.assertIs(caught.exception,original)
                            else:self.assertNotIn('PRIVATE_',str(caught.exception))
                            with self.assertRaises(validator.SourceSessionError):source.iter_records('users')
                            with self.assertRaises(validator.SourceSessionError):_=source.fingerprint
                    self.assertEqual(final.exception.code,'source_record_read_failed')
                    self.assert_denied(source,final.exception.report)
                    if stage=='execute':
                        mocks.cursors[0].close.assert_called_once_with()
                        mocks.cursors[1].close.assert_not_called()
                        mocks.db.close.assert_called_once_with()
                        mocks.fd_close.assert_called_once_with(12345)
                    else:self.assert_releases(mocks)

    def test_uncaught_read_interrupts_keep_identity_and_cleanup_every_resource(self):
        for kind in (KeyboardInterrupt,SystemExit,GeneratorExit):
            with self.subTest(kind=kind.__name__),self.resources({'cursor0','cursor1','sqlite','descriptor'}) as mocks:
                original=kind()
                with self.assertRaises(kind) as caught:
                    with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                        reader,other=self.open_readers(source)
                        mocks.cursors[0].fetchone.side_effect=original
                        next(reader)
                self.assertIs(caught.exception,original)
                self.assert_releases(mocks)
                self.assert_denied(source,original.source_cleanup_report)
                self.assertEqual(len(self.cleanup_codes(original.source_cleanup_report)),3)

    def test_reader_creation_and_registration_failure_release_acquired_cursor(self):
        class BrokenRegistry(set):
            def add(self,reader):
                super().add(reader)  # Simulate failure after partial registration.
                raise MemoryError('PRIVATE_REGISTRATION_ERROR')
        for stage in ('creation','registration'):
            with self.subTest(stage=stage),self.resources({'cursor1'}) as mocks:
                with self.assertRaises(validator.SourceSessionError) as final:
                    with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                        source.iter_records('users')
                        with contextlib.ExitStack() as stack:
                            if stage=='creation':
                                stack.enter_context(patch.object(validator,'_RecordReader',side_effect=MemoryError('PRIVATE_CREATION_ERROR')))
                            else:source._readers=BrokenRegistry(source._readers)
                            with self.assertRaises(validator.SourceSessionError):source.iter_records('companies')
                self.assert_releases(mocks)
                self.assert_denied(source,final.exception.report)
                self.assertEqual(self.cleanup_codes(final.exception.report),{'source_cursor_close_failed':1})

    def test_iterator_advancement_failure_after_successful_read_latches(self):
        with self.resources() as mocks:
            mocks.cursors[0].fetchone.side_effect=[(7,),MemoryError('PRIVATE_ADVANCEMENT')]
            with self.assertRaises(validator.SourceSessionError) as final:
                with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                    reader,other=self.open_readers(source)
                    iterator=iter(reader)
                    self.assertEqual(next(iterator),(7,))
                    with self.assertRaises(validator.SourceSessionError):next(iterator)
            self.assert_releases(mocks)
            self.assert_denied(source,final.exception.report)

    def test_normal_exhaustion_and_partial_iteration_still_finalize(self):
        for mode in ('exhaustion','break','unstarted'):
            with self.subTest(mode=mode),self.resources() as mocks:
                mocks.cursors[0].fetchone.side_effect=[(7,),None]
                with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                    reader,other=self.open_readers(source)
                    if mode=='exhaustion':
                        self.assertEqual(list(reader),[(7,)])
                        with self.assertRaises(StopIteration):next(reader)
                    elif mode=='break':
                        for row in reader:break
                self.assert_releases(mocks)
                self.assertTrue(source.finalized)
                self.assertEqual(source.fingerprint,'a'*64)

    def test_final_hash_window_changes_reject_even_matching_digest(self):
        from types import SimpleNamespace
        cases=('path_replacement','descriptor_identity','size','mtime','ctime',
               'symlink','wal','shm','journal','digest_mismatch')
        for case in cases:
            with self.subTest(case=case),self.resources() as mocks:
                with self.assertRaises(validator.SourceSessionError) as final:
                    with contextlib.ExitStack() as patches:
                        with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                            self.open_readers(source)
                            def final_digest(fd):
                                changes={'path_replacement':('st_ino',99),
                                         'descriptor_identity':('st_dev',99),
                                         'size':('st_size',8192),'mtime':('st_mtime_ns',99),
                                         'ctime':('st_ctime_ns',99)}
                                if case in changes:
                                    key,value=changes[case]
                                    changed=SimpleNamespace(**{**vars(mocks.info),key:value})
                                    if case=='path_replacement':mocks.path.lstat.return_value=changed
                                    else:validator.os.fstat.return_value=changed
                                elif case=='symlink':
                                    validator._explicit_path.side_effect=validator.UnsafeSnapshot('symlink_path_rejected')
                                elif case in ('wal','shm','journal'):
                                    # Model a newly visible sidecar using the real
                                    # sidecar detector, without filesystem access.
                                    def sidecars(path):
                                        with patch.object(validator.os.path,'lexists',
                                                side_effect=lambda name:name.endswith('-'+case)):
                                            ORIGINAL_SIDECAR_CHECK(path)
                                    validator._reject_sidecars.side_effect=sidecars
                                return 'b'*64 if case=='digest_mismatch' else 'a'*64
                            ORIGINAL_SIDECAR_CHECK=REAL_REJECT_SIDECARS
                            patches.enter_context(patch.object(validator,'_digest',side_effect=final_digest))
                self.assert_releases(mocks)
                self.assert_denied(source,final.exception.report)
                self.assertNotIn('PRIVATE_',str(final.exception))

    def test_post_hash_replacement_with_simultaneous_cleanup_failures(self):
        from types import SimpleNamespace
        with self.resources({'sqlite','descriptor'}) as mocks:
            with self.assertRaises(validator.SourceSessionError) as final:
                with contextlib.ExitStack() as patches:
                    with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                        self.open_readers(source)
                        def digest(fd):
                            mocks.path.lstat.return_value=SimpleNamespace(**{**vars(mocks.info),'st_ino':99})
                            return 'a'*64
                        patches.enter_context(patch.object(validator,'_digest',side_effect=digest))
            self.assert_releases(mocks)
            self.assert_denied(source,final.exception.report)
            self.assertEqual(final.exception.code,'snapshot_changed')
            self.assertEqual(self.cleanup_codes(final.exception.report),{
                'source_sqlite_close_failed':1,'source_descriptor_close_failed':1})

    def test_pre_hash_verification_failure_does_not_hash_or_finalize(self):
        from types import SimpleNamespace
        with self.resources() as mocks:
            with self.assertRaises(validator.SourceSessionError) as final:
                with contextlib.ExitStack() as patches:
                    with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                        self.open_readers(source)
                        mocks.path.lstat.return_value=SimpleNamespace(**{**vars(mocks.info),'st_ino':99})
                        digest=patches.enter_context(patch.object(validator,'_digest'))
            digest.assert_not_called()
            self.assert_releases(mocks)
            self.assert_denied(source,final.exception.report)

    def test_final_digest_failures_and_simultaneous_cleanup_failures(self):
        for kind in (OSError,ValueError,RuntimeError,MemoryError,KeyboardInterrupt,SystemExit,GeneratorExit):
            for cleanup in (False,True):
                with self.subTest(kind=kind.__name__,cleanup=cleanup),self.resources(
                        {'cursor0','cursor1','sqlite','descriptor'} if cleanup else ()) as mocks:
                    original=kind('PRIVATE_HASH_FAILURE')
                    expected=validator.SourceSessionError if isinstance(original,Exception) else kind
                    # Cursor failures intentionally prevent reaching hashing, so
                    # inject only SQLite/descriptor cleanup failures for this case.
                    if cleanup:
                        mocks.cursors[0].close.side_effect=lambda:mocks.calls.append('cursor0')
                        mocks.cursors[1].close.side_effect=lambda:mocks.calls.append('cursor1')
                    with self.assertRaises(expected) as caught:
                        with contextlib.ExitStack() as patches:
                            with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                                self.open_readers(source)
                                patches.enter_context(patch.object(validator,'_digest',side_effect=original))
                    self.assert_releases(mocks)
                    self.assert_denied(source,source.report)
                    if isinstance(original,Exception):
                        self.assertNotIn('PRIVATE_',str(caught.exception))
                        self.assertNotIn('PRIVATE_',json.dumps(caught.exception.report))
                    else:self.assertIs(caught.exception,original)
                    if cleanup:
                        self.assertEqual(self.cleanup_codes(source.report),{
                            'source_sqlite_close_failed':1,'source_descriptor_close_failed':1})

    def test_successful_final_digest_is_bracketed_and_cli_is_compatible(self):
        for cli in (False,True):
            with self.subTest(cli=cli),self.resources() as mocks:
                events=[]
                real_digest=validator._digest
                def digest(fd):
                    events.append('digest')
                    return real_digest(fd)
                with patch.object(validator,'_digest',side_effect=digest), \
                     patch.object(validator,'_explicit_path',side_effect=lambda path:(events.append('path'),mocks.path)[1]), \
                     patch.object(validator,'_reject_sidecars',side_effect=lambda path:events.append('sidecars')), \
                     patch.object(validator.os,'fstat',side_effect=lambda fd:(events.append('descriptor'),mocks.info)[1]), \
                     patch.object(mocks.path,'lstat',side_effect=lambda:(events.append('metadata'),mocks.info)[1]):
                    if cli:
                        output=io.StringIO()
                        with contextlib.redirect_stdout(output):
                            result=validator.main(['--snapshot','synthetic','--confirm-offline-snapshot'])
                        self.assertEqual(result,0)
                        self.assertTrue(json.loads(output.getvalue())['valid'])
                    else:
                        with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:pass
                        self.assertTrue(source.finalized)
                        self.assertEqual(source.fingerprint,'a'*64)
                self.assertEqual(events[-9:],['path','sidecars','descriptor','metadata','digest',
                                              'path','sidecars','descriptor','metadata'])
                mocks.db.close.assert_called_once_with()
                mocks.fd_close.assert_called_once_with(12345)

    def test_successful_mock_cleanup_releases_once_and_publishes_fingerprint(self):
        with self.resources() as mocks:
            with validator.guarded_source_session('synthetic',confirmed_offline=True) as source:
                self.open_readers(source)
            self.assert_releases(mocks)
            self.assertTrue(source.finalized)
            self.assertEqual(source.fingerprint,'a'*64)
            self.assertEqual(self.cleanup_codes(source.report),{})


class PlatformLockBoundaryTests(unittest.TestCase):
    def fake_fcntl(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        return SimpleNamespace(LOCK_SH=1, LOCK_NB=4, lockf=Mock())

    def test_unsupported_platform_precedes_source_access_for_validator_and_session(self):
        class UnusablePath:
            def __fspath__(self):
                raise AssertionError('Source path must not be inspected')
        for platform in ('nt', 'unsupported'):
            with self.subTest(platform=platform), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(validator, '_LOCK_PLATFORM', platform))
                guards = [stack.enter_context(patch.object(validator, name)) for name in
                          ('_explicit_path','_reject_sidecars','_digest','_acquire_source_lock')]
                opened = stack.enter_context(patch.object(validator.os,'open'))
                sqlite = stack.enter_context(patch.object(validator.sqlite3,'connect'))
                path = UnusablePath()
                report = validator.validate_snapshot(path, confirmed_offline=True)
                self.assertFalse(report['valid'])
                self.assertIsNone(report['source_sha256'])
                self.assertEqual([i['code'] for i in report['issues']], ['read_lock_unavailable'])
                with self.assertRaises(validator.SourceSessionError) as caught:
                    with validator.guarded_source_session(path, confirmed_offline=True):
                        self.fail('Unsupported source exposed records')
                self.assertEqual(caught.exception.code, 'read_lock_unavailable')
                self.assertIsNone(caught.exception.report['source_sha256'])
                for guard in [*guards,opened,sqlite]: guard.assert_not_called()

    def test_confirmation_remains_first_gate_on_unsupported_platform(self):
        with patch.object(validator, '_LOCK_PLATFORM', 'nt'), patch.object(
                validator, '_require_supported_lock_platform') as require, patch.object(
                validator, '_explicit_path') as path:
            report = validator.validate_snapshot('PRIVATE_PATH', confirmed_offline=False)
            self.assertEqual([i['code'] for i in report['issues']], ['offline_snapshot_confirmation_required'])
            with self.assertRaises(validator.SourceSessionError) as caught:
                with validator.guarded_source_session('PRIVATE_PATH', confirmed_offline=False):
                    self.fail('Unconfirmed source exposed records')
            self.assertEqual(caught.exception.code, 'offline_snapshot_confirmation_required')
            require.assert_not_called()
            path.assert_not_called()

    def test_posix_lock_preserves_range_flags_and_close_release(self):
        module = self.fake_fcntl()
        with patch.object(validator, '_LOCK_PLATFORM', 'posix'), patch.dict(sys.modules, {'fcntl':module}), patch.object(validator.os, 'close') as close:
            validator._acquire_source_lock(123)
            module.lockf.assert_called_once_with(123, 5, 514, 0x40000000)
            close.assert_not_called()
            validator._release_source_lock(123)
            close.assert_called_once_with(123)
            self.assertEqual(module.lockf.call_count, 1)  # No early LOCK_UN.

    def test_unsupported_platform_never_imports_fcntl(self):
        import builtins
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'fcntl':
                self.fail('Unsupported platform attempted POSIX import')
            return original(name, *args, **kwargs)
        with patch.object(validator, '_LOCK_PLATFORM', 'nt'), patch('builtins.__import__', side_effect=importing):
            with self.assertRaises(validator.UnsafeSnapshot) as caught:
                validator._acquire_source_lock(123)
        self.assertEqual(caught.exception.code, 'read_lock_unavailable')

    def test_missing_posix_module_is_safe_denial(self):
        with patch.object(validator, '_LOCK_PLATFORM', 'posix'), patch.dict(sys.modules, {'fcntl':None}):
            with self.assertRaises(validator.UnsafeSnapshot) as caught:
                validator._acquire_source_lock(123)
        self.assertEqual(caught.exception.code, 'read_lock_unavailable')

    def test_lock_errors_are_sanitized(self):
        for error in (BlockingIOError('PRIVATE_LOCK'), OSError('PRIVATE_LOCK')):
            module = self.fake_fcntl(); module.lockf.side_effect = error
            with self.subTest(kind=type(error).__name__), patch.object(validator, '_LOCK_PLATFORM', 'posix'), patch.dict(sys.modules, {'fcntl':module}):
                with self.assertRaises(validator.UnsafeSnapshot) as caught:
                    validator._acquire_source_lock(123)
                self.assertEqual(caught.exception.code, 'active_writer_or_read_lock_unavailable')
                self.assertNotIn('PRIVATE', str(caught.exception))

    def test_interrupt_and_descriptor_close_failure_keep_original_objects(self):
        module = self.fake_fcntl(); interrupt = KeyboardInterrupt()
        module.lockf.side_effect = interrupt
        with patch.object(validator, '_LOCK_PLATFORM', 'posix'), patch.dict(sys.modules, {'fcntl':module}):
            with self.assertRaises(KeyboardInterrupt) as caught:
                validator._acquire_source_lock(123)
            self.assertIs(caught.exception, interrupt)
        failure = OSError('PRIVATE_CLOSE')
        with patch.object(validator.os, 'close', side_effect=failure) as close:
            with self.assertRaises(OSError) as caught:
                validator._release_source_lock(123)
            self.assertIs(caught.exception, failure)
            close.assert_called_once_with(123)


if __name__=='__main__':
    unittest.main()
