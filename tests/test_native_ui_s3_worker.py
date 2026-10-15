"""Direct loopback worker entrypoint refuses capture bypasses before startup."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups import cli, storage
from paymenter_backups.common import State, atomic_json

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'tools'))
import native_ui_fixture
import native_ui_s3_worker


class LoopbackWorkerGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = pathlib.Path(temporary.name)
        app = self.work / 'app'
        app.mkdir(mode=0o700)
        self.state = State(app, self.work / 'private')
        self.metadata = {'app': str(app), 'private_root': str(self.state.root),
                         'php': '/fixture/php', 'fixture_only': True, 'native_scheduler_started': False}
        self.request_id = 'fixture-request'
        self.target = self.state.root / 'requests' / (self.request_id + '.json')
        self.argv = [str(value) for value in native_ui_fixture.command_prefix(self.metadata)[4:]]
        self.argv += ['request-worker', self.request_id, '--execute']
        saved_paths = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), saved_paths))
        self.metadata_mock = self.mock(native_ui_fixture, 'controlled_metadata', return_value=(self.work, self.metadata))
        self.server = self.mock(native_ui_s3_worker, 'ThreadingHTTPServer')
        self.thread = self.mock(native_ui_s3_worker.threading, 'Thread')
        self.engine = self.mock(cli, 'main', return_value=17)

    def mock(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def request(self, *, action='run', arguments=None, status='launching', **changes):
        item = {'request_id': self.request_id, 'installation_uuid': self.state.uuid,
                'action': action, 'status': status, 'attempts': 1}
        if arguments is not None:
            item['arguments'] = arguments
        item.update(changes)
        atomic_json(self.target, item)
        return self.target.read_bytes()

    def run_helper(self, argv=None):
        with patch.object(sys, 'argv', ['native_ui_s3_worker.py'] + (self.argv if argv is None else argv)):
            return native_ui_s3_worker.main()

    def assert_refused(self, argv=None):
        before = self.target.read_bytes() if self.target.exists() else None
        with self.assertRaises(RuntimeError):
            self.run_helper(argv)
        self.server.assert_not_called()
        self.thread.assert_not_called()
        self.engine.assert_not_called()
        self.assertFalse((self.work / 'loopback-s3').exists())
        if before is not None:
            self.assertEqual(self.target.read_bytes(), before)

    def test_direct_full_missing_and_unknown_modes_refuse_before_server_or_engine(self):
        for arguments in ({'mode': 'full-quiesced'}, {}, None, {'mode': 'unexpected'}, ['database-online']):
            with self.subTest(arguments=arguments):
                self.request(arguments=arguments)
                self.assert_refused()

    def test_arbitrary_cli_commands_and_extra_flags_are_refused(self):
        self.request(arguments={'mode': 'database-online'})
        for suffix in (['run', '--execute'], ['config', 'set'],
                       ['request-worker', self.request_id],
                       ['request-worker', self.request_id, '--execute', '--allow-full'],
                       ['request-worker', self.request_id, '--execute', '--app', self.metadata['app']]):
            with self.subTest(suffix=suffix):
                self.assert_refused(self.argv[:8] + suffix)
        self.metadata_mock.assert_not_called()

    def test_every_native_identity_argument_must_match_fixture_metadata_exactly(self):
        self.request(arguments={'mode': 'database-online'})
        for index in (1, 3, 5, 7):
            with self.subTest(argument=self.argv[index - 1]):
                changed = list(self.argv)
                changed[index] = '/unexpected-fixture-value'
                self.assert_refused(changed)

    def test_owned_launching_request_and_known_action_are_required(self):
        for changes in ({'installation_uuid': 'foreign'}, {'request_id': 'different'},
                        {'status': 'pending'}, {'status': 'deferred'}, {'status': 'running'},
                        {'action': 'unknown'}):
            with self.subTest(changes=changes):
                self.request(arguments={'mode': 'database-online'}, **changes)
                self.assert_refused()

    def test_exact_database_and_non_capture_requests_reach_only_mocked_engine(self):
        original_client = storage.client
        for action in ('run', 'verify', 'deliver', 'retrieve', 'retention', 'destination-test', 'delete'):
            with self.subTest(action=action):
                original = self.request(action=action, arguments={'mode': 'database-online'} if action == 'run' else {})
                self.assertEqual(self.run_helper(), 17)
                self.engine.assert_called_with(self.argv)
                self.assertEqual(self.target.read_bytes(), original)
                self.assertIs(storage.client, original_client)
        self.assertEqual(self.server.call_count, 7)
        self.assertEqual(self.engine.call_count, 7)
        self.server.return_value.shutdown.assert_called()
        self.thread.return_value.join.assert_called()

    def test_object_directory_symlink_or_public_permissions_refuse_before_server(self):
        self.request(arguments={'mode': 'database-online'})
        objects = self.work / 'loopback-s3'
        outside = self.work / 'outside'
        outside.mkdir(mode=0o700)
        marker = outside / 'untouched'
        marker.write_bytes(b'keep')
        objects.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'canonical and owner-private'):
            self.run_helper()
        self.assertEqual(marker.read_bytes(), b'keep')
        objects.unlink()
        objects.mkdir(mode=0o700)
        objects.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, 'canonical and owner-private'):
            self.run_helper()
        self.server.assert_not_called()
        self.engine.assert_not_called()

    def test_existing_private_object_directory_keeps_persistent_objects(self):
        self.request(action='verify', arguments={})
        objects = self.work / 'loopback-s3'
        objects.mkdir(mode=0o700)
        marker = objects / 'fixture-owned-object'
        marker.write_bytes(b'persistent fixture bytes')
        self.assertEqual(self.run_helper(), 17)
        self.assertEqual(marker.read_bytes(), b'persistent fixture bytes')
