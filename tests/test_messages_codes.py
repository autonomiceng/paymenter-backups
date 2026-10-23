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
    'staging-removal', 'transactional-database-only', 'untrusted-transport-only',
    'us-east-1', 'utf-8', 'visible-zero',
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
            codes.update(value for value in values if CODE.fullmatch(value))
        if path.name == 'bridge.php':
            match = re.search(r"\$code\s*=.*?\?\s*\$error->getMessage\(\)\s*:\s*'([^']+)'", source)
            if match:
                codes.add(match.group(1))
    return codes


@unittest.skipUnless(PHP, 'PHP required for administrator message contracts')
class EmittedMessageTests(unittest.TestCase):
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
        self.assertEqual(result.returncode, 0, result.stderr.decode())
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

    def test_frozen_upload_refusals_do_not_promise_regeneration_or_reencryption(self):
        messages = self.messages(('delivery-ciphertext-unavailable', 'legacy-plaintext-delivery-blocked',
                                  'credentials-unreadable'))
        cipher = messages['delivery-ciphertext-unavailable'][1].lower()
        legacy = messages['legacy-plaintext-delivery-blocked'][1].lower()
        self.assertIn('exact bytes', cipher)
        self.assertIn('operator', cipher)
        self.assertIn('cannot recreate', cipher)
        self.assertIn('unencrypted', legacy)
        self.assertIn('explicitly authorize', legacy)
        self.assertIn('cannot change its format', legacy)
        self.assertNotIn('upload again', cipher + legacy)
        credential = messages['credentials-unreadable'][1]
        self.assertIn('keep credentials.json', credential)
        self.assertIn('earlier S3 backups', credential)


if __name__ == '__main__':
    unittest.main()
