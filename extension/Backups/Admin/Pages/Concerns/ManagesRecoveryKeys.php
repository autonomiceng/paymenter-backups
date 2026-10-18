<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Actions\Action;
use Filament\Forms\Components\Checkbox;
use Filament\Notifications\Notification;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Schema;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Enums\Size;
use Filament\Support\Facades\FilamentView;
use Illuminate\Support\Facades\Auth;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Runtime;

trait ManagesRecoveryKeys
{
    /** Show, download and confirm, for a key that still needs its confirmation; show and download otherwise. */
    private function recoveryKeyActions(): array
    {
        return [$this->showRecoveryKeyAction(), $this->downloadRecoveryKeyAction(), $this->acknowledgeRecoveryKeyAction()];
    }

    public function showRecoveryKeyAction(): Action
    {
        return Action::make('showRecoveryKey')->label('Show recovery key')->icon('ri-eye-line')->color('gray')->button()->size(Size::Small)
            ->visible(fn () => $this->keyState() !== 'none')
            ->requiresConfirmation()->modalIcon('ri-key-2-line')->modalHeading('Show the recovery key?')
            ->modalDescription('Make sure no one else can see your screen.')
            ->modalSubmitActionLabel('Show key')
            ->action(function () {
                $this->authorizeAction();
                $this->grantReveal();
                $this->replaceMountedAction('recoveryKey');
            });
    }

    /** The key a reveal would show: the one awaiting confirmation, else the active key. */
    private function revealIdentity(): ?string
    {
        try { return Runtime::recoveryKeyFingerprint(); }
        catch (\Throwable) { return null; }
    }

    /** One-time, short-lived permission set only by a confirmed Show (or a confirmed rotation). */
    private function grantReveal(): void
    {
        $identity = $this->revealIdentity();
        if ($identity === null) { throw new \RuntimeException('archive-key-unavailable'); }
        session()->put(self::REVEAL_GRANT, ['user' => Auth::id(), 'key' => $identity, 'until' => time() + 30]);
    }

    /**
     * Whether this component may render the key now. A grant is consumed once and
     * opens a two-minute window for this component and this key only; a direct
     * mount or ?action= link has no grant and shows nothing.
     */
    private function revealAllowed(): bool
    {
        $identity = $this->revealIdentity();
        if ($identity === null) { return false; }
        if ($this->revealGrantUntil !== null && $this->revealGrantUntil >= time() && is_string($this->revealGrantKey) && hash_equals($this->revealGrantKey, $identity)) {
            return true;
        }
        $grant = session()->pull(self::REVEAL_GRANT);
        if (!is_array($grant) || ($grant['user'] ?? null) !== Auth::id() || (int) ($grant['until'] ?? 0) < time()
            || !is_string($grant['key'] ?? null) || !hash_equals($grant['key'], $identity)) {
            return false;
        }
        $this->revealGrantUntil = time() + 120;
        $this->revealGrantKey = $identity;
        return true;
    }

    /** Opened only from Show recovery key. The key lives in this modal's rendered content, never in Livewire state. */
    public function recoveryKeyAction(): Action
    {
        return Action::make('recoveryKey')->modalHeading('Recovery key')->modalWidth('2xl')
            ->mountUsing(function (Action $action) {
                $this->authorizeAction();
                if (!$this->revealAllowed()) {
                    Notification::make()->title('Recovery key not shown')->warning()->body('Confirm Show recovery key first.')->send();
                    $action->halt();
                }
            })
            ->schema(fn () => $this->recoveryKeySchema())
            ->modalSubmitAction(false)->modalCancelActionLabel('Close')
            ->extraModalFooterActions(fn () => array_filter([
                $this->downloadRecoveryKeyAction(),
                $this->pendingKeyShort() !== null ? Action::make('recoveryKeySaved')->label('I saved it')->color('primary')
                    ->action(fn () => $this->replaceMountedAction('acknowledgeRecoveryKey')) : null,
            ]));
    }

    private function revealKey(): ?array
    {
        if ($this->revealedKey !== null) { return $this->revealedKey; }
        $this->authorizeAction();
        try {
            if (!$this->revealAllowed() || $this->revealGrantKey === null) { throw new \RuntimeException('archive-key-mismatch'); }
            $key = Runtime::revealRecoveryKey($this->revealGrantKey);
            if (!is_string($key['fingerprint'] ?? null) || !hash_equals($this->revealGrantKey, $key['fingerprint'])) {
                throw new \RuntimeException('archive-key-mismatch');
            }
        } catch (\Throwable $error) {
            return $this->revealedKey = ['error' => $error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'];
        }
        if (is_string($key['fingerprint'] ?? null)) { $this->recoveryFingerprint = $key['fingerprint']; }
        return $this->revealedKey = ['secret' => (string) ($key['secret'] ?? ''), 'fingerprint' => (string) ($key['fingerprint'] ?? '')];
    }

    private function recoveryKeySchema(): array
    {
        if (!$this->revealAllowed()) {
            return [Text::make('Confirm Show recovery key first.')];
        }
        $key = $this->revealKey();
        if (isset($key['error'])) {
            return [Callout::make('The recovery key can\'t be shown')->danger()->description(Messages::error($key['error']))];
        }
        return [
            Text::make('Save this recovery key in your password manager or another safe place outside this server. You need it to restore backups from S3 if this server is lost. APP_KEY and your S3 keys can\'t replace it.'),
            $this->command($key['secret'])->size('lg'),
            Text::make('Fingerprint (not secret; it identifies the key on your backups):'),
            Text::make($key['fingerprint'])->fontFamily(FontFamily::Mono)->extraAttributes(['style' => 'overflow-wrap: anywhere']),
        ];
    }

    public function downloadRecoveryKeyAction(): Action
    {
        return Action::make('downloadRecoveryKey')->label('Download recovery key')->icon('ri-download-2-line')->color('gray')->button()->size(Size::Small)
            ->visible(fn () => $this->keyState() !== 'none')
            ->action(function () {
                $this->authorizeAction();
                try {
                    // A fresh signed address each time; it expires after a few minutes.
                    $url = Runtime::recoveryKeyUrl();
                    parse_str((string) parse_url($url, PHP_URL_QUERY), $query);
                    if (is_string($query['fingerprint'] ?? null) && preg_match('/^[a-f0-9]{64}$/D', $query['fingerprint'])) {
                        $this->recoveryFingerprint = $query['fingerprint'];
                    }
                    FilamentView::spaUrlExceptions([$url]);
                    $this->redirect($url, navigate: false);
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not downloaded')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                }
            });
    }

    public function acknowledgeRecoveryKeyAction(): Action
    {
        return Action::make('acknowledgeRecoveryKey')->label('I saved it')->icon('ri-check-line')->color('primary')->button()->size(Size::Small)
            ->visible(fn () => $this->pendingKeyShort() !== null)
            ->modalHeading('Confirm the recovery key is saved')
            ->modalDescription(fn () => 'Uploads to S3 ' . ($this->keyState() === 'active' ? 'switch to recovery key ' : 'start with recovery key ') . $this->pendingKeyShort() . ' after this.')
            ->mountUsing(function (Action $action, Schema $schema) {
                $this->authorizeAction();
                $this->acknowledgementFingerprint = null;
                try {
                    $this->acknowledgementFingerprint = $this->pendingFingerprint();
                    $schema->fill(['saved' => false]);
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not confirmed')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                    $action->halt();
                }
            })
            ->schema([
                Checkbox::make('saved')->label('I saved this recovery key outside this server.')->accepted()
                    ->validationMessages(['accepted' => 'Save the key first, then check this.']),
            ])
            ->modalSubmitActionLabel('Confirm')
            ->action(function (Action $action) {
                $this->authorizeAction();
                try {
                    $fingerprint = $this->acknowledgementIdentity();
                    Runtime::encryptionKey('acknowledge', ['fingerprint' => $fingerprint]);
                    $this->overviewCache = null;
                    Notification::make()->title('Recovery key confirmed')->body('Uploads to S3 can start.')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not confirmed')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                    $action->halt();
                }
            });
    }

    /**
     * Current full pending identity, without revealing any key material.
     * A previously shown or downloaded identity must still name this key.
     */
    private function pendingFingerprint(): string
    {
        $fingerprint = $this->keyInfo()['pending_fingerprint'] ?? null;
        if (!is_string($fingerprint) || !preg_match('/^[a-f0-9]{64}$/D', $fingerprint)) { throw new \RuntimeException('archive-key-unavailable'); }
        $short = $this->pendingKeyShort();
        if ($short === null || !str_starts_with($fingerprint, $short)
            || ($this->recoveryFingerprint !== null && !hash_equals($fingerprint, $this->recoveryFingerprint))) {
            throw new \RuntimeException('archive-key-mismatch');
        }
        return $fingerprint;
    }

    private function acknowledgementIdentity(): string
    {
        $fingerprint = $this->acknowledgementFingerprint;
        if (!is_string($fingerprint) || !preg_match('/^[a-f0-9]{64}$/D', $fingerprint)) { throw new \RuntimeException('archive-key-unavailable'); }
        if (!hash_equals($fingerprint, $this->pendingFingerprint())) { throw new \RuntimeException('archive-key-mismatch'); }
        return $fingerprint;
    }

    public function rotateRecoveryKeyAction(): Action
    {
        return Action::make('rotateRecoveryKey')->label('Create a new recovery key')->color('gray')->link()
            ->visible(fn () => $this->keyState() === 'active' && $this->pendingKeyShort() === null)
            ->requiresConfirmation()->modalHeading('Create a new recovery key?')
            ->modalDescription('New backups in S3 will use the new key. Earlier backups still need their earlier keys, so keep every recovery key you\'ve saved. Uploads keep using the current key until you save the new one and confirm.')
            ->modalSubmitActionLabel('Create key')
            ->action(function () {
                $this->authorizeAction();
                try {
                    $result = Runtime::encryptionKey('generate');
                    $this->recoveryFingerprint = is_string($result['fingerprint'] ?? null) ? $result['fingerprint'] : null;
                    $this->overviewCache = null;
                    $this->grantReveal();
                    $this->replaceMountedAction('recoveryKey');
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not created')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-invalid'))->send();
                }
            });
    }

    public function discardRecoveryKeyAction(): Action
    {
        return Action::make('discardRecoveryKey')->label('Discard this key')->color('danger')->link()
            ->visible(fn () => $this->pendingKeyShort() !== null && !($this->keyState() === 'active' && $this->keyInfo()['acknowledged'] && blank($this->keyInfo()['pending_short'])))
            ->requiresConfirmation()->modalHeading('Discard the unconfirmed recovery key?')
            ->modalDescription(fn () => $this->keyState() === 'active'
                ? 'Uploads keep using recovery key ' . $this->keyInfo()['short'] . '.'
                : 'Uploads to S3 stay paused until a new key is created and confirmed. Save settings to create one.')
            ->modalSubmitActionLabel('Discard key')
            ->action(function () {
                $this->authorizeAction();
                try {
                    Runtime::encryptionKey('discard-pending');
                    $this->recoveryFingerprint = null;
                    $this->overviewCache = null;
                    Notification::make()->title('Recovery key discarded')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('Recovery key not discarded')->danger()->body(Messages::error($error instanceof \RuntimeException ? $error->getMessage() : 'archive-key-unavailable'))->send();
                }
            });
    }
}
