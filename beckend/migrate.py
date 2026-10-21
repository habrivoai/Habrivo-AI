"""Explicit PostgreSQL schema migrations; never invoked by the web application."""
import argparse
import hashlib
from pathlib import Path
from database import DatabaseConfig, connect

MIGRATIONS = Path(__file__).resolve().parent / 'migrations' / 'postgres'


def migrate(config):
    if config.backend != 'postgres':
        raise ValueError('This migration runner requires PostgreSQL')
    db = connect(config)
    try:
        with db:
            # One transaction and lock: failed DDL rolls back; concurrent runners serialize.
            db.raw.execute('SELECT pg_advisory_xact_lock(724182603)')
            db.raw.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )''')
            for path in sorted(MIGRATIONS.glob('[0-9]*.sql')):
                sql = path.read_text()
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                row = db.raw.execute('SELECT checksum FROM schema_migrations WHERE version = %s', (path.name,)).fetchone()
                if row:
                    if row['checksum'] != checksum:
                        raise RuntimeError('Previously applied migration has changed: ' + path.name)
                    continue
                db.raw.execute(sql)
                db.raw.execute('INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)', (path.name, checksum))
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', required=True,
                        help='Explicitly apply schema changes to the configured database')
    parser.parse_args()
    migrate(DatabaseConfig.from_env())
