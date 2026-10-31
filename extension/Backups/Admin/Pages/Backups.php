<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages;

use Filament\Actions\Action;
use Filament\Notifications\Notification;
use Filament\Pages\Page;
use Filament\Pages\PageConfiguration;
use Filament\Panel;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\Utilities\Get;
use Filament\Support\Facades\FilamentView;
use Filament\Tables\Concerns\InteractsWithTable;
use Filament\Tables\Contracts\HasTable;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Route;
use Illuminate\Validation\ValidationException;
use Livewire\Attributes\Locked;
use Paymenter\Extensions\Others\Backups\Http\DownloadController;
use Paymenter\Extensions\Others\Backups\Http\RecoveryKeyController;
use Paymenter\Extensions\Others\Backups\Settings;

class Backups extends Page implements HasTable
{
    use InteractsWithTable, Concerns\BuildsHistoryTable {
        Concerns\BuildsHistoryTable::table insteadof InteractsWithTable;
    }
    use Concerns\LoadsOverview;
    use Concerns\BuildsReadiness;
    use Concerns\BuildsPageContent;
    use Concerns\BuildsHelp;
    use Concerns\BuildsCommands;
    use Concerns\BuildsSettingsForm;
    use Concerns\ValidatesSettingsInput;
    use Concerns\SavesSettings;
    use Concerns\ManagesCredentials;
    use Concerns\ManagesRecoveryKeys;
    use Concerns\QueuesBackupActions;

    protected static string|\BackedEnum|null $navigationIcon = 'ri-save-line';
    protected static string|\BackedEnum|null $activeNavigationIcon = 'ri-save-fill';
    protected static string|\UnitEnum|null $navigationGroup = 'System';
    protected static ?string $title = 'Backups';
    /** Native page that renders action modals before the tabs, not inside the Backups tab with the history table. */
    protected string $view = 'backups::page';

    /** Space settings are entered in GiB and stored in bytes. */
    private const GIB_FIELDS = ['local_byte_budget', 'minimum_free_bytes'];
    private const LINE_LISTS = ['job_lock_paths', 'extra_docroots'];
    private const WEEKDAY_FIELDS = ['full_weekdays', 'database_weekdays'];
    /** Paths the engine needs before it can check anything else. */
    private const BOOTSTRAP = ['private_root', 'extra_docroots', 'python', 'php'];
    private const ENVIRONMENT_ERRORS = ['python-binary-invalid', 'php-binary-mismatch', 'private-root-invalid', 'private-root-unavailable', 'document-roots-invalid', 'engine-unavailable', 'installation-mismatch', 'owner-mismatch', 'private-state-permissions'];
    /** Admission results that only delay a queued backup. */
    private const TRANSIENT = ['billing-window-protected', 'billing-guard-stale', 'job-lock-busy', 'busy'];
    private const SCHEDULER_STALE_SECONDS = 300;
    /** Engine run modes, in the order the page shows them. */
    private const KINDS = ['database-online' => 'database', 'full-quiesced' => 'full'];
    private const KIND_LABELS = ['database-online' => 'Database backups', 'full-quiesced' => 'Full backups'];
    private const ACTIVE = ['pending', 'deferred', 'launching', 'running'];
    /** "Runs at 00:15, 06:15, …" for the hourly database schedule, evaluated in the browser. */
    private const HOURLY_SENTENCE_JS = <<<'JS'
        (() => {
            const every = [1, 2, 3, 4, 6, 8, 12].includes(parseInt($get('database_every_hours'))) ? parseInt($get('database_every_hours')) : 1;
            const minute = Math.min(59, Math.max(0, parseInt($get('database_minute')) || 0));
            const zone = $get('schedule_timezone') ? ' ' + $get('schedule_timezone') : '';
            const pad = (value) => String(value).padStart(2, '0');
            if (every === 1) { return 'Runs every hour at :' + pad(minute) + zone + '.'; }
            const slots = [];
            for (let hour = 0; hour < 24; hour += every) { slots.push(pad(hour) + ':' + pad(minute)); }
            return 'Runs at ' + (slots.length <= 4 ? slots.slice(0, -1).join(', ') + ' and ' + slots[slots.length - 1] : slots.slice(0, 3).join(', ') + ' and so on') + zone + '.';
        })()
        JS;
    /** Codes about Paymenter's cron job itself, shown as one item. */
    private const CRON_CODES = ['scheduler-missing', 'billing-guard-stale'];
    /** Checks that only a running cron job refreshes. */
    private const CRON_QUIET = ['job-discovery-unknown', 'job-discovery-stale', 'discovery-stale'];
    /** Setup items that inform but never stop Create backup. */
    private const ADVISORY = ['billing-window-protected', 'job-lock-busy', 'busy', 'storage-untested',
        // Uploads wait for these; backups on this server still run.
        'archive-key-required', 'archive-key-save-required', 'archive-encryption-tool-missing', 'archive-encryption-tool-unusable'];

    /** Nonsecret settings form state. */
    public ?array $settings = [];
    /** Write-only. Reset on every response. */
    public ?array $s3Keys = [];
    /** Write-only. Reset on every response. The site address comes from Paymenter, never the browser. */
    public ?array $siteAccess = [];
    /** The key choice persists; the passphrase is write-only and reset on every response. */
    public ?array $encryptionInput = ['source' => 'generate', 'passphrase' => null];
    public bool $showDeleted = false;
    /** Nonsecret fingerprint of the recovery key this administrator was shown or created. */
    #[Locked]
    public ?string $recoveryFingerprint = null;
    /** Exact key named when the acknowledgement dialog opened. */
    #[Locked]
    public ?string $acknowledgementFingerprint = null;
    /** Server-set window in which this component may render the key it was granted, after the confirmed Show step. */
    #[Locked]
    public ?int $revealGrantUntil = null;
    #[Locked]
    public ?string $revealGrantKey = null;
    /** Nonsecret: which typed secret groups a response cleared before they were saved. */
    #[Locked]
    public array $secretsCleared = [];

    private ?array $overviewCache = null;
    /** Setup items for the current overview, recomputed when the overview is read again. */
    private ?array $setupCache = null;
    private ?array $setupCacheFor = null;
    /** Current native rows in form shape; the server-side baseline for change detection. */
    private ?array $storedForm = null;
    private int $storedVersion = 2;
    private array $malformedKeys = [];
    private ?array $storedRows = null;
    #[Locked]
    public ?string $publicDirectoriesRepairPin = null;
    private ?string $overviewError = null;
    private ?string $overviewErrorCause = null;
    private array $deletePreviews = [];
    /** Revealed key for this request only; never a public property. */
    private ?array $revealedKey = null;
    private bool $collectingErrors = false;
    /** Secret groups this request actually saved; the others were cleared unsaved. */
    private array $secretsSaved = [];
    private const REVEAL_GRANT = 'backups.recovery_key_reveal';

    public static function canAccess(): bool
    {
        return Auth::user()?->hasPermission('admin.extensions.update') ?? false;
    }

    private function authorizeAction(): void { abort_unless(static::canAccess(), 403); }

    /** Download and recovery-key routes live in the panel's authenticated route group. */
    public static function routes(Panel $panel, ?PageConfiguration $configuration = null): void
    {
        parent::routes($panel, $configuration);
        Route::get(static::getRoutePath($panel) . '/download/{run}', DownloadController::class)
            ->middleware(static::getRouteMiddleware($panel))
            ->withoutMiddleware(static::getWithoutRouteMiddleware($panel))
            ->where('run', '[A-Za-z0-9_-]{1,100}')
            ->name(static::getRelativeRouteName($panel) . '.download');
        // Appends /recovery-key and .recovery-key; the route also requires a signed, five-minute link.
        RecoveryKeyController::register($panel, static::getRoutePath($panel), static::getRelativeRouteName($panel),
            static::getRouteMiddleware($panel), static::getWithoutRouteMiddleware($panel));
    }

    /** Excluded from SPA navigation, which would load the attachment as a page instead of saving it. Action evaluates the URL before rendering the anchor. */
    public static function downloadUrl(string $runId): string
    {
        $url = route(static::getRouteName() . '.download', ['run' => $runId]);
        FilamentView::spaUrlExceptions([$url]);
        return $url;
    }

    public function mount(): void
    {
        $this->authorizeAction();
        $this->settingsForm->fill($this->formValues());
        $this->publicDirectoriesRepairPin = in_array('extra_docroots', $this->malformedKeys, true)
            ? hash('sha256', serialize($this->storedRows)) : null;
        $this->s3KeysForm->fill();
        $this->siteAccessForm->fill();
        $this->encryptionForm->fill(['source' => 'generate']);
    }

    /** Secrets never travel back to the browser, including after validation errors. */
    /** Runs before the view renders, so the note appears in the same response that clears the fields. */
    public function rendering(): void
    {
        $this->noteClearedSecrets();
    }

    public function dehydrate(): void
    {
        $this->noteClearedSecrets();
        $this->s3Keys = ['access_key' => null, 'secret_key' => null, 'session_token' => null];
        $this->siteAccess = ['username' => null, 'password' => null];
        $this->encryptionInput = ['source' => in_array($this->encryptionInput['source'] ?? null, ['generate', 'passphrase'], true) ? $this->encryptionInput['source'] : 'generate', 'passphrase' => null];
        $this->revealedKey = null;
    }

    /**
     * Typed secrets that this response clears without saving get an inline note,
     * so Save never silently keeps the old values the administrator meant to replace.
     */
    private function noteClearedSecrets(): void
    {
        $typed = [
            's3' => array_filter($this->s3Keys ?? [], 'filled') !== [],
            'site' => array_filter($this->siteAccess ?? [], 'filled') !== [],
            'passphrase' => filled($this->encryptionInput['passphrase'] ?? null),
        ];
        foreach ($typed as $group => $present) {
            if (isset($this->secretsSaved[$group])) { unset($this->secretsCleared[$group]); }
            elseif ($present) { $this->secretsCleared[$group] = true; }
        }
    }

    protected function onValidationError(ValidationException $exception): void
    {
        parent::onValidationError($exception);
        // Dialog fields show their own errors; only the settings form needs a notice.
        $settings = (bool) array_filter(array_keys($exception->errors()), static fn ($key) => (bool) preg_match('/^(settings|s3Keys|siteAccess|encryptionInput)\./', $key));
        if ($settings && !$this->collectingErrors) {
            Notification::make()->title('Settings not saved')->body('Fix the highlighted fields.')->danger()->send();
        }
    }

    // ---------------------------------------------------------------- Readiness

    /** A shell function defined once, so each Help command stays one short line. */
    private function terminalFunction(): string
    {
        $words = [$this->binary('python', '/usr/bin/python3'), '-I', dirname(__DIR__, 2) . '/runtime/cli.py', '--app', base_path(), '--private-root', $this->safePrivateRoot() ?? '/path/to/private', '--php', $this->binary('php', PHP_BINDIR . '/php')];
        return 'paymenter_backups() { ' . implode(' ', array_map([self::class, 'shell'], $words)) . ' "$@"; }';
    }

    // ---------------------------------------------------------------- Page content

    private function helpSections(): array
    {
        $bullets = fn (array $lines) => $this->bullets($lines);
        return [
            Section::make('What\'s in a backup')->id('backup-contents')->collapsible()->collapsed()->schema([
                $bullets([
                    'Full backup: the database and every file in Paymenter\'s folder, including .env, APP_KEY, uploads and extensions. The site stays online. Cron jobs are paused for the database and file copy; page requests can still change live data.',
                    'Database backup: the database only. The site stays online. To restore, you also need Paymenter\'s files, from a full backup or your own copy.',
                    'Backups contain customer data and secrets (.env, APP_KEY). Keep downloads private. Saved S3 keys, site passwords and recovery keys are never included.',
                ]),
            ]),
            Section::make('How backups run')->id('how-backups-run')->collapsible()->schema([
                Text::make('Backups runs from Paymenter\'s cron job, once a minute. If the cron job isn\'t running, nothing happens. Backups never edits your crontab or systemd units.'),
                Text::make(fn () => 'Add this line to the crontab of ' . ($this->checks()['jobs']['crontab_user'] ?? 'the user that owns Paymenter') . ' with crontab -e. If a scheduler entry is already there, replace it:'),
                $this->command(fn () => $this->schedulerLine()),
                Text::make('flock lets a full backup pause this job while it copies. Database backups work without it.'),
                Text::make('Copying needs HTTPS. Over HTTP, select the text to copy it.')->visible(fn () => !request()->isSecure()),
            ]),
            Section::make('Full backups: what has to be true')->id('full-backups')->collapsible()->schema([
                Text::make('A full backup waits for running cron jobs, holds their lock files, and copies the database and files. The site stays online. If extension files change during a scheduled copy, it retries at the next full backup time.'),
                Text::make('A cron job without the required lock can keep writing during the backup. The database and files in the backup could then be from different times.'),
                $bullets([
                    'Every Paymenter cron job uses a lock file. For the scheduler, use the line above.',
                    'No always-on queue worker. A worker started by systemd or Supervisor never stops, so it can\'t be paused. Run queue jobs from cron with their own lock, then stop the service:',
                ]),
                $this->command(fn () => $this->queueLine()),
                $bullets([
                    'No Redis or other outside storage for cache, sessions, queue or files. Data held there isn\'t in the backup. The Backups tab names the .env keys to change.',
                    'Nothing else writes to Paymenter\'s database or files, such as another app, a sync script or manual SQL.',
                ]),
            ]),
            Section::make('Encrypted backups in S3')->id('encryption')->collapsible()->collapsed()->schema([
                Text::make('Backups uploaded to S3 are encrypted on this server with GnuPG (AES-256) before upload. Copies on this server and downloads from this page aren\'t encrypted.'),
                Text::make('The recovery key decrypts them. Keep it in a password manager or another safe place outside this server. If this server is lost, APP_KEY and your S3 keys can\'t replace it.'),
                Text::make('A new recovery key applies to new uploads only. Earlier backups keep needing the key they were encrypted with, so keep every key you\'ve saved.'),
                Text::make('If you turn encryption off, turn on server-side encryption in the bucket if you need it.'),
            ]),
            Section::make('Schedules and missed backups')->id('schedules')->collapsible()->collapsed()->schema([
                $bullets([
                    'A new or changed schedule starts at its next time. It doesn\'t run immediately. Changing one schedule doesn\'t affect the other.',
                    'If the cron job was down and a backup was missed in the last 8 days, one backup of that type runs once it\'s back. Missed backups don\'t pile up.',
                    'When a full and a database backup are due together, the full backup runs first. If it succeeds, it also counts as the database backup.',
                    'Full backups never start close to Paymenter\'s daily billing run.',
                    'A failed upload retries the same backup, without taking a new copy. Keep its original S3 destination and recovery key available.',
                    'With a time zone other than UTC, a time skipped by a daylight-saving change doesn\'t run that day, and a repeated time runs once.',
                ]),
            ]),
            Section::make('Cleanup')->id('cleanup')->collapsible()->collapsed()->schema([
                $bullets([
                    'After each backup, Backups deletes the oldest backups beyond the number to keep, per type, on this server and in S3.',
                    'The newest verified backup in each place is always kept. So are backups not yet uploaded to S3, backups that failed verification, backups in an old S3 folder, and copies downloaded from S3 in the last 24 hours.',
                ]),
            ]),
            Section::make('Restoring')->id('restoring')->collapsible()->collapsed()->schema([
                Text::make('Restore into a new folder and a new database, never over the running site.'),
                Group::make([
                    Text::make('1. Get the backup file: Download it here, or on the server run paymenter_backups export BACKUP-ID --output /new/folder. If this server is lost, download the backup\'s files from your bucket and decrypt them with your recovery key, as the full guide shows.'),
                    Text::make('2. Check the file against its manifest: the SHA-256 and size must match.'),
                    Text::make('3. Extract it into a private folder. It holds database.sql and, for a full backup, app.tar. Extract app.tar into a new application folder.'),
                    Text::make('4. Create an empty database and import database.sql.'),
                    Text::make('5. Keep APP_KEY and the Passport keys. Change only the database connection and APP_URL in .env, and Paymenter\'s app_url setting. Before the restored site\'s cron job runs, turn off the copied Backups schedules and give it a new data directory.'),
                    Text::make('6. Test the restored site, then switch the web server over.'),
                ]),
                Text::make('Full guide on this server:'),
                $this->command(dirname(__DIR__, 2) . '/docs/RECOVERY.md'),
                Text::make(fn () => 'Installation ID: ' . ($this->overview()['installation_uuid'] ?? 'not created yet') . '.'),
            ]),
            Section::make('Command line')->id('command-line')->collapsible()->collapsed()
                ->description('Run these as the user that owns Paymenter. They work even when Paymenter won\'t start.')
                ->schema([
                    Text::make('Define this once per shell:'),
                    $this->command(fn () => $this->terminalFunction()),
                    Text::make('See backups and queued tasks:'),
                    $this->command('paymenter_backups overview --offline'),
                    Text::make('After an interrupted full backup, see what needs fixing (changes nothing):'),
                    $this->command('paymenter_backups recover'),
                    Text::make('Take the site out of the backup\'s maintenance mode, after reading the output above:'),
                    $this->command('paymenter_backups recover --execute'),
                    Text::make('If you fixed the site by hand, let new backups start:'),
                    $this->command('paymenter_backups recover acknowledge --run BACKUP-ID'),
                    Text::make('Copy a verified backup to a new folder. The backup ID is in each backup\'s Details:'),
                    $this->command('paymenter_backups export BACKUP-ID --output /new/folder'),
                ]),
        ];
    }

}
