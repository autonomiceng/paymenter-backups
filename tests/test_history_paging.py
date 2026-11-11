"""History pages use filenames before decoding terminal request/run payloads."""
import json
import pathlib
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from paymenter_backups import history, requests
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

    def test_recent_slot_replay_does_not_read_a_damaged_payload(self):
        item = self.complete_request(fixture.NOW - 86400, slot='recent-damaged-slot')
        self.sweep()
        root = history.layout(self.state)
        receipt = read_json(root / 'request-keys' / (item['request_id'] + '.json'))
        (root / 'recent-requests' / receipt['recent_name']).write_text('{broken')
        replay = requests.create(self.state, 'destination-test', slot='recent-damaged-slot')
        self.assertEqual(replay['request_id'], item['request_id'])
        self.assertEqual(replay['status'], 'complete')

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
            $fixture->showDeleted = false;
            $hidden = $fixture->page(1, 3, null, false);
            echo json_encode(['total' => $first->total(), 'first' => array_column($first->items(), '__key'),
                'reads' => $reads, 'second' => array_column($second->items(), '__key'), 'second_reads' => $secondReads,
                'search' => $search->total(), 'ascending' => array_column($ascending->items(), '__key'),
                'damaged' => array_column($damaged->items(), 'kind'), 'hidden' => $hidden->total(), 'deleted' => $fixture->deleted()]);
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
