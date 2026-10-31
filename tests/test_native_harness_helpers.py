"""Capture-free harness helpers with real CLI initialization/schedule transitions."""
import copy
import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

from paymenter_backups import cli, config, requests, tools
from paymenter_backups.common import BackupError, State, atomic_json, read_json
import test_engine_schedules as fixtures

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'tools'))
import native_integration


class GuardedWriteChecks(fixtures.PrivateFixture):
    def setUp(self):
        super().setUp()
        self.now = fixtures.instant('2026-10-15T12:00:00+00:00')
        self.envelope.update(schema_version=1, owner_uid=self.app.stat().st_uid,
                             private_root=str(self.state.root))
        lock = self.state.root / 'scheduler.lock'
        lock.touch(mode=0o600)
        scheduler = '* * * * * flock -n ' + str(lock) + ' ' + self.envelope['tools']['php'] + ' ' + str(self.app / 'artisan') + ' schedule:run'
        self.envelope['tools']['crontab'] = self.script('crontab', 'print(' + repr(scheduler) + ')')
        self.commands = []
        self.guard_failure = False
        self.cli = [sys.executable, '--app', str(self.app), '--private-root', str(self.state.root),
                    '--php', '/usr/bin/php']
        self.refresh_guard()
        self.guard_generated_at = self.now
        self.now += 181

    def refresh_guard(self):
        midnight = int(self.now) // 86400 * 86400
        atomic_json(self.state.root / 'billing-guard.json', {
            'fingerprint': config.billing_fingerprint(self.envelope['billing']),
            'generated_at': self.now, 'instants': [midnight + day * 86400 for day in range(9)],
        })

    def execute(self, command, input=None, **kwargs):
        self.commands.append((command, input, kwargs))
        if command == ['/usr/bin/php', 'artisan', 'backups:guard']:
            self.assertEqual(kwargs, {'cwd': str(self.app)})
            if self.guard_failure:
                raise RuntimeError('fixture guard refresh failed')
            self.refresh_guard()
            return b'{}'
        atomic_json(self.state.root / 'scheduler.json', {'last_tick_at': self.now})
        atomic_json(self.state.root / 'incidents.json', {'schema_version': 1,
            'installation_uuid': self.state.uuid, 'checked_at': self.now, 'active': {}})
        args = cli.parser().parse_args([str(value) for value in command[command.index('--app'):]])

        def native_write(command, input_bytes, descriptors):
            self.settings.update(json.loads(input_bytes))
            config.normalize(self.settings)
            output = json.dumps({'ok': True, 'receipt': {}, 'snapshot': copy.deepcopy(self.settings)}).encode()
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b'')

        with patch.object(cli, 'bridge', side_effect=lambda *a, **k: copy.deepcopy(self.envelope)), \
             patch.object(config, 'bridge', side_effect=lambda *a, **k: copy.deepcopy(self.envelope)), \
             patch.object(cli, 'stdin_json', side_effect=lambda: json.loads(input or b'{}')), \
             patch.object(cli, 'run_native_writer', side_effect=native_write), \
             patch.object(tools, 'checks', side_effect=fixtures.ConfigurationTests.tool_reports):
            return json.dumps(cli.dispatch(args)).encode()

    def write(self, arguments, **kwargs):
        with patch.object(native_integration, 'execute', side_effect=self.execute), \
             patch.object(native_integration.time, 'time', side_effect=lambda: self.now):
            return native_integration.guarded_write(self.cli, arguments, **kwargs)

    def test_schedule_writes_do_not_require_a_fresh_guard(self):
        for kind in ('full', 'database'):
            for verb in ('set', 'enable', 'disable'):
                with self.subTest(kind=kind, verb=verb):
                    self.now += 181
                    payload = json.dumps({'frequency': 'weekly', 'time': '02:30', 'weekdays': [0, 2],
                                          'timezone': 'UTC'}).encode() if verb == 'set' else None
                    self.write(['schedule', verb, '--kind', kind], input=payload)
                    self.assertEqual(read_json(self.state.root / 'billing-guard.json')['generated_at'], self.guard_generated_at)
                    self.assertEqual(self.commands[-1][1], payload)
        self.assertEqual(self.settings['full_schedule'], 'off')
        self.assertEqual(self.settings['database_schedule'], 'off')

    def test_config_write_with_enabled_full_schedule_ignores_expired_guard(self):
        self.settings.update(full_schedule='weekly', full_time='02:30', full_weekdays=[0, 2])
        config.normalize(self.settings)
        payload = json.dumps({'settings': {'retention_keep_full': 5}}).encode()
        self.write(['config', 'set'], input=payload, timeout=42)
        self.assertEqual(self.settings['retention_keep_full'], 5)
        self.assertEqual(self.commands[-1][2]['timeout'], 42)
        self.assertEqual(read_json(self.state.root / 'billing-guard.json')['generated_at'], self.guard_generated_at)

    def test_guard_refresh_failure_does_not_block_configuration_write(self):
        self.guard_failure = True
        self.write(['config', 'set'], input=json.dumps({'settings': {'retention_keep_full': 5}}).encode())
        self.assertEqual(self.settings['retention_keep_full'], 5)
        self.assertEqual(len(self.commands), 1)

    def test_schedule_write_succeeds_with_expired_guard_without_fixture_refresh(self):
        with patch.object(native_integration.time, 'time', side_effect=lambda: self.now):
            self.execute(self.cli + ['schedule', 'enable', '--kind', 'full'])
        self.assertEqual(self.settings['full_schedule'], 'daily')
        self.assertEqual(read_json(self.state.root / 'billing-guard.json')['generated_at'], self.guard_generated_at)


class LegacyConfigurationChecks(fixtures.PrivateFixture):
    def setUp(self):
        super().setUp()
        self.envelope.update(schema_version=1, owner_uid=self.app.stat().st_uid,
                             private_root=str(self.state.root))
        (self.app / 'vendor').mkdir()
        (self.app / 'bootstrap/cache').mkdir(parents=True)
        (self.app / '.env').write_text('DB_URL=mysql://synthetic@localhost/paymenter_fixture?unix_socket=/fixture.sock\n')
        requests.sync_schedule(self.state, self.settings, now=100)
        self.v2_schedule = (self.state.root / 'schedule.json').read_bytes()
        self.v2_settings = copy.deepcopy(self.settings)
        self.records = {str(self.app): self.envelope}
        self.commands = []
        self.broken = False

    def execute(self, command, input=None, cwd=None, **kwargs):
        self.commands.append([str(part) for part in command])
        if '--app' not in command:
            # Only fixture-native setup/writes are stubbed. Every CLI command
            # below exercises dispatch, State, config validation and schedule IO.
            if 'legacy-install' in command:
                legacy_app = pathlib.Path(command[2])
                self.assertNotEqual(legacy_app, self.app)
                envelope = copy.deepcopy(self.envelope)
                envelope['app'] = str(legacy_app)
                envelope['settings'].update(settings_version=1, private_root=str(command[4]), python=str(command[5]), enabled=False)
                config.normalize(envelope['settings'])
                envelope['private_root'] = envelope['settings']['private_root']
                self.records[str(legacy_app)] = envelope
            return b'{}'
        arguments = [str(value) for value in command[command.index('--app'):]]
        args = cli.parser().parse_args(arguments)
        envelope = self.records[args.app]

        def native_write(command, input_bytes, descriptors):
            envelope['settings'].update(json.loads(input_bytes))
            config.normalize(envelope['settings'])
            output = json.dumps({'ok': True, 'receipt': {}, 'snapshot': copy.deepcopy(envelope['settings'])}).encode()
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr=b'')

        with patch.object(cli, 'bridge', return_value=copy.deepcopy(envelope)), \
             patch.object(config, 'bridge', side_effect=lambda *a, **k: copy.deepcopy(envelope)), \
             patch.object(cli, 'stdin_json', side_effect=lambda: json.loads(input or b'{}')), \
             patch.object(cli, 'run_native_writer', side_effect=native_write), \
             patch.object(tools, 'checks', side_effect=fixtures.ConfigurationTests.tool_reports), \
             patch.object(cli, 'capture', side_effect=AssertionError('legacy helper must not capture')):
            result = cli.dispatch(args)
        if self.broken and args.command == 'schedule' and args.verb == 'show':
            result['schedule_time'] = '04:00'
        return json.dumps(result).encode()

    def installed_cli(self, app, private):
        return [sys.executable, '--app', str(app), '--private-root', str(private), '--php', '/usr/bin/php']

    def assert_v2_preserved(self):
        self.assertEqual(self.settings, self.v2_settings)
        self.assertEqual((self.state.root / 'schedule.json').read_bytes(), self.v2_schedule)
        self.assertEqual(read_json(self.state.root / 'schedule.json')['schema_version'], 2)

    def fixture(self):
        with patch.object(native_integration, 'execute', side_effect=self.execute):
            return native_integration.legacy_configuration_fixture(self.base, self.app, '/usr/bin/php',
                                                                  '/fixture/checker.php')

    def test_v1_probe_refuses_the_existing_v2_identity_before_any_write(self):
        with patch.object(native_integration, 'execute', side_effect=self.execute):
            with self.assertRaisesRegex(RuntimeError, 'isolated v1 installation'):
                native_integration.legacy_configuration_check(self.installed_cli(self.app, self.state.root))
        self.assertEqual(len(self.commands), 1)
        self.assert_v2_preserved()

    def test_disposable_legacy_identity_exercises_real_v1_schedule_state(self):
        receipt = self.fixture()
        self.assertTrue(receipt['singleton_schedule_writable'])
        self.assertTrue(receipt['total_retention_readable'])
        legacy_root = self.base / 'legacy-private'
        legacy = State(self.base / 'legacy-app', legacy_root, initialize=False)
        self.assertNotEqual(legacy.uuid, self.state.uuid)
        self.assertEqual(read_json(legacy_root / 'schedule.json')['schema_version'], 1)
        self.assertEqual(self.records[str(self.base / 'legacy-app')]['settings']['settings_version'], 1)
        self.assert_v2_preserved()
        self.assertFalse(any('run' in command for command in self.commands))

    def test_unknown_database_binding_refuses_before_native_operations(self):
        (self.app / '.env').write_text('DB_DATABASE=unexpected_fixture\n')
        with self.assertRaisesRegex(RuntimeError, 'legacy fixture database binding unavailable'):
            self.fixture()
        self.assertEqual(self.commands, [])
        self.assert_v2_preserved()

    def test_legacy_refusal_preserves_main_v2_rows_and_schedule(self):
        self.broken = True
        with self.assertRaisesRegex(RuntimeError, 'legacy v1 CLI configuration compatibility changed'):
            self.fixture()
        self.assert_v2_preserved()
