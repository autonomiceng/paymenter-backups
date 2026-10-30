<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Actions\Action;
use Filament\Forms\Components\Radio;
use Filament\Forms\Components\TextInput;
use Filament\Notifications\Notification;
use Filament\Schemas\Components\Actions;
use Filament\Schemas\Components\EmbeddedSchema;
use Filament\Schemas\Components\Grid;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\Utilities\Get;
use Filament\Schemas\Schema;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Runtime;
use Paymenter\Extensions\Others\Backups\Settings;

trait ManagesCredentials
{
    private function sitePasswordSection(): Section
    {
        $site = fn () => $this->configuration()['site_password'] ?? [];
        $saved = fn () => (bool) ($this->configuration()['saved_keys']['site'] ?? false);
        $attention = fn () => $saved() && in_array($site()['state'] ?? null, ['http', 'unavailable', 'origin-changed'], true);
        return Section::make('Site password (HTTP Basic)')->id('site-password')->compact()
            ->collapsible()->collapsed(fn () => !$saved() && !in_array('maintenance-probe-failed', $this->configuration()['admission']['full-quiesced'] ?? [], true))
            ->afterHeader([
                Text::make('Saved')->badge()->color('success')->visible(fn () => $saved() && !$attention()),
                Text::make('Needs attention')->badge()->color('warning')->visible($attention),
            ])
            ->description(fn () => 'Only if your web server asks for a username and password before Paymenter loads. Full backups use it to check the maintenance page at '
                . ($site()['probe_base'] ?? 'App URL') . '.')
            ->schema([
                Text::make(fn () => match ($site()['state'] ?? 'unavailable') {
                    'saved' => 'Saved for ' . ($site()['origin'] ?? 'App URL') . '. Only sent there.',
                    'http' => $saved() ? 'Your App URL now uses http://, so the saved password can\'t be sent. Remove it, or change App URL back to https://.'
                        : 'Your App URL uses http://. A site password can only be sent over https://.',
                    'unavailable' => 'Your App URL (Settings, General) isn\'t a valid web address.',
                    'origin-changed' => 'The saved password is for ' . ($site()['saved_origin'] ?? 'another address') . ', but App URL is now ' . ($site()['origin'] ?? 'different') . '. Enter it again or remove it.',
                    default => 'No site password saved. You only need one if your web server asks for a password.',
                }),
                EmbeddedSchema::make('siteAccessForm')->visible(fn () => $this->httpsOrigin() !== null),
                Actions::make([$this->removeSitePasswordAction()]),
            ]);
    }

    public function s3KeysForm(Schema $schema): Schema
    {
        $saved = fn () => $this->savedS3Keys();
        $secret = fn (string $key, string $label) => TextInput::make($key)->label($label)->password()->revealable(false)->maxLength(8192)->autocomplete('new-password')
            ->placeholder(fn () => $saved() ? 'Saved. Leave empty to keep.' : null)
            ->required(fn () => $this->s3KeysRequired())
            ->validationMessages(['required' => $key === 'access_key' ? 'Enter your access key ID.' : 'Enter your secret access key.']);
        return $schema->components([
            Grid::make(['default' => 1, 'md' => 2])->schema([
                $secret('access_key', 'Access key ID'),
                $secret('secret_key', 'Secret access key'),
                TextInput::make('session_token')->label('Session token (optional)')->password()->revealable(false)->maxLength(8192)->autocomplete('new-password')
                    ->helperText('Only for temporary credentials. Saving new keys clears a saved token unless you enter it again.'),
                Group::make([
                    Text::make('Keys saved.')->visible($saved),
                    Actions::make([$this->removeS3KeysAction()]),
                ]),
                Text::make('Enter the keys again. They were cleared for security when the form updated.')->color('warning')->columnSpanFull()
                    ->visible(fn () => $this->secretsCleared['s3'] ?? false),
            ]),
        ])->statePath('s3Keys');
    }

    public function siteAccessForm(Schema $schema): Schema
    {
        $saved = fn () => (bool) ($this->configuration()['saved_keys']['site'] ?? false);
        return $schema->components([
            Grid::make(['default' => 1, 'md' => 2])->schema([
                TextInput::make('username')->label('Username')->helperText('Not your Paymenter login.')->maxLength(8192)->autocomplete('off')
                    ->placeholder(fn () => $saved() ? 'Saved. Leave empty to keep.' : null)
                    ->required(fn () => $this->siteRequired())->markAsRequired(false)
                    ->rule('not_regex:/[:\x00-\x1f\x7f]/')->validationMessages(['not_regex' => 'The username can\'t contain ":" or control characters.', 'required' => 'Enter the username too.']),
                TextInput::make('password')->label('Password')->password()->revealable(false)->maxLength(8192)->autocomplete('new-password')
                    ->placeholder(fn () => $saved() ? 'Saved. Leave empty to keep.' : null)
                    ->required(fn () => $this->siteRequired())->markAsRequired(false)
                    ->validationMessages(['required' => 'Enter the password too.']),
                Text::make('Enter the username and password again. They were cleared for security when the form updated.')->color('warning')->columnSpanFull()
                    ->visible(fn () => $this->secretsCleared['site'] ?? false),
            ]),
        ])->statePath('siteAccess');
    }

    public function encryptionForm(Schema $schema): Schema
    {
        return $schema->components([
            // Shown and hidden in the browser: a server round trip would clear a typed passphrase.
            Radio::make('source')->label('Recovery key')->required()
                ->options(['generate' => 'Generate a recovery key (recommended)', 'passphrase' => 'Use my own passphrase'])
                ->descriptions([
                    'generate' => 'A random 64-character key, created when you save.',
                    'passphrase' => 'At least 20 characters. A few random words are easy to keep and hard to guess.',
                ]),
            TextInput::make('passphrase')->label('Passphrase')->password()->revealable(false)->autocomplete('new-password')
                ->helperText('Keep a copy outside this server. Without it, backups in S3 can\'t be restored if this server is lost.')
                ->required(fn (Get $get) => $get('source') === 'passphrase' && $this->keySetupApplies())
                ->rules(['nullable', 'string', 'min:20', 'max:1024', 'not_regex:/[\x00-\x1f\x7f]/'])
                ->validationMessages(['min' => 'Use at least 20 characters.', 'not_regex' => 'Don\'t use line breaks or control characters.', 'required' => 'Enter a passphrase, or generate a key instead.'])
                ->visibleJs(<<<'JS'
                    $get('source') === 'passphrase'
                    JS),
            Text::make('Enter the passphrase again. It was cleared for security when the form updated.')->color('warning')
                ->visible(fn () => $this->secretsCleared['passphrase'] ?? false),
        ])->statePath('encryptionInput');
    }

    public function removeS3KeysAction(): Action
    {
        return Action::make('removeS3Keys')->label('Remove saved keys')->color('danger')->link()
            ->visible(fn () => $this->savedS3Keys())
            ->requiresConfirmation()->modalHeading('Remove saved S3 keys?')
            ->modalDescription('Uploads to S3 stop until you add keys again. Backups already in S3 are kept. The keys stay valid at your provider; revoke them there if needed.')
            ->modalSubmitActionLabel('Remove keys')
            ->action(function () {
                $this->authorizeAction();
                try {
                    Runtime::credentials(['clear' => true]);
                    $this->overviewCache = null;
                    Notification::make()->title('S3 keys removed')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('S3 keys not removed')->danger()->body(Messages::error($error->getMessage()))->send();
                }
            });
    }

    /** Paymenter's own https origin, or null when its address is not https. */
    private function httpsOrigin(): ?string
    {
        $origin = $this->configuration()['app_origin'] ?? null;
        return is_string($origin) && str_starts_with($origin, 'https://') ? $origin : null;
    }

    public function removeSitePasswordAction(): Action
    {
        return Action::make('removeSitePassword')->label('Remove saved password')->color('danger')->link()
            ->visible(fn () => $this->configuration()['saved_keys']['site'] ?? false)
            ->requiresConfirmation()->modalHeading('Remove the saved site password?')
            ->modalDescription('Full backups will check the site without it. Your web server\'s password isn\'t changed.')
            ->modalSubmitActionLabel('Remove password')
            ->action(function () {
                $this->authorizeAction();
                try {
                    Runtime::credentials(['clear' => true], true);
                    $this->overviewCache = null;
                    Notification::make()->title('Site password removed')->success()->send();
                } catch (\Throwable $error) {
                    Notification::make()->title('Site password not removed')->danger()->body(Messages::error($error->getMessage()))->send();
                }
            });
    }
}
