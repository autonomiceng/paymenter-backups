"""Small synthetic archives exercise the actual GnuPG transport without capture."""
import hashlib
import io
import json
import os
import pathlib
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import test_storage

from paymenter_backups import artifacts, encryption, remote_artifacts, storage
from paymenter_backups.common import BackupError, State, atomic_json
from paymenter_backups.config import DEFAULTS, redact


def key_record(secret='a' * 64):
    params = {'scheme': encryption.SCHEME, 'salt': 'ab' * 32, 'iterations': encryption.ITERATIONS, 'value': ''}
    params['value'] = hashlib.pbkdf2_hmac('sha256', secret.encode(), b'paymenter-backups-key-fingerprint-v1\0' + bytes.fromhex(params['salt']), encryption.ITERATIONS).hex()
    return {'secret': secret, 'fingerprint': params, 'state': 'active', 'source': 'generated',
            'created_at': time.time(), 'acknowledgement': {'fingerprint': params['value'], 'at': time.time(), 'actor': None}}


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}}


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.trace = []
        self.fail = None
    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        return {'Body': io.BytesIO(self.objects[Key])}
    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        return {'ContentLength': len(self.objects[Key])}
    def put_object(self, Bucket, Key, Body, ContentLength):
        self.trace.append(('put', Key))
        if self.fail and Key.endswith(self.fail):
            raise RuntimeError('synthetic upload failure')
        value = Body.read() if hasattr(Body, 'read') else Body
        assert len(value) == ContentLength
        self.objects[Key] = value
    def delete_object(self, Bucket, Key):
        self.trace.append(('delete', Key))
        if self.fail and Key.endswith(self.fail):
            raise RuntimeError('synthetic delete failure')
        self.objects.pop(Key, None)


class Encryption(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        app = root / 'app'
        (app / 'public').mkdir(parents=True)
        self.state = State(app, root / 'private')
        self.record = key_record()
        self.fp = self.record['fingerprint']['value']
        self.envelope = {'settings': dict(DEFAULTS, destination='s3', s3_encryption=True, s3_bucket='synthetic',
                                         minimum_free_bytes=0, capture_timeout=10, local_byte_budget=20 * 1024**2),
                         'tools': {'gpg': '/usr/bin/gpg'},
                         'archive_encryption_credentials': {'version': 1, 'active_fingerprint': self.fp,
                             'pending_fingerprint': None, 'keys': {self.fp: self.record}}}
        self.s3 = MemoryS3()
        self.mock_client = patch('paymenter_backups.storage.client', return_value=self.s3)
        self.provider = self.mock_client.start()
        self.addCleanup(self.mock_client.stop)
    def archive(self, run_id='synthetic', payload=None):
        if payload is None:
            payload = b'-- fake disposable SQL\n'
        manifest = {'installation_uuid': self.state.uuid, 'run_id': run_id, 'mode': 'database-online',
                    'payloads': {'database.sql': remote_artifacts.digest(payload)}}
        directory = self.state.default_destination()
        directory.mkdir(mode=0o700, exist_ok=True)
        archive = directory / (run_id + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in (('database.sql', payload), ('manifest.json', json.dumps(manifest).encode())):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        archive.chmod(0o600)
        manifest['archive'] = remote_artifacts.digest(archive.read_bytes())
        path = directory / (run_id + '.manifest.json')
        atomic_json(path, manifest)
        self.state.run_path(run_id).mkdir(mode=0o700, exist_ok=True)
        item = {'run_id': run_id, 'installation_uuid': self.state.uuid, 'mode': 'database-online',
                'created_at': time.time(), 'local_complete': True, 'service_recovered': True,
                'archive_path': str(archive), 'manifest_path': str(path), 'manifest': manifest}
        self.state.save(item)
        return item
    def test_real_gpg_roundtrip_packet_profile_and_corruption(self):
        payload = os.urandom(2 * 1024**2)
        cipher = b''.join(encryption.transform(self.envelope, io.BytesIO(payload), self.record['secret']))
        self.assertEqual(b''.join(encryption.transform(self.envelope, io.BytesIO(cipher), self.record['secret'], decrypt=True)), payload)
        # First packet is SKESK v4, AES256, iterated salted SHA256; next is SEIPD.
        self.assertEqual(cipher[:5], b'\x8c\x0d\x04\x09\x03')
        self.assertEqual(cipher[5], 8)
        self.assertEqual(cipher[14], 255)
        self.assertEqual(cipher[15] & 0x3f, 18)
        for damaged in (cipher[:-1], cipher[:-20] + bytes([cipher[-20] ^ 1]) + cipher[-19:]):
            with self.assertRaises(BackupError):
                b''.join(encryption.transform(self.envelope, io.BytesIO(damaged), self.record['secret'], decrypt=True))
        with self.assertRaises(BackupError):
            b''.join(encryption.transform(self.envelope, io.BytesIO(cipher), 'wrong' * 20, decrypt=True))
    def test_acknowledgement_and_invalid_fingerprint_fail_closed(self):
        self.record['acknowledgement'] = None
        with self.assertRaisesRegex(BackupError, 'archive-key-save-required'):
            storage.deliver(self.state, self.envelope, self.archive()['run_id'])
        self.assertFalse(self.s3.trace)
        self.record['fingerprint']['iterations'] = 1
        with self.assertRaisesRegex(BackupError, 'archive-key-invalid'):
            encryption.key(self.envelope, active=True)
    def test_three_object_order_retry_frozen_and_retrieval(self):
        item = self.archive()
        original = pathlib.Path(item['archive_path']).read_bytes()
        self.s3.fail = '.recovery.json.gpg'
        with self.assertRaisesRegex(BackupError, 's3-delivery-failed'):
            storage.deliver(self.state, self.envelope, item['run_id'])
        prepared = artifacts.journal(self.state, item['run_id'])['delivery_intent']
        frozen = dict(self.s3.objects)
        self.assertFalse(any(name.endswith('.manifest.json') for name in frozen))
        self.s3.fail = None
        storage.deliver(self.state, self.envelope, item['run_id'])
        self.assertEqual(self.s3.objects[prepared['archive_key']], frozen[prepared['archive_key']])
        puts = [name for verb, name in self.s3.trace if verb == 'put']
        self.assertEqual(puts[-3:], [prepared['recovery_key'], prepared['manifest_key'], prepared['scope']['prefix'] + 'lastmanifest.json'])
        public = self.s3.objects[prepared['manifest_key']]
        self.assertNotIn(b'payloads', public)
        self.assertNotIn(original, self.s3.objects[prepared['archive_key']])
        pathlib.Path(item['archive_path']).unlink()
        pathlib.Path(item['manifest_path']).unlink()
        item = artifacts.journal(self.state, item['run_id'])
        item['local_complete'] = False
        self.state.save(item)
        storage.retrieve(self.state, self.envelope, item['run_id'])
        self.assertEqual(pathlib.Path(item['archive_path']).read_bytes(), original)
        current = artifacts.journal(self.state, item['run_id'])
        self.assertGreater(current['local_download_until'], time.time() + 86000)
        self.assertEqual(current['remote_verification']['level'], 'restore')
    def test_missing_frozen_spool_refuses_and_remote_survivor_needs_key(self):
        item = self.archive()
        self.s3.fail = '.recovery.json.gpg'
        with self.assertRaises(BackupError):
            storage.deliver(self.state, self.envelope, item['run_id'])
        intent = artifacts.journal(self.state, item['run_id'])['delivery_intent']
        (self.state.run_path(item['run_id']) / intent['spools']['recovery']['name']).unlink()
        self.s3.fail = None
        with self.assertRaisesRegex(BackupError, 'delivery-ciphertext-unavailable'):
            storage.deliver(self.state, self.envelope, item['run_id'])
        self.assertTrue(pathlib.Path(item['archive_path']).exists())
    def test_encrypted_probe_and_budget_refusal(self):
        self.assertEqual(storage.destination_test(self.state, self.envelope)['format'], encryption.FORMAT)
        self.assertFalse(self.s3.objects)
        item = self.archive()
        self.envelope['settings']['local_byte_budget'] = 1
        with self.assertRaisesRegex(BackupError, 'local-byte-budget-exceeded'):
            storage.deliver(self.state, self.envelope, item['run_id'])
        self.assertFalse(self.s3.objects)
    def test_retired_key_target_and_survivor_refuse_without_removal(self):
        item = self.archive()
        storage.deliver(self.state, self.envelope, item['run_id'])
        current = artifacts.journal(self.state, item['run_id'])
        original = dict(self.s3.objects)
        self.envelope['archive_encryption_credentials']['keys'].clear()
        with self.assertRaisesRegex(BackupError, 'archive-key-unavailable'):
            storage.remove_copy(self.state, self.envelope, current, 's3', 'delete')
        self.assertEqual(self.s3.objects, original)
        with self.assertRaisesRegex(BackupError, 'delete-no-verified-s3-survivor'):
            storage.remote_survivor(self.state, self.envelope, [current], set())
        self.assertEqual(self.s3.objects, original)

    def test_rotation_pending_retry_uses_original_key(self):
        item = self.archive()
        self.s3.fail = '.recovery.json.gpg'
        with self.assertRaises(BackupError):
            storage.deliver(self.state, self.envelope, item['run_id'])
        original = artifacts.journal(self.state, item['run_id'])['delivery_intent']
        replacement = key_record('b' * 64)
        namespace = self.envelope['archive_encryption_credentials']
        replacement_fp = replacement['fingerprint']['value']
        self.record['state'] = 'retired'
        namespace['keys'][replacement_fp] = replacement
        namespace['active_fingerprint'] = replacement_fp
        self.s3.fail = None
        storage.deliver(self.state, self.envelope, item['run_id'])
        committed = artifacts.journal(self.state, item['run_id'])['remote']
        self.assertEqual(committed['key_fingerprint'], self.fp)
        self.assertEqual(committed['archive_ciphertext'], original['archive_ciphertext'])
        with storage.checked_remote(self.state, self.envelope, artifacts.journal(self.state, item['run_id'])):
            pass

    def test_each_object_upload_interruption_and_descriptor_repair(self):
        for index, suffix in enumerate(('.tar.gz.gpg', '.recovery.json.gpg', '.manifest.json')):
            with self.subTest(suffix=suffix):
                item = self.archive('interruption-' + str(index))
                self.s3.fail = suffix
                with self.assertRaises(BackupError):
                    storage.deliver(self.state, self.envelope, item['run_id'])
                remote = artifacts.journal(self.state, item['run_id'])['delivery_intent']
                self.assertNotIn(remote['manifest_key'], self.s3.objects)
                cipher = (self.state.run_path(item['run_id']) / remote['spools']['archive']['name']).read_bytes()
                self.s3.fail = None
                storage.deliver(self.state, self.envelope, item['run_id'])
                self.assertEqual(self.s3.objects[remote['archive_key']], cipher)
                # Success can reclaim ciphertext spools; exact remote bytes can
                # still reconstruct the missing public descriptor unchanged.
                self.assertFalse((self.state.run_path(item['run_id']) / remote['spools']['archive']['name']).exists())
                del self.s3.objects[remote['manifest_key']]
                storage.deliver(self.state, self.envelope, item['run_id'])
                self.assertEqual(self.s3.objects[remote['manifest_key']], remote_artifacts.encode(remote['descriptor']))

    def test_three_object_delete_interruptions_resume_descriptor_first(self):
        for index, suffix in enumerate(('.manifest.json', '.recovery.json.gpg', '.tar.gz.gpg')):
            with self.subTest(suffix=suffix):
                item = self.archive('deletion-' + str(index))
                storage.deliver(self.state, self.envelope, item['run_id'])
                current = artifacts.journal(self.state, item['run_id'])
                remote = current['remote']
                self.s3.fail = suffix
                with self.assertRaisesRegex(BackupError, 'delete-remote-delete-failed'):
                    storage.remove_copy(self.state, self.envelope, current, 's3', 'delete')
                current = artifacts.journal(self.state, item['run_id'])
                self.assertEqual(current['remote_delete_intent']['verification_level'], 'restore')
                self.assertTrue(pathlib.Path(current['archive_path']).exists())
                self.s3.fail = None
                storage.remove_copy(self.state, self.envelope, current, 's3', 'delete')
                for field in ('manifest_key', 'recovery_key', 'archive_key'):
                    self.assertNotIn(remote[field], self.s3.objects)
                deleted = [name for verb, name in self.s3.trace if verb == 'delete' and name in remote_artifacts.inventory(remote['scope']['prefix'], item['run_id'], encryption.FORMAT).values()]
                self.assertEqual(deleted[-3:], [remote['manifest_key'], remote['recovery_key'], remote['archive_key']])

    def test_corrupt_retrieval_has_no_plaintext_publication(self):
        item = self.archive()
        storage.deliver(self.state, self.envelope, item['run_id'])
        item = artifacts.journal(self.state, item['run_id'])
        remote = item['remote']
        cipher = self.s3.objects[remote['archive_key']]
        # Update private transport digest to isolate the late MDC proof from
        # the ordinary ciphertext checksum check.
        damaged = cipher[:-1] + bytes([cipher[-1] ^ 1])
        self.s3.objects[remote['archive_key']] = damaged
        remote['archive_ciphertext'] = remote_artifacts.digest(damaged)
        remote['descriptor'] = remote_artifacts.descriptor(self.state.uuid, item['run_id'], remote)
        public = remote_artifacts.encode(remote['descriptor'])
        remote['descriptor_digest'] = remote_artifacts.digest(public)
        self.s3.objects[remote['manifest_key']] = public
        recovery = remote_artifacts.encode(remote_artifacts.recovery(self.state.uuid, item['run_id'], remote, item['manifest']))
        changed_recovery = b''.join(encryption.transform(self.envelope, io.BytesIO(recovery), self.record['secret']))
        remote['recovery_ciphertext'] = remote_artifacts.digest(changed_recovery)
        self.s3.objects[remote['recovery_key']] = changed_recovery
        remote['descriptor'] = remote_artifacts.descriptor(self.state.uuid, item['run_id'], remote)
        public = remote_artifacts.encode(remote['descriptor'])
        remote['descriptor_digest'] = remote_artifacts.digest(public)
        self.s3.objects[remote['manifest_key']] = public
        pathlib.Path(item['archive_path']).unlink()
        pathlib.Path(item['manifest_path']).unlink()
        item['local_complete'] = False
        self.state.save(item)
        with self.assertRaisesRegex(BackupError, 'archive-integrity-failed'):
            storage.retrieve(self.state, self.envelope, item['run_id'])
        self.assertFalse(pathlib.Path(item['archive_path']).exists())
        self.assertFalse(pathlib.Path(item['manifest_path']).exists())
        self.assertFalse(list(pathlib.Path(item['archive_path']).parent.glob('.remote-*')))

    def test_public_outputs_and_journal_exclude_secret(self):
        from paymenter_backups.presentation import overview
        item = self.archive()
        storage.deliver(self.state, self.envelope, item['run_id'])
        serialized = json.dumps(redact(self.envelope))
        self.assertNotIn(self.record['secret'], serialized)
        self.assertNotIn(self.record['secret'], json.dumps(artifacts.journal(self.state, item['run_id'])))
        # Use a settings error to skip unrelated full admission/destination
        # checks while still exercising the overview's safe whitelist.
        public = overview(self.state, self.envelope, error='synthetic-settings-error')
        self.assertNotIn(self.record['secret'], json.dumps(public))
        self.assertEqual(public['configuration']['encryption']['key']['state'], 'active')
        self.assertIsNone(public['configuration']['encryption']['key']['pending_fingerprint'])
        self.assertEqual(public['runs'][0]['key_short'], self.fp[:16])

    def test_fake_status_aead_profile_and_nonzero_exit_refuse(self):
        # Trusted executable test doubles isolate machine-readable profile
        # handling on 2.2, which cannot produce AEAD output itself.
        root = pathlib.Path(self.tmp.name) / 'tools'
        root.mkdir()
        (root / 'gpg-agent').symlink_to('/usr/bin/gpg-agent')
        executable = root / 'gpg'
        self.envelope['tools']['gpg'] = str(executable)
        for info, returncode in (('2 9 2', 0), ('0 9', 0), ('2 9', 1)):
            body = "#!/usr/bin/python3\nimport os,sys\nargs=sys.argv\nfd=int(args[args.index('--status-fd')+1])\nos.write(fd,b'[GNUPG:] DECRYPTION_INFO " + info + "\\n[GNUPG:] DECRYPTION_OKAY\\n[GNUPG:] END_DECRYPTION\\n')\nsys.stdin.buffer.read()\nsys.stdout.buffer.write(b'partial')\nsys.exit(" + str(returncode) + ")\n"
            executable.write_text(body)
            executable.chmod(0o700)
            with self.assertRaises(BackupError):
                b''.join(encryption.transform(self.envelope, io.BytesIO(b'disposable'), self.record['secret'], decrypt=True))

    def test_timeout_reaps_owned_gpg_and_agent(self):
        root = pathlib.Path(self.tmp.name) / 'tools'
        root.mkdir()
        (root / 'gpg-agent').symlink_to('/usr/bin/gpg-agent')
        executable = root / 'gpg'
        executable.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
        executable.chmod(0o700)
        self.envelope['tools']['gpg'] = str(executable)
        self.envelope['settings']['capture_timeout'] = 0.2
        real_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        with patch('paymenter_backups.encryption.subprocess.Popen', side_effect=spawn):
            with self.assertRaisesRegex(BackupError, 'child-timeout'):
                b''.join(encryption.transform(self.envelope, io.BytesIO(b'disposable'), self.record['secret']))
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.poll() is not None for child in children))

    @unittest.skipUnless(test_storage.boto3, "isolated S3 SDK unavailable")
    def test_real_loopback_sdk_encrypted_delivery_and_retrieval(self):
        fixture = test_storage.S3ProtocolTests('test_destination_probe_scoped_and_cleanup')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.provider.return_value = fixture.s3
        item = self.archive()
        original = pathlib.Path(item['archive_path']).read_bytes()
        storage.deliver(self.state, self.envelope, item['run_id'])
        puts = [name for verb, name, _, _ in fixture.trace if verb == 'PUT']
        self.assertTrue(puts[0].endswith('.tar.gz.gpg'))
        self.assertTrue(puts[1].endswith('.recovery.json.gpg'))
        self.assertTrue(puts[2].endswith('.manifest.json'))
        item = artifacts.journal(self.state, item['run_id'])
        pathlib.Path(item['archive_path']).unlink()
        pathlib.Path(item['manifest_path']).unlink()
        item['local_complete'] = False
        self.state.save(item)
        storage.retrieve(self.state, self.envelope, item['run_id'])
        self.assertEqual(pathlib.Path(item['archive_path']).read_bytes(), original)

    @unittest.skipUnless(test_storage.boto3, "isolated S3 SDK unavailable")
    def test_real_loopback_encrypted_multipart_retry_and_exact_retrieval(self):
        fixture = test_storage.S3ProtocolTests('test_destination_probe_scoped_and_cleanup')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.provider.return_value = fixture.s3
        self.envelope['settings'].update(local_byte_budget=40 * 1024**2, capture_timeout=30)
        item = self.archive('multipart-encrypted', payload=os.urandom(9 * 1024**2))
        original = pathlib.Path(item['archive_path']).read_bytes()
        actual_put = fixture.s3.put_object
        actual_save = self.state.save
        multipart_receipts = []
        def save(journal):
            if journal.get('multipart'):
                multipart_receipts.append(dict(journal['multipart']))
            return actual_save(journal)
        def interrupted_put(**kwargs):
            if kwargs['Key'].endswith('.recovery.json.gpg'):
                raise RuntimeError('controlled recovery upload interruption')
            return actual_put(**kwargs)
        with patch.object(self.state, 'save', side_effect=save), patch.object(fixture.s3, 'put_object', side_effect=interrupted_put):
            with self.assertRaisesRegex(BackupError, 's3-delivery-failed'):
                storage.deliver(self.state, self.envelope, item['run_id'])
        intent = artifacts.journal(self.state, item['run_id'])['delivery_intent']
        self.assertGreater(intent['archive_ciphertext']['size'], 8 * 1024**2)
        self.assertEqual(len(multipart_receipts), 1)
        self.assertEqual(multipart_receipts[0]['format'], encryption.FORMAT)
        self.assertEqual(multipart_receipts[0]['key_fingerprint'], self.fp)
        self.assertEqual(multipart_receipts[0]['key'], intent['archive_key'])
        multipart_posts = [(key, query) for verb, key, query, _ in fixture.trace if verb == 'POST' and 'uploads' in query]
        self.assertEqual(len(multipart_posts), 1)
        uploaded_parts = [(key, query) for verb, key, query, _ in fixture.trace if verb == 'PUT' and 'uploadId' in query]
        self.assertEqual(len(uploaded_parts), 2)
        remote_name = '/synthetic/' + intent['archive_key']
        frozen_cipher = fixture.objects[remote_name]
        self.assertEqual(remote_artifacts.digest(frozen_cipher), intent['archive_ciphertext'])
        self.assertFalse(any(key.endswith('.manifest.json') for key in fixture.objects))
        storage.deliver(self.state, self.envelope, item['run_id'])
        self.assertEqual(fixture.objects[remote_name], frozen_cipher)
        self.assertEqual(sum(verb == 'POST' and 'uploads' in query for verb, _, query, _ in fixture.trace), 1)
        current = artifacts.journal(self.state, item['run_id'])
        self.assertNotIn('multipart', current)
        pathlib.Path(item['archive_path']).unlink()
        pathlib.Path(item['manifest_path']).unlink()
        current['local_complete'] = False
        self.state.save(current)
        storage.retrieve(self.state, self.envelope, item['run_id'])
        self.assertEqual(pathlib.Path(item['archive_path']).read_bytes(), original)
        self.assertEqual(artifacts.journal(self.state, item['run_id'])['remote_verification']['level'], 'restore')

    def test_mixed_plaintext_encrypted_retention_and_pointer(self):
        self.envelope['settings'].update(s3_encryption=False, plaintext_offsite_acknowledged=True)
        first = self.archive('legacy-plaintext')
        storage.deliver(self.state, self.envelope, first['run_id'])
        self.envelope['settings']['s3_encryption'] = True
        second = self.archive('new-encrypted')
        storage.deliver(self.state, self.envelope, second['run_id'])
        self.envelope['settings'].update(retention_keep=1, s3_retention_policy='count', s3_retention_keep=1)
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertIn(first['run_id'], result['removed']['s3'])
        self.assertIn(first['run_id'], result['removed']['local'])
        pointer = json.loads(self.s3.objects[storage.prefix(self.state, self.envelope) + 'lastmanifest.json'])
        self.assertEqual(pointer['format'], encryption.FORMAT)
        self.assertEqual(pointer['descriptor_sha256'], artifacts.journal(self.state, second['run_id'])['remote']['descriptor_digest']['sha256'])

    def test_retention_missing_retired_key_preserves_target_and_local(self):
        first = self.archive('encrypted-retired')
        storage.deliver(self.state, self.envelope, first['run_id'])
        self.envelope['settings'].update(s3_encryption=False, plaintext_offsite_acknowledged=True)
        second = self.archive('plaintext-newest')
        storage.deliver(self.state, self.envelope, second['run_id'])
        self.envelope['archive_encryption_credentials']['keys'].clear()
        self.envelope['settings'].update(retention_keep=1, s3_retention_policy='count', s3_retention_keep=1)
        before = dict(self.s3.objects)
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertIn('archive-key-unavailable', [item['code'] for item in result['skipped']])
        self.assertTrue(pathlib.Path(first['archive_path']).exists())
        for field in ('archive_key', 'recovery_key', 'manifest_key'):
            key = artifacts.journal(self.state, first['run_id'])['remote'][field]
            self.assertEqual(self.s3.objects[key], before[key])

    def test_signal_and_parent_death_kill_only_owned_helpers(self):
        import sys
        import shutil
        from paymenter_backups.process import process_identity
        harness = pathlib.Path(self.tmp.name) / 'helper-harness.py'
        harness.write_text("""import io,json,os,pathlib,subprocess,sys,time
sys.path.insert(0,sys.argv[3])
from paymenter_backups import encryption
actual = subprocess.Popen
children=[]
def spawn(*args,**kwargs):
    proc=actual(*args,**kwargs)
    children.append({'pid':proc.pid,'args':args[0]})
    pathlib.Path(sys.argv[1]).write_text(json.dumps(children))
    return proc
encryption.subprocess.Popen=spawn
env={'settings':{'capture_timeout':30},'tools':{'gpg':'/usr/bin/gpg'}}
chunks=encryption.transform(env,io.BytesIO(os.urandom(2*1024**2)),'a'*64)
next(chunks)
if sys.argv[2]=='death':
    os._exit(0)
time.sleep(30)
""")
        for mode in ('signal', 'death'):
            receipt = pathlib.Path(self.tmp.name) / ('children-' + mode + '.json')
            parent = subprocess.Popen([sys.executable, str(harness), str(receipt), mode, str(pathlib.Path(__file__).resolve().parents[1] / "src")],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            records = []
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        records = json.loads(receipt.read_text())
                        if len(records) == 2:
                            break
                    except (FileNotFoundError, ValueError):
                        pass
                    time.sleep(0.02)
                self.assertEqual(len(records), 2)
                if mode == 'signal':
                    parent.terminate()
                parent.wait(timeout=5)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and any(process_identity(record['pid']) for record in records):
                    time.sleep(0.02)
                self.assertTrue(all(process_identity(record['pid']) is None for record in records))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=3)
                # A killed parent cannot remove its secret-free owned homedir.
                # Remove only the exact ephemeral test home its agent used.
                for record in records:
                    args = record['args']
                    if 'gpg-agent' in args[0]:
                        home = pathlib.Path(args[args.index('--homedir') + 1])
                        if home.name.startswith('pb-gpg-') and home.parent == pathlib.Path(tempfile.gettempdir()):
                            shutil.rmtree(home, ignore_errors=True)

    def test_pending_generation_cleanup_with_newer_survivor(self):
        first = self.archive('pending-cleanup')
        self.s3.fail = '.recovery.json.gpg'
        with self.assertRaises(BackupError):
            storage.deliver(self.state, self.envelope, first['run_id'])
        pending = artifacts.journal(self.state, first['run_id'])['delivery_intent']
        self.s3.fail = None
        second = self.archive('newer-survivor')
        storage.deliver(self.state, self.envelope, second['run_id'])
        self.assertIsNone(storage.delete_plan(self.state, self.envelope, first['run_id'], location='s3')['refusal'])
        storage.delete_run(self.state, self.envelope, first['run_id'], location='s3')
        self.assertNotIn(pending['archive_key'], self.s3.objects)
        self.assertTrue(pathlib.Path(first['archive_path']).exists())
        self.assertFalse((self.state.run_path(first['run_id']) / pending['spools']['archive']['name']).exists())

    def test_refresh_under_lock_selects_new_active_key(self):
        item = self.archive()
        fresh = key_record('c' * 64)
        fp = fresh['fingerprint']['value']
        envelope = dict(self.envelope, archive_encryption_credentials={'version': 1, 'active_fingerprint': fp,
                            'pending_fingerprint': None, 'keys': {fp: fresh}})
        calls = []
        def refresh():
            with self.assertRaisesRegex(BackupError, 'busy'):
                with self.state.lock():
                    pass
            calls.append(True)
            return envelope
        storage.deliver(self.state, self.envelope, item['run_id'], envelope_loader=refresh)
        self.assertEqual(artifacts.journal(self.state, item['run_id'])['remote']['key_fingerprint'], fp)
        result = storage.destination_test(self.state, self.envelope, envelope_loader=refresh)
        self.assertEqual(result['key_fingerprint'], fp)
        self.assertEqual(len(calls), 2)

    def test_blocking_source_has_wall_deadline_and_caller_timer_is_preserved(self):
        import signal
        class Drip:
            def read(self, count):
                time.sleep(2)
                return b'x'
        self.envelope['settings']['capture_timeout'] = 0.15
        started = time.monotonic()
        with self.assertRaisesRegex(BackupError, 'child-timeout'):
            b''.join(encryption.transform(self.envelope, Drip(), self.record['secret']))
        self.assertLess(time.monotonic() - started, 1.5)
        handler = signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL, 10)
        try:
            with self.assertRaisesRegex(BackupError, 'archive-encryption-tool-unusable'):
                b''.join(encryption.transform(self.envelope, io.BytesIO(b'x'), self.record['secret']))
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 8)
            self.assertEqual(signal.getsignal(signal.SIGALRM), handler)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def test_tool_detection_prefers_usr_bin_then_actual_path(self):
        envelope = {'settings': {}}
        with patch('paymenter_backups.encryption.shutil.which') as which:
            self.assertEqual(encryption.tool(envelope)[0], '/usr/bin/gpg')
            which.assert_not_called()
        root = pathlib.Path(self.tmp.name) / 'detected-tools'
        root.mkdir()
        (root / 'gpg').symlink_to('/usr/bin/gpg')
        (root / 'gpg-agent').symlink_to('/usr/bin/gpg-agent')
        access = os.access
        def missing_default(path, mode):
            return False if str(path) == '/usr/bin/gpg' else access(path, mode)
        with patch('paymenter_backups.encryption.os.access', side_effect=missing_default), patch('paymenter_backups.encryption.shutil.which', return_value=str(root / 'gpg')) as which:
            self.assertEqual(encryption.tool(envelope)[0], str(root / 'gpg'))
            which.assert_called_once_with('gpg')

    def test_pending_readiness_and_invalid_namespace(self):
        namespace = self.envelope['archive_encryption_credentials']
        namespace['active_fingerprint'] = None
        namespace['pending_fingerprint'] = self.fp
        self.record['state'] = 'pending'
        self.assertEqual(encryption.readiness(self.envelope), 'archive-key-save-required')
        namespace['version'] = True
        self.assertEqual(encryption.readiness(self.envelope), 'archive-key-invalid')
        for field, value in (('iterations', 600000.0), ('salt', int('1' * 64)), ('value', int('1' * 64))):
            parameters = dict(self.record['fingerprint'], **{field: value})
            with self.assertRaisesRegex(BackupError, 'archive-key-invalid'):
                encryption.fingerprint(self.record['secret'], parameters)
            with self.assertRaisesRegex(BackupError, 'remote-descriptor-invalid'):
                remote_artifacts.validate_parameters(parameters)

    def test_legacy_multipart_resolver_preserves_scope_refusal_and_blocks_upgrade(self):
        record = {'bucket': 'synthetic', 'key': 'owned/run.tar.gz', 'upload_id': 'old-upload', 'scope': {}}
        self.assertEqual(remote_artifacts.multipart_format(record), remote_artifacts.PLAINTEXT)
        with self.assertRaisesRegex(BackupError, 'remote-format-unsupported'):
            remote_artifacts.multipart_format(dict(record, key_fingerprint='a' * 64))
        item = self.archive()
        item['multipart'] = {k: v for k, v in record.items() if k != 'scope'}
        self.state.save(item)
        with self.assertRaisesRegex(BackupError, 'multipart-location-unbound'):
            storage.deliver(self.state, self.envelope, item['run_id'])
        item['multipart']['scope'] = storage.remote_scope(self.state, self.envelope)
        self.state.save(item)
        with self.assertRaisesRegex(BackupError, 'legacy-plaintext-delivery-blocked'):
            storage.deliver(self.state, self.envelope, item['run_id'])
        self.assertFalse(self.s3.trace)

    def test_destination_test_badge_matches_current_provider_format_and_key(self):
        from paymenter_backups.presentation import request_entry
        result = storage.destination_test(self.state, self.envelope)
        request = {'request_id': 'test-badge', 'action': 'destination-test', 'status': 'complete',
                   'created_at': time.time(), 'result': dict(result, secret_canary=self.record['secret'])}
        def badge():
            public = request_entry(request, self.state, self.envelope)
            self.assertNotIn(self.record['secret'], json.dumps(public))
            self.assertNotIn('result', public)
            return public['result_verified']
        self.assertTrue(badge())
        namespace = self.envelope['archive_encryption_credentials']
        replacement = key_record('d' * 64)
        replacement['state'] = 'pending'
        fp = replacement['fingerprint']['value']
        namespace['keys'][fp] = replacement
        namespace['pending_fingerprint'] = fp
        self.assertTrue(badge())
        namespace['active_fingerprint'] = fp
        replacement['state'] = 'active'
        self.record['state'] = 'retired'
        self.assertFalse(badge())
        request['result'] = storage.destination_test(self.state, self.envelope)
        self.assertTrue(badge())
        replacement['acknowledgement'] = None
        self.assertFalse(badge())
        replacement['acknowledgement'] = {'fingerprint': fp}
        original = self.envelope['settings']['s3_endpoint']
        self.envelope['settings']['s3_endpoint'] = 'https://changed.invalid'
        self.assertFalse(badge())
        self.envelope['settings']['s3_endpoint'] = original
        self.envelope['settings']['s3_encryption'] = False
        self.assertFalse(badge())
        # Historical successful plaintext tests had no explicit format.
        request['result'] = {'destination': 's3', 'destination_id': storage.destination_id(self.state, self.envelope), 'verified': True}
        self.assertTrue(badge())
        self.envelope['settings']['s3_encryption'] = True
        self.assertFalse(badge())
        request['status'] = 'failed'
        self.assertFalse(badge())
        ordinary = request_entry({'action': 'deliver', 'result': result}, self.state, self.envelope)
        self.assertNotIn('result_verified', ordinary)

    def test_destination_test_badge_overview_whitelist(self):
        from paymenter_backups.presentation import overview
        result = storage.destination_test(self.state, self.envelope)
        result['unexpected_secret'] = self.record['secret']
        atomic_json(self.state.root / 'requests' / 'badge.json', {'request_id': 'badge', 'action': 'destination-test',
                    'status': 'complete', 'created_at': time.time(), 'arguments': {}, 'result': result})
        public = overview(self.state, self.envelope, error='synthetic-settings-error')
        self.assertTrue(public['requests'][0]['result_verified'])
        self.assertNotIn(self.record['secret'], json.dumps(public))
        self.envelope['archive_encryption_credentials']['active_fingerprint'] = None
        self.assertFalse(overview(self.state, self.envelope, error='synthetic-settings-error')['requests'][0]['result_verified'])

    def test_overview_validates_current_test_key_once_for_many_historical_rows(self):
        from paymenter_backups.presentation import overview
        result = storage.destination_test(self.state, self.envelope)
        for index in range(50):
            request_id = 'badge-' + str(index)
            atomic_json(self.state.root / 'requests' / (request_id + '.json'),
                        {'request_id': request_id, 'action': 'destination-test', 'status': 'complete',
                         'created_at': time.time(), 'arguments': {}, 'result': result})
        with patch('paymenter_backups.encryption.key', wraps=encryption.key) as checked_key:
            public = overview(self.state, self.envelope, error='synthetic-settings-error')
        self.assertEqual(checked_key.call_count, 1)
        self.assertTrue(all(request['result_verified'] for request in public['requests']))
        self.assertNotIn(self.record['secret'], json.dumps(public))

    def test_unknown_format_and_unicode_secret(self):
        with self.assertRaisesRegex(BackupError, 'remote-format-unsupported'):
            remote_artifacts.format_of({'format': 'unknown'})
        for record in ({'key_fingerprint': 'a'*64}, {'recovery_key': 'owned/file.gpg'},
                       {'format': 'plaintext-v1', 'spools': {}}, {'format': 'plaintext-v1', 'key': {}}):
            with self.assertRaisesRegex(BackupError, 'remote-format-unsupported'):
                remote_artifacts.format_of(record)
        encryption.validate_secret('é' * 20)
        with self.assertRaisesRegex(BackupError, 'archive-key-invalid'):
            encryption.validate_secret('a' * 20 + '\n')

if __name__ == '__main__':
    unittest.main()
