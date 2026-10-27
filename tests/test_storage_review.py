"""Storage review regressions using disposable archives and an in-memory provider."""
import json
from unittest.mock import patch

from test_engine_schedules import PrivateFixture
from paymenter_backups import storage
from paymenter_backups.common import BackupError


class PointerTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        self.remote()

    def test_pointer_readback_damage_refuses_delivery_and_preserves_local(self):
        archive, _ = self.backup('candidate')
        put = self.store.put_object
        pointer = ('fixture', storage.prefix(self.state, self.envelope) + 'lastmanifest.json')
        def corrupt(**kwargs):
            result = put(**kwargs)
            if kwargs['Key'] == pointer[1]:
                self.store.objects[pointer] = b'corrupt'
            return result
        with patch.object(self.store, 'put_object', side_effect=corrupt):
            with self.assertRaisesRegex(BackupError, 's3-pointer-readback-mismatch'):
                storage.deliver(self.state, self.envelope, 'candidate')
        self.assertTrue(archive.exists())
        self.assertEqual(self.item('candidate')['delivery_error'], 's3-pointer-readback-mismatch')
        self.assertFalse(self.item('candidate')['remote_delivered'])

    def test_delivery_does_not_rewind_remote_pointer_with_stale_local_flags(self):
        self.backup('older', created_at=1)
        self.backup('newer', created_at=2, deliver=True)
        newer = self.item('newer')
        newer['remote_delivered'] = False
        self.state.save(newer)
        pointer = ('fixture', storage.prefix(self.state, self.envelope) + 'lastmanifest.json')
        before = self.store.objects[pointer]
        storage.deliver(self.state, self.envelope, 'older')
        self.assertEqual(self.store.objects[pointer], before)
        self.assertEqual(json.loads(before)['run_id'], 'newer')
        self.assertTrue(self.item('older')['remote_delivered'])

    def test_retention_repoints_from_damaged_newer_remote(self):
        self.backup('older', created_at=1, deliver=True)
        self.backup('newer', created_at=2, deliver=True)
        remote = self.item('newer')['remote']
        self.store.objects[('fixture', remote['manifest_key'])] = b'damaged'
        pointer = ('fixture', storage.prefix(self.state, self.envelope) + 'lastmanifest.json')
        self.assertEqual(json.loads(self.store.objects[pointer])['run_id'], 'newer')
        result = storage.retention(self.state, self.envelope, execute=True)
        self.assertEqual(self.item('newer')['remote_observation']['status'], 'damaged')
        self.assertIn({'run_id': 'newer', 'location': 's3', 'code': 'remote-copy-damaged'}, result['skipped'])
        self.assertEqual(json.loads(self.store.objects[pointer])['run_id'], 'older')

    def test_completed_remote_removal_does_not_block_pointer_repair(self):
        self.backup('older', created_at=1, deliver=True)
        self.backup('newer', created_at=2, deliver=True)
        pointer = ('fixture', storage.prefix(self.state, self.envelope) + 'lastmanifest.json')
        newer = self.item('newer')
        stale_pointer = self.store.objects[pointer]
        for flags in ({'remote_observation': {'status': 'none'}},
                      {'remote_pruned': {'at': 3, 'reason': 'delete'}},
                      {'retained_deleted': True}):
            with self.subTest(flags=flags):
                self.store.objects[pointer] = stale_pointer
                self.state.save(dict(newer, remote_delivered=False, **flags))
                with self.state.lock():
                    storage.publish_pointer(self.state, self.store, self.item('older'))
                self.assertEqual(json.loads(self.store.objects[pointer])['run_id'], 'older')

    def test_undelivered_flag_without_removal_evidence_keeps_newer_pointer(self):
        self.backup('older', created_at=1, deliver=True)
        self.backup('newer', created_at=2, deliver=True)
        pointer = ('fixture', storage.prefix(self.state, self.envelope) + 'lastmanifest.json')
        before = self.store.objects[pointer]
        self.state.save(dict(self.item('newer'), remote_delivered=False))
        with self.state.lock():
            storage.publish_pointer(self.state, self.store, self.item('older'))
        self.assertEqual(self.store.objects[pointer], before)
        self.assertEqual(json.loads(before)['run_id'], 'newer')


class DamagedRetentionTests(PrivateFixture):
    def check_damage(self, damage, execute, version=2):
        self.settings.update(settings_version=version, retention_keep=1, retention_keep_database=1)
        oldest, _ = self.backup('oldest', created_at=1)
        good, _ = self.backup('good', created_at=2)
        damaged, manifest = self.backup('damaged', created_at=3)
        if damage == 'missing':
            damaged.unlink()
        else:
            damaged.write_bytes(b'corrupt archive')
        before = manifest.read_bytes()
        result = storage.retention(self.state, self.envelope, execute=execute)
        self.assertEqual(result['keep'], ['good'])
        self.assertEqual(result['local_delete'], ['oldest'])
        self.assertEqual([entry['run_id'] for entry in result['damaged']], ['damaged'])
        self.assertTrue(result['damaged'][0]['code'])
        self.assertNotIn('damaged', result['delete'])
        self.assertNotIn('damaged', result['removed']['local'])
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual(damaged.exists(), damage != 'missing')
        self.assertTrue(good.exists())
        self.assertEqual(oldest.exists(), not execute)

    def test_missing_archive_dry_run(self):
        self.check_damage('missing', False)

    def test_missing_archive_execute(self):
        self.check_damage('missing', True)

    def test_corrupt_archive_dry_run(self):
        self.check_damage('corrupt', False)

    def test_corrupt_archive_execute(self):
        self.check_damage('corrupt', True)

    def test_legacy_missing_archive_dry_run(self):
        self.check_damage('missing', False, version=1)

    def test_legacy_corrupt_archive_execute(self):
        self.check_damage('corrupt', True, version=1)

    def test_filesystem_error_is_isolated_per_run(self):
        self.settings['retention_keep_database'] = 1
        oldest, _ = self.backup('oldest', created_at=1)
        self.backup('good', created_at=2)
        damaged, _ = self.backup('damaged', created_at=3)
        open_archive = storage.artifacts.open_archive
        def fail(state, run_id, *args, **kwargs):
            if run_id == 'damaged':
                raise FileNotFoundError('disposable archive stat failure')
            return open_archive(state, run_id, *args, **kwargs)
        with patch.object(storage.artifacts, 'open_archive', side_effect=fail):
            for execute in (False, True):
                with self.subTest(execute=execute):
                    result = storage.retention(self.state, self.envelope, execute=execute)
                    self.assertEqual(result['damaged'], [{'run_id': 'damaged', 'code': 'retention-local-verification-failed'}])
                    self.assertEqual(result['keep'], ['good'])
        self.assertFalse(oldest.exists())
        self.assertTrue(damaged.exists())

    def test_damaged_run_keeps_its_remote_copy(self):
        self.remote(s3_retention_policy_database='count', s3_retention_keep_database=1,
                    retention_keep_database=1)
        damaged, _ = self.backup('damaged', created_at=1, deliver=True)
        self.backup('good', created_at=2, deliver=True)
        damaged.write_bytes(b'corrupt archive')
        remote = self.item('damaged')['remote']
        for execute in (False, True):
            with self.subTest(execute=execute):
                result = storage.retention(self.state, self.envelope, execute=execute)
                self.assertEqual([entry['run_id'] for entry in result['damaged']], ['damaged'])
                self.assertNotIn('damaged', result['delete'])
                self.assertIn(('fixture', remote['archive_key']), self.store.objects)
                self.assertIn(('fixture', remote['manifest_key']), self.store.objects)


class LatestRemoteIntentTests(PrivateFixture):
    def check_latest_intent(self, version, per_copy=False):
        self.remote(settings_version=version, retention_keep=1, retention_keep_database=1,
                    s3_retention_policy='count', s3_retention_keep=1,
                    s3_retention_policy_database='count', s3_retention_keep_database=1)
        older, _ = self.backup('older', created_at=1, deliver=True)
        latest, _ = self.backup('latest', created_at=2, deliver=True)
        item = self.item('latest')
        intent = {'manifest': item['manifest'], 'remote_verified': True, 'reason': 'retention'}
        item['retention_delete_intent'] = intent
        if per_copy:
            item['remote_delete_intent'] = intent
            item['copy_state_version'] = 2
        self.state.save(item)
        result = storage.retention(self.state, self.envelope, execute=True)
        kept = self.item('latest')
        self.assertNotIn('retention_delete_intent', kept)
        self.assertNotIn('remote_delete_intent', kept)
        self.assertTrue(kept['remote_delivered'])
        self.assertTrue(latest.exists())
        self.assertNotIn('latest', result['remote_delete'])
        self.assertEqual(result['removed']['s3'], ['older'])
        self.assertFalse(older.exists())

    def test_legacy_latest_remote_retention_intent_is_cleared(self):
        self.check_latest_intent(1)

    def test_typed_latest_remote_retention_intent_is_cleared(self):
        self.check_latest_intent(2)

    def test_latest_remote_per_copy_retention_intent_is_cleared(self):
        self.check_latest_intent(2, per_copy=True)
