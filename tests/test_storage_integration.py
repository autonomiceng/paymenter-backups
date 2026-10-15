"""Merged typed storage and encryption contracts on small disposable archives."""
import json
import time
from unittest.mock import patch

import test_encryption
from test_engine_schedules import PrivateFixture

from paymenter_backups import artifacts, encryption, remote_artifacts, storage
from paymenter_backups.common import BackupError


class EncryptedStorageTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        self.remote(s3_encryption=True, keep_local=False)
        self.envelope['tools']['gpg'] = '/usr/bin/gpg'
        self.key = test_encryption.key_record()
        self.fingerprint = self.key['fingerprint']['value']
        self.envelope['archive_encryption_credentials'] = {
            'version': 1, 'active_fingerprint': self.fingerprint,
            'pending_fingerprint': None, 'keys': {self.fingerprint: self.key}}
        self.s3 = test_encryption.MemoryS3()
        provider = patch.object(storage, 'client', return_value=self.s3)
        provider.start()
        self.addCleanup(provider.stop)

    def test_encrypted_s3_only_removal_and_remote_only_retry_decrypt(self):
        archive, manifest = self.backup('only')
        result = storage.deliver(self.state, self.envelope, 'only')
        self.assertFalse(result['local_complete'])
        self.assertFalse(archive.exists())
        self.assertFalse(manifest.exists())
        item = self.item('only')
        self.assertEqual(item['remote_verification']['level'], 'restore')
        self.assertEqual(item['remote_verification']['source'], 's3-verify')
        self.assertFalse(item.get('retained_deleted'))
        remote = item['remote']
        self.assertTrue(all(remote[field] in self.s3.objects for field in ('archive_key', 'recovery_key', 'manifest_key')))
        frozen = dict(self.s3.objects)
        with patch.object(encryption, 'transform', wraps=encryption.transform) as transform:
            result = storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(result['already_delivered'])
        self.assertEqual(sum(call.kwargs.get('decrypt', False) for call in transform.call_args_list), 2)
        self.assertEqual(self.s3.objects, frozen)
        self.assertFalse(list(self.state.run_path('only').glob('delivery.*.gpg')))

    def test_interrupted_encrypted_staging_removal_retries_current_decrypt_proof(self):
        archive, manifest = self.backup('only')
        remove = artifacts.remove_owned
        def interrupt(location, names, identities):
            if location[0] == self.state.run_path('only'):
                return remove(location, names, identities)
            remove(location, names[:1], identities)
            raise OSError('disposable interruption')
        with patch.object(artifacts, 'remove_owned', side_effect=interrupt):
            with self.assertRaisesRegex(BackupError, 'staging-removal-local-delete-failed'):
                storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        self.assertFalse(manifest.exists())
        self.assertIn('staging_removal_intent', self.item('only'))
        self.envelope['archive_encryption_credentials']['keys'].clear()
        with self.assertRaisesRegex(BackupError, 'archive-key-unavailable'):
            storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        self.envelope['archive_encryption_credentials']['keys'][self.fingerprint] = self.key
        with patch.object(encryption, 'transform', wraps=encryption.transform) as transform:
            result = storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(any(call.kwargs.get('decrypt') for call in transform.call_args_list))
        self.assertFalse(result['local_complete'])
        self.assertFalse(archive.exists())
        self.assertNotIn('staging_removal_intent', self.item('only'))

    def test_current_readback_damage_preserves_encrypted_staging(self):
        archive, manifest = self.backup('only')
        deliver = remote_artifacts.deliver
        def damage_after_delivery(*args):
            result = deliver(*args)
            remote = self.item('only')['remote']
            cipher = self.s3.objects[remote['archive_key']]
            self.s3.objects[remote['archive_key']] = cipher[:-1] + bytes([cipher[-1] ^ 1])
            return result
        with patch.object(remote_artifacts, 'deliver', side_effect=damage_after_delivery):
            with self.assertRaises(BackupError):
                storage.deliver(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        self.assertTrue(manifest.exists())
        self.assertNotIn('staging_removal_intent', self.item('only'))
        self.assertEqual(self.item('only')['remote_observation']['status'], 'unknown')

    def test_expired_encrypted_retrieval_needs_key_and_ordinary_delete_stays_guarded(self):
        archive, _ = self.backup('only', deliver=True)
        storage.retrieve(self.state, self.envelope, 'only')
        self.assertTrue(archive.exists())
        storage.retention(self.state, self.envelope, execute=True)
        self.assertTrue(archive.exists())
        self.assertEqual(storage.delete_plan(self.state, self.envelope, 'only', location='local')['refusal'], 'delete-last-backup')
        item = self.item('only')
        item['local_download_until'] = time.time() - 1
        self.state.save(item)
        self.envelope['archive_encryption_credentials']['keys'].clear()
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertTrue(archive.exists())
        self.assertIn('archive-key-unavailable', [entry['code'] for entry in result['skipped']])
        self.envelope['archive_encryption_credentials']['keys'][self.fingerprint] = self.key
        with patch.object(encryption, 'transform', wraps=encryption.transform) as transform:
            result = storage.retention(self.state, self.envelope, execute=True)
        self.assertTrue(any(call.kwargs.get('decrypt') for call in transform.call_args_list))
        self.assertEqual(result['removed']['local'], ['only'])
        self.assertFalse(archive.exists())
        self.assertTrue(self.item('only')['remote_delivered'])
        self.assertFalse(self.item('only').get('retained_deleted'))

    def history(self):
        self.settings.update(retention_keep_full=1, retention_keep_database=1)
        for name, mode, at in (('f1', 'full-quiesced', 1), ('d1', 'database-online', 2),
                               ('f2', 'full-quiesced', 3), ('d2', 'database-online', 4)):
            self.backup(name, mode, at, deliver=True)

    def test_encrypted_typed_retention_protects_each_newest_and_preserves_pointer_format(self):
        self.history()
        self.assertEqual(storage.delete_plan(self.state, self.envelope, 'f2', location='s3')['refusal'], 'delete-latest-remote-copy')
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(set(result['removed']['s3']), {'f1', 'd1'})
        self.assertTrue(self.item('f2')['remote_delivered'])
        self.assertTrue(self.item('d2')['remote_delivered'])
        pointer = json.loads(self.s3.objects[storage.prefix(self.state, self.envelope) + 'lastmanifest.json'])
        self.assertEqual(pointer['run_id'], 'd2')
        self.assertEqual(pointer['format'], encryption.FORMAT)
        self.assertEqual(pointer['descriptor_sha256'], self.item('d2')['remote']['descriptor_digest']['sha256'])

    def test_other_type_cannot_replace_damaged_encrypted_survivor(self):
        self.history()
        remote = self.item('f2')['remote']
        cipher = self.s3.objects[remote['archive_key']]
        self.s3.objects[remote['archive_key']] = cipher[:-1] + bytes([cipher[-1] ^ 1])
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(result['removed']['s3'], ['d1'])
        self.assertTrue(self.item('f1')['remote_delivered'])
        self.assertTrue(self.item('f2')['remote_delivered'])
        self.assertIn('delete-no-verified-s3-survivor', [entry['code'] for entry in result['skipped']])

    def test_typed_retention_resolves_each_recorded_plaintext_or_encrypted_format(self):
        self.settings.update(retention_keep_full=1, retention_keep_database=1, s3_encryption=False)
        self.backup('f1', 'full-quiesced', 1, deliver=True)
        self.settings['s3_encryption'] = True
        self.backup('d1', 'database-online', 2, deliver=True)
        self.backup('f2', 'full-quiesced', 3, deliver=True)
        self.settings['s3_encryption'] = False
        self.backup('d2', 'database-online', 4, deliver=True)
        self.settings['s3_encryption'] = True
        self.assertTrue(storage.deliver(self.state, self.envelope, 'd2')['already_delivered'])
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(set(result['removed']['s3']), {'f1', 'd1'})
        self.assertEqual(remote_artifacts.format_of(self.item('f2')['remote']), encryption.FORMAT)
        self.assertEqual(remote_artifacts.format_of(self.item('d2')['remote']), remote_artifacts.PLAINTEXT)
        pointer = json.loads(self.s3.objects[storage.prefix(self.state, self.envelope) + 'lastmanifest.json'])
        self.assertEqual(pointer['run_id'], 'd2')
        self.assertNotIn('descriptor_sha256', pointer)

    def test_changed_provider_and_missing_key_preserve_last_staging(self):
        self.settings['keep_local'] = True
        archive, _ = self.backup('only', deliver=True)
        self.settings['keep_local'] = False
        original_scope = self.settings['s3_endpoint']
        self.settings['s3_endpoint'] = 'https://different.invalid'
        with self.state.lock(), self.assertRaisesRegex(BackupError, 'staging-removal-remote-proof-required'):
            storage.staging_removal(self.state, self.envelope, self.item('only'))
        self.assertTrue(archive.exists())
        self.settings['s3_endpoint'] = original_scope
        self.envelope['archive_encryption_credentials']['keys'].clear()
        with self.state.lock(), self.assertRaisesRegex(BackupError, 'archive-key-unavailable'):
            storage.staging_removal(self.state, self.envelope, self.item('only'))
        self.assertTrue(archive.exists())
        self.assertNotIn('staging_removal_intent', self.item('only'))
