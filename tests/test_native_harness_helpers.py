"""Capture-free harness helpers with real CLI initialization/schedule transitions."""
import copy
import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

from paymenter_backups import cli, config, requests, tools
from paymenter_backups.common import State, read_json
import test_engine_schedules as fixtures

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'tools'))
import native_integration


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
