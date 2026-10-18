"""Exercise actual page policy consumers without booting Laravel or capturing."""
import json
import pathlib
import shutil
import subprocess

from test_engine_schedules import PrivateFixture

from paymenter_backups import engine, presentation
from paymenter_backups.readiness import state_for


ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = shutil.which('php')
PROGRAM = r'''
namespace Filament\Pages {
    class Page {
        public array $forms = [];
        public function __get($name) { return $this->forms[$name]; }
        public function getId() { return 'isolated'; }
        public function dispatch(...$args) {}
    }
}
namespace Filament\Tables\Contracts { interface HasTable {} }
namespace Filament\Tables\Concerns {
    trait InteractsWithTable {
        public function table(\Filament\Tables\Table $table): \Filament\Tables\Table { return $table; }
    }
}
namespace Filament\Actions {
    class Action {
        public array $callbacks = [];
        public static function make(...$args) { return new static; }
        public function __call($name, $args) { $this->callbacks[$name] = $args[0] ?? null; return $this; }
        public function halt() { throw new \RuntimeException('halted'); }
    }
}
namespace Filament\Forms\Components { class Checkbox extends \Filament\Actions\Action {} }
namespace Filament\Notifications { class Notification extends \Filament\Actions\Action {} }
namespace Filament\Schemas { class Schema { public function fill($state) {} } }
namespace Filament\Support\Enums { enum Size { case Small; } }
namespace Illuminate\Support\Facades {
    class Auth {
        public static function user() { return new class { public function hasPermission($permission) { return true; } }; }
        public static function id() { return 1; }
    }
}
namespace Illuminate\Validation {
    class ValidationException extends \RuntimeException {
        public array $messages;
        public static function withMessages($messages) { $error = new static; $error->messages = $messages; return $error; }
        public function errors() { return $this->messages; }
    }
}
namespace Paymenter\Extensions\Others\Backups {
    class Runtime {
        public static array $calls = [];
        public static function encryptionKey($action, $input) { self::$calls[] = [$action, $input]; return []; }
        public static function command(...$args) { self::$calls[] = $args; throw new \RuntimeException('unexpected-write'); }
        public static ?string $fingerprint = null;
        public static array $revealResponse = [];
        public static function recoveryKeyFingerprint() { return self::$fingerprint; }
        public static function revealRecoveryKey($fingerprint = null) {
            self::$calls[] = ['reveal', ['fingerprint' => $fingerprint]];
            return self::$revealResponse;
        }
    }
}
namespace {
function session() {
    static $session;
    return $session ??= new class {
        public array $values = [];
        public function put($key, $value) { $this->values[$key] = $value; }
        public function pull($key) { $value = $this->values[$key] ?? null; unset($this->values[$key]); return $value; }
    };
}
function filled($value) { return $value !== null && $value !== '' && $value !== []; }
function abort_unless($condition, $status) { if (!$condition) { throw new \RuntimeException('unauthorized'); } }
require $argv[1] . '/extension/Backups/Settings.php';
require $argv[1] . '/extension/Backups/Messages.php';
require $argv[1] . '/extension/Backups/ArchiveEncryption.php';
require $argv[1] . '/extension/Backups/ProbeCredentials.php';
require $argv[1] . '/extension/Backups/CredentialInput.php';
foreach (glob($argv[1] . '/extension/Backups/Admin/Pages/Concerns/*.php') as $concern) { require $concern; }
require $argv[1] . '/extension/Backups/Admin/Pages/Backups.php';
$input = json_decode(file_get_contents('php://stdin'), true, 64, JSON_THROW_ON_ERROR);
$class = new ReflectionClass(Paymenter\Extensions\Others\Backups\Admin\Pages\Backups::class);
$page = $class->newInstanceWithoutConstructor();
$overview = ['initialized' => true, 'configuration' => $input['configuration']];
foreach (['overviewCache' => $overview, 'setupCacheFor' => $overview,
          'setupCache' => $input['groups'] ?? ['all' => [], 'database-online' => [], 'full-quiesced' => []],
          'storedForm' => Paymenter\Extensions\Others\Backups\Settings::DEFAULTS] as $name => $value) {
    $class->getProperty($name)->setValue($page, $value);
}
foreach ($input['properties'] ?? [] as $name => $value) { $class->getProperty($name)->setValue($page, $value); }
foreach ($input['forms'] ?? [] as $name => $state) {
    $page->forms[$name] = new class($state) {
        public function __construct(public array $state) {}
        public function getState() { return $this->state; }
    };
}
if ($input['method'] === 'reveal-flow') {
    \Paymenter\Extensions\Others\Backups\Runtime::$fingerprint = $input['confirmed_fingerprint'];
    $class->getMethod('grantReveal')->invoke($page);
    $class->getMethod('revealAllowed')->invoke($page);
    if (array_key_exists('current_fingerprint', $input)) {
        \Paymenter\Extensions\Others\Backups\Runtime::$fingerprint = $input['current_fingerprint'];
    }
    \Paymenter\Extensions\Others\Backups\Runtime::$revealResponse = $input['reveal_response'];
    $allowed = $class->getMethod('revealAllowed')->invoke($page);
    $key = $class->getMethod('revealKey')->invoke($page);
    $result = ['allowed' => $allowed, 'grant' => $page->revealGrantKey, 'key' => $key,
               'calls' => \Paymenter\Extensions\Others\Backups\Runtime::$calls];
} elseif ($input['method'] === 'acknowledge-flow') {
    $action = $page->acknowledgeRecoveryKeyAction();
    $halted = false;
    try {
        $action->callbacks['mountUsing']($action, new \Filament\Schemas\Schema);
        if (isset($input['changed_configuration'])) {
            $class->getProperty('overviewCache')->setValue($page, ['initialized' => true, 'configuration' => $input['changed_configuration']]);
        }
        $action->callbacks['action']($action);
    } catch (\RuntimeException $error) {
        if ($error->getMessage() !== 'halted') { throw $error; }
        $halted = true;
    }
    $result = ['halted' => $halted, 'frozen' => $page->acknowledgementFingerprint,
               'calls' => \Paymenter\Extensions\Others\Backups\Runtime::$calls];
} else {
    $arguments = $input['method'] === 'blockers' ? [$input['mode'] ?? 'database-online'] : [];
    try { $result = $class->getMethod($input['method'])->invokeArgs($page, $arguments); }
    catch (\Illuminate\Validation\ValidationException $error) {
        $result = ['validation_errors' => $error->errors(), 'calls' => \Paymenter\Extensions\Others\Backups\Runtime::$calls];
    }
}
echo json_encode($result, JSON_THROW_ON_ERROR);
}
'''


class PageEngineContracts(PrivateFixture):
    def page(self, configuration, method='blockers', groups=None, **extra):
        result = subprocess.run([PHP, '-r', PROGRAM, str(ROOT)],
                                input=json.dumps({'configuration': configuration, 'method': method,
                                                  'groups': groups, **extra}), text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def configuration(self, **changes):
        self.settings.update(upload_s3=True, destination='s3', s3_encryption=True)
        self.envelope['tools']['gpg'] = '/usr/bin/gpg'
        result = engine.plan(self.state, self.envelope, probe_locks=False)
        return {'available': True, 'valid': True,
                'admission': {'database-online': result['admission_errors']},
                'readiness': {'database-online': {'state': state_for(result['readiness_items']),
                                                 'items': result['readiness_items']}}, **changes}

    def test_missing_recovery_key_informs_without_blocking_local_capture(self):
        configuration = self.configuration()
        self.assertEqual(configuration['admission']['database-online'], [])
        self.assertEqual(configuration['readiness']['database-online']['state'], 'needs-setup')
        self.assertEqual(configuration['readiness']['database-online']['items'][0]['code'], 'archive-key-required')
        self.assertEqual(self.page(configuration), [])

    def test_malformed_encryption_key_informs_without_blocking_local_capture(self):
        self.envelope['archive_encryption_credentials'] = {'version': 99}
        configuration = self.configuration()
        self.assertEqual(configuration['admission']['database-online'], [])
        self.assertEqual(configuration['readiness']['database-online']['items'][0]['code'], 'archive-key-invalid')
        self.assertEqual(self.page(configuration), [])

    def test_capture_refusal_is_named_even_with_delivery_readiness(self):
        self.settings['no_concurrent_ddl_acknowledged'] = False
        configuration = self.configuration()
        blocked = self.page(configuration)
        self.assertEqual([item['code'] for item in blocked], ['no-concurrent-ddl-acknowledgement-required'])

    def test_capture_wait_can_queue_with_missing_key(self):
        configuration = self.configuration()
        configuration['admission']['database-online'] = ['busy', 'billing-guard-stale']
        self.assertEqual(self.page(configuration), [])
        configuration['admission']['full-quiesced'] = ['billing-guard-stale']
        configuration['readiness']['full-quiesced'] = {'state': 'waiting', 'items': [{'code': 'billing-guard-stale'}]}
        self.assertEqual(self.page(configuration, mode='full-quiesced'), [])

    def test_page_initialization_precondition_survives_empty_admission(self):
        groups = {'all': [{'code': 'installation-not-initialized', 'page': True}],
                  'database-online': [], 'full-quiesced': []}
        self.assertEqual(self.page(self.configuration(), groups=groups), groups['all'])

    def test_malformed_admission_and_unexplained_legacy_readiness_fail_closed(self):
        for admission in ('invalid', [123], {'invalid': 'code'}):
            with self.subTest(admission=admission):
                configuration = self.configuration(admission={'database-online': admission})
                self.assertEqual(self.page(configuration)[0]['code'], 'config-invalid')
        configuration = self.configuration(admission={})
        configuration['readiness']['database-online']['items'] = []
        self.assertEqual(self.page(configuration)[0]['code'], 'config-invalid')

    def test_billing_window_consumes_engine_clock_times_including_midnight(self):
        for daily in ('02:00', '00:01'):
            with self.subTest(daily=daily):
                window = presentation.billing_window(self.settings, {'daily_time': daily, 'timezone': 'UTC'})
                configuration = self.configuration(checks={'billing': window})
                actual = self.page(configuration, 'billingWindow')
                self.assertEqual(actual, {'time': daily, 'timezone': 'UTC',
                                          'start': window['window_before'], 'end': window['window_after']})

    def test_native_migration_adapter_matches_shared_vectors(self):
        result = subprocess.run([PHP, str(ROOT / 'tests/integration/settings_migration.php')],
                                text=True, capture_output=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), {'settings_migration_vectors': 11})

    def pending_configuration(self, fingerprint):
        return {'available': True, 'valid': True, 'encryption': {'key': {
            'state': 'pending', 'short': fingerprint[:16], 'pending_short': fingerprint[:16],
            'pending_fingerprint': fingerprint}}}

    def test_acknowledgement_submits_only_the_mounted_key_identity(self):
        original, replacement = 'a' * 64, 'b' * 64
        for shown in (None, original):
            with self.subTest(shown=shown):
                result = self.page(self.pending_configuration(original), 'acknowledge-flow',
                                   properties={'recoveryFingerprint': shown},
                                   changed_configuration=self.pending_configuration(replacement))
                self.assertTrue(result['halted'])
                self.assertEqual(result['frozen'], original)
                self.assertEqual(result['calls'], [])
        result = self.page(self.pending_configuration(original), 'acknowledge-flow',
                           properties={'recoveryFingerprint': original})
        self.assertFalse(result['halted'])
        self.assertEqual(result['calls'], [['acknowledge', {'fingerprint': original}]])

    def test_reveal_grant_freezes_full_identity_and_refuses_replacement_or_disappearance(self):
        original = 'a' * 64
        replacement = original[:16] + 'b' * 48  # Equal short identities are not authority.
        response = {'secret': 'synthetic-replacement-secret', 'fingerprint': replacement}
        for current in (replacement, None):
            with self.subTest(current=current):
                result = self.page(self.pending_configuration(original), 'reveal-flow',
                                   confirmed_fingerprint=original, current_fingerprint=current,
                                   reveal_response=response)
                self.assertFalse(result['allowed'])
                self.assertEqual(result['grant'], original)
                self.assertEqual(result['calls'], [])
                self.assertNotIn('secret', result['key'])

    def test_reveal_checks_identity_returned_after_the_permission_check(self):
        original, replacement = 'a' * 64, 'b' * 64
        result = self.page(self.pending_configuration(original), 'reveal-flow',
                           confirmed_fingerprint=original,
                           reveal_response={'secret': 'synthetic-replacement-secret', 'fingerprint': replacement})
        self.assertTrue(result['allowed'])
        self.assertEqual(result['key'], {'error': 'archive-key-mismatch'})
        self.assertEqual(result['calls'], [['reveal', {'fingerprint': original}]])
        result = self.page(self.pending_configuration(original), 'reveal-flow',
                           confirmed_fingerprint=original,
                           reveal_response={'secret': 'synthetic-confirmed-secret', 'fingerprint': original})
        self.assertEqual(result['grant'], original)
        self.assertEqual(result['key']['fingerprint'], original)
        self.assertEqual(result['key']['secret'], 'synthetic-confirmed-secret')

    def test_missing_or_changed_pending_metadata_cannot_reveal_or_acknowledge(self):
        original, replacement = 'a' * 64, 'b' * 64
        configuration = self.pending_configuration(original)
        del configuration['encryption']['key']['pending_fingerprint']
        result = self.page(configuration, 'acknowledge-flow')
        self.assertTrue(result['halted'])
        self.assertEqual(result['calls'], [])
        result = self.page(self.pending_configuration(replacement), 'acknowledge-flow',
                           properties={'recoveryFingerprint': original})
        self.assertTrue(result['halted'])
        self.assertEqual(result['calls'], [])

    def submitted_secrets(self, passphrase=' correct horse battery staple ', s3=None, site=None):
        settings = {'upload_s3': True, 's3_encryption': True}
        s3 = {} if s3 is None else s3
        site = {} if site is None else site
        return {'properties': {'settings': settings, 's3Keys': s3, 'siteAccess': site,
                               'encryptionInput': {'source': 'passphrase', 'passphrase': passphrase}},
                'forms': {'settingsForm': settings, 's3KeysForm': s3,
                          'siteAccessForm': site, 'encryptionForm': {'passphrase': passphrase}}}

    def test_byte_and_unicode_control_passphrase_errors_precede_settings_writes(self):
        configuration = self.configuration(app_origin='https://synthetic.invalid')
        for passphrase in ('é' * 600, 'word ' * 8 + '\u0085'):
            with self.subTest(passphrase_bytes=len(passphrase.encode())):
                result = self.page(configuration, 'saveSettings', **self.submitted_secrets(passphrase))
                self.assertIn('encryptionInput.passphrase', result['validation_errors'])
                self.assertEqual(result['calls'], [])
                self.assertNotIn(passphrase, json.dumps(result, ensure_ascii=False))

    def test_all_credential_byte_errors_are_reported_before_settings_writes(self):
        configuration = self.configuration(app_origin='https://synthetic.invalid')
        result = self.page(configuration, 'saveSettings', **self.submitted_secrets(
            s3={'access_key': 'é' * 5000, 'secret_key': 'synthetic-secret'},
            site={'username': 'synthetic-user', 'password': 'é' * 5000}))
        self.assertIn('s3Keys.access_key', result['validation_errors'])
        self.assertIn('siteAccess.password', result['validation_errors'])
        self.assertEqual(result['calls'], [])

    def test_aggregate_bridge_json_limit_is_checked_before_settings_writes(self):
        configuration = self.configuration(app_origin='https://synthetic.invalid')
        secret = 'é' * 3000  # 6000 bytes per field; JSON escaping triples it.
        result = self.page(configuration, 'saveSettings', **self.submitted_secrets(
            s3={'access_key': secret, 'secret_key': secret, 'session_token': secret},
            site={'username': secret, 'password': secret}))
        self.assertIn('s3Keys.session_token', result['validation_errors'])
        self.assertIn('siteAccess.password', result['validation_errors'])
        self.assertEqual(result['calls'], [])

    def test_valid_secret_bytes_are_preserved_by_page_validation(self):
        configuration = self.configuration(app_origin='https://synthetic.invalid')
        passphrase = '  correct horse battery staple  '
        s3 = {'access_key': 'é' * 50, 'secret_key': ' secret bytes '}
        site = {'username': 'synthetic-user', 'password': ' password bytes '}
        result = self.page(configuration, 'validatedInput', **self.submitted_secrets(passphrase, s3, site))
        self.assertEqual(result[1], s3)
        self.assertEqual(result[2], dict(site, origin='https://synthetic.invalid'))
        self.assertEqual(result[3]['passphrase'], passphrase)
