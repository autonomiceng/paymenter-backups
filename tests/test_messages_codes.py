"""Every emitted refusal has safe, specific administrator copy after integration."""
import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')
CODE = re.compile(r'[a-z][a-z0-9]+(?:-[a-z0-9]+)+\Z')

# CLI command names, protocol identifiers and display states are not errors.
# Scanning all code-shaped literals also catches readiness, journal and fallback
# codes that are emitted without a direct BackupError constructor.
NON_ERRORS = {
    # Incident states have their own staff mail copy in Messages::incidentMail.
    'backup-failed', 'backup-overdue', 'scheduler-stopped',
    'archive-key-export', 'artifact-stream', 'credentials-check', 'credentials-merge', 'credentials-rewrap',
    'credentials-write', 'database-and-full-app-checkpoint', 'database-online',
    'delete-interrupted', 'destination-test', 'discard-pending', 'document-roots',
    'enabled-runtime', 'encryption-key', 'entering-maintenance', 'full-quiesced',
    'gpg-agent', 'agent-socket', 'keep-all', 'list-timers', 'list-unit-files', 'list-units', 'local-count',
    'maintenance-probe-credentials-write', 'mariadb-dump', 'needs-recovery', 'needs-setup',
    'no-cache', 'not-saved', 'on-abnormal', 'on-abort', 'on-failure', 'on-success',
    'on-watchdog', 'openpgp-symmetric-aes256-seipd-v1', 'origin-changed',
    'paymenter-backups-private', 'pbkdf2-hmac-sha256-v1', 'plaintext-content-verified',
    'plaintext-v1', 'queue-worker', 'read-settings', 'recovery-document-bound',
    'recovery-key', 'request-worker', 's3-delivery', 's3-verify', 'scope-changed',
    'staging-removal', 'transactional-database-only', 'transactional-database-and-live-app-files', 'untrusted-transport-only',
    'us-east-1', 'utf-8', 'visible-zero',
    'command-not-registered', 'writer-failed', 'writer-output-invalid',
    'native-writer-failed', 'writer-exited-without-output', 'writer-process-failed', 'writer-timeout',
    'actor-login-failed', 'actor-not-authorized',
}


def emitted_codes():
    codes = set()
    trees = {}
    paths = list((ROOT / 'src/paymenter_backups').glob('*.py'))
    paths.append(ROOT / 'extension/Backups/runtime/verify_recovery.py')
    for path in paths:
        tree = ast.parse(path.read_text())
        trees[path.stem] = tree
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CODE.fullmatch(node.value):
                if node.value not in NON_ERRORS:
                    codes.add(node.value)

    # The removal helpers build codes from the actual operation and suffix.
    # Discover operation prefixes from callers, so a new operation cannot be
    # omitted from the message check just because its codes are concatenated.
    copy_prefixes = set()
    local_prefixes = set()
    for node in ast.walk(trees['storage']):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) > 4:
            value = node.args[4]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if node.func.id == 'remove_copy':
                    copy_prefixes.add(value.value)
                elif node.func.id in ('check_local', 'remove_local'):
                    local_prefixes.add(value.value)
    local_prefixes |= copy_prefixes
    for module in ('storage', 'remote_artifacts'):
        for function in trees[module].body:
            if not isinstance(function, ast.FunctionDef):
                continue
            prefixes = (local_prefixes if function.name in ('check_local', 'remove_local') else copy_prefixes)
            for node in ast.walk(function):
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
                        and isinstance(node.left, ast.Name) and node.left.id in ('failure', 'reason')
                        and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
                        and node.right.value.startswith('-')):
                    codes.update(prefix + node.right.value for prefix in prefixes)
    codes.update('remote-copy-' + state for state in ('missing', 'damaged', 'unknown', 'error'))
    codes.update(('cooperative-writers-acknowledged', 'external-state-acknowledged'))

    # Include native Runtime/bridge refusals, bounded fallback codes and command
    # errors. No Laravel, native database, filesystem mutation or transfer runs.
    for path in (ROOT / 'extension/Backups').rglob('*.php'):
        if path.name == 'Messages.php':
            continue
        source = path.read_text()
        expressions = re.findall(r'throw new \\?RuntimeException\(([^;]+)\);', source)
        expressions += re.findall(r'->error\(([^;]+)\);', source)
        expressions += re.findall(r"['\"](?:error|code)['\"]\s*=>\s*['\"]([a-z0-9-]+)['\"]", source)
        for expression in expressions:
            values = re.findall(r"['\"]([a-z][a-z0-9-]+)['\"]", expression)
            if CODE.fullmatch(expression):
                values.append(expression)
            codes.update(value for value in values if CODE.fullmatch(value) and value not in NON_ERRORS)
        if path.name == 'bridge.php':
            match = re.search(r"\$code\s*=.*?\?\s*\$error->getMessage\(\)\s*:\s*'([^']+)'", source)
            if match:
                codes.add(match.group(1))
    return codes


@unittest.skipUnless(PHP, 'PHP required for administrator message contracts')
class EmittedMessageTests(unittest.TestCase):
    def test_tzdata_requirement_is_limited_to_non_utc_schedules(self):
        for relative in ('README.md', 'docs/INSTALL.md'):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text()
                self.assertIn('Only non-UTC schedules need time zone data (`tzdata`).', text)
                self.assertNotIn('GNU gzip, time zone data (`tzdata`)', text)

    def messages(self, codes):
        script = r'''
require $argv[1];
$result = [];
foreach (json_decode(stream_get_contents(STDIN), true) as $code) {
    $result[$code] = [\Paymenter\Extensions\Others\Backups\Messages::known($code),
                      \Paymenter\Extensions\Others\Backups\Messages::error($code)];
}
echo json_encode($result, JSON_THROW_ON_ERROR);
'''
        result = subprocess.run([PHP, '-r', script, str(ROOT / 'extension/Backups/Messages.php')],
                                input=json.dumps(sorted(codes)).encode(), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        return json.loads(result.stdout)

    def test_every_engine_encryption_readiness_and_php_error_has_specific_copy(self):
        codes = emitted_codes()
        self.assertIn('staging-removal-local-verification-failed', codes)
        self.assertIn('staging-removal-local-delete-failed', codes)
        self.assertIn('retention-remote-delete-failed', codes)
        self.assertIn('delete-remote-scope-changed', codes)
        self.assertIn('bridge-unavailable', codes)
        self.assertIn('php-binary-mismatch', codes)
        messages = self.messages(codes)
        missing = sorted(code for code, (known, _) in messages.items() if not known)
        self.assertEqual(missing, [], 'Emitted refusals need administrator copy')
        for code, (_, text) in messages.items():
            with self.subTest(code=code):
                self.assertTrue(text.strip())
                self.assertNotIn('unrecognized error', text)
                self.assertNotIn(code, text)

    def test_unknown_errors_never_echo_driver_details_or_code_shaped_secrets(self):
        secrets = ('fake-secret-that-matches-code-pattern', 'fixture@example.test password=secret',
                   '/private/app/credentials.json', '<script>alert(1)</script>')
        for code, (known, text) in self.messages(secrets).items():
            self.assertFalse(known)
            self.assertNotIn(code, text)
            self.assertIn('unrecognized error', text)

    def test_database_driver_and_database_name_errors_are_distinct(self):
        source = (ROOT / 'extension/Backups/Messages.php').read_text()
        driver = re.search(r"'database-driver-unsupported'\s*=>\s*'([^']*)'", source).group(1)
        name = re.search(r"'unsupported-database'\s*=>\s*'([^']*)'", source).group(1)
        self.assertNotEqual(driver, name)
        self.assertIn('driver', driver.lower())
        self.assertIn('database name', name.lower())

    def test_bridge_reports_php_mysql_driver_and_connection_failures(self):
        messages = self.messages(('database-pdo-driver-unavailable', 'database-connection-failed'))
        self.assertIn('PDO MySQL', messages['database-pdo-driver-unavailable'][1])
        self.assertIn('could not connect', messages['database-connection-failed'][1])

    def test_writer_cause_copy_is_formatted_by_messages(self):
        script = r'''
require $argv[1];
echo json_encode([
    \Paymenter\Extensions\Others\Backups\Messages::withWriterCause('Settings were not saved.', 'busy'),
    \Paymenter\Extensions\Others\Backups\Messages::withWriterCause('Settings were not saved.', null),
    \Paymenter\Extensions\Others\Backups\Messages::withWriterCause(
        \Paymenter\Extensions\Others\Backups\Messages::error('credentials-unreadable'),
        'credentials-unreadable',
        'credentials-unreadable'
    ),
], JSON_THROW_ON_ERROR);
'''
        result = subprocess.run([PHP, '-r', script, str(ROOT / 'extension/Backups/Messages.php')],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertEqual(json.loads(result.stdout), [
            'Settings were not saved. Writer cause: busy.',
            'Settings were not saved.',
            'Saved keys can\'t be read. Restore the matching APP_KEY or APP_PREVIOUS_KEYS and keep credentials.json. Replacing it can lose keys for earlier S3 backups.',
        ])

    def test_save_errors_keep_the_field_and_have_direct_repair_guidance(self):
        script = r'''
require $argv[1];
require $argv[2];
require $argv[3];
function filled($value) { return !($value === null || $value === '' || $value === false || $value === []); }
$result = [
    'config' => \Paymenter\Extensions\Others\Backups\Messages::error('config-invalid'),
    'timezone' => \Paymenter\Extensions\Others\Backups\Messages::error('timezone-data-unavailable'),
    'gzip' => \Paymenter\Extensions\Others\Backups\Messages::readinessItem('gzip-busybox', [])['text'],
    'details' => \Paymenter\Extensions\Others\Backups\Runtime::errorDetails('{"error":"config-invalid","field":"schedule_time","detail":{"value":"secret"}}'),
    'database_cause' => \Paymenter\Extensions\Others\Backups\Runtime::errorDetails('{"error":"config-write-outcome-unknown","detail":{"writer_cause":"database-pdo-driver-unavailable"}}'),
    'timezone_cause' => \Paymenter\Extensions\Others\Backups\Runtime::errorDetails('{"error":"config-invalid","field":"schedule_timezone","detail":{"writer_cause":"validation:schedule_timezone:timezone"}}'),
    'unknown_cause' => \Paymenter\Extensions\Others\Backups\Runtime::errorDetails('{"error":"config-write-outcome-unknown","detail":{"writer_cause":"arbitrary-sensitive-text"}}'),
    'unknown_code' => \Paymenter\Extensions\Others\Backups\Runtime::errorDetails('{"error":"app"}'),
];
echo json_encode($result, JSON_THROW_ON_ERROR);
'''
        result = subprocess.run([PHP, '-r', script, str(ROOT / 'extension/Backups/Messages.php'),
                                 str(ROOT / 'extension/Backups/Runtime.php'), str(ROOT / 'extension/Backups/Settings.php')],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        value = json.loads(result.stdout)
        self.assertNotIn('Open Settings and save', value['config'])
        self.assertNotIn('highlighted setting', value['config'])
        self.assertNotIn('Open Settings and save', self.messages(('schedule-field-invalid',))['schedule-field-invalid'][1])
        self.assertNotIn('highlighted setting', self.messages(('schedule-field-invalid',))['schedule-field-invalid'][1])
        self.assertIn('tzdata', value['timezone'])
        self.assertEqual(value['gzip'], 'gzip on this server is BusyBox; Backups needs GNU gzip')
        self.assertEqual(value['details'], {'code': 'config-invalid', 'field': 'schedule_time', 'cause': None})
        self.assertEqual(value['database_cause']['cause'], 'database-pdo-driver-unavailable')
        self.assertEqual(value['timezone_cause']['cause'], 'validation:schedule_timezone:timezone')
        self.assertEqual(value['unknown_cause']['cause'], 'unrecognized')
        self.assertEqual(value['unknown_code']['code'], 'engine-unavailable')

    def test_cause_free_save_refusal_survives_warning_to_exception_handler(self):
        result = subprocess.run([PHP, str(ROOT / 'tests/integration/runtime_cause_free_refusal.php')],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.decode(), 'RuntimeException:busy')

    def test_scheduler_wait_copy_and_link_are_owned_by_messages(self):
        script = r'''
namespace Illuminate\Support {
    final class HtmlString { public function __construct(private string $value) {} public function __toString(): string { return $this->value; } }
}
namespace {
require $argv[1];
echo (string) \Paymenter\Extensions\Others\Backups\Messages::schedulerWaitLine('/admin/backups?x="&');
}
'''
        result = subprocess.run([PHP, '-r', script, str(ROOT / 'extension/Backups/Messages.php')],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        self.assertIn("Waiting for Paymenter's scheduled tasks.", result.stdout.decode())
        self.assertIn('href="/admin/backups?x=&quot;&amp;"', result.stdout.decode())
        self.assertIn('>the Backups tab</a>', result.stdout.decode())

    def test_engine_field_reaches_the_save_form_error(self):
        result = subprocess.run([PHP, str(ROOT / 'tests/integration/save_refusal_checks.php')],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        value = json.loads(result.stdout)
        self.assertEqual(value['details'], {'code': 'config-invalid', 'field': 'schedule_time',
                                            'cause': 'actor-login-failed'})
        self.assertIn('settings.schedule_time', value['errors'])
        self.assertIn('Writer cause: actor-login-failed.', value['notification']['body'])
        self.assertNotIn('highlighted setting', value['errors']['settings.schedule_time'])
        self.assertNotIn('highlighted setting', value['notification']['body'])
        self.assertEqual(value['credentials_notification'], {
            'title': 'Settings not saved',
            'status': 'danger',
            'body': self.messages(('credentials-unreadable',))['credentials-unreadable'][1],
        })
        self.assertFalse(value['credentials_errors'])
        self.assertNotIn('Open Settings and save', value['errors']['settings.schedule_time'])
        self.assertNotIn('Open Settings and save', value['notification']['body'])
        self.assertIn('Backups settings command', value['command_notification']['body'])
        self.assertIn('database', value['command_notification']['body'])

    def test_missing_writer_command_is_logged_and_has_save_copy(self):
        script = r'''
namespace Illuminate\Support\Facades {
    final class Log {
        public static array $calls = [];
        public static function warning(string $message, array $context = []): void {
            self::$calls[] = [$message, $context];
        }
    }
}
namespace {
require $argv[1];
require $argv[2];
require $argv[3];
$details = \Paymenter\Extensions\Others\Backups\Runtime::recordNativeWriterFailure(
    '{"error":"command-not-registered","detail":{"writer_cause":"command-not-registered"}}'
);
$redacted = \Paymenter\Extensions\Others\Backups\Runtime::errorDetails(json_encode([
    'error' => 'config-write-outcome-unknown',
    'detail' => ['writer_cause' => 'PDO failed password=secret-value username=fixture-user'],
], JSON_THROW_ON_ERROR));
$loggedRedaction = \Paymenter\Extensions\Others\Backups\Runtime::recordNativeWriterFailure(json_encode([
    'error' => 'config-write-outcome-unknown',
    'detail' => ['writer_cause' => 'PDO failed password=secret-value username=fixture-user'],
], JSON_THROW_ON_ERROR));
$actorFailure = \Paymenter\Extensions\Others\Backups\Runtime::recordNativeWriterFailure(
    '{"error":"config-invalid","detail":{"writer_cause":"actor-not-authorized"}}'
);
echo json_encode([
    'details' => $details,
    'redacted' => $redacted,
    'actor_failure' => $actorFailure,
    'logs' => \Illuminate\Support\Facades\Log::$calls,
    'message' => \Paymenter\Extensions\Others\Backups\Messages::error('command-not-registered'),
], JSON_THROW_ON_ERROR);
}
'''
        result = subprocess.run([PHP, '-r', script, str(ROOT / 'extension/Backups/Runtime.php'),
                                 str(ROOT / 'extension/Backups/Settings.php'),
                                 str(ROOT / 'extension/Backups/Messages.php')],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout.decode() + result.stderr.decode())
        value = json.loads(result.stdout)
        self.assertEqual(value['details'], {'code': 'command-not-registered', 'field': None,
                                            'cause': 'command-not-registered'})
        self.assertEqual(value['logs'][0][1], {'code': 'native-writer-failed',
                                               'cause': 'command-not-registered'})
        self.assertEqual(value['logs'][1][1], {'code': 'native-writer-failed',
                                               'cause': 'unrecognized'})
        self.assertEqual(value['actor_failure'], {'code': 'config-invalid', 'field': None,
                                                  'cause': 'actor-not-authorized'})
        self.assertEqual(value['logs'][2][1], {'code': 'native-writer-failed',
                                               'cause': 'actor-not-authorized'})
        self.assertEqual(value['redacted']['cause'], 'unrecognized')
        self.assertNotIn('secret-value', json.dumps(value['logs']))
        self.assertNotIn('fixture-user', json.dumps(value['logs']))
        self.assertIn('Backups', value['message'])
        self.assertIn('database', value['message'])

    def test_frozen_upload_refusals_do_not_promise_regeneration_or_reencryption(self):
        messages = self.messages(('delivery-ciphertext-unavailable', 'legacy-plaintext-delivery-blocked',
                                  'credentials-unreadable'))
        cipher = messages['delivery-ciphertext-unavailable'][1].lower()
        legacy = messages['legacy-plaintext-delivery-blocked'][1].lower()
        self.assertIn('exact file', cipher)
        self.assertIn('server administrator', cipher)
        self.assertIn("can't recreate", cipher)
        self.assertIn('unencrypted', legacy)
        self.assertIn('allow unencrypted uploads', legacy)
        self.assertIn("can't change that", legacy)
        self.assertNotIn('upload again', cipher + legacy)
        credential = messages['credentials-unreadable'][1]
        self.assertIn('keep credentials.json', credential)
        self.assertIn('earlier S3 backups', credential)


if __name__ == '__main__':
    unittest.main()
