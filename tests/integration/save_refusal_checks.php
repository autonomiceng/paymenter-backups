<?php
namespace Filament\Notifications {
    final class Notification
    {
        public static array $last = [];
        private array $record = [];
        public static function make(): self { return new self(); }
        public function title(string $value): self { $this->record['title'] = $value; return $this; }
        public function danger(): self { $this->record['status'] = 'danger'; return $this; }
        public function body(string $value): self { $this->record['body'] = $value; return $this; }
        public function send(): void { self::$last = $this->record; }
    }
}

namespace {
    require __DIR__ . '/../../extension/Backups/Messages.php';
    require __DIR__ . '/../../extension/Backups/Settings.php';
    require __DIR__ . '/../../extension/Backups/Runtime.php';
    require __DIR__ . '/../../extension/Backups/Admin/Pages/Concerns/SavesSettings.php';

    final class SaveRefusalProbe
    {
        use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\SavesSettings;

        public array $errors = [];
        public function addError(string $field, string $message): void { $this->errors[$field] = $message; }
        public function dispatch(string $event, mixed ...$parameters): void {}
        public function getId(): string { return 'save-refusal-test'; }
        public function refuse(string $code, ?string $field, ?string $cause = null): void { $this->refuseSettings($code, false, $field, $cause); }
    }

    $details = \Paymenter\Extensions\Others\Backups\Runtime::errorDetails(
        '{"error":"config-invalid","field":"schedule_time","detail":{"writer_cause":"actor-login-failed"}}'
    );
    $probe = new SaveRefusalProbe();
    $probe->refuse($details['code'], $details['field'], $details['cause'] ?? null);
    $fieldNotification = \Filament\Notifications\Notification::$last;
    $probe->refuse('command-not-registered', null);
    $commandNotification = \Filament\Notifications\Notification::$last;
    $credentialsDetails = \Paymenter\Extensions\Others\Backups\Runtime::errorDetails('{"error":"credentials-unreadable"}');
    $credentialsProbe = new SaveRefusalProbe();
    $credentialsProbe->refuse($credentialsDetails['code'], $credentialsDetails['field'], $credentialsDetails['cause']);
    $credentialsNotification = \Filament\Notifications\Notification::$last;
    echo json_encode([
        'details' => $details,
        'errors' => $probe->errors,
        'notification' => $fieldNotification,
        'command_notification' => $commandNotification,
        'credentials_errors' => $credentialsProbe->errors,
        'credentials_notification' => $credentialsNotification,
    ], JSON_THROW_ON_ERROR);
}
