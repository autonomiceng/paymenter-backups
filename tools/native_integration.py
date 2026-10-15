#!/usr/bin/env python3
"""Pinned native Paymenter fixture and disposable MariaDB restore tests.

No host database, live application, cron table or provider account is touched.
"""
import argparse
import base64
import contextlib
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.parse

from fetch_fixture import fetch
from fixture_environment import child_environment

ROOT = pathlib.Path(__file__).resolve().parents[1]


def fixture_http():
    # A caller's proxy variables cannot send fixture requests to another host.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def execute(command, cwd=None, input=None, env=None, timeout=180):
    result = subprocess.run(list(map(str, command)), cwd=cwd, input=input, env=child_environment(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Fixture uses only fake secrets; limit diagnostics to bounded test output.
        raise RuntimeError('command failed: ' + str(command[:3]) + '\n' + result.stdout.decode(errors='replace')[-4000:] + result.stderr.decode(errors='replace')[-4000:])
    return result.stdout


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def hashes(app):
    prefixes = ['app', 'bootstrap', 'config', 'database', 'routes', 'resources', 'themes']
    result = {}
    for prefix in prefixes:
        for path in (app / prefix).rglob('*'):
            if path.is_file() and 'cache' not in path.relative_to(app).parts:
                result[str(path.relative_to(app))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ['composer.json', 'composer.lock', 'artisan', 'public/index.php']:
        result[name] = hashlib.sha256((app / name).read_bytes()).hexdigest()
    return result


@contextlib.contextmanager
def maria(work):
    data = work / 'mariadb'
    data.mkdir(mode=0o700)
    sock = work / 'db.sock'
    execute(['mariadb-install-db', '--no-defaults', '--datadir=' + str(data), '--auth-root-authentication-method=normal', '--skip-test-db'])
    log = (work / 'mariadb.log').open('wb')
    command = [shutil.which('mariadbd') or '/usr/sbin/mariadbd', '--no-defaults', '--datadir=' + str(data), '--socket=' + str(sock), '--pid-file=' + str(work / 'db.pid'), '--skip-networking', '--log-error=' + str(work / 'server-error.log')]
    if os.getuid() == 0:
        command.append('--user=root')
    process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True, env=child_environment())
    try:
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError('disposable mariadb stopped')
            if sock.exists():
                try:
                    execute(['mariadb', '--no-defaults', '--socket=' + str(sock), '-uroot', '-e', 'SELECT 1'], timeout=2)
                    break
                except RuntimeError:
                    pass
            time.sleep(.1)
        else:
            raise RuntimeError('disposable MariaDB readiness timeout')
        yield sock
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        log.close()


@contextlib.contextmanager
def http_server(app, php, http_port, work):
    log = (work / ('http-' + str(http_port) + '.log')).open('wb')
    process = subprocess.Popen([php, '-S', '127.0.0.1:' + str(http_port), '-t', str(app / 'public'), str(app / 'public/index.php')], cwd=app, stdout=log, stderr=log, start_new_session=True, env=child_environment())
    try:
        for _ in range(100):
            try:
                fixture_http().open('http://127.0.0.1:' + str(http_port) + '/login', timeout=1).read()
                break
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError('fixture HTTP process stopped')
                time.sleep(.1)
        yield process
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True)
    parser.add_argument('--archive')
    parser.add_argument('--php', default=shutil.which('php'))
    parser.add_argument('--composer', default='/usr/bin/composer', help='Composer PHP script, invoked with the selected PHP binary')
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--skip-restore', action='store_true', help='Adapter development only; does not complete release checks')
    args = parser.parse_args()
    workbase = ROOT / '.native-work'
    workbase.mkdir(exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix='native-integration-', dir=workbase))
    # This regression runs every full drill under deliberately hostile parent
    # configuration. Its socket/cache paths are inside this disposable workroot,
    # so even a broken boundary cannot connect to an existing database or cache.
    hostile = {
        'DB_URL': 'mysql://forbidden:forbidden@localhost/inherited_do_not_use?' + urllib.parse.urlencode({'unix_socket': str(work / 'never-a-database.sock')}),
        'DB_DATABASE': 'inherited_do_not_use',
        'DB_SOCKET': str(work / 'never-a-database.sock'),
        'DB_HOST': 'inherited.invalid',
        'DB_USERNAME': 'inherited_do_not_use',
        'DB_PASSWORD': 'INHERITED-SECRET-CANARY',
        'APP_ENV': 'production',
        'APP_KEY': 'INHERITED-INVALID-KEY-CANARY',
        'APP_URL': 'http://127.0.0.1:1',
        'APP_TIMEZONE': 'Pacific/Honolulu',
        'APP_CONFIG_CACHE': str(work / 'forbidden-config-cache.php'),
        'APP_ROUTES_CACHE': str(work / 'forbidden-routes-cache.php'),
        'MYSQL_PWD': 'INHERITED-MYSQL-PASSWORD-CANARY',
        'MYSQL_HOME': str(work / 'forbidden-mysql-config'),
        'MYSQL_UNIX_PORT': str(work / 'never-a-database.sock'),
        'CACHE_STORE': 'redis',
        'QUEUE_CONNECTION': 'redis',
        'SESSION_DRIVER': 'redis',
    }
    previous_environment = {key: os.environ.get(key) for key in hostile}
    os.environ.update(hostile)
    try:
        app = fetch(work / 'app', args.archive or workbase / 'paymenter-v1.5.9.tar.gz')
        original = hashes(app)
        private = work / 'private'
        private.mkdir(mode=0o700)
        with maria(work) as sock:
            execute(['mariadb', '--no-defaults', '--socket=' + str(sock), '-uroot', '-e', "CREATE DATABASE paymenter_fixture; CREATE DATABASE paymenter_restore; CREATE USER 'paymenter_fixture_user'@'localhost' IDENTIFIED BY 'paymenter-fixture-password'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON paymenter_fixture.* TO 'paymenter_fixture_user'@'localhost'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON paymenter_restore.* TO 'paymenter_fixture_user'@'localhost';"])
            sys.path.insert(0, str(ROOT / 'tests/integration'))
            from database_visibility import check_visibility
            visibility = check_visibility(work, sock)
            http_port = port()
            native_url = 'http://127.0.0.1:' + str(http_port)
            app_key = 'base64:' + base64.b64encode(os.urandom(32)).decode()
            env = {'APP_NAME': 'Fixture', 'APP_ENV': 'local', 'APP_KEY': app_key, 'APP_DEBUG': 'false', 'APP_URL': 'http://127.0.0.1:1', 'APP_TIMEZONE': 'America/New_York', 'APP_MAINTENANCE_DRIVER': 'file', 'DB_CONNECTION': 'mysql', 'DB_SOCKET': str(sock), 'DB_HOST': 'deliberately-unused.invalid', 'DB_DATABASE': 'deliberately_unused_database', 'DB_URL': 'mysql://paymenter_fixture_user:paymenter-fixture-password@localhost/paymenter_fixture?' + urllib.parse.urlencode({'unix_socket': str(sock)}), 'DB_USERNAME': 'root', 'DB_PASSWORD': '', 'DB_COLLATION': 'utf8mb4_unicode_ci', 'CACHE_STORE': 'file', 'QUEUE_CONNECTION': 'sync', 'SESSION_DRIVER': 'file', 'FILESYSTEM_DISK': 'local', 'MAIL_MAILER': 'log', 'TELEMETRY_ENABLED': 'false'}
            # Direct removal precedes the first Laravel bootstrap.
            for cached in (app / 'bootstrap/cache').glob('*.php'):
                if cached.name not in ('packages.php', 'services.php'):
                    cached.unlink()
            (app / '.env').write_text(''.join(key + '=' + value + '\n' for key, value in env.items()))
            execute([args.php, 'artisan', 'migrate', '--force', '--seed'], cwd=app)
            execute([args.php, 'artisan', 'passport:keys', '--force'], cwd=app)
            checker = ROOT / 'tests/integration/native_checks.php'
            execute([args.php, checker, app, 'seed', native_url])
            upload = work / 'native-upload.zip'
            shutil.copy2(args.release, upload)
            # Record the bytes actually consumed by native installation, even
            # if a developer rebuilds the original release path during the drill.
            release_sha256 = hashlib.sha256(upload.read_bytes()).hexdigest()
            execute([args.php, checker, app, 'upload', upload])
            # Cold and public read-only workflows must leave an absent root absent.
            bridge = app / 'extensions/Others/Backups/bridge.php'
            absent = work / 'never-created'
            cold_absent = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', absent]))
            if cold_absent['installation_uuid'] is not None or cold_absent['credentials'] or absent.exists():
                raise RuntimeError('cold read provisions absent private state')
            read_cli = [sys.executable, '-I', '-B', app / 'extensions/Others/Backups/runtime/cli.py', '--app', app, '--private-root', absent, '--bridge', bridge, '--php', args.php]
            for read_action in [['config', 'show'], ['plan'], ['status']]:
                execute(read_cli + read_action)
            if absent.exists():
                raise RuntimeError('public read workflow provisions absent private state')
            rejected = work / 'forbidden-runtime-output.json'
            with rejected.open('wb') as handle:
                regular_output = subprocess.run([args.php, str(bridge), 'read', '--app', str(app), '--private-root', str(absent)], stdout=handle, stderr=subprocess.PIPE, env=child_environment())
            if regular_output.returncode == 0 or rejected.stat().st_size or b'private-pipe-required' not in regular_output.stderr:
                raise RuntimeError('internal secret envelope permits ordinary file output')
            # Fresh native process discovers command and actual schedule.
            settings = {'private_root': str(private), 'python': sys.executable, 'schedule_time': '02:00', 'schedule_weekdays': '[0,2]', 'local_byte_budget': 2 * 1024**3, 'minimum_free_bytes': 1024**2, 'drain_seconds': 1, 'capture_timeout': 120, 'no_concurrent_ddl_acknowledged': True, 'cooperative_writers_acknowledged': True, 'external_state_acknowledged': True}
            execute([args.php, 'artisan', 'backups:config-set'], cwd=app, input=json.dumps(settings).encode())
            php_alias = work / 'php-alias'
            php_alias.symlink_to(pathlib.Path(args.php).resolve())
            nonexecutable = work / 'not-executable-python'
            nonexecutable.write_text('#!/usr/bin/python3\n')
            nonexecutable.chmod(0o600)
            restricted_policy = str(work) + ':' + str(checker.parent) + ':/tmp'
            open_basedir_results = json.loads(execute([args.php, '-d', 'open_basedir=' + restricted_policy,
                                                     checker, app, 'open-basedir', php_alias, nonexecutable]))
            schedule = execute([args.php, 'artisan', 'schedule:list'], cwd=app)
            if b'paymenter-backups-tick' not in schedule:
                raise RuntimeError('native lazy tick not discovered')
            guard = json.loads(execute([args.php, 'artisan', 'backups:guard'], cwd=app))
            bridge = app / 'extensions/Others/Backups/bridge.php'
            cold = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
            expected_fp = hashlib.sha256(json.dumps(cold['billing'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            if cold['database']['name'] != 'paymenter_fixture' or cold['database']['host'] != 'localhost' or cold['database']['socket'] != str(sock):
                raise RuntimeError('cold DB_URL parser differs from native connection')
            before_tls = (app / '.env').read_text()
            (app / '.env').write_text(before_tls + 'MYSQL_ATTR_SSL_CA=/fixture/required-ca.pem\n')
            tls_preflight = subprocess.run([args.php, str(bridge), 'read', '--app', str(app), '--private-root', str(private)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_environment())
            (app / '.env').write_text(before_tls)
            if tls_preflight.returncode == 0 or b'database-tls-unsupported' not in tls_preflight.stderr:
                raise RuntimeError('native TLS requirement silently downgraded')
            if guard['fingerprint'] != expected_fp:
                raise RuntimeError('billing fingerprint mismatch')
            creds = {'access_key': 'ACCESS-CREDENTIAL-CANARY', 'secret_key': 'SECRET-CREDENTIAL-CANARY', 'session_token': 'TOKEN-CREDENTIAL-CANARY'}
            execute([args.php, bridge, 'credentials-write', '--app', app, '--private-root', private], input=json.dumps(creds).encode())
            results = json.loads(execute([args.php, checker, app, 'check']))
            # Cached config must be honoured without providers; native URL/TZ
            # overrides intentionally differ from .env in this fixture.
            execute([args.php, 'artisan', 'config:cache'], cwd=app)
            cached = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
            if cached['framework']['app_url'] != native_url or cached['framework']['timezone'] != 'UTC':
                raise RuntimeError('cold cached native overrides differ')
            (app / 'bootstrap/cache/config.php').unlink()
            cli = [sys.executable, '-I', '-B', app / 'extensions/Others/Backups/runtime/cli.py', '--app', app, '--private-root', private, '--bridge', bridge, '--php', args.php]
            # Exercise public JSON-stdin credentials and concurrent direct bridge
            # writers on one encrypted file, without touching host TLS/services.
            probe_url = 'https://fixture.example.invalid/billing'
            execute([args.php, checker, app, 'app-url', probe_url])
            probe_creds = {'origin': 'https://fixture.example.invalid:443', 'username': 'CLI-PROBE-CREDENTIAL-CANARY', 'password': 'CLI-PASSWORD-CREDENTIAL-CANARY'}
            try:
                public = execute(cli + ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': probe_creds}).encode())
                if b'CREDENTIAL-CANARY' in public:
                    raise RuntimeError('CLI credential mutation leaked input')
                # Native previous-key read, then namespace write, must migrate
                # the entire vault to the current key/fingerprint without losing
                # S3. Rotate back in this fixture before any normal app startup.
                import hmac
                original_env = (app / '.env').read_text()
                rotated_key = 'base64:' + base64.b64encode(os.urandom(32)).decode()
                (app / '.env').write_text(original_env.replace('APP_KEY=' + app_key + '\n', 'APP_KEY=' + rotated_key + '\n') + 'APP_PREVIOUS_KEYS=' + app_key + '\n')
                try:
                    execute(cli + ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': probe_creds}).encode())
                    rotated = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
                    stored = json.loads((private / 'credentials.json').read_text())
                    expected_key_fp = hmac.new(base64.b64decode(rotated_key[7:]), ('paymenter-backups-key-v1:' + stored['installation_uuid']).encode(), hashlib.sha256).hexdigest()
                    if rotated['credentials'] != creds or stored['key_fingerprint'] != expected_key_fp:
                        raise RuntimeError('previous-key namespace write did not migrate whole vault')
                finally:
                    (app / '.env').write_text(original_env + 'APP_PREVIOUS_KEYS=' + rotated_key + '\n')
                    execute([args.php, bridge, 'credentials-write', '--app', app, '--private-root', private], input=json.dumps(creds).encode())
                    (app / '.env').write_text(original_env)
                results['previous_key_write_migrates_vault'] = True
                # Both subprocesses are blocked on the same native flock, then
                # must reread after acquisition so independent updates survive.
                concurrent_s3 = dict(creds, session_token='CONCURRENT-S3-CREDENTIAL-CANARY')
                concurrent_probe = dict(probe_creds, username='CONCURRENT-PROBE-CREDENTIAL-CANARY')
                import fcntl
                with (private / 'credentials.lock').open('r+b') as credential_lock:
                    fcntl.flock(credential_lock, fcntl.LOCK_EX)
                    children = []
                    for verb, values in [('credentials-write', concurrent_s3), ('maintenance-probe-credentials-write', concurrent_probe)]:
                        child = subprocess.Popen([args.php, str(bridge), verb, '--app', str(app), '--private-root', str(private)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_environment())
                        child.stdin.write(json.dumps(values).encode())
                        child.stdin.close()
                        child.stdin = None
                        children.append(child)
                    time.sleep(0.2)
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError('credential writer bypasses shared lock')
                    fcntl.flock(credential_lock, fcntl.LOCK_UN)
                    for child in children:
                        output, error = child.communicate(timeout=30)
                        if child.returncode or b'CREDENTIAL-CANARY' in output + error:
                            raise RuntimeError('concurrent credential writer failed')
                merged = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
                if merged['credentials'] != concurrent_s3 or merged['maintenance_probe_credentials']['username'] != concurrent_probe['username']:
                    raise RuntimeError('concurrent writer lost credential namespace')
                public = execute(cli + ['config', 'show'])
                if b'CREDENTIAL-CANARY' in public:
                    raise RuntimeError('CLI credential display leaked input')
                execute(cli + ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': {'clear': True}}).encode())
                cleared = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
                if cleared['credentials'] != concurrent_s3 or cleared['maintenance_probe_credentials']:
                    raise RuntimeError('CLI clear lost S3 credentials')
                # Check legacy S3 reads and whole-payload writes with a probe namespace.
                execute(cli + ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': probe_creds}).encode())
                legacy_dir = work / 'legacy-s3-credentials-bridge'
                legacy_dir.mkdir(mode=0o700)
                shutil.copy2(ROOT / 'tests/integration/fixtures/legacy-s3-credentials-bridge.php', legacy_dir / 'bridge.php')
                for dependency in ['PrivateStore.php', 'Settings.php']:
                    shutil.copy2(app / 'extensions/Others/Backups' / dependency, legacy_dir / dependency)
                legacy_command = [args.php, legacy_dir / 'bridge.php']
                legacy_read = json.loads(execute(legacy_command + ['read', '--app', app, '--private-root', private]))
                if legacy_read['credentials']['access_key'] != concurrent_s3['access_key'] or not legacy_read['credentials'].get('maintenance_probe'):
                    raise RuntimeError('Legacy S3 bridge cannot read credentials with additional namespaces')
                execute(legacy_command + ['credentials-write', '--app', app, '--private-root', private], input=json.dumps(concurrent_s3).encode())
                downgraded = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
                if downgraded['credentials'] != concurrent_s3 or downgraded['maintenance_probe_credentials']:
                    raise RuntimeError('historical downgrade semantics changed')
                results['legacy_s3_credentials_readable'] = True
                results['legacy_s3_write_erases_probe'] = True
                results['probe_cli_redacted'] = True
                results['credential_writer_serialized'] = True
            finally:
                execute([args.php, checker, app, 'app-url', native_url])
            execute(cli + ['config', 'show'])
            execute(cli + ['schedule', 'show'])
            execute(cli + ['config', 'set'], input=json.dumps({'settings': {'retention_keep': 5}}).encode())
            execute(cli + ['schedule', 'set'], input=json.dumps({'schedule_frequency': 'weekly', 'schedule_time': '02:30', 'schedule_weekdays': [0, 2], 'schedule_timezone': 'UTC'}).encode())
            execute(cli + ['schedule', 'enable'])
            execute(cli + ['schedule', 'disable'])
            written = json.loads(execute(cli + ['schedule', 'show']))
            if written['enabled'] or written['schedule_weekdays'] != [0, 2] or written['schedule_time'] != '02:30':
                raise RuntimeError('native CLI schedule write did not persist')
            execute(cli + ['plan'])
            execute([args.php, checker, app, 'billing', time.strftime('%H:%M', time.gmtime())])
            current_guard = json.loads(execute([args.php, 'artisan', 'backups:guard'], cwd=app))
            minute_start = int(time.time()) // 60 * 60
            if minute_start not in current_guard['instants']:
                raise RuntimeError('billing guard omits current minute')
            current_plan = json.loads(execute(cli + ['plan']))
            if 'billing-window-protected' not in current_plan['admission_errors']:
                raise RuntimeError('current billing minute admits full hold')
            execute([args.php, checker, app, 'billing', '23:59'])
            execute([args.php, 'artisan', 'backups:guard'], cwd=app)
            if hashes(app) != original:
                raise RuntimeError('native application source/Composer changed')
            if args.skip_restore:
                print(json.dumps({'database_visibility': visibility, 'adapter_checks': results, 'restore': 'explicitly skipped', 'fixture': str(work)}))
                return
            lock = work / 'scheduler.lock'
            lock.touch(mode=0o600)
            fakebin = work / 'fixture-bin'
            fakebin.mkdir(mode=0o700)
            cron = fakebin / 'crontab'
            cron.write_text('#!/bin/sh\ncat <<\'CRON\'\n* * * * * flock -n ' + str(lock) + ' ' + args.php + ' ' + str(app / 'artisan') + ' schedule:run\nCRON\n')
            cron.chmod(0o700)
            execute([args.php, 'artisan', 'backups:config-set'], cwd=app, input=json.dumps({'job_lock_paths': json.dumps([str(lock)])}).encode())
            execute([args.php, 'artisan', 'backups:guard'], cwd=app)
            # Adapter envelope instrumentation is confined to this fixture. The
            # production bridge has no configurable crontab override.
            fixture_bridge = work / 'fixture-bridge.php'
            fixture_bridge.write_text('<?php ob_start(); require ' + json.dumps(str(bridge)) + '; $value=json_decode(ob_get_clean(),true); $value["tools"]["crontab"]=' + json.dumps(str(cron)) + '; echo json_encode($value)."\\n";')
            capture_cli = cli[:]
            capture_cli[capture_cli.index('--bridge') + 1] = fixture_bridge
            execution_env = child_environment({'PATH': str(fakebin) + ':' + child_environment()['PATH']})
            with http_server(app, args.php, http_port, work):
                request = urllib.request.Request(native_url + '/paymenter/update', data=b'{}', headers={'Content-Type': 'application/json'})
                try:
                    fixture_http().open(request, timeout=5)
                    raise RuntimeError('native Livewire request missing CSRF accepted')
                except urllib.error.HTTPError as error:
                    if error.code != 419:
                        raise RuntimeError('unexpected native CSRF response: ' + str(error.code))
                execute(capture_cli + ['run', '--execute'], env=execution_env, timeout=300)
            runs = list((private / 'runs').glob('*/journal.json'))
            journals = [json.loads(path.read_text()) for path in runs]
            successful = [journal for journal in journals if journal.get('local_complete')]
            if len(successful) != 1:
                raise RuntimeError('full native checkpoint incomplete: ' + str(journals))
            run = successful[0]
            forbidden_export = work / 'additional-site-public' / 'forbidden-recovery-kit'
            denied_export = subprocess.run(cli + ['export', run['run_id'], '--output', str(forbidden_export)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_environment())
            if denied_export.returncode == 0 or forbidden_export.exists():
                raise RuntimeError('offline export ignores native GUI document-root policy')
            inspected = execute(cli + ['verify', run['run_id'], '--show'])
            fake_auth_hash = hashlib.sha1(hashlib.sha1(b'paymenter-fixture-password').digest()).hexdigest().upper().encode()
            if fake_auth_hash in inspected or b'paymenter-fixture-password' in inspected:
                raise RuntimeError('native manifest leaks SHOW GRANTS authentication hash')
            archive = pathlib.Path(run['archive_path'])
            payload = work / 'restored-payload'
            payload.mkdir()
            with tarfile.open(archive) as backup:
                backup.extractall(payload)
            restored = work / 'restored-app'
            restored.mkdir()
            with tarfile.open(payload / 'app.tar') as files:
                files.extractall(restored)
            if not (restored / 'artisan').exists():
                # Preserve the artifact's documented root name when present.
                candidates = list(restored.glob('*/artisan'))
                if len(candidates) != 1:
                    raise RuntimeError('unexpected full app archive layout')
                restored = candidates[0].parent
            execute(['mariadb', '--no-defaults', '--socket=' + str(sock), '-uroot', 'paymenter_restore'], input=(payload / 'database.sql').read_bytes())
            restored_env = (restored / '.env').read_text().replace('DB_DATABASE=deliberately_unused_database', 'DB_DATABASE=paymenter_restore').replace('/paymenter_fixture?', '/paymenter_restore?')
            if 'APP_KEY=' + app_key not in restored_env:
                raise RuntimeError('APP_KEY not preserved in archive')
            restored_port = port()
            restored_env = restored_env.replace(env['APP_URL'], 'http://127.0.0.1:' + str(restored_port))
            (restored / '.env').write_text(restored_env)
            restored_private = work / 'restored-private'
            restored_private.mkdir(mode=0o700)
            execute([args.php, checker, restored, 'restore-prepare', 'http://127.0.0.1:' + str(restored_port), restored_private])
            restored_results = json.loads(execute([args.php, checker, restored, 'restore']))
            status = execute([args.php, 'artisan', 'migrate:status'], cwd=restored)
            if b'Pending' in status:
                raise RuntimeError('restored native migration pending')
            with http_server(restored, args.php, restored_port, work):
                login = fixture_http().open('http://127.0.0.1:' + str(restored_port) + '/login', timeout=10).read()
                if b'password' not in login.lower():
                    raise RuntimeError('restored native login missing')
            if hashes(app) != original:
                raise RuntimeError('native source changed during capture')
            if (work / 'forbidden-config-cache.php').exists() or (work / 'forbidden-routes-cache.php').exists():
                raise RuntimeError('inherited APP cache path was used')
            result = {'hostile_parent_environment_ignored': True, 'database_visibility': visibility, 'adapter_checks': results, 'open_basedir': open_basedir_results, 'restore': restored_results, 'full_artifact_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'release_sha256': release_sha256, 'native_login': True, 'native_migrations': 'no pending', 'native_source_unchanged': True}
            (workbase / 'native-integration-last.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
    finally:
        for key, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        if not args.keep:
            shutil.rmtree(work)


if __name__ == '__main__':
    main()
