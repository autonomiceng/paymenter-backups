"""Concurrent saves must govern destructive work after run-lock admission."""
import copy
import fcntl
from unittest.mock import patch

from paymenter_backups import cli, engine, storage
from test_engine_schedules import PrivateFixture


class CurrentStoragePolicy(PrivateFixture):
    def test_capture_refreshes_before_admission_or_budget_cleanup(self):
        with patch.object(engine, 'plan') as admission:
            with self.assertRaisesRegex(RuntimeError, 'current configuration refused'):
                engine.capture(self.state, self.envelope,
                               envelope_loader=lambda: (_ for _ in ()).throw(RuntimeError('current configuration refused')))
        admission.assert_not_called()

    def test_automatic_retention_observes_new_disabled_setting_under_lock(self):
        old = copy.deepcopy(self.envelope)
        old['settings']['automatic_retention'] = True
        current = copy.deepcopy(old)
        current['settings']['automatic_retention'] = False
        result = {}

        def reload():
            with (self.state.root / 'run.lock').open('rb') as probe:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return current

        with patch.object(cli, 'retention') as retention:
            cli.apply_automatic_retention(self.state, old, result, envelope_loader=reload)
        retention.assert_not_called()
        self.assertNotIn('retention', result)

    def test_retention_uses_current_keep_counts_before_any_local_unlink(self):
        self.settings['retention_keep_database'] = 1
        first, _ = self.backup('older', created_at=1)
        self.backup('newer', created_at=2)
        current = copy.deepcopy(self.envelope)
        current['settings']['retention_keep_database'] = 2
        result = storage.retention(self.state, self.envelope, execute=True, envelope_loader=lambda: current)
        self.assertEqual(result['removed']['local'], [])
        self.assertTrue(first.exists())

    def test_s3_only_changed_to_keep_local_and_keep_all_preserves_every_copy(self):
        self.remote(keep_local=True)
        first, _ = self.backup('older', created_at=1, deliver=True)
        self.backup('newer', created_at=2, deliver=True)
        current = copy.deepcopy(self.envelope)
        current['settings'].update(keep_local=True, retention_keep_database=2,
                                   s3_retention_policy_database='keep-all')
        old = copy.deepcopy(self.envelope)
        old['settings'].update(keep_local=False, retention_keep_database=1,
                               s3_retention_policy_database='count', s3_retention_keep_database=1)
        with patch.object(storage, 'staging_removal') as staging:
            result = storage.retention(self.state, old, execute=True, envelope_loader=lambda: current)
        staging.assert_not_called()
        self.assertEqual(result['removed'], {'local': [], 's3': []})
        self.assertTrue(first.exists())
        self.assertTrue(all(item['remote_delivered'] for item in self.state.journals()))

    def test_delete_and_retrieve_refresh_before_journal_or_destination_proof(self):
        current = copy.deepcopy(self.envelope)
        for operation, arguments in ((storage.delete_run, ('missing',)), (storage.retrieve, ('missing',))):
            with self.subTest(operation=operation.__name__):
                with patch.object(storage, 'owned_manifest') as proof:
                    with self.assertRaisesRegex(RuntimeError, 'current configuration refused'):
                        operation(self.state, self.envelope, *arguments,
                                  envelope_loader=lambda: (_ for _ in ()).throw(RuntimeError('current configuration refused')))
                proof.assert_not_called()
