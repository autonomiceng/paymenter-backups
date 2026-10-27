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
    if not path:
        return False
    name = pathlib.PurePosixPath(path).name
    if name in line:
        return True
    try:
        return name in ' '.join(shlex.split(line.replace(r'\%', '%')))
    except ValueError:
        return False
