"""Deployment-owned guarded scheduler selection; unset means Artisan only."""
import os
import pathlib
import re
import shlex

SETTING = 'PAYMENTER_BACKUPS_GUARDED_SCHEDULER'


def guarded_scheduler_path():
    value = os.environ.get(SETTING, '')
    if (not value.startswith('/') or not re.fullmatch(r'[A-Za-z0-9_@%+=:,./ -]+', value)
            or '//' in value
            or pathlib.PurePosixPath(value).as_posix() != value
            or any(part in ('.', '..') for part in value.split('/')) or value == '/'):
        return ''
    return value


def cron_environment_assignment(line):
    """Standalone crontab variables configure jobs; they do not launch one."""
    match = re.fullmatch(r"\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.*?)\s*", line)
    if not match:
        return False
    value = match.group(1)
    return not value.startswith(("'", '"')) or (len(value) >= 2 and value[-1] == value[0])


def guarded_relevant(line, app):
    # Malformed guarded launchers must refuse rather than disappear. This is
    # only a relevance filter: the strict parser still authorizes every token.
    if str(app) != '/app':
        return False
    path = guarded_scheduler_path()
    if 'scheduler.php' in line:
        return True
    if path and path in line:
        return True
    try:
        words = shlex.split(line.replace(r'\%', '%'))
    except ValueError:
        return '/usr/local/bin/php' in line
    if path and path in ' '.join(words):
        return True
    # A formerly configured script may have any basename or extension. Keep
    # direct PHP script launchers visible when the setting changes or clears;
    # only known Artisan launchers can be delegated to normal app filtering.
    for index, word in enumerate(words):
        # systemd prefixes the first ExecStart argument with argv[]=.
        if word.removeprefix('argv[]=') != '/usr/local/bin/php':
            continue
        tail = words[index + 1:]
        # Interpreter options are valid on ordinary Artisan launchers. They
        # cannot turn a different application's job into this app's writer.
        while tail and tail[0] in ('-q', '-f', '-d'):
            count = 2 if tail[0] == '-d' else 1
            tail = tail[count:]
        if tail and pathlib.PurePosixPath(tail[0]).name == 'artisan':
            command = tail[1:]
            while command and command[0] in ('--quiet', '-q', '--no-interaction', '-n'):
                command = command[1:]
            if command and re.fullmatch(r'[A-Za-z][A-Za-z0-9:_-]*', command[0]):
                continue  # Ordinary app/command filtering handles Artisan.
        # No command still describes a possible retired guarded script, even
        # when its basename is artisan and its output is redirected.
        return True
    return False
