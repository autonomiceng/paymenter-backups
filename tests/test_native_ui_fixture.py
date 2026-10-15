"""Fixture controls exercise private state; capture execution is always mocked."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups.common import BackupError, State, atomic_json

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'tools'))
import native_ui_fixture


class NativeUiFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = pathlib.Path(self.temporary.name)
        self.app = self.work / 'app'
        self.app.mkdir(mode=0o700)
        self.state = State(self.app, self.work / 'private')
        self.metadata = {'app': str(self.app), 'private_root': str(self.state.root), 'php': '/fixture/php'}
        self.request_id = 'fixture-request'
        self.target = self.state.root / 'requests' / (self.request_id + '.json')
        execution_patch = patch.object(native_ui_fixture, 'execute', return_value=b'{"mocked_worker":true}')
        self.execution = execution_patch.start()
        self.addCleanup(execution_patch.stop)

    def request(self, *, status='pending', action='run', arguments=None):
        item = {'request_id': self.request_id, 'installation_uuid': self.state.uuid,
                'status': status, 'attempts': 2, 'action': action}
        if arguments is not None:
            item['arguments'] = arguments
        atomic_json(self.target, item)
        return self.target.read_bytes()

    def test_refusal_preserves_pending_and_deferred_requests_without_engine_execution(self):
        for status in ('pending', 'deferred'):
            for arguments in ({'mode': 'full-quiesced'}, {}, None,
                              {'mode': 'unexpected'}, {'mode': None}, ['database-online']):
                with self.subTest(status=status, arguments=arguments):
                    original = self.request(status=status, arguments=arguments)
                    with self.assertRaisesRegex(RuntimeError, 'refuses non-database capture without --allow-full'):
                        native_ui_fixture.run_worker(self.work, self.metadata, self.request_id)
                    self.assertEqual(self.target.read_bytes(), original)
                    self.execution.assert_not_called()

    def test_database_requests_launch_once_after_promotion(self):
        for status in ('pending', 'deferred'):
            with self.subTest(status=status):
                self.request(status=status, arguments={'mode': 'database-online'})

                def worker(command, **kwargs):
                    promoted = json.loads(self.target.read_text())
                    self.assertEqual(promoted['status'], 'launching')
                    self.assertEqual(promoted['attempts'], 3)
                    self.assertEqual(command[-3:], ['request-worker', self.request_id, '--execute'])
                    return b'{"mocked_worker":true}'

                self.execution.side_effect = worker
                result = native_ui_fixture.run_worker(self.work, self.metadata, self.request_id)
                self.assertEqual(result, {'mocked_worker': True})
        self.assertEqual(self.execution.call_count, 2)

    def test_non_capture_actions_remain_available(self):
        for action in ('verify', 'deliver', 'retrieve', 'retention', 'destination-test', 'delete'):
            with self.subTest(action=action):
                self.request(action=action, arguments={'run_id': 'fixture-run'})
                native_ui_fixture.run_worker(self.work, self.metadata, self.request_id)
                self.assertEqual(json.loads(self.target.read_text())['status'], 'launching')
        self.assertEqual(self.execution.call_count, 6)

    def test_explicit_opt_in_allows_full_and_unspecified_modes_using_mock_worker_only(self):
        for arguments in ({'mode': 'full-quiesced'}, {}):
            with self.subTest(arguments=arguments):
                self.request(arguments=arguments)
                native_ui_fixture.run_worker(self.work, self.metadata, self.request_id, allow_full=True)
                self.assertEqual(json.loads(self.target.read_text())['status'], 'launching')
        self.assertEqual(self.execution.call_count, 2)

    def test_worker_cli_defaults_to_refusal_and_wires_explicit_opt_in(self):
        with patch.object(native_ui_fixture, 'controlled_metadata', return_value=(self.work, self.metadata)):
            original = self.request(arguments={'mode': 'full-quiesced'})
            command = ['native_ui_fixture.py', 'worker', '--work', str(self.work), '--request-id', self.request_id]
            with patch.object(sys, 'argv', command):
                with self.assertRaisesRegex(RuntimeError, 'refuses non-database capture'):
                    native_ui_fixture.main()
            self.assertEqual(self.target.read_bytes(), original)
            self.execution.assert_not_called()
            with patch.object(sys, 'argv', command + ['--allow-full']), contextlib.redirect_stdout(io.StringIO()):
                native_ui_fixture.main()
            self.execution.assert_called_once()

    def artifact(self):
        run_id = 'fixture-run'
        directory = self.state.default_destination()
        directory.mkdir(mode=0o700, exist_ok=True)
        archive = directory / (run_id + '.tar.gz')
        archive.write_bytes(b'fixture bytes for corruption')
        archive.chmod(0o600)
        self.state.run_path(run_id).mkdir(mode=0o700, exist_ok=True)
        atomic_json(self.state.run_path(run_id) / 'journal.json', {
            'run_id': run_id, 'installation_uuid': self.state.uuid,
            'archive_path': str(archive), 'manifest_path': str(directory / (run_id + '.manifest.json')),
        })
        return run_id, archive

    def test_corruption_uses_v04_backups_and_legacy_binding_without_capture(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                binding = dict(self.state.installation)
                if legacy:
                    binding.pop('default_local_directory')
                atomic_json(self.state.binding, binding)
                self.state = State(self.app, self.state.root, initialize=False)
                run_id, archive = self.artifact()
                self.assertEqual(archive.parent.name, 'artifacts' if legacy else 'backups')
                before = archive.read_bytes()
                receipt = native_ui_fixture.corrupt_artifact(self.metadata, run_id)
                after = archive.read_bytes()
                self.assertEqual(receipt, {'corrupted_fixture_artifact': run_id})
                changed = [index for index, (left, right) in enumerate(zip(before, after)) if left != right]
                self.assertEqual(changed, [len(before) // 2])
                self.assertEqual(before[changed[0]] ^ after[changed[0]], 1)
        self.execution.assert_not_called()

    def test_corruption_refuses_foreign_journal_and_keeps_archive(self):
        run_id, archive = self.artifact()
        before = archive.read_bytes()
        journal_path = self.state.run_path(run_id) / 'journal.json'
        item = json.loads(journal_path.read_text())
        item['installation_uuid'] = 'foreign-fixture'
        atomic_json(journal_path, item)
        with self.assertRaisesRegex(BackupError, 'artifact-identity-mismatch'):
            native_ui_fixture.corrupt_artifact(self.metadata, run_id)
        self.assertEqual(archive.read_bytes(), before)

    def test_corruption_refuses_journal_outside_fixture_default_directory(self):
        run_id, archive = self.artifact()
        outside = self.work / 'other-private-directory'
        outside.mkdir(mode=0o700)
        other = outside / archive.name
        other.write_bytes(archive.read_bytes())
        other.chmod(0o600)
        before = other.read_bytes()
        journal_path = self.state.run_path(run_id) / 'journal.json'
        item = json.loads(journal_path.read_text())
        item.update(archive_path=str(other), manifest_path=str(outside / (run_id + '.manifest.json')))
        atomic_json(journal_path, item)
        with self.assertRaisesRegex(RuntimeError, 'outside disposable default backup directory'):
            native_ui_fixture.corrupt_artifact(self.metadata, run_id)
        self.assertEqual(other.read_bytes(), before)

    def test_corruption_refuses_symlink_and_multiple_links(self):
        run_id, archive = self.artifact()
        other = self.work / 'untouched'
        archive.rename(other)
        before = other.read_bytes()
        archive.symlink_to(other)
        with self.assertRaises(OSError):
            native_ui_fixture.corrupt_artifact(self.metadata, run_id)
        self.assertEqual(other.read_bytes(), before)
        archive.unlink()
        os.link(other, archive)
        with self.assertRaisesRegex(RuntimeError, 'owner-private regular file'):
            native_ui_fixture.corrupt_artifact(self.metadata, run_id)
        self.assertEqual(other.read_bytes(), before)
