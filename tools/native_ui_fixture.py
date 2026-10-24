#!/usr/bin/env python3
"""Long-lived disposable native browser fixture, with explicit local controls.

No native scheduler is launched. All accounts and database credentials are fake.
The owning process closes HTTP/MariaDB on SIGTERM, SIGINT, parent death or TTL.
State and logs remain under a private random directory beneath TMPDIR.
"""
import argparse
import base64
import ctypes
import contextlib
import hashlib
import json
import os
import pathlib
import signal
import socket
import shutil
import subprocess
import stat
import sys
import tarfile
import time

from native_integration import (
    ROOT, execute, fetch, hashes, fixture_http, maria, port, require_maria_socket_path,
)
from fixture_environment import child_environment, private_work_directory, require_private_workroot


@contextlib.contextmanager
def browser_http(app, php, bind_address, http_port, work):
    router = work / 'fixture-http-router.php'
    router.write_text('<?php\n'
        '$public = ' + json.dumps(str(app / 'public')) + ';\n'
        '$path = rawurldecode(parse_url($_SERVER["REQUEST_URI"], PHP_URL_PATH) ?? "/");\n'
        "if (!str_contains($path, \"\\0\") && !preg_match('~(?:^|/)\\.{1,2}(?:/|$)~', $path) && is_file($public . $path)) { return false; }\n"
        'require $public . "/index.php";\n')
    router.chmod(0o600)
    with (work / 'browser-http.log').open('wb') as log:
        process = subprocess.Popen([php, '-S', bind_address + ':' + str(http_port),
            '-t', str(app / 'public'), str(router)], cwd=app,
            stdout=log, stderr=log, start_new_session=True, env=child_environment())
        try:
            for _ in range(100):
                try:
                    fixture_http().open('http://' + bind_address + ':' + str(http_port) + '/login', timeout=1).read()
                    break
                except Exception:
                    if process.poll() is not None:
                        raise RuntimeError('browser HTTP process stopped')
                    time.sleep(.1)
            else:
                raise RuntimeError('browser HTTP readiness timeout')
            yield process
        finally:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait()


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        temporary.chmod(0o600)
        output.write(json.dumps(value, indent=2) + '\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def php_helper(work):
    helper = work / 'fixture-only.php'
    helper.write_text(r'''<?php
require $argv[1].'/vendor/autoload.php';
$app = require $argv[1].'/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
if ($argv[2] === 'users') {
    foreach ([['ui-admin@example.invalid', 'Browser', 'Admin', 'ui-full-admin', ['*']],
              ['ui-limited@example.invalid', 'Limited', 'Admin', 'ui-limited-admin', ['admin.extensions.viewAny']]] as [$email,$first,$last,$name,$permissions]) {
        $role=App\Models\Role::firstOrCreate(['name'=>$name],['permissions'=>$permissions]);
        App\Models\User::withoutEvents(fn()=>App\Models\User::create(['first_name'=>$first,'last_name'=>$last,'email'=>$email,'password'=>'Fake-browser-password-42!','role_id'=>$role->id,'email_verified_at'=>now()]));
    }
    foreach (['app_url'=>$argv[3], 'timezone'=>'UTC', 'cronjob_time'=>'23:59'] as $key=>$value) {
        App\Models\Setting::whereNull('settingable_type')->updateOrCreate(['key'=>$key],['value'=>$value,'type'=>'string','encrypted'=>false]);
    }
    App\Providers\SettingsProvider::flushCache();
    echo json_encode(['fake_users_created'=>true])."\n";
}
''')
    helper.chmod(0o600)
    return helper


def command_prefix(metadata):
    app = pathlib.Path(metadata['app'])
    return [sys.executable, '-I', '-B', app / 'extensions/Others/Backups/runtime/cli.py',
            '--app', app, '--private-root', metadata['private_root'],
            '--bridge', app / 'extensions/Others/Backups/bridge.php', '--php', metadata['php']]


def controlled_metadata(work):
    work = require_private_workroot(work, 'ui-', 'paymenter-backups-ui-')
    metadata = json.loads((work / 'ui-fixture.json').read_text())
    if metadata['work'] != str(work) or metadata['app'] != str(work / 'app') or metadata['private_root'] != str(work / 'private'):
        raise RuntimeError('fixture identity mismatch')
    command = pathlib.Path('/proc', str(metadata['owner_pid']), 'cmdline').read_bytes()
    if b'native_ui_fixture.py' not in command or not any(mode in command.split(b'\0') for mode in (b'serve', b'resume')):
        raise RuntimeError('fixture owner no longer alive')
    return work, metadata


def resume(args):
    """Restart a closed UI owner's same fake datadir without changing its rows."""
    os.umask(0o077)
    work = require_private_workroot(args.work, 'ui-', 'paymenter-backups-ui-')
    metadata = json.loads((work / 'ui-fixture.json').read_text())
    if metadata['work'] != str(work) or metadata['app'] != str(work / 'app') or metadata['private_root'] != str(work / 'private') or metadata['socket'] != str(work / 'db.sock') or metadata['fixture_only'] is not True or metadata['native_scheduler_started'] is not False:
        raise RuntimeError('resume fixture identity mismatch')
    if pathlib.Path('/proc', str(metadata['owner_pid'])).exists() or (work / 'db.sock').exists():
        raise RuntimeError('previous fixture owner must be closed')
    if not json.loads((work / 'ui-closed.json').read_text())['native_source_unchanged']:
        raise RuntimeError('previous fixture core proof failed')
    shutil.copy2(work / 'ui-closed.json', work / ('ui-closed-owner-' + str(metadata['owner_pid']) + '.json'))
    app = work / 'app'
    data = work / 'mariadb'
    for root in (work, app, data, work / 'private'):
        info = root.lstat()
        if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('resume owner-private paths required')
    # Compare immutable native source directly to the pinned official archive.
    from fetch_fixture import SHA256
    archive = work.parent / 'paymenter-v1.5.9.tar.gz'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError('resume official release pin')
    original = hashes(app)
    with tarfile.open(archive) as stock:
        reference = {member.name.removeprefix('./'): hashlib.sha256(stock.extractfile(member).read()).hexdigest()
                     for member in stock if member.isfile() and member.name.removeprefix('./') in original}
    if reference != original:
        raise RuntimeError('resume native source differs from pinned stock')
    from urllib.parse import urlsplit
    parsed = urlsplit(metadata['url'])
    bind = socket.gethostbyname(parsed.hostname)
    import ipaddress
    address = ipaddress.ip_address(bind)
    if parsed.scheme != 'http' or not parsed.port or (not address.is_loopback and address not in ipaddress.ip_network('100.64.0.0/10')):
        raise RuntimeError('resume requires loopback or private Tailscale HTTP')
    owner_parent = os.getppid()
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    if os.getppid() != owner_parent: raise RuntimeError('resume parent closed')
    stopping = False
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    for marker in ('STOP', 'ui-ready.json', 'ui-closed.json'):
        (work / marker).unlink(missing_ok=True)
    log = (work / 'mariadb-resume.log').open('wb')
    command = [shutil.which('mariadbd') or '/usr/sbin/mariadbd', '--no-defaults',
        '--datadir=' + str(data), '--socket=' + metadata['socket'], '--pid-file=' + str(work / 'db.pid'),
        '--skip-networking', '--log-error=' + str(work / 'server-error.log')]
    if os.getuid() == 0: command.append('--user=root')
    database = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True, env=child_environment())
    try:
        for _ in range(200):
            if database.poll() is not None: raise RuntimeError('resumed disposable database stopped')
            if (work / 'db.sock').exists():
                try:
                    result = execute(['mariadb', '--no-defaults', '--socket=' + metadata['socket'], '-uroot', '-N', '-e', 'SHOW DATABASES'], timeout=2)
                    if b'paymenter_fixture' not in result or any(name in result for name in (b'company_data_', b'paymenter_live', b'unrelated_app')):
                        raise RuntimeError('resume fake database boundary')
                    break
                except RuntimeError:
                    if database.poll() is not None: raise
            time.sleep(.1)
        else: raise RuntimeError('resume database readiness timeout')
        metadata.update(owner_pid=os.getpid(), ttl_seconds=args.ttl)
        write_json(work / 'ui-fixture.json', metadata)
        with browser_http(app, metadata['php'], bind, parsed.port, work):
            write_json(work / 'ui-ready.json', {'ready': True, 'owner_pid': os.getpid(), 'url': metadata['url'], 'resumed_same_fake_state': True})
            print(json.dumps({'ready': str(work / 'ui-fixture.json'), 'url': metadata['url'], 'resumed_same_fake_state': True}), flush=True)
            deadline = time.monotonic() + args.ttl
            while not stopping and not (work / 'STOP').exists() and time.monotonic() < deadline:
                if database.poll() is not None: raise RuntimeError('resumed disposable database stopped')
                time.sleep(.2)
    finally:
        database.terminate()
        try: database.wait(timeout=10)
        except subprocess.TimeoutExpired: database.kill(); database.wait()
        log.close()
        write_json(work / 'ui-closed.json', {'native_source_unchanged': hashes(app) == original, 'closed_at': time.time()})


def install(work, metadata, release):
    upload = work / 'ui-upload.zip'
    shutil.copy2(release, upload)
    release_sha256 = hashlib.sha256(upload.read_bytes()).hexdigest()
    results = json.loads(execute([metadata['php'], ROOT / 'tests/integration/native_checks.php', metadata['app'], 'upload', upload]))
    metadata['release_sha256'] = release_sha256
    write_json(work / 'ui-fixture.json', metadata)
    return {**results, 'release_sha256': metadata['release_sha256']}


def run_worker(work, metadata, request_id, *, allow_full=False, loopback_s3=False):
    import fcntl
    private = pathlib.Path(metadata['private_root'])
    if not request_id or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for char in request_id) or len(request_id) > 100:
        raise RuntimeError('invalid request id')
    # This fixture-only launcher promotes precisely one user-requested item. It
    # never enumerates native schedules, runs cronjob or talks to providers.
    target = private / 'requests' / (request_id + '.json')
    with (private / 'requests.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        item = json.loads(target.read_text())
        if item['status'] not in ('pending', 'deferred'):
            raise RuntimeError('request is not pending/deferred')
        arguments = item.get('arguments')
        if (item.get('action') == 'run' and not allow_full
                and (not isinstance(arguments, dict) or arguments.get('mode') != 'database-online')):
            raise RuntimeError('fixture worker refuses non-database capture without --allow-full')
        item.update(status='launching', attempts=item['attempts'] + 1)
        write_json(target, item)
    command = command_prefix(metadata)
    if loopback_s3:
        command[3] = ROOT / 'tools/native_ui_s3_worker.py'
    return json.loads(execute(command + ['request-worker', request_id, '--execute'], timeout=300))


def corrupt_artifact(metadata, run_id):
    # Resolve through the engine's journal identity, location and ownership guards.
    sys.path.insert(0, str(ROOT / 'src'))
    from paymenter_backups import artifacts
    from paymenter_backups.common import State
    state = State(metadata['app'], metadata['private_root'], initialize=False)
    with state.lock():
        item = artifacts.journal(state, run_id)
        location = artifacts.locations(state, item)
        if location[0] != state.default_destination():
            raise RuntimeError('artifact outside disposable default backup directory')
        directory_fd = artifacts.open_directory(location[0])
        try:
            directory_info = os.fstat(directory_fd)
            if (directory_info.st_dev, directory_info.st_ino) != location.key:
                raise RuntimeError('artifact directory changed')
            descriptor = os.open(location[1].name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=directory_fd)
            with os.fdopen(descriptor, 'r+b') as output:
                info = os.fstat(output.fileno())
                if not artifacts.owned_file(info) or not info.st_size:
                    raise RuntimeError('artifact is not an owner-private regular file')
                output.seek(info.st_size // 2)
                byte = output.read(1)
                output.seek(-1, 1)
                output.write(bytes([byte[0] ^ 1]))
                output.flush()
                os.fsync(output.fileno())
        finally:
            os.close(directory_fd)
    return {'corrupted_fixture_artifact': run_id}


def seed_runs(work, metadata, count):
    cli = command_prefix(metadata)
    execute(cli + ['config', 'set'], input=json.dumps({'settings': {
        'mode': 'database-online', 'destination': 'local', 'keep_local': True, 'upload_s3': False, 'automatic_retention': False,
        'no_concurrent_ddl_acknowledged': True, 'external_state_acknowledged': True,
    }}).encode())
    result = [json.loads(execute(cli + ['run', '--mode', 'database-online', '--execute'], timeout=300)) for _ in range(count)]
    write_json(work / 'ui-runs.json', result)
    return result


def serve(args):
    os.umask(0o077)
    owner_parent = os.getppid()
    # Linux parent-death cleanup complements the process-owned contexts/TTL.
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    if os.getppid() != owner_parent:
        raise RuntimeError('fixture owner parent already closed')
    shutdown = False
    def stop(_signum, _frame):
        nonlocal shutdown
        shutdown = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    workbase = private_work_directory('paymenter-backups-ui-')
    work = private_work_directory('ui-', workbase)
    print(json.dumps({'preparing': str(work)}), flush=True)
    app = fetch(work / 'app', workbase / 'paymenter-v1.5.9.tar.gz')
    original = hashes(app)
    private = work / 'private'
    private.mkdir(mode=0o700)
    with maria(work) as (sock, _db_port):
        socket_path = require_maria_socket_path(sock)
        execute(['mariadb', '--no-defaults', '--socket=' + socket_path, '-uroot', '-e',
                 "CREATE DATABASE paymenter_fixture; CREATE USER 'paymenter_fixture_user'@'localhost' IDENTIFIED BY 'paymenter-fixture-password'; GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON paymenter_fixture.* TO 'paymenter_fixture_user'@'localhost';"])
        http_port = args.port or port()
        import ipaddress
        bind_ip = ipaddress.ip_address(args.bind)
        if not bind_ip.is_loopback and bind_ip not in ipaddress.ip_network('100.64.0.0/10'):
            raise RuntimeError('fixture browser binds only loopback or private Tailscale IPv4')
        url = args.url or 'http://' + args.bind + ':' + str(http_port)
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.port != http_port or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or socket.gethostbyname(parsed.hostname) != args.bind:
            raise RuntimeError('fixture URL must resolve to its selected private interface and port')
        env = {'APP_NAME': 'Backup UI Fixture', 'APP_ENV': 'local',
               'APP_KEY': 'base64:' + base64.b64encode(os.urandom(32)).decode(),
               'APP_DEBUG': 'false', 'APP_URL': url, 'APP_TIMEZONE': 'UTC',
               'APP_MAINTENANCE_DRIVER': 'file', 'DB_CONNECTION': 'mysql',
               'DB_SOCKET': socket_path, 'DB_HOST': 'localhost',
               'DB_DATABASE': 'paymenter_fixture', 'DB_USERNAME': 'paymenter_fixture_user',
               'DB_PASSWORD': 'paymenter-fixture-password', 'DB_COLLATION': 'utf8mb4_unicode_ci',
               'CACHE_STORE': 'file', 'QUEUE_CONNECTION': 'sync', 'SESSION_DRIVER': 'file',
               'FILESYSTEM_DISK': 'local', 'MAIL_MAILER': 'log', 'TELEMETRY_ENABLED': 'false'}
        for cached in (app / 'bootstrap/cache').glob('*.php'):
            if cached.name not in ('packages.php', 'services.php'):
                cached.unlink()
        (app / '.env').write_text(''.join(key + '=' + json.dumps(value) + '\n' for key, value in env.items()))
        execute([args.php, 'artisan', 'migrate', '--force', '--seed'], cwd=app)
        execute([args.php, 'artisan', 'passport:keys', '--force'], cwd=app)
        execute([args.php, php_helper(work), app, 'users', url])
        metadata = {'work': str(work), 'app': str(app), 'private_root': str(private),
                    'socket': socket_path, 'php': args.php, 'owner_pid': os.getpid(),
                    'url': url, 'admin_url': url + '/admin/backups',
                    'login_url': url + '/login',
                    'admin_email': 'ui-admin@example.invalid', 'limited_email': 'ui-limited@example.invalid',
                    'fake_password': 'Fake-browser-password-42!', 'fixture_only': True,
                    'native_scheduler_started': False, 'ttl_seconds': args.ttl}
        install(work, metadata, args.release)
        execute([args.php, 'artisan', 'backups:config-set'], cwd=app, input=json.dumps({
            'private_root': str(private), 'python': sys.executable, 'php': args.php,
            'minimum_free_bytes': 1024**2, 'local_byte_budget': 2 * 1024**3,
        }).encode())
        if args.seed_runs:
            seed_runs(work, metadata, args.seed_runs)
        with browser_http(app, args.php, args.bind, http_port, work):
            write_json(work / 'ui-ready.json', {'ready': True, 'owner_pid': os.getpid(), 'url': url})
            print(json.dumps({'ready': str(work / 'ui-fixture.json'), 'url': url}), flush=True)
            deadline = time.monotonic() + args.ttl
            while not shutdown and not (work / 'STOP').exists() and time.monotonic() < deadline:
                time.sleep(.2)
        unchanged = hashes(app) == original
        write_json(work / 'ui-closed.json', {'native_source_unchanged': unchanged, 'closed_at': time.time()})
        if not unchanged:
            raise RuntimeError('native core source changed during UI fixture')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    serve_parser = sub.add_parser('serve')
    serve_parser.add_argument('--release', required=True)
    serve_parser.add_argument('--php', default='/opt/plesk/php/8.3/bin/php')
    serve_parser.add_argument('--port', type=int)
    serve_parser.add_argument('--bind', default='127.0.0.1')
    serve_parser.add_argument('--url', help='HTTP URL resolving to selected loopback/Tailscale bind and port')
    serve_parser.add_argument('--ttl', type=int, default=7200)
    serve_parser.add_argument('--seed-runs', type=int, default=0)
    resume_parser = sub.add_parser('resume')
    resume_parser.add_argument('--work', required=True)
    resume_parser.add_argument('--ttl', type=int, default=7200)
    for action in ('install', 'seed-runs', 'worker', 'corrupt', 'stop'):
        item = sub.add_parser(action)
        item.add_argument('--work', required=True)
        if action == 'install': item.add_argument('--release', required=True)
        if action == 'seed-runs': item.add_argument('--count', type=int, default=3)
        if action == 'worker':
            item.add_argument('--request-id', required=True)
            item.add_argument('--loopback-s3', action='store_true', help='route fake S3 worker traffic to the isolated loopback protocol fixture')
            item.add_argument('--allow-full', action='store_true',
                              help='explicitly allow full or unspecified capture modes in this disposable fixture')
        if action == 'corrupt': item.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if args.action == 'serve':
        serve(args)
        return
    if args.action == 'resume':
        resume(args)
        return
    work, metadata = controlled_metadata(args.work)
    if args.action == 'install': result = install(work, metadata, args.release)
    elif args.action == 'seed-runs': result = seed_runs(work, metadata, args.count)
    elif args.action == 'worker': result = run_worker(work, metadata, args.request_id, allow_full=args.allow_full, loopback_s3=args.loopback_s3)
    elif args.action == 'stop':
        (work / 'STOP').touch(mode=0o600)
        result = {'stop_requested': True}
    else:
        result = corrupt_artifact(metadata, args.run_id)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
