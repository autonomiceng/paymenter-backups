"""Explicit key operations. Ordinary JSON never contains recovery secrets."""
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat
import unicodedata

from .common import BackupError

COMMANDS = frozenset(('encryption-key', 'recovery-key', 'credentials-rewrap'))
SCHEME = 'pbkdf2-hmac-sha256-v1'
ITERATIONS = 600000
DOMAIN = b'paymenter-backups-key-fingerprint-v1\0'
INSTRUCTIONS = (
    'Save this key outside this server. Keep earlier keys for earlier S3 copies. '
    'Download RUN.tar.gz.gpg, RUN.recovery.json.gpg and RUN.manifest.json. '
    'In a private scratch directory, check ciphertext sizes and hashes, decrypt both '
    'encrypted objects using GnuPG into .partial files, and require successful completion. '
    'Use the shipped verify_recovery.py to verify the complete plaintext archive before '
    'extraction, then follow docs/RECOVERY.md for the isolated database/application restore. '
    'The original server, APP_KEY and credential file are not needed to decrypt these S3 copies.'
)


def add_parsers(commands):
    sub = commands.add_parser('encryption-key', help='Prepare, acknowledge or inspect an independent S3 recovery key')
    sub.add_argument('verb', choices=('generate', 'import', 'acknowledge', 'discard-pending', 'status'))
    sub = commands.add_parser('recovery-key', help='Explicitly export one recovery secret to a new private file')
    sub.add_argument('verb', choices=('export',))
    sub.add_argument('--output', required=True)
    sub.add_argument('--fingerprint')
    commands.add_parser('credentials-rewrap', help='Rewrap all credential namespaces under the current APP_KEY')


def _public(value):
    if not isinstance(value, dict) or set(value) - {'fingerprint', 'short', 'state', 'ok'}:
        raise BackupError('archive-key-invalid')
    if 'fingerprint' in value and value['fingerprint'] is not None and not _fingerprint(value['fingerprint']):
        raise BackupError('archive-key-invalid')
    if 'state' in value and value['state'] not in ('none', 'pending', 'active', 'retired'):
        raise BackupError('archive-key-invalid')
    return value


def _fingerprint(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _record(value, installation_uuid, requested=None):
    if not isinstance(value, dict) or set(value) != {'secret', 'fingerprint', 'parameters', 'installation_uuid'}:
        raise BackupError('archive-key-invalid')
    secret = value['secret']
    parameters = value['parameters']
    try:
        valid_secret = isinstance(secret, str) and len(secret) >= 20 and len(secret.encode('utf-8')) <= 1024
        valid_secret = valid_secret and not any(unicodedata.category(char) == 'Cc' for char in secret)
    except UnicodeError:
        valid_secret = False
    if (not valid_secret or not _fingerprint(value['fingerprint']) or not isinstance(parameters, dict)
            or set(parameters) != {'scheme', 'salt', 'iterations', 'value'}
            or parameters['scheme'] != SCHEME or type(parameters['iterations']) is not int
            or parameters['iterations'] != ITERATIONS or not _fingerprint(parameters['salt'])
            or parameters['value'] != value['fingerprint'] or value['installation_uuid'] != installation_uuid):
        raise BackupError('archive-key-invalid')
    if requested is not None and value['fingerprint'] != requested:
        raise BackupError('archive-key-mismatch')
    actual = hashlib.pbkdf2_hmac('sha256', secret.encode(), DOMAIN + bytes.fromhex(parameters['salt']), ITERATIONS, 32).hex()
    if actual != value['fingerprint']:
        raise BackupError('archive-key-mismatch')
    return dict(value, instructions=INSTRUCTIONS)


def _parent(state, output):
    """Hold a no-follow directory descriptor; require a private immediate parent."""
    raw = str(output)
    if (not raw.startswith('/') or '\0' in raw or raw.endswith('/')
            or any(part in ('.', '..') for part in raw.split('/'))):
        raise BackupError('recovery-key-output-invalid')
    path = pathlib.Path(raw)
    state.guard_location(path, 'recovery-key-output-invalid')
    if path == state.app or state.app in path.parents:
        raise BackupError('recovery-key-output-invalid')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise BackupError('recovery-key-output-invalid')
        try:
            os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise BackupError('recovery-key-output-exists')
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def _owned(descriptor, name, file_fd):
    try:
        named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(file_fd)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_uid', 'st_nlink')
    return all(getattr(named, field) == getattr(opened, field) for field in fields)


def _unlink_owned(descriptor, name, file_fd):
    # The held descriptor prevents inode reuse. Compare the current full identity:
    # link/unlink legitimately changes ctime and link count during publication.
    if _owned(descriptor, name, file_fd):
        os.unlink(name, dir_fd=descriptor)


def _write(descriptor, filename, record):
    temporary = '.' + filename + '.' + secrets.token_hex(12) + '.partial'
    file_fd = None
    published = False
    try:
        data = (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=descriptor)
        view = memoryview(data)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError('short write')
            view = view[written:]
        os.fsync(file_fd)
        if not _owned(descriptor, temporary, file_fd):
            raise OSError('export identity changed')
        # Link publishes without overwriting a file created after validation.
        os.link(temporary, filename, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
        if not _owned(descriptor, filename, file_fd):
            raise OSError('export identity changed')
        published = True
        _unlink_owned(descriptor, temporary, file_fd)
        os.fsync(descriptor)
    except BaseException:
        if published and file_fd is not None:
            _unlink_owned(descriptor, filename, file_fd)
        raise
    finally:
        if file_fd is not None:
            try:
                _unlink_owned(descriptor, temporary, file_fd)
            finally:
                os.close(file_fd)


def dispatch(args, state, bridge, stdin_json):
    if not state.initialized:
        raise BackupError('installation-not-initialized')
    if args.command == 'encryption-key':
        payload = stdin_json() if args.verb in ('import', 'acknowledge') else {}
        allowed = {'passphrase', 'fingerprint'} if args.verb == 'import' else {'fingerprint'} if args.verb == 'acknowledge' else set()
        # A separately exported record can be reimported with its original identity.
        if args.verb == 'import' and set(payload) == {'secret', 'fingerprint', 'parameters', 'installation_uuid', 'instructions'}:
            if not isinstance(payload['parameters'], dict) or payload['parameters'].get('value') != payload['fingerprint']:
                raise BackupError('archive-key-invalid')
            payload = {'passphrase': payload['secret'], 'fingerprint': payload['parameters']}
        if set(payload) - allowed:
            raise BackupError('archive-key-invalid')
        return _public(bridge(args, 'archive-key-' + args.verb, payload))
    if args.command == 'credentials-rewrap':
        return _public(bridge(args, 'credentials-rewrap', {}))
    if args.command != 'recovery-key' or args.verb != 'export':
        raise BackupError('command-invalid')
    if args.fingerprint is not None and not _fingerprint(args.fingerprint):
        raise BackupError('archive-key-invalid')
    descriptor = None
    try:
        from .config import load_policy
        # Native settings may have changed even when init could not refresh policy.json.
        state.apply_policy(load_policy(args, bridge, state.uuid))
        descriptor, name = _parent(state, args.output)
        payload = {} if args.fingerprint is None else {'fingerprint': args.fingerprint}
        record = _record(bridge(args, 'archive-key-export', payload), state.uuid, args.fingerprint)
        _write(descriptor, name, record)
        return {'exported': True, 'fingerprint': record['fingerprint']}
    except OSError:
        raise BackupError('recovery-key-export-failed') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
