"""History pages use filenames before decoding terminal request/run payloads."""
import json
import pathlib
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from paymenter_backups import history, observation, requests, storage
from paymenter_backups.common import BackupError, atomic_json, read_json
from paymenter_backups.presentation import overview
from test_engine_schedules import PrivateFixture
import test_history_retention as fixture

PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')


class HistoryPaging(PrivateFixture):
    complete_request = fixture.HistoryRetention.complete_request
    deleted_run = fixture.HistoryRetention.deleted_run
    sweep = fixture.HistoryRetention.sweep

    def recent_requests(self, count=12):
        items = [self.complete_request(fixture.NOW - index * 86400) for index in range(count)]
        self.sweep()
        return items

    def test_tick_does_not_decode_completed_history(self):
        items = self.recent_requests()
        target_ids = {row['request_id'] for row in items}
        original = read_json
        def checked(path):
            if path.parent.name in ('requests', 'recent-requests') and any(value in path.name for value in target_ids):
                self.fail('tick decoded completed history')
            return original(path)
        with patch.object(history, 'read_json', side_effect=checked), patch.object(requests, 'read_json', side_effect=checked):
            self.tick(fixture.NOW + 60)

    def test_request_page_is_selected_before_decoding_and_can_advance(self):
        items = self.recent_requests()
        with patch.object(history, 'owned', wraps=history.owned) as reads:
            first = history.recent_page(self.state, 'requests', limit=3)
            payload_reads = [call for call in reads.call_args_list if call.args[1].parent.name == 'recent-requests']
        self.assertEqual(len(payload_reads), 3)
        self.assertEqual([row['request_id'] for row in first['items']], [row['request_id'] for row in items[:3]])
        second = history.recent_page(self.state, 'requests', limit=3, offset=3)
        self.assertEqual([row['request_id'] for row in second['items']], [row['request_id'] for row in items[3:6]])
        self.assertEqual(first['total'], len(items))

    def test_damaged_recent_payload_does_not_block_tick_or_another_page(self):
        items = self.recent_requests()
        # Resolve the receipt independently of its payload, which is about to fail.
        root = history.layout(self.state)
        key = read_json(root / 'request-keys' / (items[-1]['request_id'] + '.json'))
        path = root / 'recent-requests' / key['recent_name']
        path.write_text('{broken')
        self.tick(fixture.NOW + 60)
        first = history.recent_page(self.state, 'requests', limit=3)
        self.assertEqual(first['errors'], [])
        last = history.recent_page(self.state, 'requests', offset=9, limit=3)
        self.assertEqual(last['errors'][0]['id'], items[-1]['request_id'])
        self.assertEqual(len(last['items']), 2)
        self.assertTrue(path.exists())

    def test_unresolved_recovery_remains_visible_outside_the_history_page(self):
        row = self.deleted_run()
        row['service_recovered'] = False
        atomic_json(self.state.run_path(row['run_id']) / 'journal.json', row)
        self.recent_requests(35)
        result = overview(self.state)
        self.assertIn('old-run', [item['run_id'] for item in result['runs']])
        self.assertEqual(next(item for item in result['runs'] if item['run_id'] == 'old-run')['status'], 'attention')
        from paymenter_backups.engine import plan
        self.assertIn('recovery-required', plan(self.state, self.envelope, probe_locks=False)['admission_errors'])

    def test_archive_failure_keeps_recovery_status_visible(self):
        from paymenter_backups import presentation
        archive, _ = self.backup('recovering', created_at=fixture.NOW)
        journal = self.item('recovering')
        journal['service_recovered'] = False
        self.state.save(journal)
        for damage in ('changed', 'missing'):
            if damage == 'changed':
                archive.write_bytes(b'corrupted archive')
            else:
                archive.unlink()
            for alive, status in ((False, 'attention'), (True, 'running')):
                with self.subTest(damage=damage, alive=alive), patch.object(presentation, 'alive', return_value=alive):
                    row = next(row for row in overview(self.state)['runs'] if row['run_id'] == 'recovering')
                    self.assertEqual(row['status'], status)
                    self.assertNotIn(row['download'], (None, 'ready', 'verify'))

    def test_recent_run_moves_to_cold_at_13_months_without_changing_its_manifest(self):
        original = self.deleted_run(at=fixture.NOW - 86400)
        self.sweep()
        root = history.layout(self.state)
        self.assertEqual(len(list((root / 'recent-runs').glob('*.json'))), 1)
        self.assertNotIn('old-run', [row['run_id'] for row in self.state.journals()])
        page = history.recent_page(self.state, 'runs')
        self.assertEqual(page['items'][0], original)
        self.sweep(fixture.NOW + 450 * 86400)
        self.assertEqual(list((root / 'recent-runs').glob('*.json')), [])
        cold = history.recent_page(self.state, 'runs', cold=True)
        self.assertEqual(cold['items'][0], original)

    def test_overview_reads_only_its_request_page_and_reports_recent_run_metadata(self):
        items = self.recent_requests(35)
        self.deleted_run(at=fixture.NOW - 86400)
        self.sweep()
        with patch.object(history, 'owned', wraps=history.owned) as reads:
            result = overview(self.state)
        payloads = [call for call in reads.call_args_list if call.args[1].parent.name.startswith('recent-')]
        self.assertEqual(len(payloads), 30)
        self.assertEqual(len(result['requests']), 30)
        self.assertEqual(result['recent_runs'][0]['run_id'], 'old-run')
        self.assertEqual(result['requests'][0]['request_id'], items[0]['request_id'])

    def test_job_wait_evidence_survives_supersession_and_history_page_eviction(self):
        at = fixture.NOW - 2 * 86400
        first = self.complete_request(at)
        first.update(action='run', arguments={'mode': 'database-online'}, status='cancelled',
                     error='schedule-superseded', job_wait_since=at)
        atomic_json(requests.path(self.state, first['request_id']), first)
        current = requests.create(self.state, arguments={'mode': 'database-online'})
        current.update(status='deferred', created_at=fixture.NOW - 60, job_wait_since=fixture.NOW - 60)
        atomic_json(requests.path(self.state, current['request_id']), current)
        for index in range(35):
            self.complete_request(fixture.NOW - index * 60)
        self.sweep()
        with patch('paymenter_backups.presentation.time.time', return_value=fixture.NOW):
            result = overview(self.state)
            self.assertNotIn(first['request_id'], [row['request_id'] for row in result['requests']])
            self.assertTrue(result['job_wait_overdue'])
            self.backup('new-full', mode='full-quiesced', created_at=fixture.NOW - 30)
            journal = self.item('new-full')
            journal['service_recovered'] = False
            self.state.save(journal)
            self.assertTrue(overview(self.state)['job_wait_overdue'])
            journal['service_recovered'] = True
            self.state.save(journal)
            with patch('paymenter_backups.history.replay', wraps=history.replay) as replayed:
                self.assertFalse(overview(self.state)['job_wait_overdue'])
            # Only history finished after the recovered capture is reread for wait evidence.
            self.assertLessEqual(replayed.call_count, 1)

    def test_recent_slot_replay_does_not_read_a_damaged_payload(self):
        item = self.complete_request(fixture.NOW - 86400, slot='recent-damaged-slot')
        self.sweep()
        root = history.layout(self.state)
        receipt = read_json(root / 'request-keys' / (item['request_id'] + '.json'))
        (root / 'recent-requests' / receipt['recent_name']).write_text('{broken')
        replay = requests.create(self.state, 'destination-test', slot='recent-damaged-slot')
        self.assertEqual(replay['request_id'], item['request_id'])
        self.assertEqual(replay['status'], 'complete')

    def test_run_fallback_refuses_changed_audit_payload_but_active_record_wins(self):
        from paymenter_backups import artifacts
        original = self.deleted_run(at=fixture.NOW - 86400)
        self.sweep()
        self.assertEqual(artifacts.journal(self.state, 'old-run'), original)
        root = history.layout(self.state)
        receipt = read_json(root / 'run-keys/old-run.json')
        target = root / 'recent-runs' / receipt['recent_name']
        atomic_json(target, dict(original, retained_deleted=False))
        with self.assertRaisesRegex(BackupError, 'history-payload-conflict'):
            artifacts.journal(self.state, 'old-run')
        self.state.run_path('old-run').mkdir(mode=0o700)
        atomic_json(self.state.run_path('old-run') / 'journal.json', original)
        self.assertEqual(artifacts.journal(self.state, 'old-run'), original)

    def test_request_fallback_refuses_changed_terminal_status(self):
        item = self.complete_request(fixture.NOW - 86400)
        self.sweep()
        with self.assertRaisesRegex(BackupError, 'request-not-cancellable'):
            requests.cancel(self.state, item['request_id'])
        root = history.layout(self.state)
        receipt = read_json(root / 'request-keys' / (item['request_id'] + '.json'))
        atomic_json(root / 'recent-requests' / receipt['recent_name'], dict(item, status='pending'))
        with self.assertRaisesRegex(BackupError, 'history-payload-conflict'):
            requests.cancel(self.state, item['request_id'])
        atomic_json(self.state.root / 'requests' / (item['request_id'] + '.json'), item)
        with self.assertRaisesRegex(BackupError, 'request-not-cancellable'):
            requests.cancel(self.state, item['request_id'])

    def damage_request(self, item):
        root = history.layout(self.state)
        receipt = read_json(root / 'request-keys' / (item['request_id'] + '.json'))
        target = root / 'recent-requests' / receipt['recent_name']
        target.write_text('{broken')
        return target

    def configured_s3_test(self, at):
        self.remote()
        self.envelope['credentials'] = {'access_key': 'fixture-access', 'secret_key': 'fixture-secret'}
        self.envelope['framework']['app_key'] = 'fixture-app-key'
        item = self.complete_request(at)
        item['result'] = storage.destination_test(self.state, self.envelope)
        self.assertTrue(item['result']['verified'])
        atomic_json(requests.path(self.state, item['request_id']), item)
        return item

    def test_configured_s3_overview_is_bounded_with_damaged_off_page_history(self):
        current = self.configured_s3_test(fixture.NOW + 1)
        older = self.recent_requests(35)
        damaged = self.damage_request(older[-1])
        with patch.object(history, 'owned', wraps=history.owned) as reads:
            result = overview(self.state, self.envelope)
        payloads = [call.args[1] for call in reads.call_args_list if call.args[1].parent.name == 'recent-requests']
        self.assertLessEqual(len(payloads), 31)
        self.assertNotIn(damaged, payloads)
        self.assertEqual(len(result['requests']), 30)
        self.assertEqual(result['history_errors'], [])
        self.assertTrue(next(row for row in result['requests'] if row['request_id'] == current['request_id'])['result_verified'])
        self.assertEqual(result['configuration']['installation']['checks']['offsite']['state'], 'ready')

    def test_s3_readiness_keeps_off_page_evidence_past_a_damaged_matching_test(self):
        current = self.configured_s3_test(fixture.NOW - 40 * 86400)
        for index in range(35):
            item = self.complete_request(fixture.NOW - index * 86400)
            item['action'] = 'retention'
            atomic_json(requests.path(self.state, item['request_id']), item)
        damaged = self.complete_request(fixture.NOW + 1)
        # A newer passing test for another binding leaves the current proof in history.
        newer = self.complete_request(fixture.NOW + 2)
        self.settings['s3_bucket'] = 'replacement'
        newer['result'] = storage.destination_test(self.state, self.envelope)
        self.settings['s3_bucket'] = 'fixture'
        atomic_json(requests.path(self.state, newer['request_id']), newer)
        history.maintain(self.state, fixture.NOW)
        target = self.damage_request(damaged)
        from paymenter_backups.commissioning import check
        with patch.object(history, 'owned', wraps=history.owned) as reads:
            result = check(self.state, self.envelope)
        payloads = [call.args[1] for call in reads.call_args_list if call.args[1].parent.name == 'recent-requests']
        self.assertEqual(len(payloads), 2)
        self.assertIn(target, payloads)
        self.assertTrue(any(current['request_id'] in path.name for path in payloads))
        self.assertEqual(result['checks']['offsite']['state'], 'ready')

    def test_incidents_keep_off_page_coverage_and_unresolved_work_without_reading_other_history(self):
        at = fixture.NOW - 40 * 86400
        self.remote()
        self.settings.update(database_schedule='hourly')
        requests.sync_schedule(self.state, self.settings, now=at)
        epoch = read_json(self.state.root / 'schedule.json')['kinds']['database']
        self.backup('covering-full', mode='full-quiesced', created_at=at + 3 * 3600, deliver=True)
        journal = self.item('covering-full')
        journal.update(database_snapshot_lower_bound=at + 3 * 3600,
                       database_coverage={'snapshot_lower_bound': at + 3 * 3600})
        self.state.save(journal)
        covered = requests.create(self.state, arguments={'mode': 'database-online'}, slot='covered-slot',
            scheduled_metadata={'schedule_kind': 'database', 'slot_at': at + 3 * 3600,
                                'schedule_fingerprint': epoch['fingerprint'], 'schedule_since': epoch['since']})
        requests.reconcile_coverage(self.state, self.envelope, cheap_only=True)
        self.assertEqual(read_json(requests.path(self.state, covered['request_id']))['status'], 'satisfied')
        # Keep the coverage receipt off-page by its original creation time.
        covered = read_json(requests.path(self.state, covered['request_id']))
        covered.update(created_at=at, updated_at=at + 3 * 3600, finished_at=at + 3 * 3600)
        atomic_json(requests.path(self.state, covered['request_id']), covered)
        older = [self.complete_request(fixture.NOW - index * 86400) for index in range(35)]
        history.maintain(self.state, fixture.NOW)
        failed = requests.create(self.state, arguments={'mode': 'database-online'})
        failed.update(status='failed', created_at=at + 3 * 3600 + 30)
        atomic_json(requests.path(self.state, failed['request_id']), failed)
        damaged = self.damage_request(older[-1])
        with patch.object(history, 'owned', wraps=history.owned) as reads:
            result = observation.check(self.state, self.envelope, now=at + 3 * 3600 + 60)
        payloads = [call.args[1] for call in reads.call_args_list if call.args[1].parent.name == 'recent-requests']
        self.assertEqual(len(payloads), 1)
        self.assertNotIn(damaged, payloads)
        for location in ('local', 'offsite'):
            database = next(row for row in result['checks'] if row['key'] == 'database:' + location)
            self.assertEqual(database['last_good_at'], at + 3 * 3600)
            self.assertEqual(database['reason'], 'backup-failed')
        self.assertTrue(requests.path(self.state, failed['request_id']).exists())

    def test_invalid_history_names_are_local_errors_without_blocking_tick(self):
        self.recent_requests(1)
        root = history.layout(self.state)
        atomic_json(root / 'recent-requests' / 'unknown.json', {'unknown': True})
        self.assertEqual(self.tick(fixture.NOW)['launched'], [])
        page = history.recent_page(self.state, 'requests', limit=3)
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['errors'][0]['code'], 'history-name-invalid')

    def test_cli_history_page_returns_whitelisted_rows_without_bridge_access(self):
        self.recent_requests(4)
        root = pathlib.Path(__file__).resolve().parents[1]
        import sys
        result = subprocess.run([sys.executable, '-I', str(root / 'bin/paymenter-backups'), '--app', str(self.state.app),
            '--private-root', str(self.state.root), '--bridge', str(self.base / 'nonexistent.php'),
            'history-page', 'requests', '--offset', '1', '--limit', '2'], text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['total'], 4)
        self.assertEqual(len(value['items']), 2)
        self.assertNotIn(str(self.state.root), result.stdout)
        self.assertNotIn('arguments', result.stdout)
        listed = subprocess.run([sys.executable, '-I', str(root / 'bin/paymenter-backups'), '--app', str(self.state.app),
            '--private-root', str(self.state.root), 'requests', 'list', '--limit', '2', '--offset', '1'],
            text=True, capture_output=True, check=False)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)), 2)

    def test_reader_handles_a_concurrent_terminal_move_only_with_its_receipt(self):
        item = self.complete_request(fixture.NOW - 86400)
        target = requests.path(self.state, item['request_id'])
        original = read_json
        moved = False
        def rename_during_read(path):
            nonlocal moved
            if path == target and not moved:
                moved = True
                history.maintain(self.state, fixture.NOW)
            return original(path)
        with patch.object(history, 'read_json', side_effect=rename_during_read):
            self.assertEqual(requests.list_requests(self.state, history=False), [])
        self.assertTrue(moved)
        self.assertEqual(history.recent_page(self.state, 'requests')['items'][0]['request_id'], item['request_id'])

    @unittest.skipUnless(PHP, 'PHP CLI required')
    def test_native_table_fetches_only_selected_deleted_records(self):
        from paymenter_backups import storage
        from paymenter_backups.presentation import run_entry
        self.backup('survivor', created_at=fixture.NOW)
        for index in range(8):
            run_id = 'deleted-' + str(index)
            self.backup(run_id, created_at=fixture.NOW - (index + 1) * 86400)
            with patch.object(storage.time, 'time', return_value=fixture.NOW - 86400):
                storage.delete_run(self.state, self.envelope, run_id, location='local')
        self.sweep()
        view = overview(self.state)
        rows = history.recent_page(self.state, 'runs', limit=100)['items']
        data = {'overview': view, 'runs': [run_entry(self.state, row, set()) for row in rows]}
        code = r'''namespace Illuminate\Pagination {
            class LengthAwarePaginator {
                public function __construct(private array $items, private int $total, int $perPage, int $currentPage) {}
                public function items(): array { return $this->items; }
                public function total(): int { return $this->total; }
            }
        }
        namespace Paymenter\Extensions\Others\Backups {
            class Runtime {
                public static array $rows = [];
                public static array $reads = [];
                public static function command(array $args, $input = null, $timeout = null): array {
                    $ids = explode(',', $args[5]);
                    self::$reads = array_merge(self::$reads, $ids);
                    return ['items' => array_values(array_filter(self::$rows, fn ($row) => in_array($row['run_id'], $ids, true)))];
                }
            }
        }
        namespace {
            require 'extension/Backups/Messages.php';
            require 'extension/Backups/Admin/Support/HistorySearch.php';
            require 'extension/Backups/Admin/Pages/Concerns/BuildsHistoryTable.php';
            class PageFixture {
                use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsHistoryTable {
                    historyPage as public page;
                    hasDeletedRuns as public deleted;
                }
                private const ACTIVE = ['pending', 'deferred', 'launching', 'running'];
                public bool $showDeleted = true;
                public function __construct(private array $data) {}
                private function overview(): array { return $this->data; }
                private function uploadsToS3(): bool { return false; }
                private function tasksFor(string $id): array { return []; }
                private function schedulerFresh(): bool { return true; }
                private function configuration(): array { return []; }
            }
            $input = json_decode(stream_get_contents(STDIN), true, flags: JSON_THROW_ON_ERROR);
            \Paymenter\Extensions\Others\Backups\Runtime::$rows = $input['runs'];
            $fixture = new PageFixture($input['overview']);
            $first = $fixture->page(1, 3, null, false);
            $reads = \Paymenter\Extensions\Others\Backups\Runtime::$reads;
            \Paymenter\Extensions\Others\Backups\Runtime::$reads = [];
            $second = $fixture->page(2, 3, null, false);
            $secondReads = \Paymenter\Extensions\Others\Backups\Runtime::$reads;
            $search = $fixture->page(1, 3, 'Deleted', false);
            $ascending = $fixture->page(1, 3, null, true);
            \Paymenter\Extensions\Others\Backups\Runtime::$rows = [];
            $damaged = $fixture->page(2, 3, null, false);
            $damagedSearch = $fixture->page(1, 3, 'deleted-3', false);
            $unreadableSearch = $fixture->page(1, 20, 'Unreadable', false);
            $fixture->showDeleted = false;
            $hiddenSearch = $fixture->page(1, 3, 'deleted-3', false);
            $hidden = $fixture->page(1, 3, null, false);
            echo json_encode(['total' => $first->total(), 'first' => array_column($first->items(), '__key'),
                'reads' => $reads, 'second' => array_column($second->items(), '__key'), 'second_reads' => $secondReads,
                'search' => $search->total(), 'ascending' => array_column($ascending->items(), '__key'),
                'damaged' => array_column($damaged->items(), 'kind'), 'damaged_search' => $damagedSearch->items(),
                'unreadable_search' => $unreadableSearch->total(), 'hidden_search' => $hiddenSearch->total(),
                'hidden' => $hidden->total(), 'deleted' => $fixture->deleted()]);
        }'''
        result = subprocess.run([PHP, '-r', code], input=json.dumps(data), text=True, capture_output=True,
                                cwd=pathlib.Path(__file__).resolve().parents[1], check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['total'], 9)
        self.assertEqual(value['first'], ['survivor', 'deleted-0', 'deleted-1'])
        self.assertEqual(value['reads'], ['deleted-0', 'deleted-1'])
        self.assertEqual(value['second'], ['deleted-2', 'deleted-3', 'deleted-4'])
        self.assertEqual(value['second_reads'], value['second'])
        self.assertEqual(value['ascending'], ['deleted-7', 'deleted-6', 'deleted-5'])
        self.assertEqual(value['search'], 8)
        self.assertEqual(value['damaged'], ['history-error'] * 3)
        self.assertEqual(len(value['damaged_search']), 1)
        self.assertEqual(value['damaged_search'][0]['__key'], 'deleted-3')
        self.assertEqual(value['damaged_search'][0]['kind'], 'history-error')
        self.assertIsNone(value['damaged_search'][0]['run_id'])
        self.assertIsNone(value['damaged_search'][0]['run'])
        self.assertEqual(value['unreadable_search'], 8)
        self.assertEqual(value['hidden_search'], 0)
        self.assertEqual(value['hidden'], 1)
        self.assertTrue(value['deleted'])

    def test_cold_page_errors_are_local_and_private_permissions_are_checked(self):
        self.complete_request(slot='cold-damage')
        self.sweep()
        root = history.layout(self.state)
        path = next((root / 'requests').glob('*.json'))
        path.chmod(0o644)
        page = history.recent_page(self.state, 'requests', cold=True)
        self.assertEqual(page['items'], [])
        self.assertEqual(page['errors'][0]['code'], 'private-state-permissions')
        self.assertEqual(self.tick(fixture.NOW)['launched'], [])


if __name__ == '__main__':
    unittest.main()
