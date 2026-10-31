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
import time
import urllib.request
import urllib.parse

from fetch_fixture import fetch
from fixture_environment import child_environment, private_work_directory

ROOT = pathlib.Path(__file__).resolve().parents[1]


def fixture_http():
    # A caller's proxy variables cannot send fixture requests to another host.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def require_maria_socket_path(socket_path):
    if not isinstance(socket_path, str):
        raise TypeError('MariaDB socket path must be a string')
    return socket_path


def execute(command, cwd=None, input=None, env=None, timeout=180):
    result = subprocess.run(list(map(str, command)), cwd=cwd, input=input, env=child_environment(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Fixture uses only fake secrets; limit diagnostics to bounded test output.
        raise RuntimeError('command failed: ' + str(command[:3]) + '\n' + result.stdout.decode(errors='replace')[-4000:] + result.stderr.decode(errors='replace')[-4000:])
    return result.stdout


def execute_env_only_database_parent(command, database_environment, cwd=None, input=None, timeout=180):
    """Launch a fresh PHP parent with DB settings absent from .env and cache."""
    expected = {'DB_CONNECTION', 'DB_HOST', 'DB_PORT', 'DB_DATABASE', 'DB_USERNAME', 'DB_PASSWORD', 'DB_PREFIX'}
    if set(database_environment) != expected:
        raise ValueError('env-only database fixture has unexpected keys')
    environment = child_environment()
    environment.update(database_environment)
    result = subprocess.run(list(map(str, command)), cwd=cwd, input=input, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError('env-only database command failed: ' + str(command[:3]) + '\n'
                           + result.stdout.decode(errors='replace')[-4000:]
                           + result.stderr.decode(errors='replace')[-4000:])
    return result.stdout


def guarded_write(cli, arguments, **kwargs):
    """Refresh billing state before fixture captures; configuration saves do not need it."""
    if arguments and arguments[0] == 'run':
        app = cli[cli.index('--app') + 1]
        php = cli[cli.index('--php') + 1]
        execute([php, 'artisan', 'backups:guard'], cwd=app)
    return execute(cli + arguments, **kwargs)


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
    db_port = port()
    execute(['mariadb-install-db', '--no-defaults', '--datadir=' + str(data), '--auth-root-authentication-method=normal', '--skip-test-db'])
    log = (work / 'mariadb.log').open('wb')
    command = [shutil.which('mariadbd') or '/usr/sbin/mariadbd', '--no-defaults', '--datadir=' + str(data), '--socket=' + str(sock), '--pid-file=' + str(work / 'db.pid'), '--bind-address=127.0.0.1', '--port=' + str(db_port), '--log-error=' + str(work / 'server-error.log')]
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
        yield require_maria_socket_path(str(sock)), db_port
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def promote_and_run(cli, private, action):
    """Launch exactly the one pending request of this action, as the tick would."""
    pending = [json.loads(path.read_text()) for path in (private / 'requests').glob('*.json')]
    pending = [item for item in pending if item['status'] == 'pending' and item['action'] == action]
    if len(pending) != 1:
        raise RuntimeError('expected one pending ' + action + ' request')
    item = dict(pending[0], status='launching', attempts=pending[0]['attempts'] + 1)
    target = private / 'requests' / (item['request_id'] + '.json')
    temporary = target.with_name('.' + target.name + '.fixture')
    temporary.write_text(json.dumps(item))
    temporary.chmod(0o600)
    temporary.replace(target)
    result = json.loads(execute(cli + ['request-worker', item['request_id'], '--execute'], timeout=300))
    if result['status'] != 'complete':
        raise RuntimeError(action + ' request did not complete: ' + str(result))
    return result


def admin_history(args, app, checker, cli, private, run, native_url, work):
    """Admin history, download, verification and per-backup delete against real archives."""
    archive = pathlib.Path(run['archive_path'])
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    ui = [args.php, checker, app, 'ui-history']
    execute(ui + ['render', 'Verified', 'Full backup', 'Download', 'This server'])
    body = work / 'admin-download.bin'
    streamed = json.loads(execute(ui + ['download', run['run_id'], body]))
    if streamed['download'] != 'streamed' or streamed['length'] != archive.stat().st_size or hashlib.sha256(body.read_bytes()).hexdigest() != expected:
        raise RuntimeError('admin download differs from verified archive')
    if 'attachment' not in streamed['disposition'] or 'no-store' not in streamed['cache']:
        raise RuntimeError('admin download headers unsafe')
    audit = [json.loads(line) for line in (private / 'audit.jsonl').read_text().splitlines()]
    if audit[-1]['outcome'] != 'complete' or audit[-1]['run_id'] != run['run_id'] or not audit[-1]['actor']:
        raise RuntimeError('admin download not audited')
    # Real HTTP without a session never reaches the archive.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        opener.open(native_url + '/admin/backups/download/' + run['run_id'], timeout=10)
        raise RuntimeError('unauthenticated download answered')
    except urllib.error.HTTPError as error:
        if error.code != 302 or '/login' not in error.headers.get('Location', ''):
            raise RuntimeError('unauthenticated download not redirected to login: ' + str(error.code))
    # A changed file needs a new full verification before it is served again.
    stat = archive.stat()
    os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000))
    refused = json.loads(execute(ui + ['download', run['run_id'], work / 'refused-download.bin']))
    if refused['download'] != 'refused' or (work / 'refused-download.bin').exists():
        raise RuntimeError('stale verification served bytes')
    execute(ui + ['render', 'Not verified'])
    execute(ui + ['verifyFirst', run['run_id']])
    promote_and_run(cli, private, 'verify')
    again = work / 'admin-download-again.bin'
    json.loads(execute(ui + ['download', run['run_id'], again]))
    if hashlib.sha256(again.read_bytes()).hexdigest() != expected:
        raise RuntimeError('reverified download differs')
    execute(ui + ['delete-refused', run['run_id'], 'only usable backup'])
    # V2 deletion requires a survivor of the same kind. Two database copies
    # exercise retention and deletion without another full capture.
    online = json.loads(guarded_write(cli, ['run', '--execute', '--mode', 'database-online'], timeout=300))
    survivor = json.loads(guarded_write(cli, ['run', '--execute', '--mode', 'database-online'], timeout=300))
    survivor_archive = pathlib.Path(survivor['archive_path'])
    survivor_digest = hashlib.sha256(survivor_archive.read_bytes()).hexdigest()
    current = json.loads(execute(cli + ['config', 'show']))['settings']
    keep = {key: current[key] for key in ('retention_keep_full', 'retention_keep_database')}
    guarded_write(cli, ['config', 'set'], input=json.dumps({'settings': {'retention_keep_full': 1, 'retention_keep_database': 1}}).encode())
    execute(ui + ['retention', 'two verified database copies, keep one'])
    guarded_write(cli, ['config', 'set'], input=json.dumps({'settings': keep}).encode())
    execute(ui + ['delete', online['run_id']])
    promote_and_run(cli, private, 'delete')
    journal = json.loads((private / 'runs' / online['run_id'] / 'journal.json').read_text())
    if not journal.get('retained_deleted') or journal['deletion']['reason'] != 'manual' or pathlib.Path(online['archive_path']).exists():
        raise RuntimeError('admin delete did not remove the selected backup')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError('admin delete changed the surviving backup')
    if hashlib.sha256(survivor_archive.read_bytes()).hexdigest() != survivor_digest:
        raise RuntimeError('admin delete changed the same-kind surviving backup')
    execute(ui + ['render', 'Verified'])
    return {'download_matches_archive': True, 'download_audited': True, 'unauthenticated_redirected': True,
            'stale_verification_refused': True, 'queued_verify_restores_download': True,
            'last_backup_delete_refused': True, 'retention_offered_with_real_backups': True, 'queued_delete_removed_selected_only': True}


def legacy_configuration_check(cli):
    """Capture-free v1 probe; the caller must supply its own legacy identity."""
    current = json.loads(execute(cli + ['config', 'show']))['settings']
    if current['settings_version'] != 1:
        raise RuntimeError('legacy compatibility requires an isolated v1 installation')
    guarded_write(cli, ['config', 'set'], input=json.dumps({'settings': {'enabled': False, 'retention_keep': 3}}).encode())
    guarded_write(cli, ['schedule', 'set'], input=json.dumps({'schedule_frequency': 'weekly', 'schedule_time': '02:30', 'schedule_weekdays': [0, 2], 'schedule_timezone': 'UTC'}).encode())
    guarded_write(cli, ['schedule', 'disable'])
    written = json.loads(execute(cli + ['schedule', 'show']))
    legacy = json.loads(execute(cli + ['config', 'show']))['settings']
    if legacy['settings_version'] != 1 or legacy['retention_keep'] != 3 or written['enabled'] or written['schedule_weekdays'] != [0, 2] or written['schedule_time'] != '02:30':
        raise RuntimeError('legacy v1 CLI configuration compatibility changed')
    return {'settings_version': 1, 'total_retention_readable': True, 'singleton_schedule_writable': True}


def legacy_configuration_fixture(work, app, php, checker):
    """Separate native rows, application identity and private root; never downgrade v2."""
    legacy_app = work / 'legacy-app'
    legacy_private = work / 'legacy-private'
    # Runtime/application source is copied; the immutable dependency tree may be
    # shared. Laravel caches and storage remain owned by this separate app.
    shutil.copytree(app, legacy_app, ignore=shutil.ignore_patterns('vendor', '.git'))
    (legacy_app / 'vendor').symlink_to(app / 'vendor', target_is_directory=True)
    for cached in (legacy_app / 'bootstrap/cache').glob('*.php'):
        if cached.name not in ('packages.php', 'services.php'):
            cached.unlink()
    env = (legacy_app / '.env').read_text()
    if env.count('/paymenter_fixture?') != 1:
        raise RuntimeError('legacy fixture database binding unavailable')
    env = env.replace('/paymenter_fixture?', '/paymenter_legacy_fixture?')
    (legacy_app / '.env').write_text(env)
    execute([php, 'artisan', 'migrate', '--force', '--seed'], cwd=legacy_app)
    execute([php, checker, legacy_app, 'seed', 'http://127.0.0.1:1'])
    # Seed legacy rows before installing defaults or initializing private state.
    # This separate native record never contains marker/schema-2 schedule state.
    execute([php, checker, legacy_app, 'legacy-install', legacy_private, sys.executable])
    bridge = legacy_app / 'extensions/Others/Backups/bridge.php'
    legacy_cli = [sys.executable, '-I', '-B', legacy_app / 'extensions/Others/Backups/runtime/cli.py',
                  '--app', legacy_app, '--private-root', legacy_private, '--bridge', bridge, '--php', php]
    execute(legacy_cli + ['init'])
    return legacy_configuration_check(legacy_cli)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True)
    parser.add_argument('--archive')
    parser.add_argument('--php', default=shutil.which('php'))
    parser.add_argument('--composer', default='/usr/bin/composer', help='Composer PHP script, invoked with the selected PHP binary')
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--gpg-writer', default='/usr/bin/gpg')
    parser.add_argument('--gpg-reader', default='/usr/bin/gpg')
    parser.add_argument('--skip-restore', action='store_true', help='Adapter development only; does not complete release checks')
    parser.add_argument('--slow-step-delay', type=int, default=0, metavar='SECONDS',
                        help='Pause before the schedule drill to exercise expiry of the earlier billing guard')
    args = parser.parse_args()
    if args.slow_step_delay < 0:
        parser.error('--slow-step-delay must be nonnegative')
    workbase = private_work_directory('paymenter-backups-native-')
    work = private_work_directory('native-integration-', workbase)
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
        with maria(work) as (sock, db_port):
            sock = require_maria_socket_path(sock)
            execute(['mariadb', '--no-defaults', '--socket=' + sock, '-uroot', '-e', "CREATE DATABASE paymenter_fixture; CREATE DATABASE paymenter_restore; CREATE DATABASE paymenter_legacy_fixture; CREATE USER 'paymenter_fixture_user'@'localhost' IDENTIFIED BY 'paymenter-fixture-password'; CREATE USER 'paymenter_fixture_user'@'127.0.0.1' IDENTIFIED BY 'paymenter-fixture-password'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON paymenter_fixture.* TO 'paymenter_fixture_user'@'localhost'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON paymenter_fixture.* TO 'paymenter_fixture_user'@'127.0.0.1'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON paymenter_restore.* TO 'paymenter_fixture_user'@'localhost'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON paymenter_legacy_fixture.* TO 'paymenter_fixture_user'@'localhost';"])
            sys.path.insert(0, str(ROOT / 'tests/integration'))
            from database_visibility import check_visibility
            visibility = check_visibility(work, sock)
            http_port = port()
            native_url = 'http://127.0.0.1:' + str(http_port)
            app_key = 'base64:' + base64.b64encode(os.urandom(32)).decode()
            env = {'APP_NAME': 'Fixture', 'APP_ENV': 'local', 'APP_KEY': app_key, 'APP_DEBUG': 'false', 'APP_URL': 'http://127.0.0.1:1', 'APP_TIMEZONE': 'America/New_York', 'APP_MAINTENANCE_DRIVER': 'file', 'DB_CONNECTION': 'mysql', 'DB_SOCKET': sock, 'DB_HOST': 'deliberately-unused.invalid', 'DB_DATABASE': 'deliberately_unused_database', 'DB_URL': 'mysql://paymenter_fixture_user:paymenter-fixture-password@localhost/paymenter_fixture?' + urllib.parse.urlencode({'unix_socket': sock}), 'DB_USERNAME': 'root', 'DB_PASSWORD': '', 'DB_COLLATION': 'utf8mb4_unicode_ci', 'CACHE_STORE': 'file', 'QUEUE_CONNECTION': 'sync', 'SESSION_DRIVER': 'file', 'FILESYSTEM_DISK': 'local', 'MAIL_MAILER': 'log', 'TELEMETRY_ENABLED': 'false'}
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
            settings = {'settings_version': 2, 'private_root': str(private), 'python': sys.executable, 'mode': 'full-quiesced', 'full_time': '02:00', 'full_weekdays': '[0,2]', 'local_byte_budget': 2 * 1024**3, 'minimum_free_bytes': 1024**2, 'drain_seconds': 1, 'capture_timeout': 120, 'no_concurrent_ddl_acknowledged': True, 'full_host_assumptions_acknowledged': True}
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
            if cold['database']['name'] != 'paymenter_fixture' or cold['database']['host'] != 'localhost' or cold['database']['socket'] != sock:
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
            results = json.loads(execute([args.php, checker, app, 'check'], timeout=420))
            # Cached config must be honoured without providers; native URL/TZ
            # overrides intentionally differ from .env in this fixture.
            execute([args.php, 'artisan', 'config:cache'], cwd=app)
            cached = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
            if cached['framework']['app_url'] != native_url or cached['framework']['timezone'] != 'UTC':
                raise RuntimeError('cold cached native overrides differ')
            (app / 'bootstrap/cache/config.php').unlink()
            cli = [sys.executable, '-I', '-B', app / 'extensions/Others/Backups/runtime/cli.py', '--app', app, '--private-root', private, '--bridge', bridge, '--php', args.php]
            # Reproduce PHP-FPM/container-only DB settings: the fixture .env and
            # Laravel config cache cannot supply or mask any database field.
            dotenv_path = app / '.env'
            original_dotenv = dotenv_path.read_text()
            removed_db_lines = []
            for line in original_dotenv.splitlines(keepends=True):
                name = line.split('=', 1)[0].strip()
                if name.startswith('DB_'):
                    removed_db_lines.append(line)
            if not removed_db_lines:
                raise RuntimeError('env-only database fixture found no DB settings to remove')
            env_directory = app / 'var'
            env_directory.mkdir(exist_ok=True)
            dotenv_target = env_directory / '.env'
            dotenv_target.write_text('APP_KEY=' + app_key + '\n')
            dotenv_target.chmod(0o644)
            if os.geteuid() == 0:
                os.chown(dotenv_target, 0, 0)
            dotenv_path.unlink()
            dotenv_path.symlink_to(dotenv_target)
            if os.geteuid() == 0:
                os.chown(dotenv_path, 0, 0, follow_symlinks=False)
            if not dotenv_path.is_symlink() or dotenv_path.stat().st_mode & 0o777 != 0o644 or dotenv_target.stat().st_size != 60:
                raise RuntimeError('symlinked minimal .env fixture differs')
            if os.geteuid() == 0 and (dotenv_path.lstat().st_uid != 0 or dotenv_target.stat().st_uid != 0):
                raise RuntimeError('symlinked minimal .env fixture ownership differs')
            if any(line.split('=', 1)[0].strip().startswith('DB_') for line in dotenv_path.read_text().splitlines()):
                raise RuntimeError('symlinked minimal .env fixture still contains DB settings')
            config_cache = app / 'bootstrap/cache/config.php'
            config_cache.unlink(missing_ok=True)
            env_database = {
                'DB_CONNECTION': 'mariadb', 'DB_HOST': '127.0.0.1', 'DB_PORT': str(db_port),
                'DB_DATABASE': 'paymenter_fixture', 'DB_USERNAME': 'paymenter_fixture_user',
                'DB_PASSWORD': 'paymenter-fixture-password', 'DB_PREFIX': '',
            }
            try:
                result = json.loads(execute_env_only_database_parent(
                    [args.php, checker, app, 'env-db-save'], env_database, cwd=app, timeout=420))
                if result.get('env_only_mariadb_save') is not True:
                    raise RuntimeError('env-only MariaDB save regression did not complete')
            finally:
                dotenv_path.unlink(missing_ok=True)
                dotenv_target.unlink(missing_ok=True)
                dotenv_path.write_text(original_dotenv)
                config_cache.unlink(missing_ok=True)
            results['env_only_mariadb_save'] = True
            # Exercise public JSON-stdin credentials and concurrent direct bridge
            # writers on one encrypted file, without touching host TLS/services.
            probe_url = 'https://fixture.example.invalid/billing'
            execute([args.php, checker, app, 'app-url', probe_url])
            probe_creds = {'origin': 'https://fixture.example.invalid:443', 'username': 'CLI-PROBE-CREDENTIAL-CANARY', 'password': 'CLI-PASSWORD-CREDENTIAL-CANARY'}
            try:
                public = guarded_write(cli, ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': probe_creds}).encode())
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
                    guarded_write(cli, ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': probe_creds}).encode())
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
                guarded_write(cli, ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': {'clear': True}}).encode())
                cleared = json.loads(execute([args.php, bridge, 'read', '--app', app, '--private-root', private]))
                if cleared['credentials'] != concurrent_s3 or cleared['maintenance_probe_credentials']:
                    raise RuntimeError('CLI clear lost S3 credentials')
                # Check legacy S3 reads and whole-payload writes with a probe namespace.
                guarded_write(cli, ['config', 'set'], input=json.dumps({'maintenance_probe_credentials': probe_creds}).encode())
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
            guarded_write(cli, ['config', 'set'], input=json.dumps({'settings': {'retention_keep_full': 5, 'retention_keep_database': 25}}).encode())
            # This is the installed CLI's real config.lock/tick.lock path. The
            # audited Artisan write must not recursively run init under tick.lock.
            persisted_config = json.loads(execute(cli + ['config', 'show']))['settings']
            if persisted_config['settings_version'] != 2 or persisted_config['retention_keep_full'] != 5 or persisted_config['retention_keep_database'] != 25:
                raise RuntimeError('installed CLI config-set under tick.lock did not persist')
            if not (private / 'schedule.json').is_file() or not (private / 'policy.json').is_file():
                raise RuntimeError('installed CLI config-set did not publish policy and schedule state')
            results['installed_cli_config_set_under_tick_lock'] = True
            # Direct Artisan use keeps its normal init/update behavior.
            advisory_path = private / 'configuration-advisory.json'
            advisory_before = advisory_path.stat()
            execute([args.php, 'artisan', 'backups:guard'], cwd=app)
            execute([args.php, 'artisan', 'backups:config-set'], cwd=app, input=json.dumps({'retention_keep_full': '5', 'retention_keep_database': '25'}).encode())
            advisory_after = advisory_path.stat()
            if (advisory_before.st_ino, advisory_before.st_mtime_ns) == (advisory_after.st_ino, advisory_after.st_mtime_ns):
                raise RuntimeError('direct Artisan config-set skipped normal update behavior')
            advisory = json.loads((private / 'configuration-advisory.json').read_text())
            if not advisory.get('valid'):
                raise RuntimeError('direct Artisan config-set no longer initializes and validates')
            results['direct_artisan_config_set_initializes'] = True
            lock = work / 'scheduler.lock'
            lock.touch(mode=0o600)
            fakebin = work / 'fixture-bin'
            fakebin.mkdir(mode=0o700)
            cron = fakebin / 'crontab'
            cron.write_text('#!/bin/sh\ncat <<\'CRON\'\n* * * * * flock -n ' + str(lock) + ' ' + args.php + ' ' + str(app / 'artisan') + ' schedule:run\nCRON\n')
            cron.chmod(0o700)
            systemctl = fakebin / 'systemctl'
            systemctl.write_text('#!/bin/sh\nprintf \'[]\\n\'\n')
            systemctl.chmod(0o700)
            execute([args.php, 'artisan', 'backups:guard'], cwd=app)
            execute([args.php, 'artisan', 'backups:config-set'], cwd=app, input=json.dumps({'job_lock_paths': json.dumps([str(lock)])}).encode())
            # Adapter envelope instrumentation is confined to this fixture. The
            # production bridge has no configurable crontab override.
            fixture_bridge = work / 'fixture-bridge.php'
            fixture_bridge.write_text('<?php ob_start(); require ' + json.dumps(str(bridge)) + '; $value=json_decode(ob_get_clean(),true); $value["tools"]["crontab"]=' + json.dumps(str(cron)) + '; $value["tools"]["systemctl"]=' + json.dumps(str(systemctl)) + '; echo json_encode($value)."\\n";')
            capture_cli = cli[:]
            capture_cli[capture_cli.index('--bridge') + 1] = fixture_bridge
            execution_env = child_environment({'PATH': str(fakebin) + ':' + child_environment()['PATH']})
            if args.slow_step_delay:
                print('Fixture slow step: delaying schedule writes for ' + str(args.slow_step_delay) + ' seconds', file=sys.stderr, flush=True)
                time.sleep(args.slow_step_delay)
                earlier_guard = json.loads((private / 'billing-guard.json').read_text())
                print('Fixture slow step: earlier billing guard age is ' + str(round(time.time() - earlier_guard['generated_at'], 1)) +
                      ' seconds (maximum ' + str(persisted_config['billing_guard_max_age']) + ')', file=sys.stderr, flush=True)
            # Commission only the disposable fixture: real tick/observer receipts, fake host inventory.
            execute(capture_cli + ['tick', '--execute'])
            execute([args.php, 'artisan', 'backups:observe'], cwd=app)
            for kind in ('full', 'database'):
                guarded_write(capture_cli, ['schedule', 'set', '--kind', kind], input=json.dumps({'frequency': 'weekly', 'time': '02:30', 'weekdays': [0, 2], 'timezone': 'UTC'}).encode())
                guarded_write(capture_cli, ['schedule', 'enable', '--kind', kind])
                guarded_write(capture_cli, ['schedule', 'disable', '--kind', kind])
                written = json.loads(execute(cli + ['schedule', 'show', '--kind', kind]))['schedules'][kind]
                if written['schedule'] != 'off' or written['weekdays'] != [0, 2] or written['time'] != '02:30':
                    raise RuntimeError('native v2 CLI schedule write did not persist for ' + kind)
            results['legacy_v1_configuration'] = legacy_configuration_fixture(work, app, args.php, checker)
            execute(cli + ['plan'])
            # Use one UTC minute throughout; retry if slow commands cross it.
            for _ in range(3):
                billing_now = time.time()
                seconds_left = 60 - billing_now % 60
                if seconds_left < 15:
                    time.sleep(seconds_left)
                    billing_now = time.time()
                minute_start = int(billing_now) // 60 * 60
                execute([args.php, checker, app, 'billing', time.strftime('%H:%M', time.gmtime(minute_start))])
                current_guard = json.loads(execute([args.php, 'artisan', 'backups:guard'], cwd=app))
                current_plan = json.loads(execute(cli + ['plan']))
                if time.time() >= minute_start + 60:
                    continue
                if minute_start not in current_guard['instants']:
                    raise RuntimeError('billing guard omits current minute')
                if 'billing-window-protected' not in current_plan['admission_errors']:
                    raise RuntimeError('current billing minute admits full hold')
                break
            else:
                raise RuntimeError('billing minute check repeatedly crossed minute boundary')
            # Keep the disposable app's real native billing time away from
            # this drill. The current-minute refusal above stays exercised.
            fixture_billing_time = time.strftime('%H:%M', time.gmtime(time.time() + 12 * 3600))
            execute([args.php, checker, app, 'billing', fixture_billing_time])
            execute([args.php, 'artisan', 'backups:guard'], cwd=app)
            if hashes(app) != original:
                raise RuntimeError('native application source/Composer changed')
            if args.skip_restore:
                print(json.dumps({'database_visibility': visibility, 'adapter_checks': results, 'restore': 'explicitly skipped', 'fixture': str(work)}))
                return
            with http_server(app, args.php, http_port, work):
                request = urllib.request.Request(native_url + '/paymenter/update', data=b'{}', headers={'Content-Type': 'application/json'})
                try:
                    fixture_http().open(request, timeout=5)
                    raise RuntimeError('native Livewire request missing CSRF accepted')
                except urllib.error.HTTPError as error:
                    if error.code != 419:
                        raise RuntimeError('unexpected native CSRF response: ' + str(error.code))
                # Incompressible fixture data makes the encrypted drill exercise
                # a real large generation rather than only small SQL documents.
                large_payload = app / 'isolated-restore-payload.bin'
                with large_payload.open('wb') as output:
                    for _ in range(200):
                        output.write(os.urandom(1024**2))
                large_payload.chmod(0o600)
                large_payload_sha256 = hashlib.sha256(large_payload.read_bytes()).hexdigest()
                guarded_write(capture_cli, ['run', '--execute', '--mode', 'full-quiesced'], env=execution_env, timeout=300)
                runs = list((private / 'runs').glob('*/journal.json'))
                journals = [json.loads(path.read_text()) for path in runs]
                successful = [journal for journal in journals if journal.get('local_complete')]
                if len(successful) != 1:
                    raise RuntimeError('full native checkpoint incomplete: ' + str(journals))
                run = successful[0]
                # Database captures now need the same real fixture launcher proof as full capture.
                results['admin_history'] = admin_history(args, app, checker, capture_cli, private, run, native_url, work)
            forbidden_export = work / 'additional-site-public' / 'forbidden-recovery-kit'
            denied_export = subprocess.run(cli + ['export', run['run_id'], '--output', str(forbidden_export)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_environment())
            if denied_export.returncode == 0 or forbidden_export.exists():
                raise RuntimeError('offline export ignores native GUI document-root policy')
            inspected = execute(cli + ['verify', run['run_id'], '--show'])
            fake_auth_hash = hashlib.sha1(hashlib.sha1(b'paymenter-fixture-password').digest()).hexdigest().upper().encode()
            if fake_auth_hash in inspected or b'paymenter-fixture-password' in inspected:
                raise RuntimeError('native manifest leaks SHOW GRANTS authentication hash')
            archive = pathlib.Path(run['archive_path'])
            encrypted_restore = json.loads(execute([sys.executable, '-I', ROOT / 'tools/native_encrypted_restore.py',
                '--work', work, '--php', args.php, '--run', run['run_id'],
                '--gpg-writer', args.gpg_writer, '--gpg-reader', args.gpg_reader], timeout=600))
            archive = pathlib.Path(encrypted_restore['restore_archive'])
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
            # Runtime contents are deliberately excluded from online captures.
            # The archive must supply empty directories before Laravel can boot.
            for relative in ('storage/logs', 'storage/framework/cache', 'storage/framework/cache/data',
                             'storage/framework/sessions', 'storage/framework/views', 'storage/debugbar'):
                directory = restored / relative
                if not directory.is_dir() or any(path.is_file() for path in directory.rglob('*')):
                    raise RuntimeError('restored runtime directory is missing or not empty: ' + relative)
            if hashlib.sha256((restored / 'isolated-restore-payload.bin').read_bytes()).hexdigest() != large_payload_sha256:
                raise RuntimeError('large encrypted restore payload differs')
            execute(['mariadb', '--no-defaults', '--socket=' + require_maria_socket_path(sock), '-uroot', 'paymenter_restore'], input=(payload / 'database.sql').read_bytes())
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
            result = {'hostile_parent_environment_ignored': True, 'database_visibility': visibility, 'adapter_checks': results, 'open_basedir': open_basedir_results, 'restore': restored_results, 'encrypted_restore': encrypted_restore, 'full_artifact_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'release_sha256': release_sha256, 'native_login': True, 'native_migrations': 'no pending', 'native_source_unchanged': True}
            (work / 'native-integration-last.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
    finally:
        for key, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        if not args.keep:
            shutil.rmtree(work)
            shutil.rmtree(workbase)


if __name__ == '__main__':
    main()
