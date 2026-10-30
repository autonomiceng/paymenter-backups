<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Notifications\Notification;
use Paymenter\Extensions\Others\Backups\Backups as Extension;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\PrivateStore;
use Paymenter\Extensions\Others\Backups\Runtime;
use Paymenter\Extensions\Others\Backups\Settings;
use Paymenter\Extensions\Others\Backups\SettingsWriter;

trait SavesSettings
{
    public function saveSettings(): void
    {
        $this->authorizeAction();
        [$state, $s3Keys, $siteAccess, $keySetup] = $this->validatedInput();
        $this->storedForm = null;
        $stored = $this->formValues();
        $repairBaseline = $this->storedRows;
        if (($this->publicDirectoriesRepairPin !== null
                && !hash_equals($this->publicDirectoriesRepairPin, hash('sha256', serialize($repairBaseline))))
            || (($state['confirm_public_directories'] ?? false) === true && $this->publicDirectoriesRepairPin === null)) {
            $this->refuseSettings('config-changed');
            return;
        }
        if (in_array('extra_docroots', $this->malformedKeys, true)
            && (!array_key_exists('extra_docroots', $state) || ($state['confirm_public_directories'] ?? false) !== true)) {
            // Never substitute an empty default for a malformed public-directory guard.
            $this->refuseSettings('document-roots-invalid');
            return;
        }
        $v2 = Settings::v2Keys();
        $privacyRepair = in_array('extra_docroots', $this->malformedKeys, true);
        // What the administrator changed, against native rows read now.
        $userChanges = [];
        foreach ($state as $key => $value) {
            if (!in_array($key, $v2, true) || !$this->changed($key, $value)) { continue; }
            $engineValue = $this->engineValue($key, $value);
            // Repair the privacy guard first. Other damaged rows' visible
            // defaults are implicit repairs, not a second user edit; defer them.
            if ($privacyRepair && !in_array($key, self::BOOTSTRAP, true) && in_array($key, $this->malformedKeys, true)
                && json_encode($engineValue) === json_encode($this->engineValue($key, $stored[$key]))) { continue; }
            $userChanges[$key] = $engineValue;
        }
        $encrypted = (bool) ($userChanges['s3_encryption'] ?? $this->engineValue('s3_encryption', $stored['s3_encryption']));
        if (!$privacyRepair && $encrypted && $this->engineValue('plaintext_offsite_acknowledged', $stored['plaintext_offsite_acknowledged'])) {
            // Turning encryption off later needs a fresh confirmation.
            $userChanges['plaintext_offsite_acknowledged'] = false;
        }
        $changes = $userChanges;
        foreach ($this->malformedKeys as $key) {
            // Hidden optional fields still need their invalid native rows repaired.
            if (in_array($key, $v2, true) && !array_key_exists($key, $changes)) { $changes[$key] = $this->engineValue($key, $stored[$key]); }
        }
        foreach ($this->malformedKeys as $key) {
            // Malformed 0.3 rows are reset too; the CLI still reads mode as its default type.
            if (in_array($key, Settings::LEGACY, true)) { $changes[$key] = Settings::DEFAULTS[$key]; }
        }
        if ($this->storedVersion !== 2) {
            // The first save in this version writes every key explicitly.
            foreach ($v2 as $key) {
                if (!array_key_exists($key, $changes)) { $changes[$key] = $this->engineValue($key, $stored[$key]); }
            }
            $changes['settings_version'] = 2;
        }
        $repaired = array_values(array_filter($this->malformedKeys, static fn ($key) => $key !== 'extra_docroots'));
        $initialized = $this->initialized();
        $secrets = $s3Keys !== null || $siteAccess !== null;
        // The first save creates private storage, even when every default is kept.
        if (!$changes && $initialized && !$secrets && $keySetup === null) {
            $kept = array_intersect_key(['s3' => 'Saved S3 keys weren\'t changed.', 'site' => 'The saved site password wasn\'t changed.', 'passphrase' => 'No passphrase was saved.'], array_filter($this->secretsCleared));
            foreach (array_keys($kept) as $group) { $this->secretsSaved[$group] = true; }
            Notification::make()->title('No changes')->body($kept ? implode(' ', $kept) : null)->info()->send();
            return;
        }
        if (array_key_exists('private_root', $changes) && $initialized && $this->engineValue('private_root', $stored['private_root']) !== $changes['private_root']) {
            // Moving would silently start a new identity without the saved keys or history.
            $this->refuseSettings('private-root-move-refused');
            return;
        }
        $currentPrivateRoot = $this->engineValue('private_root', $stored['private_root']) ?: null;
        $privateRoot = ($changes['private_root'] ?? $currentPrivateRoot) ?: null;
        $savedRoot = $privateRoot === $currentPrivateRoot
            && PrivateStore::isSavedRoot(realpath(base_path()), $currentPrivateRoot);
        $docroots = $changes['extra_docroots'] ?? $this->engineValue('extra_docroots', $stored['extra_docroots']);
        $problem = PrivateStore::problem(realpath(base_path()), $privateRoot, $docroots, $savedRoot);
        if ($problem !== null) {
            $this->refusePrivateRootProblem($problem);
            return;
        }
        if (array_key_exists('private_root', $userChanges) || array_key_exists('extra_docroots', $userChanges)) {
            try {
                PrivateStore::root(realpath(base_path()), $privateRoot, false, $docroots);
            } catch (\RuntimeException $error) {
                $this->refuseSettings($error->getMessage());
                return;
            }
        }
        $checked = true;
        try {
            Runtime::command(['config', 'check'], ['settings' => $changes ?: new \stdClass], 30);
        } catch (\RuntimeException $error) {
            $code = $error->getMessage();
            // A broken engine path can only be repaired by saving the paths themselves.
            if (!$userChanges || !in_array($code, self::ENVIRONMENT_ERRORS, true) || array_diff(array_keys($userChanges), self::BOOTSTRAP)) {
                if (in_array('extra_docroots', $this->malformedKeys, true)) {
                    Notification::make()->title('Settings not saved')->danger()->body('Save the folder list by itself first. Then save your other changes.')->send();
                    return;
                }
                $field = $error instanceof \Paymenter\Extensions\Others\Backups\EngineCommandException ? $error->field : null;
                $this->refuseSettings($code, in_array($code, self::ENVIRONMENT_ERRORS, true), $field);
                return;
            }
            $checked = false;
            $changes = $userChanges;
        }
        $record = $this->record();
        try {
            if ($checked) {
                $saved = Runtime::saveConfiguration($changes, $secrets ? ['s3' => $s3Keys, 'site' => $siteAccess] : null);
            } else {
                SettingsWriter::repairBinaries($record, $changes, $repairBaseline);
                $saved = ['updated' => true];
            }
        } catch (\Throwable $error) {
            $this->storedForm = null;
            $this->overviewCache = null;
            $code = $error instanceof \RuntimeException ? $error->getMessage() : 'config-save-outcome-unknown';
            $cause = $error instanceof \Paymenter\Extensions\Others\Backups\EngineCommandException
                ? Runtime::safeWriterCause($error->cause) : null;
            $privateProblem = PrivateStore::problem(realpath(base_path()), $privateRoot, $docroots, $savedRoot);
            if ($privateProblem !== null) {
                $this->refusePrivateRootProblem($privateProblem);
                return;
            }
            if ($cause !== null) {
                $this->overviewError = $code;
                $this->overviewErrorCause = $cause;
            }
            if (in_array($code, ['config-save-outcome-unknown', 'config-write-outcome-unknown'], true)) {
                Notification::make()->title('Save outcome unknown')->warning()
                    ->body(Messages::withWriterCause(
                        'Open Settings again and check what was saved before trying again. Keys you entered were cleared.', $cause
                    ))->send();
            } else {
                $field = $error instanceof \Paymenter\Extensions\Others\Backups\EngineCommandException ? $error->field : null;
                $this->refuseSettings($code, false, $field, $cause);
            }
            return;
        } finally {
            unset($s3Keys, $siteAccess);
        }
        $this->storedForm = null;
        $this->overviewCache = null;
        $this->overviewError = null;
        $this->overviewErrorCause = null;
        $this->settingsForm->fill($this->formValues());
        $this->publicDirectoriesRepairPin = in_array('extra_docroots', $this->malformedKeys, true)
            ? hash('sha256', serialize($this->storedRows)) : null;
        if (!$checked) {
            (new Extension)->configurationUpdated($record);
            $this->overview();
            $body = $this->overviewError ? 'Backups still can\'t start: ' . Messages::error($this->overviewError) : 'Backups can start again. Save your other changes now.';
            if ($secrets) { $body .= ' Keys you entered weren\'t saved; enter them again.'; }
            Notification::make()->title('Setup settings saved')->warning()->body($body)->send();
            return;
        }
        if (($saved['credentials_updated'] ?? false) === true) { $this->secretsSaved['s3'] = true; }
        if (($saved['maintenance_probe_credentials_updated'] ?? false) === true) { $this->secretsSaved['site'] = true; }
        if (($saved['updated'] ?? false) !== true) {
            $privateProblem = PrivateStore::problem(realpath(base_path()), $privateRoot, $docroots, $savedRoot);
            if ($privateProblem !== null) {
                $this->refusePrivateRootProblem($privateProblem);
                return;
            }
            $writerCause = Runtime::safeWriterCause($saved['writer_cause'] ?? null);
            if ($writerCause !== null) {
                $this->overviewError = is_string($saved['error'] ?? null) ? $saved['error'] : 'config-save-outcome-unknown';
                $this->overviewErrorCause = $writerCause;
            }
            $field = is_string($saved['field'] ?? null) && array_key_exists($saved['field'], Settings::LABELS)
                ? $saved['field'] : null;
            if ($field !== null) {
                $this->addError('settings.' . $field, Messages::error('config-invalid'));
                $this->dispatch('form-validation-error', livewireId: $this->getId());
            }
            Notification::make()->title('Save incomplete')->warning()
                ->body(Messages::withWriterCause((($saved['outcome_unknown'] ?? false)
                    ? Messages::error('config-save-outcome-unknown')
                    : (($saved['concurrent_settings_preserved'] ?? false)
                        ? 'Someone else changed settings at the same time, and those changes were kept. Keys you entered weren\'t saved. Open Settings again and check them before trying again.'
                        : 'Some settings couldn\'t be restored, so backups are paused. Keys you entered weren\'t saved. Open Settings again, check them, enter the new keys and save.')), $writerCause))->send();
            return;
        }
        $problems = [];
        if ($saved['partial'] ?? false) {
            $problems[] = 'Settings and keys were saved, but setup didn\'t finish: ' . Messages::error($saved['error'] ?? 'config-save-failed') . ' Check setup on the Backups tab.';
        }
        $created = false;
        if ($keySetup !== null) {
            try {
                $result = $keySetup['source'] === 'passphrase'
                    ? Runtime::encryptionKey('import', ['passphrase' => $keySetup['passphrase']])
                    : Runtime::encryptionKey('generate');
                $this->recoveryFingerprint = is_string($result['fingerprint'] ?? null) ? $result['fingerprint'] : null;
                $created = true;
                if ($keySetup['source'] === 'passphrase') { $this->secretsSaved['passphrase'] = true; }
            } catch (\Throwable $error) {
                $problems[] = 'The recovery key wasn\'t created: ' . Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-invalid');
            } finally {
                unset($keySetup);
            }
        }
        $this->overviewCache = null;
        $title = $initialized ? 'Settings saved' : 'Setup saved';
        $body = [];
        if (!$initialized) { $body[] = 'Backups can start once the items on the Backups tab are done.'; }
        if ($repaired) {
            $body[] = 'Some saved values were invalid and were reset: ' . implode(', ', array_map(static fn ($key) => Settings::LABELS[$key][0], $repaired)) . '. Check them.';
        }
        if ($created) { $body[] = 'Save the recovery key outside this server now. Uploads to S3 start after you confirm it.'; }
        // Keys typed earlier were cleared before this Save; say plainly that the saved ones stay.
        foreach (['s3' => 'Saved S3 keys weren\'t changed.', 'site' => 'The saved site password wasn\'t changed.'] as $group => $sentence) {
            if (($this->secretsCleared[$group] ?? false) && !isset($this->secretsSaved[$group])) {
                $body[] = $sentence;
                $this->secretsSaved[$group] = true;
            }
        }
        array_push($body, ...$problems);
        Notification::make()->title($title)->body($body ? implode(' ', $body) : null)
            ->status($problems || $repaired ? 'warning' : 'success')->send();
        if ($created) {
            // The one moment the key matters most: offer it immediately, behind the same confirmation.
            // Mounted from the browser after this response, because a mount here would render
            // only the dialog and leave the saved form and setup stale.
            $this->js('$wire.mountAction(\'showRecoveryKey\')');
        }
    }

    private function refuseSettings(string $code, bool $environment = false, ?string $field = null, ?string $cause = null): void
    {
        $field = $field !== null && array_key_exists($field, Settings::LABELS) ? $field : Messages::field($code);
        $message = $code === 'schedule-overlaps-billing' && ($window = $this->billingWindow())
            ? 'This time is too close to Paymenter\'s daily billing at ' . $window['time'] . ' ' . $window['timezone'] . '. Pick a time outside ' . $window['start'] . ' to ' . $window['end'] . '.'
            : Messages::error($code);
        if ($field) {
            $this->addError('settings.' . $field, $message);
            // Opens a collapsed section and scrolls to the field, as for form validation.
            $this->dispatch('form-validation-error', livewireId: $this->getId());
        }
        $body = $environment ? $message . ' Fix Binary paths under Advanced, save, then save your other changes.' : $message;
        $body = Messages::withWriterCause($body, $cause, $code);
        Notification::make()->title('Settings not saved')->danger()->body($body)->send();
    }

    private function refusePrivateRootProblem(array $problem): void
    {
        $message = Messages::privateRootProblem($problem);
        $this->addError('settings.private_root', $message);
        $this->dispatch('form-validation-error', livewireId: $this->getId());
    }
}
