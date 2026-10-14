"""Fixed remote inventory and bounded, canonical transport/recovery documents."""
import hashlib
import json
import re

from .common import BackupError
from .encryption import FORMAT, SCHEME, ITERATIONS

PLAINTEXT = 'plaintext-v1'
LIMIT = 1024 * 1024 + 65536
DESCRIPTOR_LIMIT = 16384


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode() + b'\n'


def format_of(remote):
    if remote.get('format', PLAINTEXT) == PLAINTEXT and any(name in remote for name in ('key_fingerprint', 'key', 'archive_ciphertext', 'recovery_ciphertext', 'recovery_key', 'spools', 'descriptor')):
        raise BackupError('remote-format-unsupported')
    value = remote.get('format', PLAINTEXT)
    if value not in (PLAINTEXT, FORMAT):
        raise BackupError('remote-format-unsupported')
    return value


def multipart_format(record):
    """Multipart `key` is an object name, unlike a remote descriptor's key.

    Only this resolver removes that upload-specific field. Encryption metadata
    still requires an explicit supported format.
    """
    if not isinstance(record, dict) or not isinstance(record.get('key'), str):
        raise BackupError('remote-format-unsupported')
    return format_of({name: value for name, value in record.items() if name != 'key'})


def inventory(base, run_id, format):
    if format not in (PLAINTEXT, FORMAT):
        raise BackupError('remote-format-unsupported')
    result = {'archive_key': base + run_id + ('.tar.gz.gpg' if format == FORMAT else '.tar.gz'),
              'manifest_key': base + run_id + '.manifest.json'}
    if format == FORMAT:
        result['recovery_key'] = base + run_id + '.recovery.json.gpg'
    return result


def owned(remote, scope, run_id):
    names = inventory(scope['prefix'], run_id, format_of(remote))
    return remote.get('scope') == scope and remote.get('bucket') == scope['bucket'] and all(remote.get(k) == v for k, v in names.items())


def digest(data):
    return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def valid_hash(value):
    return (isinstance(value, dict) and set(value) == {'size', 'sha256'} and type(value['size']) is int
            and value['size'] > 0 and isinstance(value['sha256'], str) and re.fullmatch('[0-9a-f]{64}', value['sha256']))


def validate_parameters(value):
    if (not isinstance(value, dict) or set(value) != {'scheme', 'salt', 'iterations', 'value'}
            or value['scheme'] != SCHEME or type(value['iterations']) is not int or value['iterations'] != ITERATIONS
            or not all(isinstance(value[k], str) and re.fullmatch('[0-9a-f]{64}', value[k]) for k in ('salt', 'value'))):
        raise BackupError('remote-descriptor-invalid')


def descriptor(installation, run_id, remote):
    return {'version': 1, 'format': FORMAT, 'installation_uuid': installation, 'run_id': run_id,
            'key': remote['key'], 'objects': {
                'archive': dict(remote['archive_ciphertext'], name=remote['archive_key']),
                'recovery': dict(remote['recovery_ciphertext'], name=remote['recovery_key'])}}


def validate_descriptor(value, installation, run_id, remote):
    if (not isinstance(value, dict) or set(value) != {'version', 'format', 'installation_uuid', 'run_id', 'key', 'objects'}
            or type(value['version']) is not int or value['version'] != 1 or value['format'] != FORMAT or value['installation_uuid'] != installation
            or value['run_id'] != run_id or value.get('key') != remote.get('key')
            or value != descriptor(installation, run_id, remote)):
        raise BackupError('remote-descriptor-invalid')
    validate_parameters(value['key'])
    for entry in value['objects'].values():
        if not valid_hash({k: v for k, v in entry.items() if k != 'name'}):
            raise BackupError('remote-descriptor-invalid')
    return value


def recovery(installation, run_id, remote, manifest):
    return {'version': 1, 'format': FORMAT, 'installation_uuid': installation, 'run_id': run_id,
            'key': remote['key'], 'scope': remote['scope'],
            'archive': dict(remote['archive_ciphertext'], name=remote['archive_key']), 'manifest': manifest}


def decode(data, limit=LIMIT):
    if len(data) > limit:
        raise BackupError('remote-descriptor-invalid')
    try:
        value = json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if encode(value) != data:
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise BackupError('remote-descriptor-invalid') from None
    return value


def validate_recovery(value, installation, run_id, remote, manifest):
    if not isinstance(value, dict) or type(value.get('version')) is not int or value != recovery(installation, run_id, remote, manifest):
        raise BackupError('remote-descriptor-invalid')
    return value


def _location(state, item):
    from . import artifacts
    import os
    path = state.run_path(item['run_id'])
    descriptor = artifacts.open_directory(path)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BackupError('delivery-ciphertext-unavailable')
        return artifacts.Location(path, path / 'delivery.archive.gpg', path / 'delivery.recovery.gpg', (info.st_dev, info.st_ino))
    finally:
        os.close(descriptor)


def _spool_reader(state, item, kind):
    from . import artifacts
    import contextlib
    import os
    location = _location(state, item)
    receipt = item['delivery_intent']['spools'][kind]
    @contextlib.contextmanager
    def reader():
        try:
            with artifacts.open_owned(location, receipt['name']) as (fd, info):
                if artifacts.identity(info) != receipt['identity']:
                    raise BackupError('delivery-ciphertext-unavailable')
                with os.fdopen(os.dup(fd), 'rb') as stream:
                    yield stream
                if artifacts.identity(os.fstat(fd)) != receipt['identity']:
                    raise BackupError('delivery-ciphertext-unavailable')
        except BackupError as error:
            if error.code.startswith('artifact-'):
                raise BackupError('delivery-ciphertext-unavailable') from None
            raise
    return reader


def _write_spool(state, envelope, item, kind, chunks):
    from . import artifacts
    from .engine import output_limit, space
    import os
    location = _location(state, item)
    name = 'delivery.' + kind + '.gpg'
    directory = artifacts.open_directory(location[0])
    fd = None
    completed = False
    try:
        if (os.fstat(directory).st_dev, os.fstat(directory).st_ino) != location.key:
            raise BackupError('delivery-ciphertext-unavailable')
        available = output_limit(state, envelope['settings'])
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        total, hasher = 0, hashlib.sha256()
        with os.fdopen(os.dup(fd), 'wb') as output:
            for chunk in chunks:
                total += len(chunk)
                if total > available:
                    raise BackupError('local-byte-budget-exceeded')
                output.write(chunk)
                output.flush()
                hasher.update(chunk)
                space(state, envelope['settings'])
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory)
        receipt = {'name': name, 'identity': artifacts.identity(os.fstat(fd))}
        item['delivery_intent']['spools'][kind] = receipt
        state.save(item)
        completed = True
        return receipt, {'size': total, 'sha256': hasher.hexdigest()}
    finally:
        if fd is not None and not completed:
            try:
                named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if artifacts.identity(named) == artifacts.identity(os.fstat(fd)):
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
            except FileNotFoundError:
                pass
        if hasattr(chunks, 'close'):
            chunks.close()
        if fd is not None:
            os.close(fd)
        os.close(directory)


def cleanup_spools(state, item, remote):
    from . import artifacts
    import os
    location = _location(state, item)
    expected = {value['name']: value['identity'] for value in remote.get('spools', {}).values()}
    artifacts.remove_owned(location, list(expected), expected)


def prepare(state, envelope, item, manifest):
    try:
        return _prepare(state, envelope, item, manifest)
    except Exception:
        remote = item.get('delivery_intent') or {}
        # No provider call precedes preparation. A caught failure may discard
        # only receipts we created; a process crash leaves a refusing intent.
        cleanup_spools(state, item, remote)
        item.pop('delivery_intent', None)
        state.save(item)
        raise


class HashReader:
    def __init__(self, source, expected):
        self.source, self.expected = source, expected
        self.hasher, self.size = hashlib.sha256(), 0
    def read(self, size=-1):
        data = self.source.read(size)
        self.size += len(data)
        self.hasher.update(data)
        if self.size > self.expected['size']:
            raise BackupError('archive-integrity-failed')
        return data
    def tell(self):
        return self.size
    def __enter__(self):
        return self
    def __exit__(self, kind, value, traceback):
        if kind is None:
            for _ in iter(lambda: self.read(65536), b''):
                pass
            if (self.size, self.hasher.hexdigest()) != (self.expected['size'], self.expected['sha256']):
                raise BackupError('archive-integrity-failed')


def _prepare(state, envelope, item, manifest):
    from . import artifacts, encryption, storage
    from .engine import check_archive, output_limit, space
    import io
    import time
    record = encryption.key(envelope, active=True)
    scope = storage.remote_scope(state, envelope)
    remote = dict(inventory(scope['prefix'], item['run_id'], FORMAT), bucket=scope['bucket'], scope=scope,
                  format=FORMAT, key=record['fingerprint'], key_fingerprint=record['fingerprint']['value'], spools={})
    item['delivery_intent'] = remote
    # A crash during preparation refuses automatic regeneration of this generation.
    item['delivery_intent']['preparing'] = True
    state.save(item)
    with artifacts.open_archive(state, item['run_id'], item) as artifact:
        remote['source_identity'] = artifacts.identity(artifact.info)
        remote['source_archive'] = manifest['archive']
        with artifact.reader() as raw:
            with HashReader(raw, manifest['archive']) as source:
                receipt, cipher = _write_spool(state, envelope, item, 'archive',
                    encryption.transform(envelope, source, record['secret'], maximum=output_limit(state, envelope['settings']),
                                         budget_check=lambda: space(state, envelope['settings'])))
        if not artifact.unchanged():
            raise BackupError('archive-changed-during-delivery')
    remote['spools']['archive'], remote['archive_ciphertext'] = receipt, cipher
    state.save(item)
    with _spool_reader(state, item, 'archive')() as source:
        with encryption.Reader(encryption.transform(envelope, source, record['secret'], decrypt=True,
                                maximum=manifest['archive']['size'])) as plain:
            check_archive(lambda: HashReader(plain, manifest['archive']), manifest)
    recovery_bytes = encode(recovery(state.uuid, item['run_id'], remote, manifest))
    if len(recovery_bytes) > LIMIT:
        raise BackupError('remote-descriptor-invalid')
    receipt, cipher = _write_spool(state, envelope, item, 'recovery',
                                  encryption.transform(envelope, io.BytesIO(recovery_bytes), record['secret']))
    remote['spools']['recovery'], remote['recovery_ciphertext'] = receipt, cipher
    with _spool_reader(state, item, 'recovery')() as source:
        plain = b''.join(encryption.transform(envelope, source, record['secret'], decrypt=True, maximum=LIMIT))
    if plain != recovery_bytes:
        raise BackupError('archive-integrity-failed')
    remote['descriptor'] = descriptor(state.uuid, item['run_id'], remote)
    descriptor_bytes = encode(remote['descriptor'])
    if len(descriptor_bytes) > DESCRIPTOR_LIMIT:
        raise BackupError('remote-descriptor-invalid')
    remote['descriptor_digest'] = digest(descriptor_bytes)
    remote['prepared_at'] = time.time()
    remote.pop('preparing')
    state.save(item)
    return remote


def _get(s3, remote, name, expected, limit):
    from . import storage
    if not valid_hash(expected) or expected['size'] > limit:
        raise BackupError('remote-object-too-large')
    with storage.RemoteReader(s3, dict(remote, archive_key=name), expected) as source:
        data = b''.join(iter(lambda: source.read(min(65536, limit)), b''))
    if len(data) > limit:
        raise BackupError('remote-object-too-large')
    return data


def validate_remote_metadata(state, envelope, item, s3, remote, manifest):
    from . import encryption
    import io
    public = _get(s3, remote, remote['manifest_key'], remote['descriptor_digest'], DESCRIPTOR_LIMIT)
    validate_descriptor(decode(public, DESCRIPTOR_LIMIT), state.uuid, item['run_id'], remote)
    record = encryption.key(envelope, remote['key_fingerprint'])
    if record['fingerprint'] != remote['key']:
        raise BackupError('archive-key-mismatch')
    cipher = _get(s3, remote, remote['recovery_key'], remote['recovery_ciphertext'], LIMIT + 4096)
    plain = b''.join(encryption.transform(envelope, io.BytesIO(cipher), record['secret'], decrypt=True, maximum=LIMIT))
    validate_recovery(decode(plain), state.uuid, item['run_id'], remote, manifest)
    return record


def deliver(state, envelope, item, manifest):
    from . import artifacts, encryption, storage
    import time
    remote = item.get('delivery_intent') or item.get('remote')
    if remote:
        if format_of(remote) != FORMAT or not owned(remote, storage.remote_scope(state, envelope), item['run_id']):
            raise BackupError('delivery-destination-changed')
        if remote.get('preparing'):
            raise BackupError('delivery-ciphertext-unavailable')
        encryption.key(envelope, remote['key_fingerprint'])
        validate_descriptor(remote['descriptor'], state.uuid, item['run_id'], remote)
        item['delivery_intent'] = remote
    else:
        remote = prepare(state, envelope, item, manifest)
    s3 = storage.client(envelope)
    try:
        old = item.get('multipart')
        if old:
            if (old.get('scope') != remote['scope'] or old.get('bucket') != remote['bucket']
                    or old.get('key') not in (remote['archive_key'], remote['recovery_key'])
                    or old.get('format') != FORMAT or old.get('key_fingerprint') != remote['key_fingerprint']):
                raise BackupError('multipart-destination-mismatch')
            try:
                s3.abort_multipart_upload(Bucket=old['bucket'], Key=old['key'], UploadId=old['upload_id'])
            except Exception as error:
                if getattr(error, 'response', {}).get('Error', {}).get('Code') != 'NoSuchUpload':
                    raise
            item.pop('multipart')
            state.save(item)
        changed = False
        for kind, field in (('archive', 'archive_key'), ('recovery', 'recovery_key')):
            expected = remote[kind + '_ciphertext']
            observed = storage.hash_or_missing(s3, remote['bucket'], remote[field], expected['size'])
            if observed != (expected['sha256'], expected['size']):
                if item.get('remote_delivered'):
                    # Never replace a committed randomized generation.
                    raise BackupError('s3-readback-mismatch')
                storage.upload(s3, remote['bucket'], remote[field], _spool_reader(state, item, kind), item, state)
                changed = True
            if storage.whole_hash(s3, remote['bucket'], remote[field], expected['size']) != (expected['sha256'], expected['size']):
                raise BackupError('s3-readback-mismatch')
        data = encode(remote['descriptor'])
        expected = remote['descriptor_digest']
        if storage.hash_or_missing(s3, remote['bucket'], remote['manifest_key'], DESCRIPTOR_LIMIT) != (expected['sha256'], expected['size']):
            s3.put_object(Bucket=remote['bucket'], Key=remote['manifest_key'], Body=data, ContentLength=len(data))
            changed = True
        if storage.whole_hash(s3, remote['bucket'], remote['manifest_key'], DESCRIPTOR_LIMIT) != (expected['sha256'], expected['size']):
            raise BackupError('s3-manifest-readback-mismatch')
        # Lost spools are safe only when all exact frozen remote bytes survived.
        item.update(remote_delivered=True, delivery_error=None,
                    remote=dict(remote, delivered_at=time.time()),
                    remote_observation={'status': 'delivered', 'checked_at': time.time()},
                    remote_verification={'verified_at': time.time(), 'source': 's3-delivery',
                                         'sha256': manifest['archive']['sha256'], 'scope': remote['scope'],
                                         'level': 'restore', 'format': FORMAT, 'key_fingerprint': remote['key_fingerprint']})
        item.pop('delivery_intent', None)
        item.pop('remote_pruned', None)
        state.save(item)
        try:
            cleanup_spools(state, item, remote)
            item.pop('ciphertext_cleanup_error', None)
        except BackupError as error:
            item['ciphertext_cleanup_error'] = error.code
        state.save(item)
        newer = [other for other in state.journals() if other.get('remote_delivered')
                 and not other.get('retained_deleted') and other['created_at'] > item['created_at']
                 and storage.scope_matches(state, envelope, other)]
        if not newer:
            pointer = encode({'installation_uuid': state.uuid, 'run_id': item['run_id'],
                              'manifest_key': remote['manifest_key'], 'descriptor_sha256': expected['sha256'], 'format': FORMAT})
            s3.put_object(Bucket=remote['bucket'], Key=remote['scope']['prefix'] + 'lastmanifest.json', Body=pointer, ContentLength=len(pointer))
        return {'run_id': item['run_id'], 'remote_delivered': True, 'already_delivered': not changed}
    except Exception as error:
        item['delivery_error'] = error.code if isinstance(error, BackupError) else 's3-delivery-failed'
        state.save(item)
        raise BackupError(item['delivery_error']) from None


def checked_remote(state, envelope, item, record=False, directory=None, directory_fd=None):
    """Context manager yielding only a fully verified plaintext retrieval spool."""
    import contextlib
    import os
    import pathlib
    import time
    import uuid
    from . import artifacts, encryption, storage
    from .engine import check_archive, output_limit, space
    @contextlib.contextmanager
    def checked():
        if not storage.scope_matches(state, envelope, item):
            raise BackupError('remote-scope-changed')
        remote = item.get('remote') or item['delivery_intent']
        manifest = storage.owned_manifest(state, item)
        s3 = storage.client(envelope)
        temporary = None
        fd = None
        try:
            record_key = validate_remote_metadata(state, envelope, item, s3, remote, manifest)
            with storage.RemoteReader(s3, remote, remote['archive_ciphertext']) as source:
                with encryption.Reader(encryption.transform(envelope, source, record_key['secret'], decrypt=True,
                                       maximum=manifest['archive']['size'])) as raw:
                    with HashReader(raw, manifest['archive']) as plain:
                        if directory is None:
                            check_archive(lambda: contextlib.nullcontext(plain), manifest)
                        else:
                            if directory_fd is None:
                                raise BackupError('artifact-location-invalid')
                            available = output_limit(state, envelope['settings'], pathlib.Path(directory))
                            if manifest['archive']['size'] > available:
                                raise BackupError('local-byte-budget-exceeded')
                            temporary = pathlib.Path(directory) / ('.remote-' + uuid.uuid4().hex + '.partial')
                            item['retrieve_work'] = {'temporary': temporary.name, 'scope': remote['scope'], 'identity': None}
                            state.save(item)
                            fd = os.open(temporary.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         0o600, dir_fd=directory_fd)
                            item['retrieve_work']['identity'] = artifacts.identity(os.fstat(fd))
                            state.save(item)
                            with os.fdopen(os.dup(fd), 'wb') as output:
                                for chunk in iter(lambda: plain.read(65536), b''):
                                    output.write(chunk)
                                    output.flush()
                                    space(state, envelope['settings'])
                                os.fsync(output.fileno())
            if fd is not None:
                def reader():
                    os.lseek(fd, 0, os.SEEK_SET)
                    return os.fdopen(os.dup(fd), 'rb')
                check_archive(reader, manifest)
                identity = artifacts.identity(os.fstat(fd))
                if artifacts.identity(os.stat(temporary.name, dir_fd=directory_fd, follow_symlinks=False)) != identity:
                    raise BackupError('retrieve-spool-changed')
                item['retrieve_work']['verified_identity'] = identity
                state.save(item)
            if record:
                item['remote_observation'] = {'status': 'delivered', 'checked_at': time.time()}
                item['remote_verification'] = {'verified_at': time.time(), 'source': 's3-verify', 'level': 'restore',
                    'format': FORMAT, 'key_fingerprint': remote['key_fingerprint'],
                    'sha256': manifest['archive']['sha256'], 'scope': remote['scope']}
                state.save(item)
            yield temporary, manifest
        except Exception as error:
            code = error.code if isinstance(error, BackupError) else 'remote-check-failed'
            if getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound'):
                code = 'remote-copy-missing'
            if record:
                item.pop('remote_verification', None)
                item['remote_observation'] = {'status': 'missing' if code == 'remote-copy-missing' else 'unknown',
                                              'checked_at': time.time(), 'error': code}
                state.save(item)
            raise BackupError(code) from None
        finally:
            if fd is not None:
                try:
                    named = os.stat(temporary.name, dir_fd=directory_fd, follow_symlinks=False)
                    if artifacts.identity(named) == artifacts.identity(os.fstat(fd)):
                        os.unlink(temporary.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                os.close(fd)
                if item.get('retrieve_work', {}).get('temporary') == temporary.name:
                    item.pop('retrieve_work', None)
                    state.save(item)
    return checked()


def observe(state, envelope, item):
    from . import storage
    import time
    remote = item.get('remote') or item['delivery_intent']
    s3 = storage.client(envelope)
    result = 'present'
    try:
        validate_descriptor(remote['descriptor'], state.uuid, item['run_id'], remote)
        for key, receipt in ((remote['archive_key'], remote['archive_ciphertext']),
                             (remote['recovery_key'], remote['recovery_ciphertext']),
                             (remote['manifest_key'], remote['descriptor_digest'])):
            try:
                length = s3.head_object(Bucket=remote['bucket'], Key=key)['ContentLength']
            except Exception as error:
                if getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404', 'NotFound'):
                    result = 'missing'
                    break
                raise
            if length != receipt['size']:
                result = 'damaged'
                break
        if result == 'present':
            public = _get(s3, remote, remote['manifest_key'], remote['descriptor_digest'], DESCRIPTOR_LIMIT)
            validate_descriptor(decode(public, DESCRIPTOR_LIMIT), state.uuid, item['run_id'], remote)
    except Exception as error:
        item['remote_observation'] = {'status': 'unknown', 'checked_at': time.time(),
                                      'error': error.code if isinstance(error, BackupError) else 'remote-check-failed'}
        state.save(item)
        raise BackupError(item['remote_observation']['error']) from None
    item['remote_observation'] = {'status': result, 'checked_at': time.time()}
    if result != 'present':
        item.pop('remote_verification', None)
    state.save(item)
    return result


def remove(state, s3, bucket, base, item, intent_key, failure, preverified):
    from . import storage
    remote = item.get('remote') or item['delivery_intent']
    expected = inventory(base, item['run_id'], FORMAT)
    if remote.get('bucket') != bucket or any(remote.get(k) != v for k, v in expected.items()):
        raise BackupError(failure + '-ownership-mismatch')
    validate_descriptor(remote['descriptor'], state.uuid, item['run_id'], remote)
    intent = item[intent_key]
    if not preverified and not intent.get('remote_verified'):
        raise BackupError(failure + '-remote-verification-failed')
    try:
        for key, receipt in ((remote['manifest_key'], remote['descriptor_digest']),
                             (remote['recovery_key'], remote['recovery_ciphertext']),
                             (remote['archive_key'], remote['archive_ciphertext'])):
            observed = storage.hash_or_missing(s3, bucket, key, receipt['size'])
            if observed is not None and observed != (receipt['sha256'], receipt['size']):
                raise BackupError(failure + '-remote-verification-failed')
            if observed is None and not intent.get('remote_verified') and not item.get('delivery_intent'):
                raise BackupError(failure + '-remote-verification-failed')
        intent['remote_verified'] = True
        intent['objects'] = {key: remote[key] for key in expected}
        state.save(item)
        for field in ('manifest_key', 'recovery_key', 'archive_key'):
            s3.delete_object(Bucket=bucket, Key=remote[field])
        cleanup_spools(state, item, remote)
    except BackupError:
        raise
    except Exception:
        raise BackupError(failure + '-remote-delete-failed') from None


def destination_test(state, envelope):
    from . import encryption, storage
    import io
    import os
    record = encryption.key(envelope, active=True)
    payload = os.urandom(1024)
    cipher = b''.join(encryption.transform(envelope, io.BytesIO(payload), record['secret'], maximum=16384))
    s3 = storage.client(envelope)
    import uuid
    name = storage.prefix(state, envelope) + 'probes/' + uuid.uuid4().hex + '.gpg'
    bucket = envelope['settings']['s3_bucket']
    failure = None
    try:
        s3.put_object(Bucket=bucket, Key=name, Body=cipher, ContentLength=len(cipher))
        remote = {'bucket': bucket}
        received = _get(s3, remote, name, digest(cipher), 16384)
        plain = b''.join(encryption.transform(envelope, io.BytesIO(received), record['secret'], decrypt=True, maximum=len(payload)))
        if plain != payload:
            raise BackupError('destination-readback-mismatch')
    except Exception as error:
        failure = error.code if isinstance(error, BackupError) else 'destination-test-failed'
    finally:
        try:
            s3.delete_object(Bucket=bucket, Key=name)
        except Exception:
            failure = 'destination-probe-cleanup-failed'
    if failure:
        raise BackupError(failure)
    return {'destination': 's3', 'destination_id': storage.destination_id(state, envelope),
            'verified': True, 'format': FORMAT, 'key_fingerprint': record['fingerprint']['value']}


def check_pending_source(state, envelope, item, manifest):
    """Authorize cleanup of a frozen, partially uploaded generation.

    Its public commit may not exist. The intact local source and exact frozen
    archive must still prove a complete restore; removal then checks every
    existing object and permits only journaled absences.
    """
    from . import artifacts, encryption
    from .engine import verify, check_archive
    remote = item['delivery_intent']
    if remote.get('preparing'):
        raise BackupError('delivery-ciphertext-unavailable')
    validate_descriptor(remote['descriptor'], state.uuid, item['run_id'], remote)
    record = encryption.key(envelope, remote['key_fingerprint'])
    if record['fingerprint'] != remote['key']:
        raise BackupError('archive-key-mismatch')
    if verify(state, item['run_id'], show=True) != manifest:
        raise BackupError('archive-changed-during-delivery')
    with artifacts.open_archive(state, item['run_id'], item) as artifact:
        if artifacts.identity(artifact.info) != remote['source_identity']:
            raise BackupError('archive-changed-during-delivery')
    with _spool_reader(state, item, 'archive')() as source:
        with HashReader(source, remote['archive_ciphertext']) as cipher:
            with encryption.Reader(encryption.transform(envelope, cipher, record['secret'], decrypt=True,
                                    maximum=manifest['archive']['size'])) as plain:
                check_archive(lambda: HashReader(plain, manifest['archive']), manifest)
