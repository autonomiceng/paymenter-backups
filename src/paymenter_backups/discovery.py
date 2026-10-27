"""Shared strict launcher parser and private, sanitized discovery cache.

Discovery covers the account crontab and system/user systemd services/timers.
Host cooperation still excludes panel wrappers, Supervisor and undeclared writers.
Cached diagnostics never authorize a capture.
"""
import hashlib
import json
import os
import pathlib
import pwd
import re
import shlex
import stat
import time

from .common import BackupError, atomic_json, private_file, read_json
from .process import run, probe
from .scheduler import guarded_scheduler_path, guarded_relevant

PHP_NAME = re.compile(r'(?:php(?:[0-9]+(?:\.[0-9]+)?)?|php-cli|ea-php[0-9]+|lsphp[0-9]+)')
UNIT_NAME = re.compile(r'(?:[A-Za-z0-9_]|\\x[0-9A-Fa-f]{2})(?:[A-Za-z0-9_.@:-]|\\x[0-9A-Fa-f]{2}){0,199}\.service')
KINDS = {'schedule:run': 'scheduler', 'queue:work': 'queue-worker', 'queue:listen': 'queue-worker', 'horizon': 'queue-worker', 'schedule:work': 'scheduler'}
PARSER_VERSION = 4
SYSTEMD_SHOW_PROPERTIES = ('Id', 'ExecStart', 'User', 'WorkingDirectory', 'DynamicUser', 'LoadState', 'Restart')
SYSTEMD_RESTART_POLICIES = frozenset(('always', 'on-success', 'on-failure', 'on-abnormal', 'on-watchdog', 'on-abort'))


def cron_quote(value):
    # Keep each percent outside the shell-quoted fragments. A backslash at the
    # end of a fragment then cannot consume the cron escape for the percent.
    return r'\%'.join(shlex.quote(part) for part in value.split('%'))


def cron_command(line):
    """Apply cron's pre-shell escaping; stdin-delimited commands refuse."""
    result = []
    index = 0
    while index < len(line):
        character = line[index]
        if character == '%':
            raise ValueError('cron stdin is unsupported')
        if character == '\\' and index + 1 < len(line):
            index += 1
            character = line[index]
            if character != '%':
                result.append('\\')
        result.append(character)
        index += 1
    return ''.join(result)


def suggestions(state, envelope, inventory=None):
    php = envelope.get('tools', {}).get('php') or '/usr/bin/php'
    prefix = '* * * * * /usr/bin/flock -n '
    suffix = ' ' + cron_quote(php) + ' ' + cron_quote(str(state.app / 'artisan'))
    scheduler = prefix + cron_quote(str(state.root / 'scheduler.lock')) + suffix + ' schedule:run >/dev/null 2>&1'
    for item in (inventory or {}).get('items', []):
        try:
            parsed = parse(item['command'], state.app, cron=False)
        except (BackupError, KeyError):
            continue
        if guarded_command(parsed['command']):
            scheduler = '* * * * * ' + ' '.join(cron_quote(word) for word in shlex.split(parsed['command'])) + ' >/dev/null 2>&1'
            break
    return {'scheduler': scheduler,
            'queue': prefix + cron_quote(str(state.root / 'queue.lock')) + suffix + ' queue:work --stop-when-empty --max-time=50 >/dev/null 2>&1'}


def guarded_command(command):
    path = guarded_scheduler_path()
    try:
        tokens = shlex.split(command)
        return bool(path and len(tokens) == 5 and tokens[:2] == ['/usr/bin/flock', '-n']
                    and tokens[3:] == ['/usr/local/bin/php', path])
    except ValueError:
        return False


def guarded_script_safe():
    """Require the configured root-owned script without file or parent symlinks."""
    try:
        configured = guarded_scheduler_path()
        if not configured:
            return False
        target = pathlib.Path(configured)
        # Reuse lock_safe's canonical-path and lstat regular-file checks.
        if target.resolve(strict=True) != target:
            return False
        info = target.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
    except (OSError, RuntimeError):
        return False


def parse(line, app, cron=True, working=None):
    """Return validated tokens only. Shell interpretation/wrappers fail closed."""
    try:
        if cron:
            line = cron_command(line)
        # Only the documented /dev/null tail is removed, never arbitrary redirects.
        line = re.sub(r'\s*(?:>>|>)\s*/dev/null(?:\s+2>&1)?\s*$', '', line)
        tokens = shlex.split(line, comments=True)
        if cron:
            if not tokens:
                raise ValueError()
            if tokens[0].startswith('@'):
                if tokens[0] not in ('@reboot', '@hourly', '@daily', '@weekly', '@monthly', '@yearly', '@annually', '@midnight'):
                    raise ValueError()
                tokens = tokens[1:]
            else:
                if len(tokens) < 6 or any(not re.fullmatch(r'[0-9*/?,\-A-Za-z]+', word) for word in tokens[:5]):
                    raise ValueError()
                tokens = tokens[5:]
        relative = working == str(app)
        if tokens[:3] == ['cd', str(app), '&&']:
            relative = True
            tokens = tokens[3:]
        if any(any(symbol in word for symbol in (';', '|', '&', '`', '$', '>', '<', '\0', '\n', '\r')) for word in tokens):
            raise ValueError()
        lock = None
        if tokens and tokens[0] in ('flock', '/usr/bin/flock', '/bin/flock'):
            tokens = tokens[1:]
            while tokens and tokens[0].startswith('-'):
                flag = tokens.pop(0)
                if flag in ('-n', '--nonblock', '-x', '--exclusive'):
                    continue
                if flag in ('-w', '--timeout', '-E', '--conflict-exit-code'):
                    value = tokens.pop(0)
                    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) or (flag in ('-E', '--conflict-exit-code') and (not value.isdecimal() or int(value) > 255)):
                        raise ValueError()
                    continue
                if re.fullmatch(r'--(?:timeout|conflict-exit-code)=[0-9]+', flag):
                    continue
                raise ValueError()
            lock = tokens.pop(0)
            if not lock.startswith('/') or '\0' in lock or pathlib.Path(lock).as_posix() != lock or any(part in ('.', '..') for part in lock.split('/')):
                raise ValueError()
        php = tokens.pop(0)
        guarded = guarded_scheduler_path()
        if guarded and tokens == [guarded]:
            if str(app) != '/app' or php != '/usr/local/bin/php' or not lock:
                raise ValueError()
            display = ['/usr/bin/flock', '-n', lock, php, guarded]
            safe = guarded_script_safe()
            return {'kind': 'scheduler', 'coordination': 'coordinated' if safe else 'unsupported',
                    'command': shlex.join(display), 'lock': lock,
                    'reason': None if safe else 'job-command-unsupported'}
        if not PHP_NAME.fullmatch(pathlib.Path(php).name):
            raise ValueError()
        # PHP ini options can contain credentials. Validate but never display them.
        while tokens and tokens[0].startswith('-'):
            flag = tokens.pop(0)
            if flag in ('-q', '-f'):
                continue
            if flag == '-d':
                value = tokens.pop(0)
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*=[A-Za-z0-9_./:\-]+', value):
                    raise ValueError()
                continue
            raise ValueError()
        artisan = tokens.pop(0)
        if artisan != str(app / 'artisan') and not (artisan == 'artisan' and relative):
            raise ValueError()
        safe = []
        while tokens and tokens[0] in ("--quiet", "-q", "--no-interaction", "-n"):
            safe.append(tokens.pop(0))
        command = tokens.pop(0)
        if command not in KINDS:
            raise ValueError()
        for word in tokens:
            if word in ('--quiet', '-q', '--no-interaction', '-n', '--stop-when-empty', '--once'):
                safe.append(word)
            elif re.fullmatch(r'--(?:max-time|max-jobs|sleep|tries|timeout|backoff|memory)=[0-9]+', word):
                safe.append(word)
            else:
                raise ValueError()
        continuous = command in ('queue:listen', 'horizon', 'schedule:work') or (command == 'queue:work' and not any(word in safe for word in ('--stop-when-empty', '--once')) and not any(word.startswith(('--max-time=', '--max-jobs=')) and word.split('=')[1] != '0' for word in safe))
        coordination = 'continuous' if continuous else 'coordinated' if lock else 'uncoordinated'
        display = (["/usr/bin/flock", "-n", lock] if lock else []) + [php, str(app / 'artisan'), command] + safe
        return {'kind': KINDS[command], 'coordination': coordination, 'command': shlex.join(display),
                'lock': lock, 'reason': 'continuous-worker-present' if continuous else 'job-uncoordinated' if not lock else None}
    except (ValueError, IndexError):
        raise BackupError('job-command-unsupported') from None


def relevant(line, app, working=None, cron=True):
    # Include malformed guarded launchers so they refuse rather than disappear.
    if guarded_relevant(line, app):
        return True
    if not any(word in line for word in KINDS):
        return False
    try:
        shell = cron_command(line) if cron else line
    except ValueError:
        return 'artisan' in line
    if working == str(app) or re.search(re.escape(str(app)) + r'(?:/|[\s;\'\"]|$)', shell):
        return True
    try:
        tokens = shlex.split(shell)
    except ValueError:
        return 'artisan' in line
    # A directly identified different application's absolute artisan is outside
    # this inventory. Relative/shell-wrapped unknown jobs remain fail-closed.
    absolute = [word for word in tokens if word.startswith('/') and word.endswith('/artisan')]
    if len(absolute) == 1:
        other_app = pathlib.Path(absolute[0]).parent.resolve()
        invocation = line
        if working is not None:
            matches = re.findall(r'argv\[\]=(.*?)\s*;\s*ignore_errors=', line)
            if len(matches) == 1:
                invocation = matches[0]
        try:
            parse(invocation, other_app, cron=cron, working=working)
        except BackupError:
            pass
        else:
            return other_app == app
    return 'artisan' in line


def record(line, app, source, label, working=None, continuous=False):
    try:
        item = parse(line, app, cron=source == 'crontab', working=working)
        if continuous and item['kind'] == 'queue-worker':
            item.update(coordination='continuous', reason='continuous-worker-present')
    except BackupError:
        item = {'kind': 'queue-worker' if any(word in line for word in ('queue:', 'horizon')) else 'scheduler',
                'coordination': 'unsupported', 'command': '', 'lock': None, 'reason': 'job-command-unsupported'}
    item.update(source=source, label=label, id=hashlib.sha256((source + ':' + label).encode()).hexdigest()[:16])
    return item


def systemd_inventory(manager, read, verb, kind):
    flags = ['--all'] if verb != 'list-unit-files' else []
    if verb != 'list-timers':
        flags += ['--type=service']
    try:
        value = json.loads(read(manager + [verb, *flags, '--output=json', '--no-pager']))
        if not isinstance(value, list):
            raise ValueError()
        return value
    except (ValueError, BackupError) as failure:
        if isinstance(failure, BackupError) and failure.code != 'child-failed':
            raise
        table = read(manager + [verb, *flags, '--no-legend', '--plain', '--full', '--no-pager'])
        value = []
        for line in table.splitlines():
            words = line.split()
            if not words:
                continue
            if kind == 'timer':
                if len(words) < 2 or not words[-2].endswith('.timer') or not UNIT_NAME.fullmatch(words[-1]):
                    raise ValueError()
                value.append({'activates': words[-1]})
            elif kind == 'service':
                if len(words) < 4 or not UNIT_NAME.fullmatch(words[0]):
                    raise ValueError()
                value.append({'unit': words[0], 'active': words[2]})
            else:
                if len(words) < 2 or not UNIT_NAME.fullmatch(words[0]):
                    raise ValueError()
                value.append({'unit_file': words[0], 'state': words[1]})
        return value


def systemd_service_units(manager, read):
    """Read standalone services that can run, including enabled inactive units."""
    services = systemd_inventory(manager, read, 'list-units', 'service')
    files = systemd_inventory(manager, read, 'list-unit-files', 'file')
    if any(len(rows) > 1000 or any(not isinstance(row, dict) for row in rows) for rows in (services, files)):
        raise ValueError()
    units = [row['unit'] for row in services if row.get('active') in ('active', 'activating')]
    file_units = [row.get('unit_file', row.get('unit')) for row in files if row.get('state') in ('enabled', 'enabled-runtime', 'static')]
    if any(not isinstance(unit, str) or not UNIT_NAME.fullmatch(unit) for unit in units + file_units) or len(set(units + file_units)) > 1000:
        raise ValueError()
    # Templates cannot be queried with show; list-units supplies running instances.
    units.extend(unit for unit in file_units if not unit.endswith('@.service'))
    return set(units)


def systemd(app, tools):
    executable = tools.get('systemctl', '/usr/bin/systemctl')
    # No system manager on a cron-only host. An expected but unreadable manager
    # is different and must refuse admission.
    if not pathlib.Path('/run/systemd/system').exists() and 'systemctl' not in tools:
        return []
    managers = [[executable, '--system']]
    runtime = pathlib.Path('/run/user') / str(os.getuid())
    if runtime.is_dir() and not runtime.is_symlink() and runtime.stat().st_uid == os.getuid() and not runtime.stat().st_mode & 0o077:
        managers.append(['/usr/bin/env', 'XDG_RUNTIME_DIR=' + str(runtime), executable, '--user'])
    deadline = time.monotonic() + 5
    def read(command):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackupError('job-discovery-incomplete')
        return run(command, remaining).decode()
    result = []
    for manager in managers:
        # Include standalone enabled/running services, not just timer targets.
        try:
            timers = systemd_inventory(manager, read, 'list-timers', 'timer')
            if len(timers) > 1000:
                raise ValueError()
            units = {row['activates'] for row in timers}
            units.update(systemd_service_units(manager, read))
            if len(units) > 1000 or any(not isinstance(unit, str) or not UNIT_NAME.fullmatch(unit) for unit in units):
                raise ValueError()
            if not units:
                continue
            raw = read(manager + ['show', '--no-pager', '--property=' + ','.join(SYSTEMD_SHOW_PROPERTIES), '--', *sorted(units)])
            blocks = re.split(r'\n\s*\n', raw.strip())
            seen = set()
            for block in blocks:
                props = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
                unit = props.get('Id')
                if unit not in units or unit in seen:
                    raise ValueError()
                seen.add(unit)
                execution, working = props.get('ExecStart', ''), props.get('WorkingDirectory')
                if props.get('LoadState') != 'loaded' or not relevant(execution, app, working, cron=False):
                    continue
                owner = props.get('User', '')
                uid = os.getuid() if '--user' in manager and not owner else int(owner) if owner.isdecimal() else pwd.getpwnam(owner or 'root').pw_uid
                if uid != os.getuid():
                    continue
                invocations = re.findall(r'argv\[\]=(.*?)\s*;\s*ignore_errors=', execution)
                if len(invocations) != 1 or props.get('DynamicUser') == 'yes':
                    result.append(record('', app, 'systemd', unit))
                    continue
                result.append(record(invocations[0], app, 'systemd', unit, working, props.get('Restart') in SYSTEMD_RESTART_POLICIES))
            if seen != units:
                raise ValueError()
        except (KeyError, ValueError, UnicodeError, BackupError, OSError):
            raise BackupError('job-discovery-incomplete') from None
    return result


def discover(state, envelope, now=None):
    items = []
    error = None
    try:
        returncode, stdout, stderr = probe([envelope.get('tools', {}).get('crontab', '/usr/bin/crontab'), '-l'])
        if returncode and not (returncode == 1 and re.fullmatch(rb'no crontab for [A-Za-z0-9_.\-]+\s*', stderr)):
            raise BackupError('job-discovery-failed')
        for index, line in enumerate(stdout.decode().splitlines()):
            if line.strip() and not line.lstrip().startswith('#') and relevant(line, state.app):
                items.append(record(line, state.app, 'crontab', 'line ' + str(index + 1)))
        items += systemd(state.app, envelope.get('tools', {}))
        if len(items) > 256:
            raise BackupError('job-discovery-incomplete')
    except (OSError, UnicodeError, BackupError) as failure:
        error = failure.code if isinstance(failure, BackupError) and failure.code in ('job-discovery-failed', 'job-discovery-incomplete') else 'job-discovery-incomplete' if isinstance(failure, BackupError) and failure.code == 'child-output-limit' else 'job-discovery-failed'
    return {'schema_version': 1, 'parser_version': PARSER_VERSION, 'app': str(state.app),
            'checked_at': time.time() if now is None else now, 'state': 'failed' if error else 'ok',
            'error': error, 'items': items[:256]}


def cached(state, now=None):
    now = time.time() if now is None else now
    try:
        target = state.root / 'discovery.json'
        private_file(target)
        value = read_json(target)
        if value.get('app') != str(state.app) or value.get('parser_version') != PARSER_VERSION or type(value.get('checked_at')) not in (int, float) or not isinstance(value.get('items'), list) or len(value['items']) > 256:
            raise ValueError()
        # Reconstruct rather than exposing cached command material wholesale.
        for item in value['items']:
            if set(item) != {'id', 'source', 'label', 'kind', 'coordination', 'command', 'lock', 'reason'} or item['source'] not in ('crontab', 'systemd'):
                raise ValueError()
            if not re.fullmatch(r'[a-f0-9]{16}', item['id']) or not (re.fullmatch(r'line [0-9]{1,6}', item['label']) if item['source'] == 'crontab' else UNIT_NAME.fullmatch(item['label'])):
                raise ValueError()
            if item['coordination'] not in ('coordinated', 'uncoordinated', 'unsupported', 'continuous') or item['kind'] not in ('scheduler', 'queue-worker') or item['reason'] not in (None, 'job-command-unsupported', 'job-uncoordinated', 'continuous-worker-present'):
                raise ValueError()
            if item['command']:
                parsed = parse(item['command'], state.app, cron=False)
                if parsed['command'] != item['command'] or parsed['lock'] != item['lock']:
                    raise ValueError()
                # Command revalidation may add a refusal, but cannot clear
                # service-derived coordination or its reason.
                if item['coordination'] == 'coordinated' and parsed['coordination'] != 'coordinated':
                    item.update(coordination=parsed['coordination'], reason=parsed['reason'])
            elif item['lock'] is not None:
                raise ValueError()
        if value['state'] not in ('ok', 'failed') or value['error'] not in (None, 'job-discovery-incomplete', 'job-discovery-failed'):
            raise ValueError()
        if not 0 <= now - value['checked_at'] <= 900:
            value['state'] = 'stale'
        return value
    except (OSError, BackupError, KeyError, TypeError, ValueError):
        return {'checked_at': None, 'state': 'unknown', 'error': None, 'items': []}


def refresh_cache(state, envelope, now=None):
    now = time.time() if now is None else now
    with state.lock('discovery.lock'):
        current = cached(state, now)
        if current['checked_at'] is not None and 0 <= now - current['checked_at'] < 300:
            return current
        value = discover(state, envelope, now)
        atomic_json(state.root / 'discovery.json', value)
        return value


def lock_safe(path):
    try:
        target = pathlib.Path(path)
        if not target.is_absolute() or target.resolve() != target:
            return False
        info = target.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
    except OSError:
        return False


def admission(envelope, inventory):
    items = []
    def add(code, field=None, detail=None):
        items.append({'code': code, 'field': field, 'detail': detail or {}})
    if inventory['state'] != 'ok':
        add(inventory.get('error') or ('job-discovery-stale' if inventory['state'] == 'stale' else 'job-discovery-unknown'))
    jobs = inventory['items']
    if not any(item['kind'] == 'scheduler' for item in jobs):
        add('scheduler-missing')
    for job in jobs:
        if job['coordination'] != 'coordinated':
            add(job['reason'] or 'job-command-unsupported', detail={'job': job['id']})
        elif (not any(word in shlex.split(job['command']) for word in KINDS)
              and (not guarded_command(job['command']) or not guarded_script_safe())):
            add('job-command-unsupported', detail={'job': job['id']})
        elif not lock_safe(job['lock']):
            add('job-lock-unsafe', 'job_lock_paths', {'job': job['id'], 'lock': job['lock']})
    derived = {item['lock'] for item in jobs if item['coordination'] == 'coordinated'}
    override = envelope['settings']['job_lock_paths']
    if override and set(override) != derived:
        add('job-lock-override-mismatch', 'job_lock_paths')
    return items, sorted(derived if not override else set(override))
