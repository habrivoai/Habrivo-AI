"""Database boundary. Importing this module never opens a connection."""
from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import sqlite3
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DatabaseConfig:
    backend: str
    data_dir: Path
    url: str = field(default='', repr=False)

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        mode = env.get('APP_ENV', 'development').strip().lower()
        if mode not in {'development', 'test', 'production'}:
            raise ValueError('APP_ENV must be development, test, or production')
        production = mode == 'production' or env.get('RENDER', '').lower() == 'true'
        url = env.get('DATABASE_URL', '').strip()
        backend = env.get('DB_BACKEND', 'postgres' if url or production else 'sqlite').strip().lower()
        if backend not in {'sqlite', 'postgres'}:
            raise ValueError('DB_BACKEND must be sqlite or postgres')
        if production and backend != 'postgres':
            raise ValueError('Production requires PostgreSQL; SQLite is local only')
        if backend == 'sqlite' and url:
            raise ValueError('DATABASE_URL cannot be combined with SQLite')
        if backend == 'postgres':
            try:
                parsed = urlsplit(url)
                valid = (parsed.scheme in {'postgres', 'postgresql'} and parsed.hostname
                         and parsed.path not in {'', '/'} and not parsed.fragment)
                parsed.port  # Validate malformed ports without exposing the URL.
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('PostgreSQL requires an explicit valid DATABASE_URL')
        return cls(backend, Path(env.get('DATA_DIR', str(Path(__file__).resolve().parent))).resolve(), url)


def integrity_errors(config):
    if config.backend == 'sqlite':
        return (sqlite3.IntegrityError,)
    import psycopg
    return (psycopg.IntegrityError,)


class Record(dict):
    """Support existing sqlite.Row consumers using both names and indexes."""
    def __getitem__(self, key):
        return tuple(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def _record_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(zip(names, values))


class PostgresConnection:
    """Compatibility for this app's bounded SQLite-style parameterized DML.

    Not a general SQL translator. Schema SQL goes directly to psycopg migrations.
    """
    def __init__(self, connection):
        self.raw = connection

    def execute(self, sql, parameters=()):
        if isinstance(parameters, dict):
            sql = re.sub(r':([a-zA-Z_][a-zA-Z_0-9]*)', r'%(\1)s', sql)
        else:
            sql = sql.replace('?', '%s')
        inserted = bool(re.match(r'\s*INSERT\s+INTO\s+(users|companies)\b', sql, re.I))
        if inserted:
            sql = sql.rstrip().rstrip(';') + ' RETURNING id'
        cursor = self.raw.execute(sql, parameters)
        return InsertResult(cursor) if inserted else cursor

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.raw.commit()
        else:
            self.raw.rollback()
        return False

    def close(self):
        self.raw.close()


class InsertResult:
    def __init__(self, cursor):
        self.lastrowid = cursor.fetchone()['id']
        self.rowcount = cursor.rowcount


def connect(config):
    if config.backend == 'sqlite':
        db = sqlite3.connect(str(config.data_dir / 'habrivo.db'), timeout=20)
        db.row_factory = sqlite3.Row
        return db
    import psycopg
    # Do not log URLs or retry against another backend if connection fails.
    return PostgresConnection(psycopg.connect(config.url, row_factory=_record_factory, connect_timeout=10))


def init_sqlite_schema(config):
    if config.backend != 'sqlite':
        raise RuntimeError('SQLite initialization is local only')
    from subscriptions_db import init_subscriptions_db
    config.data_dir.mkdir(parents=True, exist_ok=True)
    init_subscriptions_db(config)
    db = connect(config)
    try:
        with db:
            db.execute('''CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT, owner TEXT, whatsapp TEXT, email TEXT,
                category TEXT, logo TEXT, address TEXT, website TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )''')
            db.execute('''CREATE TABLE IF NOT EXISTS marketing_content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                content_digest TEXT NOT NULL,
                product_name TEXT NOT NULL,
                details_json TEXT NOT NULL,
                outputs_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (customer_id, content_digest)
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_marketing_content_customer_id ON marketing_content(customer_id, id)')
            db.execute('''CREATE TABLE IF NOT EXISTS account_action_limits (
                scope TEXT NOT NULL,
                subject_hash TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                attempts INTEGER NOT NULL,
                PRIMARY KEY (scope, subject_hash, window_start)
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_account_action_limits_window ON account_action_limits(window_start)')
            # Local development only. PostgreSQL uses the explicit versioned runner.
            for migration in ('0004_paddle_billing.sql', '0005_paddle_payment_holds.sql',
                              '0006_paddle_subscription_holds.sql', '0007_customer_security.sql', '0008_email_verification_trial.sql'):
                billing_sql = (Path(__file__).resolve().parent / 'migrations' / 'postgres' /
                               migration).read_text()
                for statement in re.sub(r'--[^\n]*', '', billing_sql).split(';'):
                    if statement.strip():
                        statement = statement.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ')
                        statement = statement.replace('CREATE INDEX ', 'CREATE INDEX IF NOT EXISTS ')
                        db.execute(statement)
            cols = {r['name'] for r in db.execute('PRAGMA table_info(companies)')}
            for name in ('email', 'address', 'website', 'created_at'):
                if name not in cols:
                    db.execute(f'ALTER TABLE companies ADD COLUMN {name} TEXT')
    finally:
        db.close()
