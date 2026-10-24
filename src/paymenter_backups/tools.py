"""Deterministic executable discovery and bounded, nonsecret capability checks."""
import os
import pathlib
import re
import shutil

from .process import probe
from .common import BackupError

TRUSTED_PATH = '/usr/local/bin:/usr/bin:/bin'
CANDIDATES = {'python': ('python3',), 'php': ('php',),
              'dump': ('mariadb-dump', 'mysqldump'), 'mysql': ('mariadb', 'mysql'),
              'tar': ('tar',), 'gzip': ('gzip',), 'gpg': ('gpg',)}


def inspect(name, configured='', fallback=None, capabilities=True):
    result = {'configured': configured or '', 'resolved': None, 'ok': False, 'code': None}
    value = configured or fallback
    if not value:
        value = next((path for binary in CANDIDATES[name]
                      if (path := shutil.which(binary, path=TRUSTED_PATH))), None)
    if not value:
        result['code'] = 'tool-unavailable'
        return result
    if not isinstance(value, str) or not os.path.isabs(value) or '\0' in value:
        result['code'] = 'tool-path-invalid'
        return result
    path = pathlib.Path(value).resolve()
    result['resolved'] = str(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        result['code'] = 'tool-unavailable'
        return result
    if capabilities:
        command = [str(path), '--version']
        if name == 'python':
            command = [str(path), '-I', '-c', 'import sys; print("%d.%d" % sys.version_info[:2])']
        elif name == 'php':
            command = [str(path), '-r', 'echo PHP_SAPI, " ", PHP_VERSION;']
        try:
            returncode, stdout, stderr = probe(command, timeout=3, stdout_limit=8192, stderr_limit=8192)
            output = stdout.decode('utf-8', errors='replace')
            diagnostic = (output + '\n' + stderr.decode('utf-8', errors='replace')).lower()
            valid = returncode == 0
            if name == 'python':
                match = re.fullmatch(r'(\d+)\.(\d+)\s*', output)
                valid = valid and bool(match) and tuple(map(int, match.groups())) >= (3, 10)
            elif name == 'php':
                match = re.match(r'cli (\d+)\.(\d+)', output)
                valid = valid and bool(match) and tuple(map(int, match.groups())) >= (8, 3)
            elif name == 'tar':
                valid = valid and 'GNU tar' in output
            elif name in ('dump', 'mysql'):
                valid = valid and any(word in output.lower() for word in ('mariadb', 'mysql'))
            elif name == 'gzip':
                if 'busybox' in diagnostic:
                    result['code'] = 'gzip-busybox'
                    return result
                if not valid:
                    _, help_stdout, help_stderr = probe([str(path), '--help'], timeout=3,
                                                        stdout_limit=8192, stderr_limit=8192)
                    help_diagnostic = (help_stdout + b'\n' + help_stderr).decode('utf-8', errors='replace').lower()
                    if 'busybox' in help_diagnostic:
                        result['code'] = 'gzip-busybox'
                        return result
                valid = valid and 'gzip' in output.lower() and 'free software foundation' in diagnostic
            if not valid:
                result['code'] = 'tool-capability-unsupported'
                return result
        except (OSError, BackupError):
            result['code'] = 'tool-unavailable'
            return result
    result['ok'] = True
    return result


def checks(envelope, capabilities=False):
    settings = envelope['settings']
    return {name: inspect(name, settings.get(name, ''), envelope.get('tools', {}).get(name), capabilities)
            for name in CANDIDATES}
