"""Configuration publication uses real shared locks and synthetic private state."""
import copy
import json
import os
import pathlib
import sys
import threading
import time
import types
from unittest.mock import patch

from paymenter_backups import cli, config, requests
from paymenter_backups.common import BackupError, atomic_json, read_json
from test_engine_schedules import PrivateFixture


class CoordinatedSnapshotTests(PrivateFixture):
    def test_worker_snapshot_waits_for_combined_settings_and_credentials(self):
        args = types.SimpleNamespace(app=str(self.app), private_root=str(self.state.root))
        envelope = copy.deepcopy(self.envelope)
        envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(self.state.root))
        envelope['credentials'] = {'access_key': 'fake-old', 'secret_key': 'fake-old'}
        started = threading.Event()
        finished = threading.Event()
        results = []

        def read():
            started.set()
            try:
                results.append(config.load(args))
            except BaseException as error:
                results.append(error)
            finally:
                finished.set()

        with patch.object(config, 'bridge', side_effect=lambda *_: copy.deepcopy(envelope)), patch.object(config, 'validate'):
            with self.state.lock('config.lock'):
                envelope['settings']['s3_bucket'] = 'new-destination'
                reader = threading.Thread(target=read)
                reader.start()
                self.assertTrue(started.wait(1))
                blocked = not finished.wait(0.15)
                envelope['credentials'] = {'access_key': 'fake-new', 'secret_key': 'fake-new'}
            reader.join(3)
        self.assertTrue(blocked, 'Worker read new settings with old credentials while Save was publishing')
        self.assertFalse(reader.is_alive())
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], dict, repr(results[0]))
        self.assertEqual(results[0]['settings']['s3_bucket'], 'new-destination')
        self.assertEqual(results[0]['credentials']['access_key'], 'fake-new')


# Real SettingsWriter and ConfigSetCommand run against an audited JSON model seam.
# This seam deliberately hydrates encrypted as an integer like SQL, and records
# transactions/row-lock reads. Native integration tests exercise Eloquent.
PHP_WRITER = r'''<?php
namespace Illuminate\Console {
    class Command {
        const SUCCESS = 0; const FAILURE = 1;
        public function option($key) {
            foreach (array_slice($GLOBALS['argv'], 1) as $option) {
                if ($option === '--' . $key) { return true; }
                if (str_starts_with($option, '--' . $key . '=')) { return substr($option, strlen($key) + 3); }
            }
            return null;
        }
        public function line($line) { echo $line, "\n"; }
        // Illuminate Command::error uses the styled console output stream.
        public function error($line) { echo "   ERROR  ", $line, "\n"; }
    }
}
namespace Illuminate\Support\Facades {
    class DB {
        public static bool $active = false;
        public static function transaction($callback) {
            if (self::$active) { return $callback(); }
            $handle = fopen(\fixtureFile(), 'r+b'); flock($handle, LOCK_EX);
            \App\Models\Extension::$data = json_decode(stream_get_contents($handle), true);
            self::$active = true;
            try {
                $result = $callback();
                rewind($handle); ftruncate($handle, 0);
                fwrite($handle, json_encode(\App\Models\Extension::$data)); fflush($handle);
                return $result;
            } finally { self::$active = false; fclose($handle); }
        }
    }
    class Validator {
        public static function make(...$args) { return new class { public function validate() {} }; }
    }
    class Auth {
        public static int $actor = 0;
        public static function loginUsingId($actor) { self::$actor = $actor; return true; }
        public static function user() { return new class { public function hasPermission($permission) { return true; } }; }
    }
}
namespace App\Models {
    class Extension {
        public static array $data;
        public static function where(...$args) { return new static; }
        public function firstOrFail() { return $this; }
        public function settings() { return new Query; }
    }
    class Query {
        private string $key;
        private bool $locked = false;
        public function where($field, $key) { $this->key = $key; return $this; }
        public function lockForUpdate() {
            if (!\Illuminate\Support\Facades\DB::$active) { throw new \RuntimeException('compare-outside-transaction'); }
            $this->locked = true; return $this;
        }
        public function first() {
            if (!$this->locked) { throw new \RuntimeException('compare-without-row-lock'); }
            return isset(Extension::$data['rows'][$this->key]) ? new Row($this->key) : null;
        }
        public function get() {
            if (!$this->locked) { throw new \RuntimeException('snapshot-without-row-lock'); }
            $rows = array_map(static fn ($key) => new Row($key), array_keys(Extension::$data['rows']));
            return new class($rows) {
                public function __construct(private array $rows) {}
                public function all() { return $this->rows; }
            };
        }
        public function updateOrCreate($keys, $attributes) {
            $key = $keys['key'];
            $previous = Extension::$data['rows'][$key] ?? ['id' => ++Extension::$data['next_id'], 'key' => $key];
            Extension::$data['rows'][$key] = array_replace($previous, $attributes);
            $row = new Row($key); $row->save(); return $row;
        }
    }
    class Row {
        public function __construct(private string $key) {}
        public function __get($name) { return Extension::$data['rows'][$this->key][$name]; }
        public function getAttributes() { return Extension::$data['rows'][$this->key]; }
        public function refresh() {
            Extension::$data['rows'][$this->key]['encrypted'] = (int) Extension::$data['rows'][$this->key]['encrypted'];
            return $this;
        }
        public function forceFill($values) { Extension::$data['rows'][$this->key] = array_replace(Extension::$data['rows'][$this->key], $values); return $this; }
        public function save() {
            Extension::$data['rows'][$this->key]['updated_at'] = ++Extension::$data['sequence'];
            Extension::$data['audits'][] = ['key' => $this->key, 'actor' => \Illuminate\Support\Facades\Auth::$actor, 'action' => 'save'];
        }
        public function delete() {
            unset(Extension::$data['rows'][$this->key]);
            Extension::$data['audits'][] = ['key' => $this->key, 'actor' => \Illuminate\Support\Facades\Auth::$actor, 'action' => 'delete'];
        }
    }
}
namespace Paymenter\Extensions\Others\Backups {
    class Backups { public function getConfig() { return []; } }
    class Runtime {
        public static function publishCredentialsWhileLocked($input, $root) {
            $saved = json_decode(file_get_contents($root . '/native-credentials.json'), true);
            if (!is_array($saved)) { throw new \RuntimeException('credentials-unreadable'); }
            if (($input['s3'] ?? null) !== null) {
                $saved = ($input['s3']['clear'] ?? false) === true ? (object) [] : $input['s3'];
            }
            file_put_contents($root . '/native-credentials.json', json_encode($saved));
        }
    }
}
namespace {
    function base_path() { return __DIR__; }
    function config($values) {}
    function fixtureFile() {
        $command = new \Paymenter\Extensions\Others\Backups\ConfigSetCommand;
        return $command->option('coordination-root') . '/native-rows.json';
    }
    require __ROOT__ . '/extension/Backups/Settings.php';
    require __ROOT__ . '/extension/Backups/PrivateStore.php';
    require __ROOT__ . '/extension/Backups/SettingsWriter.php';
    require __ROOT__ . '/extension/Backups/ConfigSetCommand.php';
    exit((new \Paymenter\Extensions\Others\Backups\ConfigSetCommand)->handle());
}
'''


class CoordinatedSaveTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        import shutil
        self.php = shutil.which('php')
        if not self.php:
            self.fail('PHP is required for the audited configuration writer regression')
        root = pathlib.Path(__file__).resolve().parents[1]
        (self.app / 'artisan').write_text(PHP_WRITER.replace('__ROOT__', json.dumps(str(root))))
        self.rows_file = self.state.root / 'native-rows.json'
        self.rows_file.write_text(json.dumps({'rows': {
            's3_bucket': {'id': 1, 'key': 's3_bucket', 'value': 'old-destination', 'type': 'string', 'encrypted': 0, 'updated_at': 1},
            'retention_keep_full': {'id': 2, 'key': 'retention_keep_full', 'value': '7', 'type': 'string', 'encrypted': 0, 'updated_at': 1}},
            'next_id': 2, 'sequence': 1, 'audits': []}))
        self.rows_file.chmod(0o600)
        self.credentials_file = self.state.root / 'native-credentials.json'
        self.credentials = {'access_key': 'fake-old', 'secret_key': 'fake-old'}
        self.args = types.SimpleNamespace(app=str(self.app), private_root=str(self.state.root), php=self.php)
        for target in (cli, config):
            mocked = patch.object(target, 'bridge', side_effect=self.bridge)
            mocked.start(); self.addCleanup(mocked.stop)
            mocked = patch.object(target, 'validate')
            mocked.start(); self.addCleanup(mocked.stop)
        mocked = patch.object(cli, 'schedule_admission')
        mocked.start(); self.addCleanup(mocked.stop)

    @property
    def credentials(self):
        return json.loads(self.credentials_file.read_text())

    @credentials.setter
    def credentials(self, value):
        self.credentials_file.write_text(json.dumps(value))
        self.credentials_file.chmod(0o600)

    def rows(self):
        return json.loads(self.rows_file.read_text())

    def bridge(self, args, verb='read', value=None):
        if verb == 'credentials-check':
            return {'valid': True}
        if verb == 'credentials-merge':
            self.credentials = value['s3'] or self.credentials
            return {'ok': True}
        envelope = copy.deepcopy(self.envelope)
        envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(self.state.root))
        envelope['settings'].update({key: row['value'] for key, row in self.rows()['rows'].items()})
        envelope['credentials'] = {} if verb == 'read-settings' else copy.deepcopy(self.credentials)
        return envelope

    def test_audited_native_receipts_skip_unchanged_rows_and_restore_only_own_rows(self):
        with self.state.lock('config.lock') as config_fd, self.state.lock('tick.lock') as tick_fd:
            receipt = cli.native_settings(self.args, self.state, {'s3_bucket': 'replacement', 'retention_keep_full': '7',
                's3_region': 'new-region'}, (config_fd, tick_fd), actor=9)['receipt']
            self.assertEqual(set(receipt), {'s3_bucket', 's3_region'})
            # A native editor does not participate in our coordinator. Rollback
            # must still preserve this later publication under a database lock.
            cli.native_settings(self.args, self.state, {'s3_bucket': 'concurrent-editor'}, (config_fd, tick_fd), actor=11)
            restored = cli.native_settings(self.args, self.state, receipt, (config_fd, tick_fd), actor=9, restore=True)
        self.assertEqual(restored['skipped'], ['s3_bucket'])
        rows = self.rows()
        self.assertEqual(rows['rows']['s3_bucket']['value'], 'concurrent-editor')
        self.assertNotIn('s3_region', rows['rows'])
        self.assertEqual([(audit['key'], audit['actor'], audit['action']) for audit in rows['audits']],
            [('s3_bucket', 9, 'save'), ('s3_region', 9, 'save'), ('s3_bucket', 11, 'save'), ('s3_region', 9, 'delete')])

    def test_credentials_failure_restores_settings_and_schedule_before_unlock(self):
        original_native = cli.native_settings
        def refuse(*args, **kwargs):
            if kwargs.get('finalize'):
                raise BackupError('credentials-unreadable')
            return original_native(*args, **kwargs)
        with patch.object(cli, 'native_settings', side_effect=refuse):
            with self.assertRaisesRegex(BackupError, 'credentials-unreadable'):
                cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
                    'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}, '_actor': 9})
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'old-destination')
        self.assertEqual(self.credentials['access_key'], 'fake-old')
        self.assertEqual(len(self.rows()['audits']), 2)
        self.assertFalse((self.state.root / 'config-save.json').exists())

    def test_actual_native_unreadable_store_refusal_rolls_back_and_clears_guard(self):
        corrupted = b'unreadable-fixture-credential-store'
        self.credentials_file.write_bytes(corrupted)
        with self.assertRaisesRegex(BackupError, 'credentials-unreadable'):
            cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
                'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
        self.assertEqual(self.credentials_file.read_bytes(), corrupted)
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'old-destination')
        self.assertFalse((self.state.root / 'config-save.json').exists(),
            'Definite native credential refusal left backup work paused as an uncertain commit')

    def test_initialization_failure_preserves_interleaved_editor_and_reports_partial(self):
        real_load = cli.load
        failed = False
        def fail_initialization(args, *positional, **keywords):
            nonlocal failed
            if not failed:
                failed = True
                rows = self.rows()
                rows['rows']['s3_bucket'].update(value='concurrent-editor', updated_at=100)
                self.rows_file.write_text(json.dumps(rows))
                raise BackupError('destination-inside-docroot')
            return real_load(args, *positional, **keywords)
        with patch.object(cli, 'load', side_effect=fail_initialization):
            result = cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
                'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
        self.assertFalse(result['updated'])
        self.assertTrue(result['partial'])
        self.assertTrue(result['concurrent_settings_preserved'])
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'concurrent-editor')
        self.assertEqual(self.credentials['access_key'], 'fake-old')

    def test_interleaved_saves_keep_each_destination_and_credentials_together(self):
        published = threading.Event()
        release = threading.Event()
        second_done = threading.Event()
        reader_done = threading.Event()
        outcomes = []
        snapshots = []
        real_native = cli.native_settings
        def pause_writer(*args, **kwargs):
            result = real_native(*args, **kwargs)
            if args[2].get('s3_bucket') == 'first-destination':
                published.set()
                if not release.wait(3):
                    raise AssertionError('Writer release did not arrive')
            return result
        def save(destination, key, done=None):
            try:
                outcomes.append(cli.set_config(self.args, self.state, {'settings': {'s3_bucket': destination},
                    'credentials': {'access_key': key, 'secret_key': key}}))
            except BaseException as error:
                outcomes.append(error)
            finally:
                if done: done.set()
        def read():
            try: snapshots.append(config.load(self.args))
            finally: reader_done.set()
        with patch.object(cli, 'native_settings', side_effect=pause_writer):
            first = threading.Thread(target=save, args=('first-destination', 'fake-first'))
            first.start()
            self.assertTrue(published.wait(2))
            second = threading.Thread(target=save, args=('second-destination', 'fake-second', second_done))
            reader = threading.Thread(target=read)
            second.start(); reader.start()
            second_blocked = not second_done.wait(0.15)
            reader_blocked = not reader_done.wait(0.15)
            release.set()
            first.join(4); second.join(4); reader.join(4)
        self.assertTrue(second_blocked, 'Second save crossed the first publication')
        self.assertTrue(reader_blocked, 'Worker crossed the settings/credentials publication gap')
        self.assertFalse(first.is_alive() or second.is_alive() or reader.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(isinstance(item, dict) and item.get('updated') for item in outcomes), repr(outcomes))
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'second-destination')
        self.assertEqual(self.credentials['access_key'], 'fake-second')
        self.assertIn((snapshots[0]['settings']['s3_bucket'], snapshots[0]['credentials']['access_key']),
            [('first-destination', 'fake-first'), ('second-destination', 'fake-second')])

    def test_internal_writer_requires_inherited_coordinator(self):
        import subprocess
        result = subprocess.run([self.php, str(self.app / 'artisan'), '--defer-init',
            '--coordination-root=' + str(self.state.root)], input=b'{"s3_bucket":"replacement"}', capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'config-coordination-required', result.stderr)
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'old-destination')

    def test_config_dispatch_repairs_malformed_native_scalar_before_validation(self):
        rows = self.rows()
        rows['rows']['retention_keep_full']['value'] = 'invalid-integer'
        self.rows_file.write_text(json.dumps(rows))
        args = cli.parser().parse_args(['--app', str(self.app), '--private-root', str(self.state.root),
            '--php', self.php, 'config', 'set'])
        with patch.object(cli, 'stdin_json', return_value={'settings': {'retention_keep_full': 7}}):
            result = cli.dispatch(args)
        self.assertTrue(result['updated'])
        self.assertEqual(self.rows()['rows']['retention_keep_full']['value'], '7')
        self.assertEqual(len(self.rows()['audits']), 1)
        self.assertFalse((self.state.root / 'config-save.json').exists())

    def test_config_dispatch_still_refuses_unrepaired_malformed_native_values(self):
        rows = self.rows()
        rows['rows']['retention_keep_full']['value'] = 'invalid-integer'
        self.rows_file.write_text(json.dumps(rows))
        args = cli.parser().parse_args(['--app', str(self.app), '--private-root', str(self.state.root),
            '--php', self.php, 'config', 'set'])
        with patch.object(cli, 'stdin_json', return_value={'settings': {'s3_bucket': 'replacement'}}):
            with self.assertRaisesRegex(BackupError, 'config-invalid'):
                cli.dispatch(args)
        self.assertEqual(self.rows()['rows']['retention_keep_full']['value'], 'invalid-integer')
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'old-destination')
        self.assertEqual(self.rows()['audits'], [])
        self.assertFalse((self.state.root / 'config-save.json').exists())

    def test_rejected_credential_save_preserves_exact_schedule_epoch_and_pending_request(self):
        self.settings.update(database_schedule='hourly')
        epoch = requests._sync_schedule(self.state, self.settings, 100)
        item = requests.create(self.state, arguments={'mode': 'database-online'}, slot='database:fixture-slot',
            scheduled_metadata={'schedule_kind': 'database', 'slot_at': 120,
                'schedule_fingerprint': epoch['kinds']['database']['fingerprint'], 'schedule_since': 100})
        before_schedule = (self.state.root / 'schedule.json').read_bytes()
        request_path = requests.path(self.state, item['request_id'])
        before_request = request_path.read_bytes()
        real_native = cli.native_settings
        def refuse(*args, **kwargs):
            if kwargs.get('finalize'):
                raise BackupError('credentials-unreadable')
            return real_native(*args, **kwargs)
        with patch.object(cli, 'native_settings', side_effect=refuse):
            with self.assertRaisesRegex(BackupError, 'credentials-unreadable'):
                cli.set_config(self.args, self.state, {'settings': {'database_schedule': 'daily'},
                    'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
        self.assertEqual((self.state.root / 'schedule.json').read_bytes(), before_schedule)
        self.assertEqual(request_path.read_bytes(), before_request)
        self.assertFalse((self.state.root / 'config-save.json').exists())

    def test_editor_change_at_credential_commit_refuses_and_preserves_concurrent_rows(self):
        real_native = cli.native_settings
        def edit_then_finalize(*args, **kwargs):
            if kwargs.get('finalize'):
                rows = self.rows()
                rows['rows']['s3_bucket'].update(value='concurrent-editor', updated_at=500)
                self.rows_file.write_text(json.dumps(rows))
            return real_native(*args, **kwargs)
        with patch.object(cli, 'native_settings', side_effect=edit_then_finalize):
            result = cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
                'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
        self.assertTrue(result['partial'])
        self.assertEqual(result['error'], 'config-changed')
        self.assertTrue(result['concurrent_settings_preserved'])
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'concurrent-editor')
        self.assertEqual(self.credentials['access_key'], 'fake-old')
        self.assertFalse((self.state.root / 'config-save.json').exists())

    def test_native_commit_then_missing_stdout_guards_workers_and_reports_unknown(self):
        import subprocess
        original_run = cli.run_native_writer
        def lose_receipt(command, *args, **kwargs):
            completed = original_run(command, *args, **kwargs)
            if 'backups:config-set' in command and '--restore' not in command:
                return subprocess.CompletedProcess(command, 0, b'', b'')
            return completed
        with patch.object(cli, 'run_native_writer', side_effect=lose_receipt):
            result = cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
                'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
        self.assertTrue(result['outcome_unknown'])
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'replacement')
        self.assertEqual(self.credentials['access_key'], 'fake-old')
        for coordinated in (True, False):
            with self.subTest(coordinated=coordinated), self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
                config.load(self.args, coordinated=coordinated)
        with self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
            cli.set_config(self.args, self.state, {'settings': {}})
        complete = cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
            'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
        self.assertTrue(complete['updated'])
        self.assertFalse((self.state.root / 'config-save.json').exists())
        self.assertEqual(config.load(self.args)['credentials']['access_key'], 'fake-new')

    def test_lost_receipt_then_rejected_retry_keeps_prior_uncertainty(self):
        import subprocess
        real_run = cli.run_native_writer
        def lose_receipt(command, *args, **kwargs):
            completed = real_run(command, *args, **kwargs)
            return subprocess.CompletedProcess(command, 0, b'', b'') if '--finalize' not in command else completed
        replacement = {'settings': {'s3_bucket': 'replacement'},
            'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}}
        with patch.object(cli, 'run_native_writer', side_effect=lose_receipt):
            self.assertTrue(cli.set_config(self.args, self.state, copy.deepcopy(replacement))['outcome_unknown'])
        guard = self.state.root / 'config-save.json'
        before_guard = guard.read_bytes()
        real_native = cli.native_settings
        def edit_then_finalize(*args, **kwargs):
            if kwargs.get('finalize'):
                rows = self.rows()
                rows['rows']['retention_keep_full'].update(value='8', updated_at=500)
                self.rows_file.write_text(json.dumps(rows))
            return real_native(*args, **kwargs)
        with patch.object(cli, 'native_settings', side_effect=edit_then_finalize):
            with self.assertRaisesRegex(BackupError, 'config-changed'):
                cli.set_config(self.args, self.state, copy.deepcopy(replacement))
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'replacement')
        self.assertEqual(self.credentials['access_key'], 'fake-old')
        self.assertTrue(guard.exists(), 'Rejected retry removed the earlier ambiguous-publication guard')
        self.assertEqual(guard.read_bytes(), before_guard)
        for coordinated in (True, False):
            with self.subTest(coordinated=coordinated), self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
                config.load(self.args, coordinated=coordinated)
        with self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
            cli.set_config(self.args, self.state, {'settings': {}})
        self.assertTrue(cli.set_config(self.args, self.state, replacement)['updated'])
        self.assertFalse(guard.exists())
        self.assertEqual(config.load(self.args)['credentials']['access_key'], 'fake-new')

    def test_definite_rollback_after_schedule_sync_preserves_waiting_slot_on_next_tick(self):
        import datetime
        before_slot = datetime.datetime(2026, 10, 14, 8, 50, tzinfo=datetime.timezone.utc).timestamp()
        now = before_slot + 20 * 60
        slot_at = before_slot + 10 * 60
        self.settings.update(database_schedule='daily', database_time='09:00')
        requests._sync_schedule(self.state, self.settings, before_slot)
        with patch.object(requests.time, 'time', return_value=slot_at), patch.object(requests, 'detach', return_value=0), patch.object(requests, 'process_identity', return_value=None):
            requests.tick(self.state, self.envelope, ['unused'])
        item = requests.list_requests(self.state)[0]
        item.update(status='deferred', retry_at=now + 60)
        request_path = requests.path(self.state, item['request_id'])
        atomic_json(request_path, item)
        before_request = request_path.read_bytes()
        paths = [self.state.root / name for name in ('schedule.json', 'policy.json', 'configuration-advisory.json')]
        atomic_json(paths[1], {'schema_version': 1, 'extra_docroots': [], 'fixture': 'prior-policy'})
        atomic_json(paths[2], {'schema_version': 1, 'checked_at': before_slot, 'valid': True})
        before = {path: path.read_bytes() for path in paths}
        real_atomic = cli.atomic_json
        def fail_advisory(path, value):
            real_atomic(path, value)
            if pathlib.Path(path).name == 'configuration-advisory.json':
                raise OSError('one-off advisory directory fsync failure')
        with patch.object(cli.time, 'time', return_value=now), patch.object(cli, 'atomic_json', side_effect=fail_advisory):
            with self.assertRaisesRegex(BackupError, 'config-save-failed'):
                cli.set_config(self.args, self.state, {'settings': {'database_time': '09:30'}})
        self.assertNotIn('database_time', self.rows()['rows'])
        after = {path: path.read_bytes() for path in paths}
        self.assertEqual(request_path.read_bytes(), before_request)
        with patch.object(requests.time, 'time', return_value=now), patch.object(requests, 'detach') as detach:
            requests.tick(self.state, self.bridge(self.args), ['unused'])
        detach.assert_not_called()
        waiting = read_json(request_path)
        self.assertEqual(waiting['status'], 'deferred')
        self.assertEqual(waiting['slot_at'], slot_at)
        self.assertEqual(len(requests.list_requests(self.state)), 1)
        for path in paths:
            with self.subTest(file=path.name):
                self.assertEqual(after[path], before[path])

    def test_explicit_clearing_requires_every_uncertain_credential_group(self):
        guard = self.state.root / 'config-save.json'
        atomic_json(guard, {'schema_version': 1, 's3_requested': True, 'site_requested': True})
        before_guard = guard.read_bytes()
        with self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
            cli.set_config(self.args, self.state, {'settings': {}, 'credentials': {'clear': True}})
        self.assertEqual(guard.read_bytes(), before_guard)
        self.assertEqual(self.credentials['access_key'], 'fake-old')
        self.assertEqual(self.rows()['audits'], [])
        result = cli.set_config(self.args, self.state, {'settings': {}, 'credentials': {'clear': True},
            'maintenance_probe_credentials': {'clear': True}})
        self.assertTrue(result['credentials_updated'])
        self.assertTrue(result['maintenance_probe_credentials_updated'])
        self.assertEqual(self.credentials, {})
        self.assertFalse(guard.exists())

    def test_definite_rollback_after_guard_fsync_restores_absent_derived_files(self):
        paths = [self.state.root / name for name in ('schedule.json', 'policy.json', 'configuration-advisory.json')]
        self.assertTrue(all(not path.exists() for path in paths))
        real_finish = cli.finish_config_save
        calls = 0
        def fail_after_unlink(state):
            nonlocal calls
            calls += 1
            real_finish(state)
            if calls == 1:
                raise OSError('one-off intent directory fsync failure')
        with patch.object(cli, 'finish_config_save', side_effect=fail_after_unlink):
            with self.assertRaisesRegex(BackupError, 'config-save-failed'):
                cli.set_config(self.args, self.state, {'settings': {'database_time': '09:30'}})
        self.assertNotIn('database_time', self.rows()['rows'])
        self.assertTrue(all(not path.exists() for path in paths), 'Rejected save left newly created derived state')
        self.assertFalse((self.state.root / 'config-save.json').exists())

    def test_guard_unlink_failure_then_rollback_failure_still_blocks_workers(self):
        real_finish = cli.finish_config_save
        real_load = cli.load
        loads = 0
        def fail_after_unlink(state):
            real_finish(state)
            raise OSError('one-off intent fsync failure')
        def fail_rollback_load(*args, **kwargs):
            nonlocal loads
            loads += 1
            if loads == 2:
                raise BackupError('billing-settings-unreadable')
            return real_load(*args, **kwargs)
        with patch.object(cli, 'finish_config_save', side_effect=fail_after_unlink), patch.object(cli, 'load', side_effect=fail_rollback_load):
            result = cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'}})
        self.assertTrue(result['partial'])
        self.assertFalse(result['settings_restored'])
        self.assertTrue((self.state.root / 'config-save.json').exists(), 'Rollback failure unpaused workers after cleanup removed the intent')
        with self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
            config.load(self.args)

    def test_committed_credentials_with_repeated_cleanup_fsync_failure_keeps_guard(self):
        import stat
        guard = self.state.root / 'config-save.json'
        real_fsync = os.fsync
        def fail_cleanup_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not guard.exists():
                raise OSError('intent-removal directory fsync failure')
            return real_fsync(descriptor)
        with patch.object(cli.os, 'fsync', side_effect=fail_cleanup_fsync):
            result = cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
                'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
        self.assertTrue(result['updated'])
        self.assertTrue(result['outcome_unknown'])
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'replacement')
        self.assertEqual(self.credentials['access_key'], 'fake-new')
        self.assertTrue(guard.exists(), 'Cleanup failure reported uncertainty without retaining its guard')
        with self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
            config.load(self.args)

    def test_rollback_reconciles_editor_change_to_untouched_schedule_row(self):
        self.settings.update(database_schedule='daily', database_time='09:00')
        original_epoch = requests._sync_schedule(self.state, self.settings, 100)
        real_atomic = cli.atomic_json
        def edit_and_fail(path, value):
            real_atomic(path, value)
            if pathlib.Path(path).name == 'configuration-advisory.json':
                rows = self.rows()
                rows['rows']['database_time'] = {'id': 3, 'key': 'database_time', 'value': '09:45',
                    'type': 'string', 'encrypted': 0, 'updated_at': 500}
                self.rows_file.write_text(json.dumps(rows))
                raise OSError('advisory fsync failure after native editor publication')
        with patch.object(cli.time, 'time', return_value=1000), patch.object(cli, 'atomic_json', side_effect=edit_and_fail):
            with self.assertRaisesRegex(BackupError, 'config-save-failed'):
                cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'}})
        self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'old-destination')
        self.assertEqual(self.rows()['rows']['database_time']['value'], '09:45')
        epoch = read_json(self.state.root / 'schedule.json')
        self.assertEqual(epoch['kinds']['full'], original_epoch['kinds']['full'])
        self.assertEqual(epoch['kinds']['database']['since'], 1000)
        expected = requests.schedule_fingerprint(config.schedule_settings(config.load(self.args)['settings'], 'database'))
        self.assertEqual(epoch['kinds']['database']['fingerprint'], expected)

    def test_credential_timeout_before_or_after_commit_never_rolls_back_destination(self):
        import subprocess
        original_run = cli.run_native_writer
        for commit in (False, True):
            with self.subTest(commit=commit):
                def timeout(command, *args, **kwargs):
                    if '--finalize' in command:
                        if commit:
                            original_run(command, *args, **kwargs)
                        raise subprocess.TimeoutExpired(command, 30)
                    return original_run(command, *args, **kwargs)
                with patch.object(cli, 'run_native_writer', side_effect=timeout):
                    result = cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'replacement'},
                        'credentials': {'access_key': 'fake-new', 'secret_key': 'fake-new'}})
                self.assertTrue(result['outcome_unknown'])
                self.assertEqual(self.rows()['rows']['s3_bucket']['value'], 'replacement')
                self.assertEqual(self.credentials['access_key'], 'fake-new' if commit else 'fake-old')
                with self.assertRaisesRegex(BackupError, 'config-save-outcome-unknown'):
                    config.load(self.args)
                cli.set_config(self.args, self.state, {'settings': {'s3_bucket': 'old-destination'},
                    'credentials': {'access_key': 'fake-old', 'secret_key': 'fake-old'}})


class NativeWriterContainmentTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        import shutil
        self.php = shutil.which('php')
        if not self.php:
            self.fail('PHP required for writer/grandchild containment')
        self.sentinel = self.base / 'credential-committed'
        self.ready = self.base / 'writer-ready'
        self.native = self.base / 'slow-native.php'
        self.native.write_text(r'''<?php
$child = 'usleep(1800000); file_put_contents($argv[1], "late-credential-commit");';
$process = proc_open([PHP_BINARY, '-r', $child, $argv[1]],
    [0 => ['file', '/dev/null', 'r'], 1 => ['file', '/dev/null', 'w'], 2 => ['file', '/dev/null', 'w']], $pipes);
file_put_contents($argv[2], (string) posix_getpgid(0));
sleep(60);
proc_close($process);
''')
        self.command = [self.php, str(self.native), str(self.sentinel), str(self.ready)]

    def test_native_timeout_kills_delayed_credential_grandchild_before_unlock(self):
        import fcntl
        from paymenter_backups.native_writer import run
        from paymenter_backups.process import boot_id, group_members
        with self.state.lock('config.lock') as config_fd, self.state.lock('tick.lock') as tick_fd:
            result = run(self.command, b'{}', (config_fd, tick_fd), timeout=1.2)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(self.ready.exists())
            group = {'pgid': int(self.ready.read_text()), 'boot_id': boot_id()}
            self.assertEqual(group_members(group), [], 'Credential child survived its native writer timeout')
            with (self.state.root / 'config.lock').open('r+b') as observer:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        time.sleep(1.2)
        self.assertFalse(self.sentinel.exists(), 'Timed-out credential grandchild committed after Save unlocked')

    def test_python_owner_death_kills_native_and_bridge_before_coordinator_unlock(self):
        import fcntl
        import subprocess
        from paymenter_backups.process import boot_id, group_members
        root = pathlib.Path(__file__).resolve().parents[1]
        owner_script = self.base / 'owner.py'
        owner_script.write_text('import os,sys,fcntl\n'
            'sys.path.insert(0, ' + repr(str(root / 'src')) + ')\n'
            'from paymenter_backups.native_writer import run\n'
            'fd=os.open(sys.argv[1],os.O_RDWR)\nfcntl.flock(fd,fcntl.LOCK_EX)\n'
            'run(sys.argv[2:],b"{}",(fd,),timeout=20)\n')
        with self.state.lock('config.lock'):
            pass
        owner = subprocess.Popen([sys.executable, '-I', str(owner_script), str(self.state.root / 'config.lock'), *self.command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # The owned coordination descriptor is initialized before the fixture owner.
        # State's ordinary constructor creates only launcher locks.
        try:
            deadline = time.monotonic() + 2
            while not self.ready.exists() and owner.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.ready.exists(), owner.stderr.read() if owner.poll() is not None else 'Native writer did not start')
            group = {'pgid': int(self.ready.read_text()), 'boot_id': boot_id()}
            owner.kill(); owner.communicate(timeout=3)
            with (self.state.root / 'config.lock').open('r+b') as observer:
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline: self.fail('Parent-death writer did not release coordinator lock')
                        time.sleep(0.02)
                self.assertEqual(group_members(group), [], 'Coordinator unlocked before credential grandchild stopped')
            time.sleep(1.9)
            self.assertFalse(self.sentinel.exists(), 'Orphan credential grandchild committed after Python owner death')
        finally:
            if owner.poll() is None: owner.kill()
            owner.communicate()
