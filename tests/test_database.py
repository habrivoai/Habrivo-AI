"""Offline suite: no production configuration or network connections are used."""
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'beckend'))
from database import DatabaseConfig, PostgresConnection, Record, connect, init_sqlite_schema, _record_factory
from migrate import migrate, MIGRATIONS

# An existing Werkzeug PBKDF2 hash representation, independent of current defaults.
PASSWORD = 'existing-password'
LEGACY_HASH = 'pbkdf2:sha256:260000$audit-salt$' + hashlib.pbkdf2_hmac(
    'sha256', PASSWORD.encode(), b'audit-salt', 260000).hex()


class ConfigurationTests(unittest.TestCase):
    def test_local_default(self):
        self.assertEqual(DatabaseConfig.from_env({}).backend, 'sqlite')

    def test_production_requires_explicit_url(self):
        for env in ({'APP_ENV': 'production'}, {'RENDER': 'true'},
                    {'APP_ENV': 'production', 'DB_BACKEND': 'sqlite'},
                    {'RENDER': 'true', 'APP_ENV': 'development', 'DB_BACKEND': 'sqlite'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                DatabaseConfig.from_env(env)

    def test_reject_invalid_configuration_without_disclosing_url(self):
        for env in ({'APP_ENV': 'prod'}, {'DB_BACKEND': 'mysql'},
                    {'DATABASE_URL': 'sqlite:///tmp/x'},
                    {'DATABASE_URL': 'postgresql://user:private@host'},
                    {'DATABASE_URL': 'postgresql://user:private@host:bad/db'},
                    {'DATABASE_URL': 'postgresql://host/db', 'DB_BACKEND': 'sqlite'}):
            with self.subTest(env=env), self.assertRaises(ValueError) as error:
                DatabaseConfig.from_env(env)
            self.assertNotIn('private', str(error.exception))

    def test_postgres_configuration_is_lazy_and_redacted(self):
        with patch('sqlite3.connect', side_effect=AssertionError('Unexpected SQLite connection')):
            config = DatabaseConfig.from_env({'APP_ENV': 'production', 'DATABASE_URL': 'postgresql://user:private@localhost/test'})
        self.assertEqual(config.backend, 'postgres')
        self.assertNotIn('private', repr(config))

    def test_failed_postgres_connection_never_falls_back(self):
        driver = MagicMock()
        driver.connect.side_effect = ConnectionError('offline')
        config = DatabaseConfig.from_env({'DATABASE_URL': 'postgresql://localhost/test'})
        with patch.dict(sys.modules, {'psycopg': driver}), patch('sqlite3.connect') as sqlite:
            with self.assertRaises(ConnectionError):
                connect(config)
            sqlite.assert_not_called()


class SQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = DatabaseConfig.from_env({'DATA_DIR': self.temp.name})

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_creation_idempotency_and_hash_preservation(self):
        from werkzeug.security import check_password_hash
        init_sqlite_schema(self.config)
        db = connect(self.config)
        try:
            with db:
                db.execute('INSERT INTO users (name,email,password_hash) VALUES (?,?,?)', ('Legacy', 'legacy@example.invalid', LEGACY_HASH))
            init_sqlite_schema(self.config)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'users','companies','subscriptions','promo_usage'} <= tables)
            stored = db.execute('SELECT password_hash FROM users').fetchone()[0]
            self.assertEqual(stored, LEGACY_HASH)
            self.assertTrue(check_password_hash(stored, PASSWORD))
            self.assertFalse(check_password_hash(stored, 'wrong'))
            with self.assertRaises(sqlite3.IntegrityError), db:
                db.execute('INSERT INTO users (name,email,password_hash) VALUES (?,?,?)', ('Duplicate', 'legacy@example.invalid', LEGACY_HASH))
        finally:
            db.close()

    def test_upgrade_legacy_subscription_schema(self):
        db = connect(self.config)
        try:
            with db:
                db.execute('CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, customer_id INTEGER)')
                db.execute('INSERT INTO subscriptions VALUES (1, 7)')
            init_sqlite_schema(self.config)
            row = db.execute('SELECT customer_id, payment_verified FROM subscriptions').fetchone()
            self.assertEqual(tuple(row), (7, 0))
        finally:
            db.close()


class AdapterTests(unittest.TestCase):
    def test_rows_and_no_result_row_factory(self):
        self.assertEqual(Record(id=7)['id'], 7)
        self.assertEqual(Record(id=7)[0], 7)
        cursor = MagicMock(description=None)
        self.assertEqual(_record_factory(cursor)([]), {})

    def test_existing_dml_parameter_styles_and_insert_ids(self):
        raw = MagicMock()
        raw.execute.return_value.fetchone.return_value = Record(id=42)
        db = PostgresConnection(raw)
        result = db.execute('INSERT INTO users (name) VALUES (?)', ('A',))
        raw.execute.assert_called_with('INSERT INTO users (name) VALUES (%s) RETURNING id', ('A',))
        self.assertEqual(result.lastrowid, 42)
        db.execute('UPDATE companies SET company=:company WHERE id=:id', {'company':'A','id':42})
        raw.execute.assert_called_with('UPDATE companies SET company=%(company)s WHERE id=%(id)s', {'company':'A','id':42})
        with db:
            pass
        raw.commit.assert_called_once()
        with self.assertRaises(ValueError):
            with db:
                raise ValueError('rollback')
        raw.rollback.assert_called_once()


class MigrationTests(unittest.TestCase):
    def test_versioned_schema_contents(self):
        # Structural guard only; the optional integration suite tests PostgreSQL execution.
        sql = (MIGRATIONS / '0001_initial.sql').read_text()
        for table in ('users', 'companies', 'subscriptions', 'promo_usage'):
            self.assertIn('CREATE TABLE ' + table + ' (', sql)
        self.assertIn('password_hash TEXT NOT NULL', sql)
        self.assertIn('payment_verified BIGINT NOT NULL DEFAULT 0', sql)
        self.assertNotIn('FOREIGN KEY', sql)  # Match current semantics, no new constraints.

    def test_migration_version_checksum_and_skip(self):
        config = DatabaseConfig.from_env({'DATABASE_URL':'postgresql://localhost/test'})
        raw = MagicMock()
        raw.execute.return_value.fetchone.return_value = None
        db = PostgresConnection(raw)
        with patch('migrate.connect', return_value=db):
            migrate(config)
        statements = [c.args[0] for c in raw.execute.call_args_list]
        self.assertTrue(any('CREATE TABLE users' in sql for sql in statements))
        self.assertTrue(any('INSERT INTO schema_migrations' in sql for sql in statements))
        raw.commit.assert_called_once()
        raw.close.assert_called_once()
        checksum = hashlib.sha256((MIGRATIONS/'0001_initial.sql').read_text().encode()).hexdigest()
        for stored, failure in ((checksum, False), ('changed', True)):
            raw.reset_mock()
            def ledger_lookup(sql, parameters=()):
                result = MagicMock()
                if sql.startswith('SELECT checksum'):
                    actual = hashlib.sha256((MIGRATIONS / parameters[0]).read_text().encode()).hexdigest()
                    result.fetchone.return_value = {'checksum': 'changed' if failure else actual}
                return result
            raw.execute.side_effect = ledger_lookup
            with patch('migrate.connect', return_value=db):
                if failure:
                    with self.assertRaises(RuntimeError):
                        migrate(config)
                    raw.rollback.assert_called_once()
                else:
                    migrate(config)
                    self.assertFalse(any('CREATE TABLE users' in c.args[0] for c in raw.execute.call_args_list))

    def test_postgres_import_has_no_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'APP_ENV':'production', 'DATABASE_URL':'postgresql://localhost/test',
                   'SECRET_KEY':'offline-test-only', 'DATA_DIR':directory}
            with patch.dict(os.environ, env, clear=True), patch('dotenv.load_dotenv'), \
                 patch('database.connect', side_effect=AssertionError('Import must not connect')), \
                 patch('database.init_sqlite_schema', side_effect=AssertionError('Import must not initialize')):
                path = Path(__file__).resolve().parents[1] / 'beckend' / 'app.py'
                spec = importlib.util.spec_from_file_location('audit_postgres_app', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            self.assertFalse((Path(directory)/'habrivo.db').exists())

    def test_existing_hash_customer_login_and_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {'APP_ENV':'test', 'SECRET_KEY':'offline-test-only', 'DATA_DIR':directory}
            with patch.dict(os.environ, env, clear=True), patch('dotenv.load_dotenv'):
                path = Path(__file__).resolve().parents[1] / 'beckend' / 'app.py'
                spec = importlib.util.spec_from_file_location('audit_sqlite_app', path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                db = module.conn()
                try:
                    with db:
                        db.execute('INSERT INTO users (name,email,password_hash) VALUES (?,?,?)', ('Legacy','legacy@example.invalid',LEGACY_HASH))
                finally:
                    db.close()
                client = module.app.test_client()
                result = client.post('/api/customer/login', base_url='https://localhost', json={'email':'legacy@example.invalid','password':PASSWORD})
                self.assertEqual(result.status_code, 200)
                self.assertTrue(client.get('/api/check-auth', base_url='https://localhost').json['authenticated'])
                payload = {'name':'New','email':'new@example.invalid','password':'new-password'}
                self.assertEqual(client.post('/api/register', json=payload).status_code, 201)
                self.assertEqual(client.post('/api/register', json=payload).status_code, 201)


if __name__ == '__main__':
    unittest.main()
