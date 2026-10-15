"""Private credential namespace and recovery-key API, without Laravel or a DB.

The credential harness runs the production bridge transaction body with a
synthetic APP_KEY encrypter. Native Laravel wrapping remains an integration check.
"""
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from paymenter_backups.common import BackupError, State
from paymenter_backups import key_cli

ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET = '  exact passphrase café & spaces  '
DOMAIN = b'paymenter-backups-key-fingerprint-v1\0'
PARAMETERS = {'scheme': 'pbkdf2-hmac-sha256-v1', 'salt': 'ab' * 32, 'iterations': 600000,
              'value': hashlib.pbkdf2_hmac('sha256', SECRET.encode(), DOMAIN + bytes.fromhex('ab' * 32), 600000, 32).hex()}

HARNESS = r'''<?php
umask(0077);
require $argv[1] . '/extension/Backups/PrivateStore.php';
require $argv[1] . '/extension/Backups/ProbeCredentials.php';
require $argv[1] . '/extension/Backups/ArchiveEncryption.php';
use Paymenter\Extensions\Others\Backups\PrivateStore;
class Encrypter {
    private array $previous = [];
    public function __construct(private string $key, string $cipher) {}
    public function previousKeys(array $keys): void { $this->previous = $keys; }
    public function encryptString(string $value): string {
        $iv = random_bytes(16);
        $encrypted = openssl_encrypt($value, 'AES-256-CBC', $this->key, OPENSSL_RAW_DATA, $iv);
        return base64_encode($iv . hash_hmac('sha256', $iv . $encrypted, $this->key, true) . $encrypted);
    }
    public function decryptString(string $value): string {
        $bytes = base64_decode($value, true);
        if (!is_string($bytes) || strlen($bytes) < 64) { throw new RuntimeException('fixture-decryption-failed'); }
        $iv = substr($bytes, 0, 16); $mac = substr($bytes, 16, 32); $encrypted = substr($bytes, 48);
        foreach ([$this->key, ...$this->previous] as $key) {
            if (hash_equals($mac, hash_hmac('sha256', $iv . $encrypted, $key, true))) {
                $value = openssl_decrypt($encrypted, 'AES-256-CBC', $key, OPENSSL_RAW_DATA, $iv);
                if (is_string($value)) { return $value; }
            }
        }
        throw new RuntimeException('fixture-decryption-failed');
    }
}
$config = new class {
    public function get(string $name, mixed $fallback = null): mixed {
        return match ($name) {
            'app.key' => str_repeat('a', 32), 'app.previous_keys' => [], 'app.cipher' => 'AES-256-CBC',
            default => $fallback,
        };
    }
};
$root = $argv[2];
$verb = $argv[3];
$identity = file_exists($root . '/installation.json') ? PrivateStore::read($root . '/installation.json') : ['installation_uuid' => '11111111-2222-4333-8444-555555555555'];
$billingRows = ['app_url' => 'https://example.test'];
if (str_starts_with($verb, 'fixture-')) {
    try { $input = json_decode(stream_get_contents(STDIN), true, 8, JSON_THROW_ON_ERROR); }
    catch (Throwable $error) { if ($verb === 'fixture-secret') { echo '{"valid":false}'; exit; } throw $error; }
    if ($verb === 'fixture-fingerprint') {
        try { echo json_encode(Paymenter\Extensions\Others\Backups\ArchiveEncryption::fingerprint($input['secret'], $input['parameters'] ?? null)); }
        catch (Throwable $error) { echo json_encode(['error' => $error->getMessage()]); }
        exit;
    }
    if ($verb === 'fixture-secret') {
        try { Paymenter\Extensions\Others\Backups\ArchiveEncryption::secret($input['secret']); echo '{"valid":true}'; }
        catch (Throwable $error) { echo '{"valid":false}'; }
        exit;
    }
    if ($verb === 'fixture-read') {
        $stored = PrivateStore::read($root . '/credentials.json');
        $encrypter = new Encrypter(str_repeat($input['key'] ?? 'a', 32), 'AES-256-CBC');
        echo $encrypter->decryptString($stored['payload']); exit;
    }
    if ($verb === 'fixture-corrupt-namespace') {
        $stored = PrivateStore::read($root . '/credentials.json');
        $encrypter = new Encrypter(str_repeat('a', 32), 'AES-256-CBC');
        $payload = json_decode($encrypter->decryptString($stored['payload']), true, 8, JSON_THROW_ON_ERROR);
        $id = $payload['archive_encryption']['pending_fingerprint'];
        if ($input['change'] === 'version') { $payload['archive_encryption']['version'] = 2; }
        if ($input['change'] === 'iteration') { $payload['archive_encryption']['keys'][$id]['fingerprint']['iterations'] = true; }
        if ($input['change'] === 'selection') { unset($payload['archive_encryption']['active_fingerprint']); }
        if ($input['change'] === 'secret') { $payload['archive_encryption']['keys'][$id]['secret'] = 'short'; }
        if ($input['change'] === 'type') { $payload['archive_encryption'] = 'malformed'; }
        $stored['payload'] = $encrypter->encryptString(json_encode($payload, JSON_THROW_ON_ERROR));
        PrivateStore::atomic($root . '/credentials.json', $stored); echo '{"ok":true}'; exit;
    }
    if ($verb === 'fixture-old-wrap') {
        $stored = PrivateStore::read($root . '/credentials.json');
        $plain = (new Encrypter(str_repeat('a', 32), 'AES-256-CBC'))->decryptString($stored['payload']);
        $stored['payload'] = (new Encrypter(str_repeat('b', 32), 'AES-256-CBC'))->encryptString($plain);
        $stored['key_fingerprint'] = hash_hmac('sha256', 'paymenter-backups-key-v1:' . $identity['installation_uuid'], str_repeat('b', 32));
        PrivateStore::atomic($root . '/credentials.json', $stored); echo '{"ok":true}'; exit;
    }
}
if ($verb === 'credentials-rewrap') {
    $config = new class {
        public function get(string $name, mixed $fallback = null): mixed {
            return match ($name) {
                'app.key' => str_repeat('a', 32), 'app.previous_keys' => [str_repeat('b', 32)], 'app.cipher' => 'AES-256-CBC',
                default => $fallback,
            };
        }
    };
}
try {
__TRANSACTION__
} catch (Throwable $error) { fwrite(STDERR, json_encode(['error' => $error->getMessage()])); exit(1); }
'''


@unittest.skipUnless(shutil.which('php'), 'PHP required for credential namespace tests')
class ArchiveKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='backups-archive-keys-')
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.private = self.base / 'private'
        self.private.mkdir(mode=0o700)
        self.runner = self.base / 'transaction.php'
        bridge = (ROOT / 'extension/Backups/bridge.php').read_text()
        transaction = bridge[bridge.index('    $decodeKey ='):bridge.index("    if ($verb !== 'read')")]
        self.runner.write_text(HARNESS.replace('__TRANSACTION__', transaction))

    def call(self, verb, value=None, check=True):
        result = subprocess.run(['php', str(self.runner), str(ROOT), str(self.private), verb],
                                input=json.dumps(value or {}, ensure_ascii=True).encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)
        return result

    def assert_error(self, verb, value, code):
        result = self.call(verb, value, check=False)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(json.loads(result.stderr), {'error': code})

    def test_php_fingerprint_matches_independent_python_canary(self):
        result = self.call('fixture-fingerprint', {'secret': SECRET, 'parameters': PARAMETERS})
        self.assertEqual(result, PARAMETERS)
        for field in PARAMETERS:
            missing = {key: value for key, value in PARAMETERS.items() if key != field}
            self.assertEqual(self.call('fixture-fingerprint', {'secret': SECRET, 'parameters': missing}), {'error': 'archive-key-invalid'})
        for wrong in (True, '600000', 600000.0):
            self.assertEqual(self.call('fixture-fingerprint', {'secret': SECRET, 'parameters': dict(PARAMETERS, iterations=wrong)}), {'error': 'archive-key-invalid'})
        changed = dict(PARAMETERS, iterations=1)
        self.assertEqual(self.call('fixture-fingerprint', {'secret': SECRET, 'parameters': changed}), {'error': 'archive-key-invalid'})
        self.assertEqual(self.call('fixture-fingerprint', {'secret': SECRET + 'wrong', 'parameters': PARAMETERS}), {'error': 'archive-key-mismatch'})

    def test_exact_utf8_character_and_byte_bounds(self):
        from paymenter_backups.encryption import validate_secret
        canaries = [(SECRET, True), ('é' * 20, True), ('é' * 512, True), ('é' * 513, False),
                    ('x' * 19, False), ('x' * 20, True), ('x' * 1024, True), ('x' * 1025, False),
                    ('x' * 20 + '\n', False), ('x' * 20 + '\r', False), ('x' * 20 + '\0', False),
                    ('x' * 20 + '\x7f', False), ('x' * 20 + '\x85', False), ('x' * 20 + '\u200d', True),
                    ('x' * 20 + '\ue000', True), ('x' * 20 + '\ud800', False)]
        for value, expected in canaries:
            with self.subTest(value=repr(value)):
                self.assertEqual(self.call('fixture-secret', {'secret': value}), {'valid': expected})
                try:
                    validate_secret(value)
                    valid = True
                except BackupError:
                    valid = False
                self.assertEqual(valid, expected)

    def test_generated_pending_key_acknowledgement_and_rotation_retains_old_key(self):
        first = self.call('archive-key-generate', {'actor': 4})
        self.assertEqual(set(first), {'fingerprint', 'short', 'state'})
        stored = self.call('fixture-read')['archive_encryption']
        record = stored['keys'][first['fingerprint']]
        self.assertRegex(record['secret'], '^[a-f0-9]{64}$')
        self.assertIsNone(record['acknowledgement'])
        self.assertIsNone(stored['active_fingerprint'])
        self.assert_error('archive-key-acknowledge', {'fingerprint': 'f' * 64}, 'archive-key-mismatch')
        self.assert_error('archive-key-generate', {}, 'archive-key-pending-exists')
        active = self.call('archive-key-acknowledge', {'fingerprint': first['fingerprint'], 'actor': 4})
        self.assertEqual(active['state'], 'active')
        second = self.call('archive-key-import', {'passphrase': SECRET})
        pending = self.call('fixture-read')['archive_encryption']
        self.assertEqual(pending['active_fingerprint'], first['fingerprint'])
        self.assertIsNone(pending['keys'][second['fingerprint']]['acknowledgement'])
        self.call('archive-key-acknowledge', {'fingerprint': second['fingerprint'], 'actor': 8})
        final = self.call('fixture-read')['archive_encryption']
        self.assertEqual(final['active_fingerprint'], second['fingerprint'])
        self.assertEqual(final['keys'][first['fingerprint']]['state'], 'retired')
        self.assertEqual(final['keys'][first['fingerprint']]['secret'], record['secret'])
        self.assertEqual(final['keys'][second['fingerprint']]['acknowledgement']['actor'], 8)
        self.assertEqual(self.call('archive-key-reveal', {'fingerprint': first['fingerprint']})['secret'], record['secret'])

    def test_import_export_preserves_exact_secret_and_fingerprint_identity(self):
        self.assert_error('archive-key-import', {'passphrase': SECRET, 'fingerprint': None}, 'archive-key-invalid')
        pending = self.call('archive-key-import', {'passphrase': SECRET, 'fingerprint': PARAMETERS})
        exported = self.call('archive-key-export')
        self.assertEqual(exported['secret'], SECRET)
        self.assertEqual(exported['parameters'], PARAMETERS)
        self.assertEqual(exported['fingerprint'], pending['fingerprint'])
        self.call('archive-key-discard-pending')
        imported = self.call('archive-key-import', {'passphrase': exported['secret'], 'fingerprint': exported['parameters']})
        self.assertEqual(imported, pending)

    def test_run_lock_busy_refuses_activation_without_changing_acknowledgement(self):
        pending = self.call('archive-key-generate')
        before = (self.private / 'credentials.json').read_bytes()
        with (self.private / 'run.lock').open('w') as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_error('archive-key-acknowledge', {'fingerprint': pending['fingerprint']}, 'archive-key-change-busy')
        self.assertEqual((self.private / 'credentials.json').read_bytes(), before)
        record = self.call('fixture-read')['archive_encryption']['keys'][pending['fingerprint']]
        self.assertIsNone(record['acknowledgement'])

    def test_namespace_transactions_preserve_s3_and_probe(self):
        probe = {'origin': 'https://example.test', 'username': 'fake-user', 'password': 'fake-password'}
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(self.call, 'credentials-write', {'access_key': 'fake-access', 'secret_key': 'fake-secret'}),
                       workers.submit(self.call, 'maintenance-probe-credentials-write', probe),
                       workers.submit(self.call, 'archive-key-generate')]
            results = [future.result() for future in futures]
        payload = self.call('fixture-read')
        self.assertEqual(payload['access_key'], 'fake-access')
        self.assertEqual(payload['maintenance_probe']['username'], 'fake-user')
        self.assertEqual(payload['archive_encryption']['pending_fingerprint'], results[2]['fingerprint'])
        self.call('credentials-write', {'clear': True})
        payload = self.call('fixture-read')
        self.assertNotIn('access_key', payload)
        self.assertIn('maintenance_probe', payload)
        self.assertIn('archive_encryption', payload)
        self.call('maintenance-probe-credentials-write', {'clear': True})
        self.call('archive-key-discard-pending')
        self.assertEqual(self.call('fixture-read')['archive_encryption']['keys'], {})

    def test_current_previous_app_key_rewrap_preserves_all_namespaces(self):
        self.call('credentials-write', {'access_key': 'fake-access', 'secret_key': 'fake-secret'})
        pending = self.call('archive-key-import', {'passphrase': SECRET, 'fingerprint': PARAMETERS})
        before = self.call('fixture-read')
        self.call('fixture-old-wrap')
        self.assert_error('archive-key-status', {}, 'credentials-unreadable')
        self.call('credentials-rewrap')
        self.assertEqual(self.call('fixture-read'), before)
        self.assertEqual(self.call('archive-key-status')['fingerprint'], pending['fingerprint'])

    def test_unreadable_store_and_failed_audit_never_replace_or_release_secrets(self):
        self.call('archive-key-import', {'passphrase': SECRET, 'fingerprint': PARAMETERS})
        before = (self.private / 'credentials.json').read_bytes()
        audit = self.private / 'audit.jsonl'
        audit.unlink()
        audit.symlink_to(self.base / 'unrelated')
        self.assert_error('archive-key-reveal', {}, 'archive-key-audit-failed')
        self.assert_error('archive-key-export', {}, 'archive-key-audit-failed')
        self.assert_error('archive-key-acknowledge', {'fingerprint': PARAMETERS['value']}, 'archive-key-audit-failed')
        self.assertEqual((self.private / 'credentials.json').read_bytes(), before)
        (self.private / 'credentials.json').write_text('{"schema_version":1,"payload":"broken"}')
        broken = (self.private / 'credentials.json').read_bytes()
        self.assert_error('archive-key-generate', {}, 'credentials-unreadable')
        self.assertEqual((self.private / 'credentials.json').read_bytes(), broken)

    def test_malformed_stored_namespace_refuses_private_reads_and_mutations_without_overwrite(self):
        self.call('archive-key-generate')
        original = (self.private / 'credentials.json').read_bytes()
        for change in ('version', 'iteration', 'selection', 'secret', 'type'):
            with self.subTest(change=change):
                (self.private / 'credentials.json').write_bytes(original)
                self.call('fixture-corrupt-namespace', {'change': change})
                corrupt = (self.private / 'credentials.json').read_bytes()
                for verb in ('archive-key-status', 'archive-key-reveal', 'archive-key-export', 'archive-key-generate', 'credentials-rewrap'):
                    self.assert_error(verb, {}, 'credentials-unreadable')
                self.assertEqual((self.private / 'credentials.json').read_bytes(), corrupt)

    def test_audit_contains_public_authorization_receipts_and_no_secrets(self):
        pending = self.call('archive-key-import', {'passphrase': SECRET, 'fingerprint': PARAMETERS, 'actor': 19})
        self.call('archive-key-reveal', {'actor': 19})
        self.call('archive-key-export', {'actor': 19})
        lines = (self.private / 'audit.jsonl').read_text()
        self.assertNotIn(SECRET, lines)
        for line in lines.splitlines():
            entry = json.loads(line)
            self.assertEqual(set(entry), {'at', 'event', 'fingerprint', 'actor', 'outcome'})
            self.assertEqual(entry['fingerprint'], pending['fingerprint'])
            self.assertEqual(entry['actor'], 19)
            self.assertEqual(entry['outcome'], 'authorized')


@unittest.skipUnless(shutil.which('php'), 'PHP required for CLI key transaction tests')
class RecoveryKeyCliTests(unittest.TestCase):
    call = ArchiveKeyTests.call
    def setUp(self):
        ArchiveKeyTests.setUp(self)
        self.app = self.base / 'app'
        (self.app / 'public').mkdir(parents=True)
        self.state = State(self.app, self.private)
        self.output_directory = self.base / 'export'
        self.output_directory.mkdir(mode=0o700)

    def operation(self, *arguments, payload=None):
        from paymenter_backups.cli import parser
        args = parser().parse_args(['--app', str(self.app), '--private-root', str(self.state.root), *arguments])
        def bridge(_args, verb, value=None):
            if verb == 'document-roots':
                return {'schema_version': 1, 'app': str(self.app), 'owner_uid': os.getuid(),
                        'private_root': str(self.state.root), 'installation_uuid': self.state.uuid,
                        'settings': {'extra_docroots': []}}
            result = self.call(verb, value, check=False)
            if result.returncode:
                raise BackupError(json.loads(result.stderr)['error'])
            return json.loads(result.stdout)
        return key_cli.dispatch(args, self.state, bridge, lambda: payload or {})

    def test_cli_metadata_never_contains_key_and_export_reimports_its_identity(self):
        pending = self.operation('encryption-key', 'import', payload={'passphrase': SECRET, 'fingerprint': PARAMETERS})
        self.assertNotIn(SECRET, json.dumps(pending))
        output = self.output_directory / 'key.json'
        result = self.operation('recovery-key', 'export', '--output', str(output))
        self.assertEqual(result, {'exported': True, 'fingerprint': pending['fingerprint']})
        self.assertNotIn(SECRET, json.dumps(result))
        record = json.loads(output.read_text())
        self.assertEqual(record['secret'], SECRET)
        self.assertEqual(record['installation_uuid'], self.state.uuid)
        self.assertEqual(record['parameters'], PARAMETERS)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.stat().st_nlink, 1)
        self.operation('encryption-key', 'discard-pending')
        imported = self.operation('encryption-key', 'import', payload=record)
        self.assertEqual(imported, pending)
        self.operation('encryption-key', 'acknowledge', payload={'fingerprint': pending['fingerprint']})
        self.assertEqual(self.operation('encryption-key', 'status')['state'], 'active')

    def test_export_refuses_unsafe_paths_and_existing_names_without_a_secret_read(self):
        self.operation('encryption-key', 'generate')
        existing = self.output_directory / 'existing'
        existing.write_text('keep this')
        symlink = self.base / 'linked'
        symlink.symlink_to(self.output_directory, target_is_directory=True)
        public = self.app / 'public'
        public.chmod(0o700)
        self.base.chmod(0o755)
        candidates = [str(existing), str(symlink / 'key'), str(public / 'key'), str(self.base / 'key'), 'relative-key',
                      str(self.output_directory / '..' / 'key')]
        audit_before = (self.private / 'audit.jsonl').read_bytes()
        for output in candidates:
            with self.subTest(output=output), self.assertRaises(BackupError):
                self.operation('recovery-key', 'export', '--output', output)
        self.assertEqual(existing.read_text(), 'keep this')
        self.assertEqual((self.private / 'audit.jsonl').read_bytes(), audit_before)
        self.assertFalse((public / 'key').exists())

    def test_failed_export_leaves_no_final_or_partial_file(self):
        from unittest.mock import patch
        self.operation('encryption-key', 'generate')
        output = self.output_directory / 'failed.json'
        with patch('paymenter_backups.key_cli.os.write', side_effect=OSError('synthetic write failure')):
            with self.assertRaisesRegex(BackupError, 'recovery-key-export-failed'):
                self.operation('recovery-key', 'export', '--output', str(output))
        self.assertEqual(list(self.output_directory.iterdir()), [])
        audit = self.private / 'audit.jsonl'
        audit.unlink()
        audit.symlink_to(self.base / 'missing')
        with self.assertRaisesRegex(BackupError, 'archive-key-audit-failed'):
            self.operation('recovery-key', 'export', '--output', str(output))
        self.assertEqual(list(self.output_directory.iterdir()), [])

    def test_export_fsync_failure_rolls_back_only_owned_publication(self):
        from unittest.mock import patch
        self.operation('encryption-key', 'generate')
        output = self.output_directory / 'failed.json'
        original = os.fsync
        def failed_directory(descriptor):
            if os.fstat(descriptor).st_mode & 0o170000 == 0o040000:
                raise OSError('synthetic directory fsync failure')
            return original(descriptor)
        with patch('paymenter_backups.key_cli.os.fsync', side_effect=failed_directory):
            with self.assertRaisesRegex(BackupError, 'recovery-key-export-failed'):
                self.operation('recovery-key', 'export', '--output', str(output))
        self.assertEqual(list(self.output_directory.iterdir()), [])

    def test_cleanup_preserves_replaced_partial_and_final_names(self):
        from unittest.mock import patch
        self.operation('encryption-key', 'generate')
        original = os.fsync
        for replace in ('partial', 'final'):
            with self.subTest(replace=replace):
                output = self.output_directory / (replace + '.json')
                def replace_then_fail(descriptor):
                    directory = os.fstat(descriptor).st_mode & 0o170000 == 0o040000
                    if (replace == 'partial' and not directory) or (replace == 'final' and directory):
                        target = output if directory else next(self.output_directory.glob('.*.partial'))
                        target.unlink()
                        target.write_text('unrelated replacement')
                        if directory:
                            raise OSError('synthetic directory failure')
                    return original(descriptor)
                with patch('paymenter_backups.key_cli.os.fsync', side_effect=replace_then_fail):
                    with self.assertRaisesRegex(BackupError, 'recovery-key-export-failed'):
                        self.operation('recovery-key', 'export', '--output', str(output))
                survivors = list(self.output_directory.iterdir())
                self.assertEqual(len(survivors), 1)
                self.assertEqual(survivors[0].read_text(), 'unrelated replacement')
                survivors[0].unlink()

    def test_generic_metadata_rejects_a_secret_bearing_bridge_result(self):
        from paymenter_backups.cli import parser
        args = parser().parse_args(['encryption-key', 'status'])
        with self.assertRaisesRegex(BackupError, 'archive-key-invalid'):
            key_cli.dispatch(args, self.state, lambda *a: {'state': 'active', 'secret': SECRET}, lambda: {})

PHP_RUNTIME = r'''<?php
namespace Illuminate\Support\Facades {
    class Auth {
        public static bool $allowed = true;
        public static function id() { return 42; }
        public static function user() { return new class { public function hasPermission($permission) { return Auth::$allowed && $permission === 'admin.extensions.update'; } }; }
    }
    class URL {
        public static array $signed = [];
        public static function temporarySignedRoute($name, $expires, $parameters) {
            self::$signed = [$name, $expires, $parameters]; return '/signed-key?fingerprint=' . $parameters['fingerprint'];
        }
    }
    class Route {
        public static array $registered = [];
        public static function get($path, $controller) {
            self::$registered = ['path' => $path, 'controller' => $controller];
            return new class {
                public function middleware($value) { Route::$registered['middleware'] = $value; return $this; }
                public function withoutMiddleware($value) { Route::$registered['without'] = $value; return $this; }
                public function name($value) { Route::$registered['name'] = $value; return $this; }
            };
        }
    }
}
namespace Filament { class Panel {} }
namespace Filament\Support\Facades { class FilamentView { public static function spaUrlExceptions($urls) {} } }
namespace App\Models {
    class Extension {
        private string $key = '';
        public static function where(...$args) { return new self; }
        public function firstOrFail() { return $this; }
        public function settings() { return $this; }
        public function where($key, $value) { $this->key = $value; return $this; }
        public function value($key) { return match ($this->key) { 'private_root' => $GLOBALS['private'], 'extra_docroots' => '[]', 'php' => PHP_BINARY, default => null }; }
    }
}
namespace Paymenter\Extensions\Others\Backups\Admin\Pages {
    class Backups {
        public static function getRouteName() { return 'filament.admin.pages.backups'; }
        public static function canAccess() { return \Illuminate\Support\Facades\Auth::$allowed; }
    }
}
namespace Symfony\Component\Process {
    class Process {
        public static array $calls = [];
        public static array $response = [];
        public static bool $success = true;
        public function __construct(private array $command, $cwd=null, $environment=null, public $input=null) { self::$calls[] = [$command, $input]; }
        public function setTimeout($seconds) {}
        public function run() {}
        public function mustRun() {}
        public function isSuccessful() { return self::$success; }
        public function getOutput() {
            if (in_array('-r', $this->command, true)) { return 'cli ' . PHP_VERSION_ID; }
            return in_array('-c', $this->command, true) ? json_encode(['python_valid'=>true,'php_valid'=>true,'php_canonical'=>PHP_BINARY,'current_canonical'=>PHP_BINARY]) : json_encode(self::$response);
        }
        public function getErrorOutput() { return '{"error":"archive-key-audit-failed"}'; }
    }
}
namespace Illuminate\Http {
    class Request {
        public function __construct(private bool $signed, private string $fingerprint) {}
        public function hasValidSignature() { return $this->signed; }
        public function query($key) { return $this->fingerprint; }
    }
}
namespace Symfony\Component\HttpFoundation {
    class Response {
        public object $headers;
        public function __construct(public string $content, public int $status=200, array $headers=[]) {
            $this->headers = new class($headers) {
                public function __construct(public array $values) {}
                public function set($key,$value) { $this->values[$key]=$value; }
                public function makeDisposition($type,$filename) { return $type . '; filename=' . $filename; }
            };
        }
    }
}
namespace {
    umask(0077);
    function base_path() { return $GLOBALS['app']; }
    function abort_unless($condition, $code) { if (!$condition) { throw new \RuntimeException('denied-' . $code); } }
    function now() { return new class { public function addMinutes($minutes) { return $minutes * 60; } }; }
    $GLOBALS['app'] = $argv[2]; $GLOBALS['private'] = $argv[3];
    require $argv[1] . '/extension/Backups/PrivateStore.php';
    require $argv[1] . '/extension/Backups/Runtime.php';
    require $argv[1] . '/extension/Backups/Http/RecoveryKeyController.php';
    $fp = str_repeat('a',64);
    $result = [];
    \Symfony\Component\Process\Process::$response = ['fingerprint'=>$fp,'short'=>substr($fp,0,16),'state'=>'pending'];
    $result['generated'] = \Paymenter\Extensions\Others\Backups\Runtime::encryptionKey('generate');
    $result['actor'] = json_decode(end(\Symfony\Component\Process\Process::$calls)[1],true)['actor'];
    $result['url'] = \Paymenter\Extensions\Others\Backups\Runtime::recoveryKeyUrl();
    $result['signed'] = \Illuminate\Support\Facades\URL::$signed;
    foreach (['reveal','export'] as $action) {
        try { \Paymenter\Extensions\Others\Backups\Runtime::encryptionKey($action); }
        catch (\RuntimeException $e) { $result['generic-' . $action] = $e->getMessage(); }
    }
    try { \Paymenter\Extensions\Others\Backups\Runtime::command(['recovery-key','export']); }
    catch (\RuntimeException $e) { $result['generic-command'] = $e->getMessage(); }
    \Illuminate\Support\Facades\Auth::$allowed = false;
    $before = count(\Symfony\Component\Process\Process::$calls);
    try { \Paymenter\Extensions\Others\Backups\Runtime::revealRecoveryKey(); }
    catch (\RuntimeException $e) { $result['denied'] = $e->getMessage(); }
    $result['denied-before-child'] = count(\Symfony\Component\Process\Process::$calls) === $before;
    \Illuminate\Support\Facades\Auth::$allowed = true;
    \Paymenter\Extensions\Others\Backups\Http\RecoveryKeyController::register(new \Filament\Panel, '/backups', 'backups', ['auth'], []);
    $result['route'] = \Illuminate\Support\Facades\Route::$registered;
    $controller = new \Paymenter\Extensions\Others\Backups\Http\RecoveryKeyController;
    try { $controller(new \Illuminate\Http\Request(false,$fp)); }
    catch (\RuntimeException $e) { $result['unsigned'] = $e->getMessage(); }
    \Symfony\Component\Process\Process::$response = ['secret'=>'fixture secret with 20 characters','fingerprint'=>$fp,
        'parameters'=>['scheme'=>'pbkdf2-hmac-sha256-v1','salt'=>str_repeat('b',64),'iterations'=>600000,'value'=>$fp],
        'installation_uuid'=>'11111111-2222-4333-8444-555555555555'];
    $response = $controller(new \Illuminate\Http\Request(true,$fp));
    $result['attachment'] = ['status'=>$response->status,'headers'=>$response->headers->values,'has-secret'=>str_contains($response->content,'fixture secret'),
        'has-params'=>str_contains($response->content,'600000'),'has-instructions'=>str_contains($response->content,'verify_recovery.py')];
    \Symfony\Component\Process\Process::$success = false;
    $failed = $controller(new \Illuminate\Http\Request(true,$fp));
    $result['failed'] = ['status'=>$failed->status,'body'=>$failed->content,'headers'=>$failed->headers->values];
    echo json_encode($result);
}
'''


@unittest.skipUnless(shutil.which('php'), 'PHP required for Runtime/controller contract tests')
class RecoveryKeyHttpTests(unittest.TestCase):
    def test_sensitive_api_auth_signed_identity_attachment_and_safe_failure(self):
        with tempfile.TemporaryDirectory(prefix='backups-recovery-http-') as temporary:
            base = pathlib.Path(temporary)
            app = base / 'app'
            app.mkdir()
            private = base / 'private'
            private.mkdir(mode=0o700)
            script = base / 'runtime.php'
            # PHP cannot declare static and instance methods with the same name.
            source = PHP_RUNTIME.replace("public static function where(...$args) { return new self; }", "public static function __callStatic($name, $args) { return new self; }")
            source = source.replace("public function where($key, $value) { $this->key = $value; return $this; }", "public function __call($name, $args) { if ($name === 'where') { $this->key = $args[1]; } return $this; }")
            script.write_text(source)
            process = subprocess.run(['php', str(script), str(ROOT), str(app), str(private)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr.decode())
            result = json.loads(process.stdout)
        self.assertEqual(result['actor'], 42)
        self.assertEqual(result['signed'], ['filament.admin.pages.backups.recovery-key', 300, {'fingerprint': 'a' * 64}])
        self.assertEqual(result['generic-reveal'], 'bridge-command-invalid')
        self.assertEqual(result['generic-export'], 'bridge-command-invalid')
        self.assertEqual(result['generic-command'], 'sensitive-command-refused')
        self.assertEqual(result['denied'], 'denied-403')
        self.assertTrue(result['denied-before-child'])
        self.assertEqual(result['unsigned'], 'denied-403')
        self.assertEqual(result['route']['middleware'], ['auth', 'signed'])
        self.assertEqual(result['route']['name'], 'backups.recovery-key')
        attachment = result['attachment']
        self.assertEqual(attachment['status'], 200)
        self.assertTrue(attachment['has-secret'])
        self.assertTrue(attachment['has-params'])
        self.assertTrue(attachment['has-instructions'])
        self.assertIn('paymenter-backups-recovery-key-11111111-2222-4333-8444-555555555555.txt', attachment['headers']['Content-Disposition'])
        for response in (attachment, result['failed']):
            self.assertIn('no-store', response['headers']['Cache-Control'])
            self.assertEqual(response['headers']['Referrer-Policy'], 'no-referrer')
        self.assertEqual(result['failed']['status'], 409)
        self.assertNotIn('fixture secret', result['failed']['body'])
