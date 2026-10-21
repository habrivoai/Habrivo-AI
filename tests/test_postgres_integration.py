"""Real PostgreSQL tests: explicitly disposable LOCAL databases only.

Requires HABRIVO_TEST_POSTGRES_URL and
HABRIVO_TEST_DISPOSABLE=habrivo_foundation_test. The operator attests this is a
new independent local database, never a tunnel/proxy to another service. A fixed
local Docker container and matching PostgreSQL cluster identity are also required. No DATABASE_URL is read. Each test
creates/drops only its own UUID schema; no databases are created or dropped.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import ipaddress
import json
import re
import subprocess
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'beckend'))
from database import DatabaseConfig, connect
from migrate import migrate, MIGRATIONS
from test_database import LEGACY_HASH, PASSWORD


def disposable_config(environ):
    """Validate before importing a driver or attempting any connection."""
    if environ.get('HABRIVO_TEST_DISPOSABLE') != 'habrivo_foundation_test':
        raise ValueError('Explicit disposable-local-database acknowledgement required')
    # libpq environment routing/credentials must not select another target.
    if any(key.startswith('PG') for key in environ):
        raise ValueError('Remove inherited PG* settings before integration tests')
    try:
        parsed = urlsplit(environ.get('HABRIVO_TEST_POSTGRES_URL', ''))
        valid = (parsed.scheme in {'postgres', 'postgresql'}
                 and parsed.hostname in {'127.0.0.1', '::1'}
                 and parsed.port == 55432
                 and parsed.username == 'foundation_test'
                 and parsed.password is not None
                 and parsed.path == '/habrivo_foundation_test'
                 and not parsed.query and not parsed.fragment)
        if not valid:
            raise ValueError()
        # Rebuild from validated components: no multi-host/service/host overrides.
        host = '[::1]' if parsed.hostname == '::1' else '127.0.0.1'
        url = urlunsplit(('postgresql',
            'foundation_test:' + quote(unquote(parsed.password), safe='') + '@' + host + ':' + str(parsed.port),
            '/habrivo_foundation_test', '', ''))
    except ValueError:
        raise ValueError('Use a dedicated loopback test URL, foundation_test role, '
                         'habrivo_foundation_test database, port 55432, no query/fragment') from None
    return DatabaseConfig.from_env({'DATABASE_URL':url, 'APP_ENV':'test'})


CONTAINER_NAME = 'habrivo-foundation-pg18'
DISPOSABLE_MARKER = 'habrivo_foundation_test'
DOCKER_SOCKET = 'unix:///var/run/docker.sock'
# Deliberately omit environment values and credentials from inspect output.
CONTAINER_FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},'
    '"running":{{json .State.Running}},"paused":{{json .State.Paused}},'
    '"restarting":{{json .State.Restarting}},"started":{{json .State.StartedAt}},'
    '"image":{{json .Image}},"image_ref":{{json .Config.Image}},'
    '"marker":{{json (index .Config.Labels "habrivo.integration.disposable")}},'
    '"network_mode":{{json .HostConfig.NetworkMode}},'
    '"privileged":{{json .HostConfig.Privileged}},'
    '"pid_mode":{{json .HostConfig.PidMode}},'
    '"auto_remove":{{json .HostConfig.AutoRemove}},'
    '"binds":{{json .HostConfig.Binds}},"volumes_from":{{json .HostConfig.VolumesFrom}},'
    '"tmpfs":{{json .HostConfig.Tmpfs}},"mounts":{{json .Mounts}},'
    '"ports":{{json .NetworkSettings.Ports}},'
    '"bindings":{{json .HostConfig.PortBindings}},'
    '"networks":{{json .NetworkSettings.Networks}}}')
CLUSTER_SQL = """SELECT json_build_object(
    'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
    'database', current_database(), 'role', current_user,
    'version', current_setting('server_version_num')::integer,
    'port', current_setting('port')::integer,
    'data_directory', current_setting('data_directory'),
    'server_address', host(inet_server_addr()),
    'in_recovery', pg_is_in_recovery())"""


def docker_json(*arguments):
    """Only the managed local socket; never inherit remote Docker selectors."""
    env = {key:value for key,value in os.environ.items() if key not in {
        'DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'}}
    try:
        result = subprocess.run(['docker', '--host='+DOCKER_SOCKET, *arguments],
                                env=env, capture_output=True, text=True,
                                check=True, timeout=10)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Do not echo command output: it might contain unexpected credentials.
        raise ValueError('Independent local Docker verification unavailable') from None


def _require(condition):
    if not condition:
        raise ValueError("Disposable Docker safety requirement failed")


def inspect_target(config):
    """Fail closed before DB access; return a stable, exact Docker fingerprint."""
    obj = docker_json('container', 'inspect', '--format', CONTAINER_FORMAT, CONTAINER_NAME)
    try:
        _require(isinstance(obj, dict))
        _require(re.fullmatch(r'[0-9a-f]{64}', obj['id']))
        _require(obj['name'] == '/'+CONTAINER_NAME)
        _require(obj['running'] is True and obj['paused'] is False and obj['restarting'] is False)
        _require(obj['started'] and obj['started'] != '0001-01-01T00:00:00Z')
        _require(obj['image_ref'] == 'postgres:18')
        _require(re.fullmatch(r'sha256:[0-9a-f]{64}', obj['image']))
        _require(obj['marker'] == DISPOSABLE_MARKER)
        _require(obj['network_mode'] == 'bridge' and obj['privileged'] is False)
        _require(obj['pid_mode'] == '' and obj['auto_remove'] is True)
        _require(not obj['binds'] and not obj['volumes_from'])
        _require(set(obj['tmpfs']) == {'/var/lib/postgresql'})
        # No bind mounts, persistent volumes, or shared database/socket directories.
        _require(all(m['Type'] == 'tmpfs' and m['Destination'] == '/var/lib/postgresql'
                   for m in obj['mounts']))
        host = urlsplit(config.url).hostname
        for key in ('ports','bindings'):
            _require(set(obj[key]) == {'5432/tcp'})
            bindings = obj[key]['5432/tcp']
            _require(bindings and len(bindings) <= 2)
            actual = {(b['HostIp'], b['HostPort']) for b in bindings}
            _require(len(actual) == len(bindings))
            _require((host, '55432') in actual)
            _require(actual <= {('127.0.0.1','55432'), ('::1','55432')})
        _require(obj['ports'] == obj['bindings'])
        _require(set(obj['networks']) == {'bridge'})
        network = obj['networks']['bridge']
        addresses = tuple(sorted(str(ipaddress.ip_address(network[key]))
                                 for key in ('IPAddress','GlobalIPv6Address') if network.get(key)))
        _require(addresses)
        _require(all(not (ipaddress.ip_address(a).is_unspecified or
                        ipaddress.ip_address(a).is_loopback or
                        ipaddress.ip_address(a).is_multicast) for a in addresses))
        digests = docker_json('image', 'inspect', '--format', '{{json .RepoDigests}}', obj['image'])
        _require(digests and any(re.fullmatch(r'(?:docker.io/library/)?postgres@sha256:[0-9a-f]{64}', d)
                               for d in digests))
        return {'id':obj['id'], 'image':obj['image'], 'started':obj['started'],
                'addresses':addresses}
    except (AssertionError, KeyError, TypeError, ValueError):
        raise ValueError('Docker target is not the dedicated disposable PostgreSQL 18 container') from None


def container_identity(target):
    # Read-only query via the container's OWN local Unix socket. No password,
    # inherited libpq routing, host tunnel, or host database URL participates.
    return docker_json('exec', target['id'], 'env', '-i',
        'PATH=/usr/local/bin:/usr/bin:/bin', 'psql', '-X', '-w', '-A', '-t',
        '-h', '/var/run/postgresql', '-p', '5432', '-U', 'foundation_test',
        '-d', DISPOSABLE_MARKER, '-v', 'ON_ERROR_STOP=1', '-c', CLUSTER_SQL)


def validate_identity(identity):
    try:
        _require(isinstance(identity, dict))
        _require(re.fullmatch(r'[0-9]{10,20}', identity['system_identifier']))
        _require(identity['database'] == DISPOSABLE_MARKER)
        _require(identity['role'] == 'foundation_test')
        _require(type(identity['version']) is int and 180000 <= identity['version'] < 190000)
        _require(identity['port'] == 5432 and identity['in_recovery'] is False)
        _require(identity['data_directory'] == '/var/lib/postgresql/18/docker')
    except (AssertionError, KeyError, TypeError):
        raise ValueError('PostgreSQL identity does not match disposable PostgreSQL 18') from None


def verified_target(config):
    target = inspect_target(config)
    identity = container_identity(target)
    validate_identity(identity)
    if identity.get('server_address') is not None:
        raise ValueError('Independent PostgreSQL identity query did not use a local Unix socket')
    if inspect_target(config) != target:
        raise ValueError('Docker target changed during independent verification')
    return target, identity


def verify_connection(config, connection, target, identity):
    # Revalidate running state/ports/mounts and cluster identity before allowing DDL.
    current_target, current_identity = verified_target(config)
    if current_target != target or current_identity != identity:
        raise ValueError('Disposable Docker cluster identity changed')
    actual = connection.execute(CLUSTER_SQL).fetchone()[0]
    validate_identity(actual)
    if any(actual[key] != identity[key] for key in identity if key != 'server_address'):
        raise ValueError('Client PostgreSQL cluster differs from independently verified container')
    try:
        address = str(ipaddress.ip_address(actual['server_address']))
    except (KeyError, ValueError, TypeError):
        raise ValueError('Client PostgreSQL server address unavailable') from None
    if address not in target['addresses']:
        raise ValueError('Client PostgreSQL server is not at the exact Docker-assigned address')


class DisposableGuardTests(unittest.TestCase):
    """Always run offline; guards must reject unsafe targets before connecting."""
    def test_accepts_only_explicit_disposable_loopback_target(self):
        env = {'HABRIVO_TEST_DISPOSABLE':'habrivo_foundation_test',
               'HABRIVO_TEST_POSTGRES_URL':'postgresql://foundation_test:local-only@127.0.0.1:55432/habrivo_foundation_test'}
        self.assertEqual(disposable_config(env).backend, 'postgres')
        for changes in (
            {'HABRIVO_TEST_DISPOSABLE':''}, {'PGSERVICE':'old-service'},
            {'HABRIVO_TEST_POSTGRES_URL':env['HABRIVO_TEST_POSTGRES_URL'].replace('127.0.0.1', 'remote.example')},
            {'HABRIVO_TEST_POSTGRES_URL':env['HABRIVO_TEST_POSTGRES_URL'].replace('55432', '5432')},
            {'HABRIVO_TEST_POSTGRES_URL':env['HABRIVO_TEST_POSTGRES_URL'].replace('55432', '55433')},
            {'HABRIVO_TEST_POSTGRES_URL':env['HABRIVO_TEST_POSTGRES_URL'].replace('foundation_test:local-only', 'postgres:local-only')},
            {'HABRIVO_TEST_POSTGRES_URL':env['HABRIVO_TEST_POSTGRES_URL'].replace('habrivo_foundation_test', 'habrivo-db')},
            {'HABRIVO_TEST_POSTGRES_URL':env['HABRIVO_TEST_POSTGRES_URL']+'?host=remote.example'},
            {'HABRIVO_TEST_POSTGRES_URL':env['HABRIVO_TEST_POSTGRES_URL']+'#fragment'},
        ):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                disposable_config(dict(env, **changes))


class DockerGuardTests(unittest.TestCase):
    """Offline metadata and identity fixtures; no Docker commands or DB access."""
    def setUp(self):
        self.config = disposable_config({
            'HABRIVO_TEST_DISPOSABLE':DISPOSABLE_MARKER,
            'HABRIVO_TEST_POSTGRES_URL':'postgresql://foundation_test:local-only@127.0.0.1:55432/habrivo_foundation_test'})
        self.obj = {
            'id':'a'*64, 'name':'/'+CONTAINER_NAME, 'running':True,
            'paused':False, 'restarting':False, 'started':'2026-10-20T00:00:00Z',
            'image':'sha256:'+'b'*64, 'image_ref':'postgres:18',
            'marker':DISPOSABLE_MARKER, 'network_mode':'bridge',
            'privileged':False, 'pid_mode':'', 'auto_remove':True,
            'binds':None, 'volumes_from':None,
            'tmpfs':{'/var/lib/postgresql':''}, 'mounts':[],
            'ports':{'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'55432'}]},
            'bindings':{'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'55432'}]},
            'networks':{'bridge':{'IPAddress':'172.17.0.2','GlobalIPv6Address':''}},
        }
        self.identity = {'system_identifier':'1234567890123456789',
            'database':DISPOSABLE_MARKER, 'role':'foundation_test', 'version':180001,
            'port':5432, 'data_directory':'/var/lib/postgresql/18/docker',
            'server_address':None, 'in_recovery':False}

    def fake_docker(self, *args):
        if args[0] == 'image':
            return ['postgres@sha256:'+'c'*64]
        return copy.deepcopy(self.obj)

    def inspect_fixture(self):
        with patch(__name__+'.docker_json', side_effect=self.fake_docker):
            return inspect_target(self.config)

    def test_allows_only_exact_independently_verified_docker_address(self):
        with patch(__name__+'.docker_json', side_effect=self.fake_docker), \
             patch(__name__+'.container_identity', return_value=self.identity):
            target, identity = verified_target(self.config)
            connection = MagicMock()
            connection.execute.return_value.fetchone.return_value = (
                dict(identity, server_address='172.17.0.2'),)
            verify_connection(self.config, connection, target, identity)
            # Even another Docker-private address is insufficient.
            connection.execute.return_value.fetchone.return_value = (
                dict(identity, server_address='172.17.0.3'),)
            with self.assertRaises(ValueError):
                verify_connection(self.config, connection, target, identity)

    def test_cluster_query_requests_host_only_address(self):
        # PostgreSQL's inet::text includes /32 or /128; host(inet) returns
        # address-only text and still returns NULL for Unix-socket sessions.
        self.assertIn("'server_address', host(inet_server_addr())", CLUSTER_SQL)
        self.assertNotIn('inet_server_addr()::text', CLUSTER_SQL)

    def test_address_format_regression_preserves_strict_comparison(self):
        target = self.inspect_fixture()
        for expected, masked, wrong in (
            ('172.17.0.2', '172.17.0.2/32', '172.17.0.3'),
            ('fd00::2', 'fd00::2/128', 'fd00::3'),
        ):
            exact_target = dict(target, addresses=(expected,))
            connection = MagicMock()
            with patch(__name__+'.verified_target', return_value=(exact_target,self.identity)):
                connection.execute.return_value.fetchone.return_value = (
                    dict(self.identity,server_address=expected),)
                verify_connection(self.config,connection,exact_target,self.identity)
                for rejected in (masked, wrong, None, ''):
                    connection.execute.return_value.fetchone.return_value = (
                        dict(self.identity,server_address=rejected),)
                    with self.subTest(address=rejected), self.assertRaises(ValueError):
                        verify_connection(self.config,connection,exact_target,self.identity)

    def test_ipv6_loopback_binding(self):
        config = disposable_config({'HABRIVO_TEST_DISPOSABLE':DISPOSABLE_MARKER,
            'HABRIVO_TEST_POSTGRES_URL':'postgresql://foundation_test:x@[::1]:55432/habrivo_foundation_test'})
        for key in ('ports','bindings'):
            self.obj[key]['5432/tcp'][0]['HostIp'] = '::1'
        with patch(__name__+'.docker_json', side_effect=self.fake_docker):
            self.assertEqual(inspect_target(config)['addresses'], ('172.17.0.2',))

    def test_rejects_missing_or_unsafe_container_metadata(self):
        changes = [
            {'id':'short'}, {'name':'/habrivo-db'}, {'running':False},
            {'paused':True}, {'restarting':True}, {'started':''},
            {'image_ref':'postgres:17'}, {'marker':None}, {'image':'not-an-image-id'},
            {'network_mode':'host'}, {'privileged':True}, {'pid_mode':'host'},
            {'auto_remove':False}, {'binds':['/data:/var/lib/postgresql']},
            {'volumes_from':['another-container']}, {'tmpfs':{}},
            {'mounts':[{'Type':'volume','Destination':'/var/lib/postgresql'}]},
            {'ports':{}}, {'bindings':{}}, {'networks':{}},
        ]
        original = copy.deepcopy(self.obj)
        for change in changes:
            with self.subTest(change=list(change)):
                self.obj = dict(original, **change)
                with self.assertRaises(ValueError):
                    self.inspect_fixture()
        self.obj = original
        del self.obj['running']
        with self.assertRaises(ValueError):
            self.inspect_fixture()

    def test_rejects_wildcard_external_wrong_or_ambiguous_bindings(self):
        for ip, port in [('0.0.0.0','55432'), ('::','55432'), ('192.168.1.1','55432'),
                         ('127.0.0.1','5432'), ('127.0.0.1','55433')]:
            for key in ('ports','bindings'):
                self.obj[key] = {'5432/tcp':[{'HostIp':ip,'HostPort':port}]}
            with self.subTest(ip=ip, port=port), self.assertRaises(ValueError):
                self.inspect_fixture()
        for key in ('ports','bindings'):
            self.obj[key] = {'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'55432'}]*2}
        with self.assertRaises(ValueError):
            self.inspect_fixture()

    def test_rejects_unverified_image_and_unavailable_docker(self):
        for digests in (None, [], ['someone/postgres@sha256:'+'d'*64]):
            def response(*args):
                return digests if args[0] == 'image' else copy.deepcopy(self.obj)
            with patch(__name__+'.docker_json', side_effect=response), self.assertRaises(ValueError):
                inspect_target(self.config)
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired('docker', 10),
                        subprocess.CalledProcessError(1,'docker')):
            with patch('subprocess.run', side_effect=failure), self.assertRaises(ValueError):
                docker_json('container','inspect',CONTAINER_NAME)
        with patch('subprocess.run', return_value=MagicMock(stdout='not JSON')), self.assertRaises(ValueError):
            docker_json('container','inspect',CONTAINER_NAME)

    def test_rejects_changed_container_before_any_client_query(self):
        target = self.inspect_fixture()
        connection = MagicMock()
        with patch(__name__+'.verified_target', return_value=(dict(target, id='d'*64), self.identity)), \
             self.assertRaises(ValueError):
            verify_connection(self.config, connection, target, self.identity)
        connection.execute.assert_not_called()

    def test_rejects_cluster_version_role_directory_and_identity_mismatches(self):
        for change in ({'system_identifier':'unknown'}, {'database':'habrivo-db'},
                       {'role':'postgres'}, {'version':170000}, {'version':190000},
                       {'port':55432}, {'data_directory':'/data'}, {'in_recovery':True}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                validate_identity(dict(self.identity, **change))
        target = self.inspect_fixture()
        with patch(__name__+'.verified_target', return_value=(target, self.identity)):
            connection = MagicMock()
            for change in ({'system_identifier':'9999999999999999999'},
                           {'server_address':'10.0.0.1'}, {'server_address':None}):
                actual = dict(self.identity, server_address='172.17.0.2', **{
                    k:v for k,v in change.items() if k!='server_address'})
                if 'server_address' in change:
                    actual['server_address'] = change['server_address']
                connection.execute.return_value.fetchone.return_value = (actual,)
                with self.subTest(change=list(change)), self.assertRaises(ValueError):
                    verify_connection(self.config, connection, target, self.identity)

    def test_rejects_tcp_independent_identity_or_container_replacement(self):
        target = self.inspect_fixture()
        with patch(__name__+'.inspect_target', return_value=target), \
             patch(__name__+'.container_identity', return_value=dict(self.identity,server_address='172.17.0.2')), \
             self.assertRaises(ValueError):
            verified_target(self.config)
        with patch(__name__+'.inspect_target', side_effect=[target,dict(target,id='d'*64)]), \
             patch(__name__+'.container_identity', return_value=self.identity), \
             self.assertRaises(ValueError):
            verified_target(self.config)

    def test_failed_preflight_never_registers_destructive_cleanup(self):
        target = self.inspect_fixture()
        driver = MagicMock()
        connection = driver.connect.return_value
        env = {'HABRIVO_TEST_DISPOSABLE':DISPOSABLE_MARKER,
               'HABRIVO_TEST_POSTGRES_URL':self.config.url}
        # Reject identity, an existing client, or populated data. Each path must
        # close the connection but never create/drop a schema.
        for reason in ('identity','other_clients','populated'):
            instance = PostgresIntegrationTests('test_successful_dml_is_rolled_back_on_exception')
            connection.reset_mock()
            counts = [1] if reason == 'other_clients' else [0,1]
            connection.execute.return_value.fetchone.side_effect = [(n,) for n in counts]
            with patch.dict(os.environ, env, clear=True), \
                 patch('importlib.util.find_spec', return_value=object()), \
                 patch.dict(sys.modules, {'psycopg':driver}), \
                 patch(__name__+'.verified_target', return_value=(target,self.identity)), \
                 patch(__name__+'.verify_connection', side_effect=ValueError('identity mismatch') if reason=='identity' else None), \
                 patch.object(instance, 'drop_schema') as drop:
                with self.assertRaises((ValueError, AssertionError)):
                    instance.setUp()
                instance.doCleanups()
                drop.assert_not_called()
            connection.close.assert_called_once()
            for call in connection.execute.call_args_list:
                self.assertNotIn('CREATE SCHEMA', str(call.args[0]))
                self.assertNotIn('DROP SCHEMA', str(call.args[0]))

    def test_local_socket_and_container_exec_are_pinned(self):
        with patch.dict(os.environ, {'DOCKER_HOST':'tcp://remote:2375','DOCKER_CONTEXT':'remote'}), \
             patch('subprocess.run', return_value=MagicMock(stdout='{}')) as run:
            docker_json('container','inspect',CONTAINER_NAME)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:2], ['docker','--host='+DOCKER_SOCKET])
        self.assertNotIn('DOCKER_HOST', kwargs['env'])
        self.assertNotIn('DOCKER_CONTEXT', kwargs['env'])
        self.assertNotIn('shell', kwargs)
        target = self.inspect_fixture()
        with patch(__name__+'.docker_json', return_value=self.identity) as docker:
            container_identity(target)
        self.assertEqual(docker.call_args.args[:4], ('exec',target['id'],'env','-i'))
        self.assertIn('/var/run/postgresql', docker.call_args.args)


@unittest.skipUnless(os.environ.get('HABRIVO_TEST_POSTGRES_URL'),
                     'No explicit disposable local PostgreSQL URL; NOT VERIFIED')
class PostgresIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.config = disposable_config(os.environ)
        if importlib.util.find_spec('psycopg') is None:
            self.skipTest('Psycopg unavailable; PostgreSQL integration NOT VERIFIED')
        import psycopg
        from psycopg import sql
        self.psycopg, self.sql = psycopg, sql
        self.target, self.identity = verified_target(self.config)
        self.schema = 'foundation_' + uuid4().hex
        self.control = psycopg.connect(self.config.url, autocommit=True, connect_timeout=5,
                                       options='-c statement_timeout=12000 -c lock_timeout=8000')
        self.addCleanup(self.control.close)
        verify_connection(self.config, self.control, self.target, self.identity)
        # Fail closed on a populated/shared database before creating anything.
        self.assertEqual(self.control.execute("""SELECT count(*) FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid()
            AND backend_type='client backend'""").fetchone()[0], 0, 'Other database clients detected')
        self.assertEqual(self.control.execute("""SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'""").fetchone()[0], 0,
            'Test database must be empty of user relations')
        verify_connection(self.config, self.control, self.target, self.identity)
        self.control.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_schema)

    def drop_schema(self):
        verify_connection(self.config, self.control, self.target, self.identity)
        self.control.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def isolated_connection(self, config):
        db = connect(config)
        try:
            verify_connection(self.config, db.raw, self.target, self.identity)
            db.raw.execute(self.sql.SQL('SET search_path TO {}').format(self.sql.Identifier(self.schema)))
            db.raw.execute("SET statement_timeout='12s'")
            db.raw.execute("SET lock_timeout='8s'")
            db.raw.commit()
            return db
        except BaseException:
            db.close()
            raise

    def apply_migrations(self, directory=None):
        with patch('migrate.connect', side_effect=self.isolated_connection):
            if directory is None:
                migrate(self.config)
            else:
                with patch('migrate.MIGRATIONS', directory):
                    migrate(self.config)

    def ledger(self):
        return self.control.execute(self.sql.SQL(
            'SELECT version, checksum FROM {}.schema_migrations ORDER BY version'
        ).format(self.sql.Identifier(self.schema))).fetchall()

    def test_schema_creation_idempotency_dml_and_legacy_hash(self):
        from werkzeug.security import check_password_hash
        self.apply_migrations()
        before = self.ledger()
        self.apply_migrations()
        self.assertEqual(self.ledger(), before)
        db = self.isolated_connection(self.config)
        self.addCleanup(db.close)
        with db:
            tables = {r[0] for r in db.execute('SELECT tablename FROM pg_tables WHERE schemaname = ?', (self.schema,))}
            self.assertTrue({'users','companies','subscriptions','promo_usage','schema_migrations'} <= tables)
            user_id = db.execute('INSERT INTO users (name,email,password_hash) VALUES (?,?,?)', ('Legacy','legacy@example.invalid',LEGACY_HASH)).lastrowid
            company_id = db.execute('INSERT INTO companies (company) VALUES (:company)', {'company':'Example'}).lastrowid
            db.execute('UPDATE companies SET company=:company WHERE id=:id', {'company':'Updated','id':company_id})
            db.execute('INSERT INTO subscriptions (customer_id,plan,billing_cycle,amount_usd,status,payment_verified,expiry_date) VALUES (?,?,?,?,?,?,?)', (user_id,'starter','monthly',39,'active',1,'2099-01-01 00:00:00'))
            subscription = db.execute('SELECT id,plan FROM subscriptions WHERE customer_id=? AND payment_verified=1 AND expiry_date>?', (user_id,'2026-01-01 00:00:00')).fetchone()
            db.execute('INSERT INTO promo_usage (customer_id,subscription_id) VALUES (?,?)', (user_id,subscription['id']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM promo_usage').fetchone()[0], 1)
            stored = db.execute('SELECT password_hash FROM users WHERE id=?', (user_id,)).fetchone()['password_hash']
            self.assertEqual(stored, LEGACY_HASH)
            self.assertTrue(check_password_hash(stored, PASSWORD))
            self.assertEqual(db.execute('DELETE FROM companies WHERE id=?', (company_id,)).rowcount, 1)
        with self.assertRaises(self.psycopg.IntegrityError), db:
            db.execute('INSERT INTO users (name,email,password_hash) VALUES (?,?,?)', ('Duplicate','legacy@example.invalid',LEGACY_HASH))
        with self.assertRaises(self.psycopg.IntegrityError), db:
            db.execute("INSERT INTO subscriptions (customer_id,plan,billing_cycle,amount_usd) VALUES (1,'invalid','monthly',39)")

    def test_exact_application_indexes_from_catalogs(self):
        self.apply_migrations()
        rows = self.control.execute('''SELECT t.relname, i.relname, am.amname,
                   x.indisunique, x.indisprimary, x.indisvalid, x.indisready,
                   x.indnkeyatts, x.indnatts,
                   pg_get_expr(x.indpred, x.indrelid),
                   pg_get_expr(x.indexprs, x.indrelid),
                   ARRAY(SELECT pg_get_indexdef(x.indexrelid, k, true)
                         FROM generate_series(1, x.indnatts) k ORDER BY k),
                   pg_get_indexdef(x.indexrelid)
            FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid
            JOIN pg_class t ON t.oid=x.indrelid
            JOIN pg_namespace n ON n.oid=t.relnamespace
            JOIN pg_am am ON am.oid=i.relam
            WHERE n.nspname=%s AND t.relname IN
                ('users','companies','subscriptions','promo_usage')
            ORDER BY i.relname''', (self.schema,)).fetchall()
        expected = {
            'users_pkey': ('users', True, True, ['id']),
            'users_email_key': ('users', True, False, ['email']),
            'companies_pkey': ('companies', True, True, ['id']),
            'subscriptions_pkey': ('subscriptions', True, True, ['id']),
            'promo_usage_pkey': ('promo_usage', True, True, ['id']),
            'idx_subscriptions_customer': ('subscriptions', False, False, ['customer_id']),
            'idx_promo_usage_customer_date': ('promo_usage', False, False, ['customer_id','created_at']),
        }
        self.assertEqual({r[1] for r in rows}, set(expected))
        for table, name, method, unique, primary, valid, ready, keys, total, predicate, expressions, columns, definition in rows:
            etable, eunique, eprimary, ecolumns = expected[name]
            self.assertEqual((table, method, unique, primary, valid, ready, keys, total, predicate, expressions, columns),
                             (etable, 'btree', eunique, eprimary, True, True,
                              len(ecolumns), len(ecolumns), None, None, ecolumns))
            expected_definition = ('CREATE ' + ('UNIQUE ' if eunique else '') +
                'INDEX ' + name + ' ON ' + self.schema + '.' + table +
                ' USING btree (' + ', '.join(ecolumns) + ')')
            self.assertEqual(definition, expected_definition)

    def test_successful_dml_is_rolled_back_on_exception(self):
        self.apply_migrations()
        db = self.isolated_connection(self.config)
        self.addCleanup(db.close)
        with self.assertRaisesRegex(RuntimeError, 'intentional rollback'):
            with db:
                db.execute('INSERT INTO companies (company) VALUES (?)', ('rollback-marker',))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM companies').fetchone()[0], 1)
                raise RuntimeError('intentional rollback')
        # Independent connection verifies nothing committed; then reuse original.
        fresh = self.isolated_connection(self.config)
        try:
            self.assertEqual(fresh.execute('SELECT COUNT(*) FROM companies').fetchone()[0], 0)
        finally:
            fresh.close()
        with db:
            db.execute('INSERT INTO companies (company) VALUES (?)', ('committed-marker',))
        self.assertEqual(self.control.execute(self.sql.SQL('SELECT company FROM {}.companies').format(
            self.sql.Identifier(self.schema))).fetchall(), [('committed-marker',)])

    def test_failing_ddl_rolls_back_schema_and_ledger_updates(self):
        self.apply_migrations()
        before = self.ledger()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            # 0002 is recorded before 0003 fails; both must roll back together.
            (path/'0002_probe.sql').write_text('CREATE TABLE ddl_probe (id INTEGER);')
            (path/'0003_fail.sql').write_text('CREATE TABLE ddl_partial (id INTEGER); '
                                           'CREATE TABLE ddl_partial (id INTEGER);')
            with self.assertRaises(self.psycopg.errors.DuplicateTable):
                self.apply_migrations(path)
        self.assertEqual(self.ledger(), before)
        for table in ('ddl_probe','ddl_partial'):
            self.assertIsNone(self.control.execute('SELECT to_regclass(%s)',
                             (self.schema+'.'+table,)).fetchone()[0])
        # Database remains usable after the failed migration transaction.
        self.apply_migrations()
        self.assertEqual(self.ledger(), before)

    def test_modified_checksum_rejected_by_real_ledger(self):
        self.apply_migrations()
        before = self.ledger()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for source in MIGRATIONS.glob('[0-9]*.sql'):
                (path/source.name).write_text(source.read_text()+'\n-- changed checksum\n')
            with self.assertRaisesRegex(RuntimeError, 'Previously applied migration has changed'):
                self.apply_migrations(path)
        self.assertEqual(self.ledger(), before)
        self.assertEqual(before, [(p.name, hashlib.sha256(p.read_text().encode()).hexdigest())
                                 for p in sorted(MIGRATIONS.glob('[0-9]*.sql'))])
        self.apply_migrations()
        self.assertEqual(self.ledger(), before)

    def test_concurrent_runners_serialize_at_advisory_lock(self):
        first_locked = threading.Event()
        release_first = threading.Event()
        second_connected = threading.Event()
        state_lock = threading.Lock()
        state = {'count': 0}
        original_factory = self.isolated_connection

        class ObserveRaw:
            def __init__(self, raw, number):
                self.raw, self.number = raw, number

            def __getattr__(self, name):
                return getattr(self.raw, name)

            def execute(self, query, *args, **kwargs):
                result = self.raw.execute(query, *args, **kwargs)
                # Pause only after the real first runner has acquired its lock.
                if self.number == 1 and isinstance(query, str) and 'pg_advisory_xact_lock' in query:
                    first_locked.set()
                    if not release_first.wait(15):
                        raise TimeoutError('Test did not release first migration runner')
                return result

        def factory(config):
            db = original_factory(config)
            with state_lock:
                state['count'] += 1
                number = state['count']
                if number == 2:
                    state['second_pid'] = db.raw.info.backend_pid
                    second_connected.set()
            db.raw = ObserveRaw(db.raw, number)
            return db

        # Patch once outside both threads; only connection observation is wrapped.
        # Real runner SQL, advisory locks, DDL and commits are not mocked.
        with patch('migrate.connect', side_effect=factory), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(migrate, self.config)
            try:
                self.assertTrue(first_locked.wait(3), 'First runner did not acquire lock')
                second = executor.submit(migrate, self.config)
                self.assertTrue(second_connected.wait(3), 'Second runner did not connect')
                deadline = time.monotonic()+3
                blocked = False
                while time.monotonic() < deadline:
                    locks = self.control.execute('''SELECT granted FROM pg_locks
                        WHERE locktype='advisory' AND pid=%s AND classid=0
                        AND objid=724182603 AND objsubid=1 AND mode='ExclusiveLock' ''',
                        (state['second_pid'],)).fetchall()
                    if (False,) in locks:
                        blocked = True
                        break
                    if second.done():
                        second.result()  # Surface real failures instead of hiding them.
                        break
                    time.sleep(0.02)
                self.assertTrue(blocked, 'Second runner was not observed waiting for the migration lock')
                self.assertFalse(second.done(), 'Second runner completed while first held lock')
            finally:
                release_first.set()
            first.result(timeout=15)
            second.result(timeout=15)
        self.assertEqual(self.ledger(), [(p.name, hashlib.sha256(p.read_text().encode()).hexdigest())
                                        for p in sorted(MIGRATIONS.glob('[0-9]*.sql'))])
        self.apply_migrations()  # A third run must also be idempotent.


if __name__ == '__main__':
    unittest.main()
