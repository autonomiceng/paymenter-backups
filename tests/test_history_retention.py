"""History retention through the existing private-state and archive fixtures."""
import datetime
import unittest
from unittest.mock import patch

from paymenter_backups import requests, storage
from paymenter_backups.common import BackupError, atomic_json, read_json
from test_engine_schedules import PrivateFixture


NOW = datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc).timestamp()
OLD = datetime.datetime(2025, 9, 11, tzinfo=datetime.timezone.utc).timestamp()


class HistoryRetention(PrivateFixture):
    def complete_request(self, at=OLD, slot=None):
        with patch.object(requests.time, 'time', return_value=at):
            item = requests.create(self.state, 'destination-test', slot=slot)
        item.update(status='complete', finished_at=at, updated_at=at)
        atomic_json(requests.path(self.state, item['request_id']), item)
        return item

    def deleted_run(self, run_id='old-run', at=OLD):
        archive, manifest = self.backup(run_id, created_at=at)
        self.backup('survivor', created_at=NOW)
        with patch.object(storage.time, 'time', return_value=at):
            storage.delete_run(self.state, self.envelope, run_id, location='local')
        self.assertFalse(archive.exists())
        self.assertFalse(manifest.exists())
        return self.item(run_id)

    def sweep(self, at=NOW):
        self.tick(at)
        path = self.state.root / 'history-maintenance.json'
        return read_json(path) if path.exists() else {}

    def test_tick_archives_old_completed_request_and_preserves_slot_replay(self):
        item = self.complete_request(slot='historic-slot')
        self.sweep()
        self.assertFalse(requests.path(self.state, item['request_id']).exists())
        self.assertEqual(requests.list_requests(self.state), [])
        replay = requests.create(self.state, 'destination-test', slot='historic-slot')
        self.assertEqual(replay['request_id'], item['request_id'])
        self.assertEqual(replay['status'], 'complete')
        self.assertEqual(len(list((self.state.root / 'history/v1/requests').glob('*.json'))), 1)
        self.assertFalse(requests.path(self.state, item['request_id']).exists())

    def test_latest_passing_storage_test_survives_retention_and_failed_retests(self):
        from paymenter_backups.commissioning import check
        from paymenter_backups.presentation import destination_test_binding, destination_test_current
        self.remote()
        self.envelope['credentials'] = {'access_key': 'fixture-access', 'secret_key': 'fixture-secret'}
        self.envelope['framework']['app_key'] = 'fixture-app-key'

        def passing(at):
            item = self.complete_request(at)
            item['result'] = storage.destination_test(self.state, self.envelope)
            self.assertTrue(item['result']['verified'])
            atomic_json(requests.path(self.state, item['request_id']), item)
            return item

        def proof():
            binding = destination_test_binding(self.state, self.envelope)
            return any(destination_test_current(self.state, self.envelope, item, item.get('result') or {}, binding)
                       for item in requests.list_requests(self.state, history=False)
                       if item.get('action') == 'destination-test')

        older = passing(OLD - 86400)
        current = passing(OLD)
        failed = self.complete_request(OLD + 86400)
        failed.update(status='failed', result={'verified': False})
        atomic_json(requests.path(self.state, failed['request_id']), failed)
        self.assertTrue(proof())
        self.sweep()
        self.assertTrue(proof())
        active = {item['request_id'] for item in requests.list_requests(self.state, history=False)}
        self.assertIn(current['request_id'], active)
        self.assertIn(failed['request_id'], active)
        self.assertNotIn(older['request_id'], active)
        self.assertEqual(check(self.state, self.envelope)['checks']['offsite']['state'], 'ready')
        # A newer passing test releases the old proof to ordinary retention.
        replacement = passing(NOW + 1)
        self.sweep(NOW + 2)
        active = {item['request_id'] for item in requests.list_requests(self.state, history=False)}
        self.assertIn(replacement['request_id'], active)
        self.assertNotIn(current['request_id'], active)
        self.sweep(NOW + 4 * 366 * 86400)
        self.assertTrue(proof())
        self.assertTrue((self.state.root / 'requests' / (replacement['request_id'] + '.json')).exists())

    def test_deleted_recovered_run_moves_with_its_authoritative_manifest(self):
        original = self.deleted_run()
        self.sweep()
        self.assertNotIn('old-run', [row['run_id'] for row in self.state.journals()])
        cold = list((self.state.root / 'history/v1/runs').glob('*.json'))
        self.assertEqual(len(cold), 1)
        self.assertEqual(read_json(cold[0]), original)

    def test_calendar_month_boundary_and_terminal_date_control_age(self):
        boundary = datetime.datetime(2025, 10, 11, tzinfo=datetime.timezone.utc).timestamp()
        old = self.complete_request(boundary - 1)
        fresh = self.complete_request(boundary)
        unfinished_age = self.complete_request(OLD)
        unfinished_age['finished_at'] = NOW
        atomic_json(requests.path(self.state, unfinished_age['request_id']), unfinished_age)
        self.sweep()
        self.assertFalse(requests.path(self.state, old['request_id']).exists())
        self.assertTrue(requests.path(self.state, fresh['request_id']).exists())
        self.assertTrue(requests.path(self.state, unfinished_age['request_id']).exists())

    def test_uncertain_requests_and_unresolved_or_partial_runs_stay_and_are_listed(self):
        item = self.complete_request()
        item.update(status='failed', error='worker-interrupted')
        atomic_json(requests.path(self.state, item['request_id']), item)
        run = self.deleted_run()
        run['service_recovered'] = False
        atomic_json(self.state.run_path(run['run_id']) / 'journal.json', run)
        linked = self.complete_request()
        linked.update(action='delete', arguments={'run_id': run['run_id']})
        atomic_json(requests.path(self.state, linked['request_id']), linked)
        report = self.sweep()
        self.assertTrue(requests.path(self.state, item['request_id']).exists())
        self.assertTrue(requests.path(self.state, linked['request_id']).exists())
        self.assertIn(run['run_id'], [row['run_id'] for row in self.state.journals()])
        listed = {row['id'] for row in report['unclassified']}
        self.assertTrue({item['request_id'], linked['request_id'], run['run_id']} <= listed)

    def test_incomplete_intents_and_extra_work_prevent_run_archival(self):
        original = self.deleted_run()
        for key in ('delivery_intent', 'retrieve_intent', 'local_delete_intent',
                    'remote_delete_intent', 'retention_delete_intent', 'staging_removal_intent', 'multipart'):
            with self.subTest(key=key):
                row = dict(original, **{key: {'at': OLD}})
                atomic_json(self.state.run_path(row['run_id']) / 'journal.json', row)
                self.sweep()
                self.assertTrue((self.state.run_path(row['run_id']) / 'journal.json').exists())
        atomic_json(self.state.run_path(original['run_id']) / 'journal.json', original)
        atomic_json(self.state.run_path(original['run_id']) / 'leftover.json', {})
        self.sweep()
        self.assertTrue((self.state.run_path(original['run_id']) / 'journal.json').exists())

    def test_cold_payload_expires_after_three_years_but_replay_key_survives(self):
        very_old = datetime.datetime(2023, 11, 10, tzinfo=datetime.timezone.utc).timestamp()
        item = self.complete_request(very_old, slot='expired-slot')
        self.deleted_run(at=very_old)
        report = self.sweep()
        self.assertEqual(report['deleted'], 2)
        for family in ('runs', 'requests'):
            self.assertEqual(list((self.state.root / 'history/v1' / family).glob('*.json')), [])
        replay = requests.create(self.state, 'destination-test', slot='expired-slot')
        self.assertEqual(replay['request_id'], item['request_id'])
        self.assertEqual(replay['status'], 'complete')

    def test_crash_after_key_or_move_resumes_without_reexecution_or_lost_record(self):
        from paymenter_backups import history
        item = self.complete_request(slot='crash-slot')
        original = history.move
        for after in (False, True):
            def interrupted(source, target):
                if after:
                    original(source, target)
                raise OSError('controlled crash')
            with patch.object(history, 'move', side_effect=interrupted):
                self.sweep()
            replay = requests.create(self.state, 'destination-test', slot='crash-slot')
            self.assertEqual(replay['status'], 'complete')
            self.assertTrue(requests.path(self.state, item['request_id']).exists()
                            or list((self.state.root / 'history/v1/requests').glob('*.json')))
        self.sweep()
        self.assertFalse(requests.path(self.state, item['request_id']).exists())
        self.assertEqual(len(list((self.state.root / 'history/v1/requests').glob('*.json'))), 1)

    def test_history_parents_are_durable_before_receipt_and_move_even_on_retry(self):
        from paymenter_backups import history
        item = self.complete_request(slot='durable-slot')
        root = self.state.root / 'history/v1'
        synced = []
        publish = history.atomic_json

        def durable():
            self.assertIn(self.state.root, synced)
            self.assertIn(root.parent, synced)
            self.assertGreaterEqual(synced.count(root), 6)

        def receipt(path, value):
            durable()
            publish(path, value)

        def interrupted(source, target):
            durable()
            raise OSError('controlled crash before move')

        # The second attempt sees a hierarchy and receipt left by the first.
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                synced.clear()
                with patch.object(history, 'sync', side_effect=synced.append), patch.object(
                        history, 'atomic_json', side_effect=receipt), patch.object(
                        history, 'move', side_effect=interrupted):
                    history.maintain(self.state, NOW)
                self.assertTrue(requests.path(self.state, item['request_id']).exists())
        history.maintain(self.state, NOW)
        self.assertEqual(requests.create(self.state, 'destination-test', slot='durable-slot')['status'], 'complete')

    def test_receipt_retry_crossing_cold_boundary_keeps_recorded_destination(self):
        from paymenter_backups import history
        at = datetime.datetime(2025, 10, 11, tzinfo=datetime.timezone.utc).timestamp()
        item = self.complete_request(at, slot='boundary-crash-slot')
        with patch.object(history, 'move', side_effect=OSError('controlled crash before move')):
            history.maintain(self.state, NOW)
        root = history.layout(self.state)
        key = root / 'request-keys' / (item['request_id'] + '.json')
        receipt = read_json(key)
        self.assertIn('recent_name', receipt)
        report = history.maintain(self.state, NOW + 86400)
        self.assertEqual(report['unclassified'], [])
        self.assertEqual(report['damaged'], [])
        self.assertFalse(requests.path(self.state, item['request_id']).exists())
        self.assertEqual(read_json(root / 'requests' / receipt['cold_name']), item)
        self.assertEqual(read_json(key), receipt)
        self.assertEqual(requests.create(self.state, 'destination-test', slot='boundary-crash-slot')['status'], 'complete')

    def test_damaged_or_foreign_cold_records_are_retained_and_reported(self):
        self.complete_request(slot='damaged-slot')
        self.sweep()
        cold = next((self.state.root / 'history/v1/requests').glob('*.json'))
        cold.write_text('{damaged')
        later = datetime.datetime(2029, 11, 11, tzinfo=datetime.timezone.utc).timestamp()
        report = self.sweep(later)
        self.assertTrue(cold.exists())
        self.assertEqual(report['damaged'][0]['code'], 'state-unreadable')
        requests.create(self.state, 'destination-test', slot='new-active-slot')
        self.assertEqual(len(requests.list_requests(self.state)), 1)

    def test_busy_run_lock_skips_maintenance_without_lock_inversion(self):
        item = self.complete_request()
        with self.state.lock():
            result = self.tick(NOW)
        self.assertEqual(result['launched'], [])
        self.assertTrue(requests.path(self.state, item['request_id']).exists())
        self.sweep()
        self.assertFalse(requests.path(self.state, item['request_id']).exists())

    def test_archived_run_receipt_allows_later_completed_request_retention(self):
        self.deleted_run()
        item = self.complete_request(NOW - 86400)
        item.update(action='delete', arguments={'run_id': 'old-run'})
        atomic_json(requests.path(self.state, item['request_id']), item)
        self.sweep()
        self.assertTrue(requests.path(self.state, item['request_id']).exists())
        later = datetime.datetime(2028, 1, 11, tzinfo=datetime.timezone.utc).timestamp()
        self.sweep(later)
        self.assertFalse(requests.path(self.state, item['request_id']).exists())

    def test_fractional_terminal_timestamp_can_expire(self):
        item = self.complete_request(OLD + 0.1234567, slot='fractional-slot')
        self.sweep()
        later = datetime.datetime(2029, 11, 11, tzinfo=datetime.timezone.utc).timestamp()
        report = self.sweep(later)
        self.assertEqual(report['deleted'], 1)
        self.assertEqual(requests.create(self.state, 'destination-test', slot='fractional-slot')['status'], 'complete')

    def test_unknown_record_shapes_stay_active_and_are_listed(self):
        item = self.complete_request()
        item['arguments'] = ['unknown']
        atomic_json(requests.path(self.state, item['request_id']), item)
        run = self.deleted_run()
        run['delete_intent'] = 'unknown'
        atomic_json(self.state.run_path(run['run_id']) / 'journal.json', run)
        report = self.sweep()
        self.assertTrue(requests.path(self.state, item['request_id']).exists())
        self.assertTrue((self.state.run_path(run['run_id']) / 'journal.json').exists())
        self.assertTrue({item['request_id'], run['run_id']} <= {row['id'] for row in report['unclassified']})

    def test_failed_request_keeps_its_deleted_run_in_the_work_set(self):
        row = self.deleted_run()
        item = self.complete_request()
        item.update(action='delete', arguments={'run_id': row['run_id']}, status='failed', error='worker-interrupted')
        atomic_json(requests.path(self.state, item['request_id']), item)
        report = self.sweep()
        self.assertTrue((self.state.run_path(row['run_id']) / 'journal.json').exists())
        self.assertIn(row['run_id'], {entry['id'] for entry in report['unclassified']})

    def test_history_directory_symlink_is_refused_without_following_it(self):
        self.complete_request()
        external = self.base / 'external'
        external.mkdir(mode=0o700)
        (self.state.root / 'history').symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(BackupError, 'private-state-permissions'):
            self.sweep()
        self.assertEqual(list(external.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
