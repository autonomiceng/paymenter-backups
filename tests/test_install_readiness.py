"""Install checks use private fixture files and real launcher discovery, never host writes."""
import copy
import json
import os
import subprocess
import sys
import time
from unittest.mock import patch

from paymenter_backups import cli, commissioning, config, discovery
from paymenter_backups.common import BackupError, atomic_json
from test_engine_schedules import PHP, ROOT, PrivateFixture


class InstallReadinessTests(PrivateFixture):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.envelope['tool_checks'] = {
            name: {'ok': True, 'resolved': path, 'configured': '', 'code': None}
            for name, path in self.envelope['tools'].items() if name in ('python', 'php', 'dump', 'mysql', 'tar', 'gzip')}
        scheduler = '* * * * * ' + self.envelope['tools']['php'] + ' ' + str(self.app / 'artisan') + ' schedule:run'
        self.envelope['tools']['crontab'] = self.script('crontab', 'print(' + repr(scheduler) + ')')
        atomic_json(self.state.root / 'discovery.json', discovery.discover(self.state, self.envelope, now=self.now))
        atomic_json(self.state.root / 'scheduler.json', {'last_tick_at': self.now})
        atomic_json(self.state.root / 'incidents.json', {'schema_version': 1,
            'installation_uuid': self.state.uuid, 'checked_at': self.now, 'active': {}})

    def check(self, prepare=True):
        return commissioning.check(self.state, self.envelope, prepare=prepare, now=self.now)

    def test_prepare_repairs_local_storage_but_never_enables_schedules(self):
        self.assertFalse((self.state.root / 'backups').exists())
        before = copy.deepcopy(self.settings)
        result = self.check()
        self.assertEqual(result['checks']['local_storage']['state'], 'ready')
        self.assertEqual(result['checks']['offsite']['state'], 'pending')
        self.assertTrue(result['schedules']['database']['ready'])
        self.assertFalse(result['schedules']['full']['ready'], 'Full still needs cooperative launcher locks')
        self.assertEqual(self.settings, before)
        self.assertEqual(list((self.state.root / 'backups').iterdir()), [])
        self.assertEqual(self.check(False)['checks']['local_storage']['state'], 'ready')

    def test_scheduler_and_observer_are_independent_and_stale_receipts_fail(self):
        self.check()
        (self.state.root / 'incidents.json').unlink()
        result = self.check(False)
        self.assertEqual(result['checks']['observer']['code'], 'observer-not-run')
        self.assertFalse(result['schedules']['database']['ready'])
        atomic_json(self.state.root / 'incidents.json', {'schema_version': 1,
            'installation_uuid': self.state.uuid, 'checked_at': self.now - 901, 'active': {}})
        self.assertEqual(self.check(False)['checks']['observer']['code'], 'observer-stale')
        atomic_json(self.state.root / 'incidents.json', [])
        self.assertEqual(self.check(False)['checks']['observer']['code'], 'observer-stale')
        atomic_json(self.state.root / 'scheduler.json', {'last_tick_at': self.now - 301})
        self.assertEqual(self.check(False)['checks']['scheduler']['code'], 'scheduler-not-running')
        self.envelope['tools']['crontab'] = self.script('crontab', "print('# no scheduler')")
        self.assertEqual(self.check()['checks']['scheduler']['code'], 'scheduler-missing')

    def test_unsafe_local_path_tools_and_reserve_fail_without_capture(self):
        self.settings['local_path'] = str(self.app / 'public' / 'backups')
        self.assertEqual(self.check()['checks']['local_storage']['code'], 'destination-unsafe')
        self.assertFalse((self.app / 'public' / 'backups').exists())
        self.settings['local_path'] = ''
        self.settings['minimum_free_bytes'] = 2**63
        self.assertEqual(self.check()['checks']['local_storage']['code'], 'disk-reserve-exceeded')
        self.envelope['tool_checks']['dump'].update(ok=False, code='tool-unavailable')
        self.assertEqual(self.check()['checks']['tools']['code'], 'tool-unavailable')

    def test_offsite_pending_blocks_only_when_selected_and_test_must_match(self):
        self.assertTrue(self.check()['schedules']['database']['ready'])
        self.settings.update(upload_s3=True, s3_bucket='fixture', s3_encryption=False,
                             plaintext_offsite_acknowledged=True)
        config.normalize(self.settings)
        self.envelope['credentials'] = {'access_key': 'fake-key', 'secret_key': 'fake-secret'}
        self.envelope['framework']['app_key'] = 'fixture-private-app-key'
        self.assertFalse(self.check()['schedules']['database']['ready'])
        from paymenter_backups import requests, storage
        item = requests.create(self.state, 'destination-test')
        item.update(status='complete', result=storage.destination_test(self.state, self.envelope))
        atomic_json(requests.path(self.state, item['request_id']), item)
        self.assertEqual(self.check()['checks']['offsite']['state'], 'ready')
        self.settings['s3_bucket'] = 'changed'
        self.assertEqual(self.check()['checks']['offsite']['state'], 'pending')

    def test_schedule_gate_runs_before_native_publication_and_off_remains_savable(self):
        from types import SimpleNamespace
        args = SimpleNamespace(php=self.envelope['tools']['php'])
        original = copy.deepcopy(self.envelope)
        (self.state.root / 'incidents.json').unlink()
        with patch.object(cli, 'bridge', return_value=original), patch.object(cli, 'validate'), \
                patch.object(cli, 'native_settings') as native:
            with self.assertRaises(BackupError) as raised:
                cli.set_config(args, self.state, {'settings': {'database_schedule': 'hourly'}})
            self.assertEqual(raised.exception.code, 'observer-not-run')
            native.assert_not_called()
            self.assertFalse((self.state.root / 'config-save.json').exists())
        self.assertEqual(commissioning.chosen_schedules(self.settings, dict(self.settings, database_schedule='off'),
            {'database_schedule': 'off'}), [])

    def test_database_readiness_checks_launcher_lock_safety_without_taking_locks(self):
        lock = self.base / 'launcher.lock'
        line = f'* * * * * /usr/bin/flock {lock} {self.envelope["tools"]["php"]} {self.app}/artisan schedule:run'
        self.envelope['tools']['crontab'] = self.script('crontab', 'print(' + repr(line) + ')')
        with patch('fcntl.flock', side_effect=AssertionError('readiness took a lock')):
            result = self.check()['schedules']['database']
            self.assertFalse(result['ready'])
            self.assertIn('job-lock-unsafe', result['blockers'])
            lock.write_bytes(b'')
            lock.chmod(0o644)
            self.assertIn('job-lock-unsafe', self.check()['schedules']['database']['blockers'])
            lock.chmod(0o600)
            self.assertTrue(self.check()['schedules']['database']['ready'])
            unlocked = f'* * * * * {self.envelope["tools"]["php"]} {self.app}/artisan schedule:run'
            self.envelope['tools']['crontab'] = self.script('crontab', 'print(' + repr(unlocked) + ')')
            self.assertTrue(self.check()['schedules']['database']['ready'])

    def test_legacy_enabled_mode_switch_refuses_before_native_publication(self):
        from types import SimpleNamespace
        self.settings.update(settings_version=1, enabled=True, mode='database-online')
        original = copy.deepcopy(self.envelope)
        (self.state.root / 'incidents.json').unlink()
        with patch.object(cli, 'bridge', return_value=original), patch.object(cli, 'validate'), \
                patch.object(cli, 'native_settings') as native:
            with self.assertRaisesRegex(BackupError, 'observer-not-run'):
                cli.set_config(SimpleNamespace(php=self.envelope['tools']['php']), self.state,
                               {'settings': {'mode': 'full-quiesced'}})
            native.assert_not_called()
        self.assertEqual(original['settings']['mode'], 'database-online')
        self.assertEqual(commissioning.chosen_schedules(original['settings'],
            dict(original['settings'], enabled=False, mode='full-quiesced'), {'enabled': False, 'mode': 'full-quiesced'}), [])

    def test_removed_legacy_launcher_lock_is_a_readiness_blocker(self):
        lock = self.state.root / 'scheduler.lock'
        self.settings.update(settings_version=1, job_lock_paths=[str(lock)])
        line = f'* * * * * /usr/bin/flock {lock} {self.envelope["tools"]["php"]} {self.app}/artisan schedule:run'
        self.envelope['tools']['crontab'] = self.script('crontab', 'print(' + repr(line) + ')')
        lock.unlink()
        result = self.check()['schedules']['database']
        self.assertFalse(result['ready'])
        self.assertIn('job-locks-unverified', result['blockers'])

    def test_real_cli_prepares_private_state_and_refuses_unready_schedule_without_native_write(self):
        envelope = copy.deepcopy(self.envelope)
        envelope.pop('tool_checks')
        envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(self.state.root))
        envelope['tools']['php'] = PHP
        for name, output in (('dump', 'mariadb-dump 11.4'), ('mysql', 'MariaDB 11.4'),
                             ('tar', 'tar (GNU tar) 1.35'), ('gzip', 'gzip 1.13 Free Software Foundation')):
            envelope['tools'][name] = self.script(name, 'print(' + repr(output) + ')')
        payload = self.base / 'settings.json'
        payload.write_text(json.dumps(envelope))
        bridge = self.base / 'bridge.php'
        bridge.write_text('<?php echo file_get_contents(' + json.dumps(str(payload)) + ');')
        command = [sys.executable, '-I', str(ROOT / 'bin/paymenter-backups'), '--app', str(self.app),
                   '--private-root', str(self.state.root), '--php', PHP, '--bridge', str(bridge)]
        fresh = self.base / 'fresh-private'
        envelope['private_root'] = str(fresh)
        payload.write_text(json.dumps(envelope))
        fresh_command = list(command)
        fresh_command[fresh_command.index('--private-root') + 1] = str(fresh)
        result = subprocess.run(fresh_command + ['readiness'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checks']['private_state']['code'], 'installation-not-initialized')
        self.assertFalse(fresh.exists())
        result = subprocess.run(fresh_command + ['readiness', '--prepare'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['checks']['local_storage']['state'], 'ready')
        self.assertTrue((fresh / 'installation.json').is_file())
        self.assertFalse((fresh / 'schedule.json').exists())
        envelope['private_root'] = str(self.state.root)
        payload.write_text(json.dumps(envelope))
        (self.state.root / 'incidents.json').unlink()
        result = subprocess.run(command + ['readiness', '--prepare'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['checks']['local_storage']['state'], 'ready')
        self.assertEqual(report['checks']['observer']['code'], 'observer-not-run')
        self.assertNotIn(str(self.app), result.stdout)
        result = subprocess.run(command + ['schedule', 'enable', '--kind', 'database'],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stderr)['error'], 'observer-not-run')
        self.assertFalse((self.state.root / 'schedule.json').exists())

    def test_real_native_readiness_command_kind_exit_and_safe_next_steps(self):
        script = r'''
namespace Illuminate\Console {
    class Command {
        const SUCCESS = 0; const FAILURE = 1;
        public function option($name) { return $GLOBALS['input'][$name] ?? null; }
        public function line($line) { $GLOBALS['output'] = json_decode($line, true); }
        public function error($line) { $GLOBALS['error'] = $line; }
    }
}
namespace App\Models {
    class Extension {
        public static function where(...$arguments) { return new self; }
        public function firstOrFail() { return $this; }
    }
}
namespace Paymenter\Extensions\Others\Backups {
    class Runtime {
        public static function saveConfiguration($settings) { throw new \RuntimeException('observer-not-run'); }
        public static function command($arguments, $input = null, $timeout = 10) {
            $GLOBALS['arguments'] = $arguments;
            if ($GLOBALS['input']['fail'] ?? false) { throw new \RuntimeException('private fake-secret-canary'); }
            return $GLOBALS['input']['report'];
        }
    }
}
namespace {
    require $argv[1] . '/extension/Backups/Messages.php';
    require $argv[1] . '/extension/Backups/ReadinessCommand.php';
    if (($argv[2] ?? null) === 'config') {
        require $argv[1] . '/extension/Backups/Settings.php';
        require $argv[1] . '/extension/Backups/ConfigSetCommand.php';
        $GLOBALS['input'] = [];
        $status = (new \Paymenter\Extensions\Others\Backups\ConfigSetCommand)->handle();
    } else {
        $GLOBALS['input'] = json_decode(stream_get_contents(STDIN), true);
        $status = (new \Paymenter\Extensions\Others\Backups\ReadinessCommand)->handle();
    }
    echo json_encode(['status' => $status, 'output' => $GLOBALS['output'] ?? null,
        'error' => $GLOBALS['error'] ?? null, 'arguments' => $GLOBALS['arguments'] ?? null]);
}
'''
        report = self.check()
        for kind, expected in (('database', 0), ('full', 1)):
            result = subprocess.run([PHP, '-r', script, str(ROOT)], input=json.dumps({
                'kind': kind, 'prepare': True, 'report': report}), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output['status'], expected)
            self.assertEqual(output['arguments'], ['readiness', '--prepare'])
            self.assertTrue(output['output']['schedules']['full']['next_steps'])
        result = subprocess.run([PHP, '-r', script, str(ROOT)], input=json.dumps({'fail': True}),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['status'], 1)
        self.assertFalse(output['output']['ready'])
        self.assertNotIn('fake-secret', result.stdout)
        result = subprocess.run([PHP, '-r', script, str(ROOT), 'config'], input='{}',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output['status'], 1)
        self.assertIn('host cron', output['error'])
        self.assertNotIn('observer-not-run', output['error'])
