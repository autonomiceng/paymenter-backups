"""Deployment-owned guarded scheduler selection; unset means Artisan only."""
import os
import pathlib
import re
import shlex

SETTING = 'PAYMENTER_BACKUPS_GUARDED_SCHEDULER'


def guarded_scheduler_path():
    value = os.environ.get(SETTING, '')
    if (not value.startswith('/') or not re.fullmatch(r'[A-Za-z0-9_@%+=:,./ -]+', value)
            or pathlib.PurePosixPath(value).as_posix() != value
            or any(part in ('.', '..') for part in value.split('/')) or value == '/'):
        return ''
    return value


def guarded_relevant(line, app):
    # Malformed guarded launchers must refuse rather than disappear. This is
    # only a relevance filter: the strict parser still authorizes every token.
    if str(app) != '/app':
        return False
    path = guarded_scheduler_path()
    if 'scheduler.php' in line:
        return True
    name = pathlib.PurePosixPath(path).name if path else ''
    if name and name in line:
        return True
    try:
        words = shlex.split(line.replace(r'\%', '%'))
    except ValueError:
        return '/usr/local/bin/php' in line
    if name and name in ' '.join(words):
        return True
    # A formerly configured script may have any basename or extension. Keep
    # direct PHP script launchers visible when the setting changes or clears;
    # only known Artisan launchers can be delegated to normal app filtering.
    for index, word in enumerate(words):
        if word != '/usr/local/bin/php':
            continue
        tail = words[index + 1:]
        if not (tail and pathlib.PurePosixPath(tail[0]).name == 'artisan'
                and any(value in ('schedule:run', 'schedule:work', 'queue:work', 'queue:listen', 'horizon') for value in tail[1:])):
            return True
    return False
