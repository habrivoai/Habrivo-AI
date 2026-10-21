"""Synthetic offline importer preflight; never use application or existing databases."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from beckend import import_sqlite_snapshot as importer
from beckend import validate_sqlite_snapshot as validator
from test_sqlite_snapshot_validator import fixture, HASH


class ImportDryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'PRIVATE_SOURCE.sqlite'
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        (self.uploads / 'logo.png').write_bytes(b'synthetic image')
        fixture(self.source)

    def inventory(self):
        return {p.relative_to(self.root).as_posix():
                (p.stat().st_mtime_ns, p.stat().st_mode,
                 hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in self.root.rglob('*')}

    def run_source(self, **options):
        before = self.inventory()
        try:
            return importer.source_only_dry_run(self.source, confirmed_offline=True, **options)
        finally:
            self.assertEqual(self.inventory(), before)

    def assert_private(self, value):
        rendered = json.dumps(value) if isinstance(value, dict) else str(value)
        for private in ('PRIVATE_', HASH, 'private-customer@example.invalid', str(self.root)):
            self.assertNotIn(private, rendered)

    @contextlib.contextmanager
    def altered_session(self, transform):
        original = importer.guarded_source_session
        @contextlib.contextmanager
        def guard(*args, **kwargs):
            with original(*args, **kwargs) as source:
                yield transform(source)
        with patch.object(importer, 'guarded_source_session', side_effect=guard):
            yield

    def test_valid_manifest_single_session_single_connection_full_sequential_consumption(self):
        seen = []
        original_guard = importer.guarded_source_session
        @contextlib.contextmanager
        def guard(*args, **kwargs):
            with original_guard(*args, **kwargs) as source:
                original = source.iter_records
                def records(table):
                    seen.append(('start', table))
                    for values in original(table):
                        yield values
                    seen.append(('end', table))
                source.iter_records = records
                yield source
        with patch.object(importer, 'guarded_source_session', side_effect=guard) as session, \
             patch.object(validator.sqlite3, 'connect', wraps=sqlite3.connect) as connection:
            manifest = self.run_source(uploads=self.uploads)
        session.assert_called_once()
        connection.assert_called_once()
        self.assertEqual(seen, [(stage, table) for table in importer.TABLES for stage in ('start','end')])
        self.assertTrue(manifest['completed'])
        self.assertFalse(manifest['migration_performed'])
        self.assertEqual(manifest['source_sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        for table in importer.TABLES:
            self.assertEqual(manifest['tables'][table], {'validated_count':1,'consumed_count':1})
        self.assertTrue(manifest['uploads']['checked'])
        self.assert_private(manifest)

    def test_empty_snapshot(self):
        self.source.unlink()
        fixture(self.source, populated=False)
        manifest = self.run_source()
        self.assertTrue(manifest['completed'])
        self.assertTrue(all(t['consumed_count']==0 for t in manifest['tables'].values()))
        self.assertTrue(any(w['code']=='empty_application_snapshot' for w in manifest['warnings']))

    def test_missing_confirmation_missing_corrupt_and_invalid_sources_fail_closed(self):
        for source, confirmation in ((self.source,False),(self.root/'missing.sqlite',True)):
            with self.subTest(source_exists=source.exists()),self.assertRaises(importer.ImportDryRunError) as error:
                importer.source_only_dry_run(source, confirmed_offline=confirmation)
            self.assert_private(error.exception)
        self.assertFalse((self.root/'missing.sqlite').exists())
        self.source.write_bytes(b'PRIVATE_CORRUPTION')
        with self.assertRaises(importer.ImportDryRunError):self.run_source()

    def test_incompatible_schema_and_missing_upload_are_blocking(self):
        (self.uploads/'logo.png').unlink()
        with self.assertRaises(importer.ImportDryRunError):self.run_source(uploads=self.uploads)
        db=sqlite3.connect(self.source)
        try:
            db.execute('DROP TABLE promo_usage')
            db.commit()
        finally:db.close()
        with self.assertRaises(importer.ImportDryRunError):self.run_source()

    def test_partial_duplicate_and_out_of_order_enumeration_rejected(self):
        for mode in ('partial','duplicate','wrong_order'):
            def transform(source):
                original = source.iter_records
                def records(table):
                    rows=list(original(table))
                    if table=='users':
                        if mode=='partial':return iter(())
                        if mode=='duplicate':return iter(rows+rows)
                        return iter([(8,)+rows[0][1:], rows[0]])
                    return iter(rows)
                source.iter_records=records
                return source
            with self.subTest(mode=mode),self.altered_session(transform):
                with self.assertRaises(importer.ImportDryRunError) as error:self.run_source()
                self.assert_private(error.exception)

    def test_validated_count_mismatch_is_rejected(self):
        class Proxy:
            def __init__(self,source):self.source=source
            def __getattr__(self,name):return getattr(self.source,name)
            @property
            def report(self):
                report=self.source.report
                report['tables']['users']=2
                return report
        with self.altered_session(Proxy):
            with self.assertRaises(importer.ImportDryRunError) as error:self.run_source()
        self.assertEqual(str(error.exception),'source_count_mismatch')

    def test_maximum_id_mismatch_is_rejected(self):
        def transform(source):
            original=source.iter_records
            def records(table):
                for row in original(table):
                    yield ((8,)+row[1:]) if table=='users' else row
            source.iter_records=records
            return source
        with self.altered_session(transform):
            with self.assertRaises(importer.ImportDryRunError) as error:self.run_source()
        self.assertEqual(str(error.exception),'source_id_enumeration_failed')

    def test_caught_guarded_read_failure_cannot_produce_manifest(self):
        class CursorProxy:
            def __init__(self,cursor):self.cursor=cursor
            def fetchone(self):raise RuntimeError('PRIVATE_READ')
            def close(self):return self.cursor.close()
        def transform(source):
            original=source.iter_records
            def records(table):
                reader=original(table)
                if table=='users':
                    first=next(reader)
                    reader._cursor=CursorProxy(reader._cursor)
                    try:next(reader)
                    except validator.SourceSessionError:pass
                    return iter([first])
                return reader
            source.iter_records=records
            return source
        with self.altered_session(transform):
            with self.assertRaises(importer.ImportDryRunError) as error:self.run_source()
        self.assert_private(error.exception)

    def test_final_digest_mutation_blocks_matching_hash(self):
        original=validator._digest
        calls=0
        def digest(fd):
            nonlocal calls
            calls+=1
            result=original(fd)
            if calls==2:
                # Only the synthetic fixture is deliberately modified by this test.
                with self.source.open('ab') as stream:stream.write(b'fault injection')
            return result
        with patch.object(validator,'_digest',side_effect=digest):
            with self.assertRaises(importer.ImportDryRunError) as error:
                importer.source_only_dry_run(self.source,confirmed_offline=True)
        self.assert_private(error.exception)

    def test_finalization_cleanup_failure_never_returns_manifest(self):
        original=importer.guarded_source_session
        @contextlib.contextmanager
        def guard(*args,**kwargs):
            with original(*args,**kwargs) as source:
                yield source
            raise RuntimeError('PRIVATE_CLEANUP')
        with patch.object(importer,'guarded_source_session',side_effect=guard):
            with self.assertRaises(importer.ImportDryRunError) as error:self.run_source()
        self.assert_private(error.exception)

    def test_pending_finalization_cannot_publish_manifest(self):
        from types import SimpleNamespace
        report={'valid':True,'tables':dict.fromkeys(importer.TABLES,0),
                'sequences':{t:{'maximum_existing_id':0} for t in importer.TABLES},
                'source_sha256':None,'issues':[],'uploads':{'checked':False}}
        source=SimpleNamespace(report=report,columns=lambda table:('id',),
                               iter_records=lambda table:iter(()),
                               finalized=False,fingerprint=None)
        with patch.object(importer,'guarded_source_session',return_value=contextlib.nullcontext(source)):
            with self.assertRaises(importer.ImportDryRunError) as error:self.run_source()
        self.assertEqual(str(error.exception),'source_finalization_failed')

    def test_sequence_high_water_and_unchecked_upload_warning_preserved(self):
        db=sqlite3.connect(self.source)
        try:
            db.execute("INSERT INTO users(id,name,email,password_hash) VALUES(100,'synthetic','deleted@example.invalid',?)",(HASH,))
            db.execute('DELETE FROM users WHERE id=100')
            db.commit()
        finally:db.close()
        manifest=self.run_source()
        self.assertEqual(manifest['sequences']['users']['recorded_sqlite_high_water'],100)
        self.assertEqual(manifest['sequences']['users']['minimum_next_id'],101)
        self.assertFalse(manifest['uploads']['checked'])
        self.assertTrue(any(w['code']=='referenced_logos_not_checked' for w in manifest['warnings']))

    def test_cli_success_failure_privacy_and_no_target_modes(self):
        cases=[(['--snapshot',str(self.source),'--confirm-offline-snapshot'],0),
               (['--snapshot',str(self.source)],1),
               ([],2),
               (['--snapshot',str(self.source),'--apply','PRIVATE_TOKEN'],2)]
        for args,status in cases:
            with self.subTest(status=status):
                before=self.inventory()
                output=io.StringIO()
                with contextlib.redirect_stdout(output):result=importer.main(args)
                self.assertEqual(result,status)
                manifest=json.loads(output.getvalue())
                self.assertEqual(manifest['completed'],status==0)
                if status:self.assertNotIn('source_sha256',manifest)
                self.assert_private(manifest)
                self.assertEqual(self.inventory(),before)

    def test_cli_interrupt_diagnostic_never_echoes_private_text(self):
        with patch.object(importer,'source_only_dry_run',side_effect=KeyboardInterrupt('PRIVATE_INTERRUPT')):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                status=importer.main(['--snapshot','PRIVATE_PATH','--confirm-offline-snapshot'])
        self.assertEqual(status,130)
        self.assert_private(json.loads(output.getvalue()))

    def test_script_and_module_entry_points(self):
        for command in ([sys.executable,'-B','beckend/import_sqlite_snapshot.py'],
                        [sys.executable,'-B','-m','beckend.import_sqlite_snapshot']):
            result=subprocess.run(command+['--snapshot',str(self.source),'--confirm-offline-snapshot'],
                                  capture_output=True,text=True,check=True,timeout=10)
            self.assertTrue(json.loads(result.stdout)['completed'])
            self.assertEqual(result.stderr,'')
            self.assert_private(json.loads(result.stdout))


if __name__=='__main__':
    unittest.main()
