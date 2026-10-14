"""Pinned OpenPGP streams. Secrets travel only through an inherited private pipe."""
import contextlib
import hashlib
import hmac
import os
import pathlib
import selectors
import shutil
import signal
import threading
import subprocess
import tempfile
import time
import unicodedata

from .common import BackupError
from .process import child_setup, safe_environment

FORMAT = 'openpgp-symmetric-aes256-seipd-v1'
SCHEME = 'pbkdf2-hmac-sha256-v1'
ITERATIONS = 600000
STATUS_LIMIT = 65536
CHUNK = 65536


def validate_secret(secret):
    try:
        encoded = secret.encode('utf-8') if isinstance(secret, str) else b''
    except UnicodeError:
        raise BackupError('archive-key-invalid') from None
    if (not isinstance(secret, str) or len(secret) < 20 or len(encoded) > 1024
            or any(unicodedata.category(char) == 'Cc' for char in secret)):
        raise BackupError('archive-key-invalid')
    return encoded


def fingerprint(secret, parameters):
    import re
    if (not isinstance(parameters, dict) or set(parameters) != {'scheme', 'salt', 'iterations', 'value'}
            or parameters.get('scheme') != SCHEME or type(parameters.get('iterations')) is not int or parameters.get('iterations') != ITERATIONS
            or not all(isinstance(parameters.get(name), str) and re.fullmatch('[0-9a-f]{64}', parameters[name]) for name in ('salt', 'value'))):
        raise BackupError('archive-key-invalid')
    value = hashlib.pbkdf2_hmac('sha256', validate_secret(secret),
                              b'paymenter-backups-key-fingerprint-v1\0' + bytes.fromhex(parameters['salt']),
                              ITERATIONS, 32).hex()
    if not hmac.compare_digest(value, parameters['value']):
        raise BackupError('archive-key-mismatch')
    return value


def key(envelope, selected=None, active=False):
    import re
    namespace = envelope.get('archive_encryption_credentials') or {}
    if not namespace:
        raise BackupError('archive-key-required' if active else 'archive-key-unavailable')
    if (not isinstance(namespace, dict) or type(namespace.get('version')) is not int or namespace['version'] != 1
            or not isinstance(namespace.get('keys'), dict)
            or any(value is not None and (not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value))
                   for value in (namespace.get('active_fingerprint'), namespace.get('pending_fingerprint')))):
        raise BackupError('archive-key-invalid')
    selected = selected or namespace.get('active_fingerprint')
    if active and selected is None:
        selected = namespace.get('pending_fingerprint')
    if selected is not None and (not isinstance(selected, str) or not re.fullmatch('[0-9a-f]{64}', selected)):
        raise BackupError('archive-key-invalid')
    record = namespace['keys'].get(selected)
    if not record:
        raise BackupError('archive-key-required' if active else 'archive-key-unavailable')
    if (not isinstance(record, dict) or record.get('acknowledgement') is not None and not isinstance(record.get('acknowledgement'), dict)
            or record.get('source') not in ('generated', 'passphrase')
            or record.get('state') not in ('pending', 'active', 'retired')
            or fingerprint(record.get('secret'), record.get('fingerprint')) != selected):
        raise BackupError('archive-key-invalid')
    if active and (record.get('state') != 'active' or (record.get('acknowledgement') or {}).get('fingerprint') != selected):
        raise BackupError('archive-key-save-required')
    return record


def enabled(envelope):
    return bool(envelope['settings'].get('s3_encryption', True))


def tool(envelope):
    configured = (envelope.get('tools') or {}).get('gpg') or envelope['settings'].get('gpg')
    binary = configured or ('/usr/bin/gpg' if pathlib.Path('/usr/bin/gpg').is_file() and os.access('/usr/bin/gpg', os.X_OK) else shutil.which('gpg'))
    if binary and not configured:
        binary = str(pathlib.Path(binary).absolute())
    if not binary or not pathlib.Path(binary).is_absolute() or not pathlib.Path(binary).is_file() or not os.access(binary, os.X_OK):
        raise BackupError('archive-encryption-tool-missing')
    agent = str(pathlib.Path(binary).with_name('gpg-agent'))
    if not pathlib.Path(agent).is_file() or not os.access(agent, os.X_OK):
        raise BackupError('archive-encryption-tool-unusable')
    config = pathlib.Path(agent).resolve().with_name('gpgconf')
    if not config.is_file() or not os.access(config, os.X_OK):
        raise BackupError('archive-encryption-tool-unusable')
    return binary, agent


def readiness(envelope, key_status=None):
    if not enabled(envelope):
        return None
    try:
        tool(envelope)
        if key_status is None:
            key(envelope, active=True)
        elif key_status.get("code"):
            raise BackupError(key_status["code"])
    except BackupError as error:
        return error.code
    return None


def _stop(proc):
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


@contextlib.contextmanager
def owned_home(agent, deadline):
    """A foreground agent has parent-death SIGKILL; GPG cannot autostart helpers."""
    parent = os.getpid()
    with tempfile.TemporaryDirectory(prefix='pb-gpg-') as directory:
        os.chmod(directory, 0o700)
        config = str(pathlib.Path(agent).resolve().with_name('gpgconf'))
        if not pathlib.Path(config).is_file() or not os.access(config, os.X_OK):
            raise BackupError('archive-encryption-tool-unusable')
        try:
            discovered = subprocess.run([config, '--homedir', directory, '--list-dirs', 'agent-socket'],
                                        capture_output=True, timeout=max(0.01, min(5, deadline - time.monotonic())),
                                        env=safe_environment())
            if discovered.returncode or len(discovered.stdout) > 4096:
                raise ValueError()
            from urllib.parse import unquote
            socket = pathlib.Path(unquote(discovered.stdout.decode().strip()))
            if not socket.is_absolute() or '\0' in str(socket):
                raise ValueError()
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            raise BackupError('archive-encryption-tool-unusable') from None
        proc = subprocess.Popen([agent, '--homedir', directory, '--no-options', '--daemon', '--no-detach',
                                 '--disable-scdaemon', '--allow-loopback-pinentry', '--no-allow-external-cache'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, start_new_session=True,
                                preexec_fn=lambda: child_setup(parent), env=safe_environment())
        try:
            while not socket.is_socket():
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise BackupError('archive-encryption-tool-unusable')
                time.sleep(0.01)
            yield directory
        finally:
            _stop(proc)
            # gpgconf derives this invocation's socket directory from its unique
            # homedir; personal agents and sockets are never addressed.
            try:
                subprocess.run([config, '--homedir', directory, '--remove-socketdir'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, env=safe_environment())
            except (OSError, subprocess.TimeoutExpired):
                pass


@contextlib.contextmanager
def stream_deadline(seconds):
    """Bound blocking source reads as well as process pipes on Linux.

    An active caller timer is never overwritten; non-main-thread invocations
    refuse because Python cannot install a safe process-wide alarm there.
    """
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        raise BackupError('archive-encryption-tool-unusable')
    previous_handler = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    if timer[0] or timer[1]:
        raise BackupError('archive-encryption-tool-unusable')
    def expired(signum, frame):
        raise BackupError('child-timeout')
    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def transform(envelope, source, secret, decrypt=False, maximum=None, budget_check=None, timeout=None):
    seconds = envelope['settings'].get('capture_timeout', 300) if timeout is None else timeout
    with stream_deadline(seconds):
        yield from _transform(envelope, source, secret, decrypt, maximum, budget_check, seconds)


def _transform(envelope, source, secret, decrypt=False, maximum=None, budget_check=None, timeout=None):
    """Yield bounded output and require successful exit/profile before completion.

    Consumers must withhold publication until iteration finishes. No stderr is
    returned: tool diagnostics can contain sensitive plaintext or key material.
    """
    binary, agent = tool(envelope)
    secret_bytes = validate_secret(secret)
    deadline = time.monotonic() + (envelope['settings'].get('capture_timeout', 300) if timeout is None else timeout)
    parent = os.getpid()
    with owned_home(agent, deadline) as home:
        phrase_read, phrase_write = os.pipe()
        status_read, status_write = os.pipe()
        proc = None
        try:
            command = [binary, '--no-options', '--homedir', home, '--batch', '--no-tty',
                       '--pinentry-mode', 'loopback', '--no-symkey-cache', '--no-autostart',
                       '--disable-dirmngr', '--no-auto-key-retrieve', '--no-auto-key-import', '--rfc4880']
            if not decrypt:
                command += ['--cipher-algo', 'AES256', '--compress-algo', 'none', '--s2k-mode', '3',
                            '--s2k-digest-algo', 'SHA256', '--s2k-count', '65011712', '--set-filename', '']
            command += ['--passphrase-fd', str(phrase_read), '--status-fd', str(status_write),
                        '--output', '-', '--decrypt' if decrypt else '--symmetric']
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    pass_fds=(phrase_read, status_write), close_fds=True, start_new_session=True,
                                    preexec_fn=lambda: child_setup(parent), env=safe_environment())
            os.close(phrase_read); phrase_read = None
            os.close(status_write); status_write = None
            os.write(phrase_write, secret_bytes + b'\n')
            os.close(phrase_write); phrase_write = None
            status = bytearray()
            stderr_size = 0
            total = 0
            pending = b''
            with selectors.DefaultSelector() as selector:
                for stream, kind in ((proc.stdout, 'out'), (proc.stderr, 'err'), (status_read, 'status')):
                    os.set_blocking(stream if isinstance(stream, int) else stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, kind)
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE, 'in')
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise BackupError('child-timeout')
                    if budget_check:
                        budget_check()
                    for event, _ in selector.select(0.05):
                        if event.data == 'in':
                            if not pending:
                                pending = source.read(CHUNK)
                                if not pending:
                                    selector.unregister(proc.stdin)
                                    proc.stdin.close()
                                    continue
                            try:
                                written = os.write(event.fd, pending)
                                pending = pending[written:]
                            except BrokenPipeError:
                                selector.unregister(proc.stdin)
                                proc.stdin.close()
                        else:
                            chunk = os.read(event.fd, CHUNK)
                            if not chunk:
                                selector.unregister(event.fileobj)
                                continue
                            if event.data == 'out':
                                total += len(chunk)
                                if maximum is not None and total > maximum:
                                    raise BackupError('remote-object-too-large' if decrypt else 'local-byte-budget-exceeded')
                                yield chunk
                            elif event.data == 'status':
                                status.extend(chunk)
                                if len(status) > STATUS_LIMIT:
                                    raise BackupError('archive-encryption-tool-unusable')
                            else:
                                stderr_size += len(chunk)
                                if stderr_size > STATUS_LIMIT:
                                    raise BackupError('archive-encryption-tool-unusable')
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            tokens = [line.split()[1:] for line in bytes(status).splitlines() if line.startswith(b'[GNUPG:] ')]
            names = {parts[0] for parts in tokens if parts}
            if decrypt:
                infos = [parts[1:] for parts in tokens if parts and parts[0] == b'DECRYPTION_INFO']
                if (b'BADMDC' in names or b'ERRMDC' in names or b'NODATA' in names
                        or not infos or any(info[:2] != [b'2', b'9'] or len(info) > 2 and info[2] != b'0' for info in infos)):
                    raise BackupError('archive-integrity-failed')
                if proc.returncode or not {b'DECRYPTION_OKAY', b'END_DECRYPTION'} <= names:
                    raise BackupError('archive-decryption-failed')
            elif (proc.returncode or b'END_ENCRYPTION' not in names
                  or [parts[1:] for parts in tokens if parts and parts[0] == b'BEGIN_ENCRYPTION'] not in ([[b'2', b'9']], [[b'2', b'9', b'0']])):
                raise BackupError('archive-encryption-failed')
        except (OSError, subprocess.TimeoutExpired):
            raise BackupError('archive-decryption-failed' if decrypt else 'archive-encryption-failed') from None
        finally:
            _stop(proc)
            if proc:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream and not stream.closed:
                        stream.close()
            for descriptor in (phrase_read, phrase_write, status_read, status_write):
                if descriptor is not None:
                    os.close(descriptor)


class Reader:
    """Small pull adapter over a supervised transformation generator."""
    def __init__(self, chunks):
        self.chunks = chunks
        self.pending = b''
        self.position = 0
    def read(self, size=-1):
        size = CHUNK if size < 0 else size
        while len(self.pending) < size:
            try:
                self.pending += next(self.chunks)
            except StopIteration:
                break
        data, self.pending = self.pending[:size], self.pending[size:]
        self.position += len(data)
        return data
    def tell(self):
        return self.position
    def close(self):
        self.chunks.close()
    def __enter__(self):
        return self
    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                for _ in iter(lambda: self.read(CHUNK), b''):
                    pass
        finally:
            self.close()
