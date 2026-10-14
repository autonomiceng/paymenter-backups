"""Current privacy boundaries at the real CLI dispatch seam, with unit data only."""
import copy
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups.cli import dispatch, parser
from paymenter_backups.common import BackupError, State, read_json
from paymenter_backups.config import DEFAULTS


SECRET = 'isolated recovery export test secret'
PARAMETERS = {'scheme': 'pbkdf2-hmac-sha256-v1', 'salt': 'ab' * 32, 'iterations': 600000}
PARAMETERS['value'] = hashlib.pbkdf2_hmac(
    'sha256', SECRET.encode(), b'paymenter-backups-key-fingerprint-v1\0' + bytes.fromhex(PARAMETERS['salt']),
    PARAMETERS['iterations'], 32).hex()


class ExportPolicyDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.app = self.base / 'app'
        (self.app / 'public').mkdir(parents=True)
        self.state = State(self.app, self.base / 'private')
        self.state.apply_policy({'extra_docroots': []}, write=True)
        self.webroot = self.base / 'new-webroot'
        self.webroot.mkdir(mode=0o700)
        self.output = self.base / 'safe'
        self.output.mkdir(mode=0o700)
        self.envelope = {'schema_version': 1, 'app': str(self.app), 'owner_uid': os.getuid(),
                         'private_root': str(self.state.root), 'installation_uuid': self.state.uuid,
                         'settings': dict(DEFAULTS, extra_docroots=[str(self.webroot)]),
                         'tools': dict.fromkeys(('python', 'php', 'dump', 'mysql', 'tar', 'gzip'), sys.executable)}
        self.envelope['tools']['dump'] = str(self.base / 'missing-dump')
        self.record = {'secret': SECRET, 'fingerprint': PARAMETERS['value'], 'parameters': PARAMETERS,
                       'installation_uuid': self.state.uuid}
        self.calls = []

    def bridge(self, _args, verb='read', payload=None):
        self.calls.append(verb)
        if verb in ('read', 'read-settings', 'document-roots'):
            return copy.deepcopy(self.envelope)
        if verb == 'archive-key-export':
            return copy.deepcopy(self.record)
        self.fail('unexpected bridge operation: ' + verb)

    def arguments(self, *arguments):
        return parser().parse_args(['--app', str(self.app), '--private-root', str(self.state.root), *arguments])

    def test_failed_init_cannot_leave_recovery_key_export_using_cached_roots(self):
        with patch('paymenter_backups.cli.bridge', side_effect=self.bridge):
            with self.assertRaisesRegex(BackupError, 'tool-unavailable'):
                dispatch(self.arguments('init'))
            self.assertEqual(read_json(self.state.root / 'policy.json')['extra_docroots'], [])
            self.calls.clear()
            with self.assertRaisesRegex(BackupError, 'recovery-key-output-invalid'):
                dispatch(self.arguments('recovery-key', 'export', '--output', str(self.webroot / 'key.json')))
        self.assertFalse((self.webroot / 'key.json').exists())
        self.assertEqual(list(self.webroot.iterdir()), [])
        self.assertEqual(self.calls, ['document-roots'])

    def test_recovery_key_export_does_not_require_capture_readiness(self):
        self.envelope['settings']['schedule_weekdays'] = 'invalid unrelated native row'
        output = self.output / 'key.json'
        with patch('paymenter_backups.cli.bridge', side_effect=self.bridge):
            result = dispatch(self.arguments('recovery-key', 'export', '--output', str(output)))
        self.assertEqual(result, {'exported': True, 'fingerprint': PARAMETERS['value']})
        self.assertEqual(json.loads(output.read_text())['secret'], SECRET)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.calls, ['document-roots', 'archive-key-export'])

    def test_unreadable_current_roots_refuse_before_reading_or_writing_key(self):
        def unreadable(_args, verb='read', payload=None):
            self.calls.append(verb)
            if verb == 'document-roots':
                raise BackupError('document-roots-invalid')
            return copy.deepcopy(self.record)
        output = self.output / 'key.json'
        with patch('paymenter_backups.cli.bridge', side_effect=unreadable):
            with self.assertRaisesRegex(BackupError, 'document-roots-invalid'):
                dispatch(self.arguments('recovery-key', 'export', '--output', str(output)))
        self.assertEqual(self.calls, ['document-roots'])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_recovery_kit_uses_current_roots_before_creating_output(self):
        with patch('paymenter_backups.cli.bridge', side_effect=self.bridge), \
                patch('paymenter_backups.engine.verify', return_value={}), \
                patch('paymenter_backups.engine.artifacts.journal', return_value={}), \
                patch('paymenter_backups.engine.artifacts.open_archive', side_effect=AssertionError('unsafe export proceeded')):
            with self.assertRaisesRegex(BackupError, 'export-destination-unsafe'):
                dispatch(self.arguments('export', 'unit-run', '--output', str(self.webroot / 'kit')))
        self.assertFalse((self.webroot / 'kit').exists())
        self.assertEqual(self.calls, ['document-roots'])

    def test_invalid_or_missing_current_roots_never_use_cached_policy(self):
        regular = self.base / 'not-a-directory'
        regular.write_text('unit data')
        bad_roots = [None, {}, '[]', ['relative'], [42], ['/invalid\0root'],
                     [str(self.base / 'absent')], [str(regular)]]
        output = self.output / 'key.json'
        for roots in bad_roots:
            with self.subTest(roots=roots):
                self.envelope['settings']['extra_docroots'] = roots
                self.calls.clear()
                with patch('paymenter_backups.cli.bridge', side_effect=self.bridge):
                    with self.assertRaisesRegex(BackupError, 'document-roots-invalid'):
                        dispatch(self.arguments('recovery-key', 'export', '--output', str(output)))
                self.assertEqual(self.calls, ['document-roots'])
                self.assertEqual(list(self.output.iterdir()), [])
        del self.envelope['settings']['extra_docroots']
        with patch('paymenter_backups.cli.bridge', side_effect=self.bridge):
            with self.assertRaisesRegex(BackupError, 'document-roots-invalid'):
                dispatch(self.arguments('recovery-key', 'export', '--output', str(output)))

    def test_foreign_bridge_identity_refuses_before_secret_read(self):
        changes = [{'schema_version': 2}, {'app': str(self.base)}, {'owner_uid': os.getuid() + 1},
                   {'private_root': str(self.output)}, {'installation_uuid': 'foreign-installation'}]
        for change in changes:
            with self.subTest(change=change):
                original = copy.deepcopy(self.envelope)
                self.envelope.update(change)
                self.calls.clear()
                with patch('paymenter_backups.cli.bridge', side_effect=self.bridge):
                    with self.assertRaisesRegex(BackupError, 'bridge-identity-mismatch'):
                        dispatch(self.arguments('recovery-key', 'export', '--output', str(self.output / 'key.json')))
                self.assertEqual(self.calls, ['document-roots'])
                self.envelope = original
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_bridge_installation_identity_fails_closed(self):
        del self.envelope['installation_uuid']
        with patch('paymenter_backups.cli.bridge', side_effect=self.bridge):
            with self.assertRaisesRegex(BackupError, 'bridge-identity-mismatch'):
                dispatch(self.arguments('recovery-key', 'export', '--output', str(self.output / 'key.json')))
        self.assertEqual(self.calls, ['document-roots'])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_current_root_covering_private_storage_refuses_export(self):
        self.envelope['settings']['extra_docroots'] = [str(self.state.root)]
        with patch('paymenter_backups.cli.bridge', side_effect=self.bridge):
            with self.assertRaisesRegex(BackupError, 'private-root-inside-docroot'):
                dispatch(self.arguments('recovery-key', 'export', '--output', str(self.output / 'key.json')))
        self.assertEqual(self.calls, ['document-roots'])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_recovery_kit_refuses_unavailable_bridge_even_with_cached_policy(self):
        with patch('paymenter_backups.cli.bridge', side_effect=BackupError('bridge-failed')), \
                patch('paymenter_backups.cli.export') as export:
            with self.assertRaisesRegex(BackupError, 'bridge-failed'):
                dispatch(self.arguments('export', 'unit-run', '--output', str(self.output / 'kit')))
        export.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_inaccessible_or_unresolvable_current_root_fails_closed(self):
        resolve = pathlib.Path.resolve
        for failure in (PermissionError('unit refusal'), RuntimeError('unit symlink loop')):
            def checked_resolve(path, *args, **kwargs):
                if path == self.webroot:
                    raise failure
                return resolve(path, *args, **kwargs)
            with self.subTest(failure=type(failure).__name__), \
                    patch('paymenter_backups.cli.bridge', side_effect=self.bridge), \
                    patch('pathlib.Path.resolve', checked_resolve):
                with self.assertRaisesRegex(BackupError, 'document-roots-invalid'):
                    dispatch(self.arguments('recovery-key', 'export', '--output', str(self.output / 'key.json')))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_recovery_kit_privacy_read_ignores_capture_readiness(self):
        self.envelope['settings']['schedule_weekdays'] = 'invalid unrelated native row'
        def checked_export(state, run_id, output):
            self.assertIn(self.webroot, state.document_roots)
            self.assertEqual(run_id, 'unit-run')
            self.assertEqual(output, str(self.output / 'kit'))
            return {'verified': True}
        with patch('paymenter_backups.cli.bridge', side_effect=self.bridge), \
                patch('paymenter_backups.cli.export', side_effect=checked_export):
            self.assertEqual(dispatch(self.arguments('export', 'unit-run', '--output', str(self.output / 'kit'))),
                             {'verified': True})
        self.assertEqual(self.calls, ['document-roots'])

    def test_cold_bridge_policy_returns_before_billing_or_credential_access(self):
        # The native DB/Paymenter bootstrap is outside this unit-only test lane.
        source = (pathlib.Path(__file__).resolve().parents[1] / 'extension/Backups/bridge.php').read_text()
        branch_start = source.index("    if ($verb === 'document-roots') {")
        branch_end = source.index('    $billingRows =', branch_start)
        branch = source[branch_start:branch_end]
        self.assertLess(source.index('    $root = PrivateStore::root('), branch_start)
        self.assertLess(source.index('        $identity = PrivateStore::read($identityPath);'), branch_start)
        self.assertLess(branch_end, source.index('    $decodeKey ='))
        self.assertLess(branch_end, source.index("    $credentialPath ="))
        self.assertIn("'settings' => ['extra_docroots' => array_map('realpath', $settings['extra_docroots'])]", branch)
        self.assertIn('        exit;', branch)
        for secret_access in ('$credentialPath', '$encrypter', '$billingRows', "$config->get('app.key'"):
            self.assertNotIn(secret_access, branch)


if __name__ == '__main__':
    unittest.main()
