"""Merged engine/encryption bridge contracts using private synthetic credentials."""
import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups import config, key_cli, tools
from paymenter_backups.common import BackupError, State
import test_archive_keys as archive_keys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')


@unittest.skipUnless(PHP, 'PHP required for bridge transactions')
class MergedBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='backups-merged-bridge-')
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.private = self.base / 'private'
        self.private.mkdir(mode=0o700)
        self.runner = self.base / 'transaction.php'
        bridge = (ROOT / 'extension/Backups/bridge.php').read_text()
        # Include privacy and billing dispatch, not just credential transactions:
        # export must exercise the PHP branch that once required cronjob_time.
        transaction = bridge[bridge.index("    if ($verb === 'document-roots') {"):bridge.index('} catch (Throwable $error) {\n    $safe =')]
        harness = archive_keys.HARNESS.replace(
            "require $argv[1] . '/extension/Backups/ArchiveEncryption.php';",
            "require $argv[1] . '/extension/Backups/ArchiveEncryption.php';\n"
            "require $argv[1] . '/extension/Backups/CredentialInput.php';\n"
            "require $argv[1] . '/extension/Backups/FrameworkStores.php';")
        harness = harness.replace('public function __construct(private string $key, string $cipher) {}',
                                  "public function __construct(private string $key, string $cipher) {\n"
                                  "if (getenv('FIXTURE_REFUSE_ENCRYPTER')) { throw new RuntimeException('encrypter-constructed'); }\n}")
        prelude = (
            "$base = $identity['app'] ?? $argv[1];\n"
            "$db = ['driver' => 'mysql', 'database' => 'fixture', 'username' => 'fixture', 'password' => 'fake-db-secret'];\n"
            "$settings = json_decode(" + json.dumps(json.dumps(config.DEFAULTS)) + ", true);\n"
            "$extension = ['enabled' => true];\n"
            "$prefix = '';\n"
            "$pdo = new class {\n"
            "    public function query(string $query): object {\n"
            "        return new class {\n"
            "            public function fetchAll(int $mode): array {\n"
            "                $rows = ['app_url' => 'https://example.test', 'timezone' => 'UTC'];\n"
            "                if (!getenv('FIXTURE_MISSING_BILLING')) { $rows['cronjob_time'] = '02:00'; }\n"
            "                return $rows;\n"
            "            }\n"
            "        };\n"
            "    }\n"
            "};\n")
        self.runner.write_text(harness.replace('__TRANSACTION__', prelude + transaction))

    def call(self, verb, value=None, check=True, env=None):
        result = subprocess.run([PHP, str(self.runner), str(ROOT), str(self.private), verb],
                                input=json.dumps(value or {}).encode(), capture_output=True,
                                timeout=15, env=env)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)
        return result

    def saved(self):
        return self.call('fixture-read')

    def test_recovery_key_export_and_key_lifecycle_do_not_require_billing_row(self):
        from paymenter_backups.cli import parser
        app = self.base / 'app'
        (app / 'public').mkdir(parents=True)
        state = State(app, self.private)
        output_directory = self.base / 'export'
        output_directory.mkdir(mode=0o700)
        output = output_directory / 'recovery-key.json'
        pending = self.call('archive-key-import', {
            'passphrase': archive_keys.SECRET, 'fingerprint': archive_keys.PARAMETERS})
        missing_billing = dict(os.environ, FIXTURE_MISSING_BILLING='1')
        calls = []

        def bridge(_args, verb, payload=None):
            calls.append(verb)
            result = self.call(verb, payload, check=False, env=missing_billing)
            if result.returncode:
                raise BackupError(json.loads(result.stderr)['error'])
            return json.loads(result.stdout)

        args = parser().parse_args(['--app', str(app), '--private-root', str(self.private),
                                   'recovery-key', 'export', '--output', str(output)])
        result = key_cli.dispatch(args, state, bridge, lambda: {})
        self.assertEqual(calls, ['document-roots', 'archive-key-export'])
        self.assertEqual(result, {'exported': True, 'fingerprint': pending['fingerprint']})
        self.assertNotIn(archive_keys.SECRET, json.dumps(result))
        record = json.loads(output.read_text())
        self.assertEqual(record['secret'], archive_keys.SECRET)
        self.assertEqual(record['parameters'], archive_keys.PARAMETERS)
        self.assertEqual(record['installation_uuid'], state.uuid)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(bridge(None, 'archive-key-status')['state'], 'pending')
        self.assertEqual(bridge(None, 'archive-key-reveal', {'fingerprint': pending['fingerprint']})['secret'], archive_keys.SECRET)
        self.assertEqual(bridge(None, 'archive-key-acknowledge', {
            'fingerprint': pending['fingerprint']})['state'], 'active')
        rotated = bridge(None, 'archive-key-generate')
        self.assertEqual(rotated['state'], 'pending')
        bridge(None, 'archive-key-discard-pending')
        self.assertEqual(bridge(None, 'credentials-rewrap'), {'ok': True})
        for verb in ('read', 'read-settings'):
            with self.subTest(verb=verb), self.assertRaisesRegex(BackupError, 'billing-settings-unreadable'):
                bridge(None, verb)

    def test_settings_read_never_constructs_encrypter_or_reads_corrupt_archive_store(self):
        self.call('archive-key-generate')
        self.call('fixture-corrupt-namespace', {'change': 'type'})
        credential_file = self.private / 'credentials.json'
        before = credential_file.read_bytes()
        envelope = self.call('read-settings', env=dict(os.environ, FIXTURE_REFUSE_ENCRYPTER='1'))
        self.assertEqual(envelope['credentials'], [])  # Empty PHP array on the private pipe.
        self.assertEqual(envelope['maintenance_probe_credentials'], [])
        self.assertEqual(envelope['archive_encryption_credentials'], [])
        self.assertIn('stores', envelope['framework'])
        self.assertEqual(envelope['tools']['gpg'], '')
        self.assertEqual(credential_file.read_bytes(), before)
        refused = self.call('credentials-merge', {'s3': {'access_key': 'fake', 'secret_key': 'fake'}}, check=False)
        self.assertEqual(json.loads(refused.stderr), {'error': 'credentials-unreadable'})
        self.assertEqual(credential_file.read_bytes(), before)

    def test_combined_merge_preserves_archive_records_and_validates_before_publication(self):
        self.call('archive-key-generate')
        namespace = self.saved()['archive_encryption']
        s3 = {'access_key': 'fake-new', 'secret_key': ' fake secret ', 'session_token': 'fake-token'}
        site = {'origin': 'https://example.test', 'username': 'fixture', 'password': ' fake password '}
        self.call('credentials-merge', {'s3': s3, 'site': site})
        saved = self.saved()
        self.assertEqual(saved['archive_encryption'], namespace)
        self.assertEqual(saved['maintenance_probe'], site)
        credential_file = self.private / 'credentials.json'
        before = credential_file.read_bytes()
        failed = self.call('credentials-merge', {'s3': {'access_key': 'bad-new', 'secret_key': 'bad-new'},
                                                'site': dict(site, origin='https://other.invalid')}, check=False)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(credential_file.read_bytes(), before)
        self.call('credentials-merge', {'s3': {'access_key': 'fake-next', 'secret_key': 'fake-next'}, 'site': None})
        saved = self.saved()
        self.assertNotIn('session_token', saved)
        self.assertEqual(saved['maintenance_probe'], site)
        self.assertEqual(saved['archive_encryption'], namespace)

    def test_empty_key_map_serialization_survives_combined_credential_write(self):
        self.call('archive-key-generate')
        self.call('archive-key-discard-pending')
        self.call('credentials-merge', {'s3': {'access_key': 'fake', 'secret_key': 'fake'}})
        self.assertEqual(self.saved()['archive_encryption']['keys'], {})
        self.call('archive-key-generate')
        self.assertEqual(self.call('archive-key-status')['state'], 'pending')

    def test_combined_write_obeys_same_namespace_flock(self):
        self.call('archive-key-generate')
        namespace = self.saved()['archive_encryption']
        with (self.private / 'credentials.lock').open('r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen([PHP, str(self.runner), str(ROOT), str(self.private), 'credentials-merge'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                process.stdin.write(json.dumps({'s3': {'access_key': 'fake', 'secret_key': 'fake'}}).encode())
                process.stdin.flush()
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.15)
                fcntl.flock(lock, fcntl.LOCK_UN)
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(json.loads(output), {'ok': True})
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        self.assertEqual(self.saved()['archive_encryption'], namespace)


@unittest.skipUnless(PHP, 'PHP required for Runtime API')
class MergedRuntimeTests(unittest.TestCase):
    def test_combined_save_and_encryption_methods_share_validated_runtime(self):
        with tempfile.TemporaryDirectory(prefix='backups-merged-runtime-') as temporary:
            base = pathlib.Path(temporary)
            app = base / 'app'
            app.mkdir()
            private = base / 'private'
            private.mkdir(mode=0o700)
            source = archive_keys.PHP_RUNTIME.replace(
                "public static function where(...$args) { return new self; }",
                "public static function __callStatic($name, $args) { return new self; }")
            source = source.replace(
                "public function where($key, $value) { $this->key = $value; return $this; }",
                "public function __call($name, $args) { if ($name === 'where') { $this->key = $args[1]; } return $this; }")
            combined = r'''
    \Symfony\Component\Process\Process::$success = true;
    \Symfony\Component\Process\Process::$response = ['updated' => true, 'credentials_updated' => true];
    \Paymenter\Extensions\Others\Backups\Runtime::saveCredentials([
        's3' => ['access_key' => 'fake', 'secret_key' => ' fake secret '], 'site' => null,
    ]);
    $result['combined'] = array_values(array_filter(\Symfony\Component\Process\Process::$calls,
        static fn ($entry) => array_slice($entry[0], -2) === ['config', 'set']));
'''
            source = source.replace('    echo json_encode($result);', combined + '    echo json_encode($result);')
            script = base / 'runtime.php'
            script.write_text(source)
            process = subprocess.run([PHP, str(script), str(ROOT), str(app), str(private)],
                                     capture_output=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            result = json.loads(process.stdout)
            self.assertEqual(result['generated']['state'], 'pending')
            self.assertEqual(result['generic-command'], 'sensitive-command-refused')
            self.assertEqual(len(result['combined']), 1)
            combined_input = json.loads(result['combined'][0][1])
            self.assertEqual(combined_input['settings'], {})
            self.assertEqual(combined_input['credentials'], {'access_key': 'fake', 'secret_key': ' fake secret '})
            self.assertIsNone(combined_input['maintenance_probe_credentials'])
            self.assertEqual(combined_input['_actor'], 42)


class MergedConfigurationTests(unittest.TestCase):
    def test_encrypted_upload_without_bucket_needs_configuration_not_plaintext_consent(self):
        value = dict(config.DEFAULTS, settings_version=2, upload_s3=True,
                     s3_encryption=True, plaintext_offsite_acknowledged=False)
        config.normalize(value)
        with self.assertRaises(BackupError) as refused:
            config.validate({'settings': value})
        self.assertEqual(refused.exception.code, 'remote-configuration-required')
        self.assertEqual(refused.exception.field, 's3_bucket')

    def test_legacy_encryption_projection_depends_on_destination_and_prior_consent(self):
        for destination in ('local', 's3'):
            for consent in (False, True):
                with self.subTest(destination=destination, consent=consent):
                    result = config.from_legacy({'destination': destination, 'plaintext_offsite_acknowledged': consent})
                    self.assertEqual(result['s3_encryption'], not (destination == 's3' and consent))

    def test_v2_plaintext_consent_is_required_only_for_plaintext_uploads(self):
        reports = {name: {'configured': '', 'resolved': '/fixture/' + name, 'ok': True, 'code': None}
                   for name in tools.CANDIDATES}
        for upload, encryption, consent in ((False, False, False), (True, True, False),
                                            (True, False, True), (True, False, False)):
            value = dict(config.DEFAULTS, settings_version=2, upload_s3=upload,
                         s3_encryption=encryption, plaintext_offsite_acknowledged=consent, s3_bucket='fixture')
            config.normalize(value)
            with self.subTest(upload=upload, encryption=encryption, consent=consent), patch.object(tools, 'checks', return_value=reports):
                if upload and not encryption and not consent:
                    with self.assertRaisesRegex(BackupError, 'plaintext-offsite-acknowledgement-required'):
                        config.validate({'settings': value})
                else:
                    config.validate({'settings': value})

    def test_secret_redaction_covers_both_merged_encryption_namespaces(self):
        secret_names = ('archive_encryption', 'archive_encryption_credentials', 'passphrase',
                        'secret', 'recovery_key', 'recovery_secret')
        value = {'nested': [{name: 'fixture-secret' for name in secret_names}], 'state': 'pending'}
        redacted = config.redact(value)
        self.assertNotIn('fixture-secret', json.dumps(redacted))
        self.assertEqual(redacted['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
