<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Notifications\Notification;
use Filament\Schemas\Components\Form;
use Illuminate\Validation\ValidationException;
use Paymenter\Extensions\Others\Backups\ArchiveEncryption;
use Paymenter\Extensions\Others\Backups\CredentialInput;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Settings;

trait ValidatesSettingsInput
{
    /** Form values from native rows. A malformed row falls back to its default. */
    private function formValues(): array
    {
        if ($this->storedForm !== null) { return $this->storedForm; }
        $rows = []; $this->storedRows = [];
        foreach ($this->record()->settings()->get() as $row) {
            $rows[$row->key] = $row->value;
            $this->storedRows[$row->key] = $row->getAttributes();
        }
        ksort($this->storedRows);
        $this->malformedKeys = [];
        $this->storedVersion = Settings::parse('settings_version', $rows['settings_version'] ?? null) ?? 1;
        $invalid = function (string $key) use ($rows): bool {
            if (!array_key_exists($key, $rows)) { return false; }
            $value = $rows[$key];
            if ($value === null && is_string(Settings::DEFAULTS[$key]) && !isset(Settings::SELECTS[$key]) && !in_array($key, Settings::TIME_KEYS, true)) { return false; }
            if (Settings::parse($key, $value) === null) { return true; }
            return \Illuminate\Support\Facades\Validator::make([$key => $value], [$key => Settings::rules($key)])->fails();
        };
        // A malformed public-directory guard is never defaulted.
        try { Settings::normalize(array_intersect_key($rows, ['extra_docroots' => true])); }
        catch (\Throwable $error) { $this->malformedKeys[] = 'extra_docroots'; }
        if ($this->storedVersion === 2) {
            $values = [];
            foreach (Settings::v2Keys() as $key) {
                if ($invalid($key)) { $this->malformedKeys[] = $key; }
                $values[$key] = in_array($key, $this->malformedKeys, true) ? Settings::DEFAULTS[$key] : (Settings::parse($key, $rows[$key] ?? null) ?? Settings::DEFAULTS[$key]);
            }
        } else {
            foreach (array_keys(Settings::DEFAULTS) as $key) {
                if ($key !== 'extra_docroots' && $invalid($key)) { $this->malformedKeys[] = $key; }
            }
            $values = Settings::fromLegacy(array_diff_key($rows, array_flip($this->malformedKeys)));
            // The page proposes encryption. Uploads keep their 0.3 behaviour until this form is saved.
            $values['s3_encryption'] = true;
            $values['plaintext_offsite_acknowledged'] = false;
        }
        $form = [];
        foreach ($values as $key => $value) { $form[$key] = $this->formValue($key, $value); }
        return $this->storedForm = $form;
    }

    /** Form shape of an engine value. */
    private function formValue(string $key, mixed $value): mixed
    {
        if (in_array($key, self::WEEKDAY_FIELDS, true)) { return array_map('strval', is_array($value) ? array_values(array_filter($value, 'is_int')) : []); }
        if (in_array($key, self::LINE_LISTS, true)) { return implode("\n", is_array($value) ? array_filter($value, 'is_string') : []); }
        if (in_array($key, self::GIB_FIELDS, true)) {
            $gib = round((int) $value / Settings::GIB, 2);
            return $gib == floor($gib) ? (int) $gib : $gib;
        }
        return $value;
    }

    /** Engine value for a form value. */
    private function engineValue(string $key, mixed $value): mixed
    {
        $default = Settings::DEFAULTS[$key];
        if (in_array($key, Settings::WEEKDAY_KEYS, true)) { $days = array_values(array_unique(array_map('intval', (array) $value))); sort($days); return $days; }
        if (in_array($key, self::LINE_LISTS, true)) { return array_values(array_filter(array_map('trim', preg_split('/\R/', (string) $value)), static fn ($line) => $line !== '')); }
        if (in_array($key, self::GIB_FIELDS, true)) { return (int) round((float) $value * Settings::GIB); }
        if (is_int($default)) { return (int) $value; }
        if (is_bool($default)) { return (bool) $value; }
        return trim((string) ($value ?? ''));
    }

    private function changed(string $key, mixed $value): bool
    {
        // Compare with native rows read on this request, never with anything the browser can send.
        $before = $this->formValues()[$key] ?? null;
        if (in_array($key, $this->malformedKeys, true)) { return true; }
        if (in_array($key, self::GIB_FIELDS, true)) { return round((float) $value, 2) !== round((float) $before, 2); }
        return json_encode($this->engineValue($key, $value)) !== json_encode($this->engineValue($key, $before));
    }

    /**
     * Validate settings and every secret group together, so one response shows
     * every problem. Returns [settings, s3 keys|null, site password|null, key setup|null].
     */
    private function validatedInput(): array
    {
        $errors = [];
        $states = [];
        $this->collectingErrors = true;
        try {
            foreach (['settingsForm', 's3KeysForm', 'siteAccessForm', 'encryptionForm'] as $form) {
                try { $states[$form] = $this->{$form}->getState(); }
                catch (ValidationException $exception) { $errors += $exception->errors(); }
            }
            $siteEntered = filled($this->siteAccess['username'] ?? null) || filled($this->siteAccess['password'] ?? null);
            if ($siteEntered && $this->httpsOrigin() === null) {
                $errors['siteAccess.username'] = [Messages::error('maintenance-probe-https-required')];
            }
        } finally {
            $this->collectingErrors = false;
        }
        if ($errors) {
            Notification::make()->title('Settings not saved')->body('Fix the highlighted fields.')->danger()->send();
            $this->dispatch('form-validation-error', livewireId: $this->getId());
            throw ValidationException::withMessages($errors);
        }
        $state = $states['settingsForm'];
        $uploads = (bool) ($this->settings['upload_s3'] ?? false);
        $s3 = $states['s3KeysForm'] ?? [];
        $s3Keys = $uploads && (filled($s3['access_key'] ?? null) || filled($s3['secret_key'] ?? null))
            ? array_filter(['access_key' => (string) $s3['access_key'], 'secret_key' => (string) $s3['secret_key'], 'session_token' => filled($s3['session_token'] ?? null) ? (string) $s3['session_token'] : null], static fn ($value) => $value !== null)
            : null;
        $site = $states['siteAccessForm'] ?? [];
        // The address is Paymenter's own; a browser cannot point the check elsewhere.
        $siteAccess = filled($site['username'] ?? null) ? ['origin' => $this->httpsOrigin(), 'username' => (string) $site['username'], 'password' => (string) $site['password']] : null;
        $keySetup = null;
        if ($this->keySetupApplies()) {
            $source = ($this->encryptionInput['source'] ?? 'generate') === 'passphrase' ? 'passphrase' : 'generate';
            $keySetup = $source === 'passphrase' ? ['source' => 'passphrase', 'passphrase' => (string) ($states['encryptionForm']['passphrase'] ?? '')] : ['source' => 'generate'];
        }
        // Laravel's character limits do not enforce the bridge's UTF-8 byte
        // limits. Validate every group before config check or native writes.
        if ($s3Keys !== null) {
            try { CredentialInput::merge([], ['s3' => $s3Keys], ''); }
            catch (\RuntimeException $error) {
                foreach (array_keys($s3Keys) as $field) { $errors['s3Keys.' . $field] = [Messages::error($error->getMessage())]; }
            }
        }
        if ($siteAccess !== null) {
            try { CredentialInput::merge([], ['site' => $siteAccess], $this->httpsOrigin() ?? ''); }
            catch (\RuntimeException $error) {
                foreach (['username', 'password'] as $field) { $errors['siteAccess.' . $field] = [Messages::error($error->getMessage())]; }
            }
        }
        if ($s3Keys !== null || $siteAccess !== null) {
            try {
                if (strlen(json_encode(['s3' => $s3Keys, 'site' => $siteAccess], JSON_THROW_ON_ERROR)) > 65536) {
                    throw new \RuntimeException('credentials-invalid');
                }
            } catch (\JsonException|\RuntimeException $error) {
                foreach ($s3Keys !== null ? array_keys($s3Keys) : [] as $field) { $errors['s3Keys.' . $field] = [Messages::error('credentials-invalid')]; }
                foreach ($siteAccess !== null ? ['username', 'password'] : [] as $field) { $errors['siteAccess.' . $field] = [Messages::error('credentials-invalid')]; }
            }
        }
        if (($keySetup['source'] ?? null) === 'passphrase') {
            try { ArchiveEncryption::secret($keySetup['passphrase']); }
            catch (\RuntimeException $error) { $errors['encryptionInput.passphrase'] = [Messages::error($error->getMessage())]; }
        }
        if ($errors) {
            Notification::make()->title('Settings not saved')->body('Fix the highlighted fields.')->danger()->send();
            $this->dispatch('form-validation-error', livewireId: $this->getId());
            throw ValidationException::withMessages($errors);
        }
        return [$state, $s3Keys, $siteAccess, $keySetup];
    }

    /** A recovery key is created on save when encrypted uploads are on and no key exists. */
    private function keySetupApplies(): bool
    {
        return (bool) ($this->settings['upload_s3'] ?? false) && (bool) ($this->settings['s3_encryption'] ?? false) && $this->keyState() === 'none';
    }

    private function s3KeysRequired(): bool
    {
        if (!($this->settings['upload_s3'] ?? false)) { return false; }
        if (filled($this->s3Keys['access_key'] ?? null) || filled($this->s3Keys['secret_key'] ?? null) || filled($this->s3Keys['session_token'] ?? null)) { return true; }
        // Turning S3 on needs keys. An S3 setup whose keys were removed still saves other
        // changes; Setup then asks for the keys.
        return !$this->savedS3Keys() && !($this->formValues()['upload_s3'] ?? false);
    }

    private function siteRequired(): bool
    {
        return filled($this->siteAccess['username'] ?? null) || filled($this->siteAccess['password'] ?? null);
    }
}
