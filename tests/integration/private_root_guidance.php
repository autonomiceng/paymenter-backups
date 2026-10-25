<?php
/** Page behavior with native rows and process responses supplied at their boundaries. */
namespace App\Models {
    final class Extension
    {
        public static array $rows = [];
        public static function where(...$arguments): ExtensionQuery { return new ExtensionQuery; }
        public function settings(): SettingQuery { return new SettingQuery; }
    }
    final class ExtensionQuery
    {
        public function where(...$arguments): self { return $this; }
        public function firstOrFail(): Extension { return new Extension; }
    }
    final class SettingQuery
    {
        private ?string $key = null;
        public function where(string $column, string $value): self { $this->key = $value; return $this; }
        public function value(string $column): mixed { return Extension::$rows[$this->key] ?? null; }
    }
}
namespace Filament\Notifications {
    final class Notification
    {
        public static array $last = [];
        private array $values = [];
        public static function make(): self { return new self; }
        public function __call(string $name, array $arguments): self { $this->values[$name] = $arguments[0] ?? true; return $this; }
        public function send(): void { self::$last = $this->values; }
    }
}
namespace Paymenter\Extensions\Others\Backups {
    final class Runtime
    {
        public static string $failure = '';
        public static function privateRoot(): string { return PrivateStore::root(\base_path(), \App\Models\Extension::$rows['private_root'] ?: null); }
        public static function command(array $command, mixed ...$arguments): array { return ['initialized' => false]; }
        public static function saveConfiguration(mixed ...$arguments): array
        {
            chmod(PrivateStore::defaultPath(\base_path()), 0000);
            clearstatcache();
            if (self::$failure === 'throw') { throw new \RuntimeException('private-root-invalid'); }
            return ['updated' => false, 'error' => 'private-root-invalid'];
        }
        public static function safeWriterCause(mixed $cause): ?string { return null; }
    }
}
namespace {
    $input = json_decode(file_get_contents('php://stdin'), true, 32, JSON_THROW_ON_ERROR);
    function base_path(): string { global $input; return $input['app']; }
    foreach (['PrivateStore', 'Settings', 'Messages'] as $name) { require __DIR__ . '/../../extension/Backups/' . $name . '.php'; }
    foreach (['LoadsOverview', 'SavesSettings'] as $name) { require __DIR__ . '/../../extension/Backups/Admin/Pages/Concerns/' . $name . '.php'; }
    final class RootGuidancePage
    {
        use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\LoadsOverview;
        use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\SavesSettings;
        private const BOOTSTRAP = ['private_root', 'extra_docroots', 'php', 'python'];
        private const ENVIRONMENT_ERRORS = [];
        private ?array $overviewCache = null;
        private ?string $overviewError = null;
        private ?string $overviewErrorCause = null;
        private ?array $storedForm = null;
        private array $storedRows = [];
        private int $storedVersion = 2;
        private array $malformedKeys = [];
        private ?string $publicDirectoriesRepairPin = null;
        private array $secretsCleared = [];
        public array $errors = [];
        public function __get(string $name): object { return new class { public function fill(array $values): void {} }; }
        private function authorizeAction(): void {}
        private function validatedInput(): array { global $input; return [$input['changes'], null, null, null]; }
        private function formValues(): array { return array_replace(\Paymenter\Extensions\Others\Backups\Settings::DEFAULTS, \App\Models\Extension::$rows, ['extra_docroots' => []]); }
        private function engineValue(string $key, mixed $value): mixed { return $value; }
        private function changed(string $key, mixed $value): bool { return $value !== $this->formValues()[$key]; }
        public function addError(string $key, string $message): void { $this->errors[$key] = $message; }
        public function dispatch(mixed ...$arguments): void {}
        public function getId(): string { return 'root-guidance'; }
        public function inspect(): array { return ['overview' => $this->overview(), 'problem' => $this->privateRootProblem(), 'initialized' => $this->initialized()]; }
    }
    \App\Models\Extension::$rows = ['private_root' => $input['configured'], 'extra_docroots' => '[]'];
    \Paymenter\Extensions\Others\Backups\Runtime::$failure = $input['failure'] ?? '';
    $page = new RootGuidancePage;
    if ($input['action'] === 'save') { $page->saveSettings(); }
    $result = $page->inspect();
    $result['message'] = $result['problem'] === null ? null
        : \Paymenter\Extensions\Others\Backups\Messages::privateRootProblem($result['problem']);
    $result['errors'] = $page->errors;
    $result['notification'] = \Filament\Notifications\Notification::$last;
    echo json_encode($result, JSON_THROW_ON_ERROR);
}
