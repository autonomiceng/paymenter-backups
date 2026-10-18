<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages;

use Filament\Actions\Action;
use Filament\Actions\ActionGroup;
use Filament\Forms\Components\Radio;
use Filament\Infolists\Components\TextEntry;
use Filament\Notifications\Notification;
use Filament\Pages\Page;
use Filament\Pages\PageConfiguration;
use Filament\Panel;
use Filament\Schemas\Components\Actions;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\Grid;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\Utilities\Get;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Enums\Size;
use Filament\Support\Facades\FilamentView;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Concerns\InteractsWithTable;
use Filament\Tables\Contracts\HasTable;
use Filament\Tables\Table;
use Illuminate\Pagination\LengthAwarePaginator;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Route;
use Illuminate\Validation\ValidationException;
use Livewire\Attributes\Locked;
use Paymenter\Extensions\Others\Backups\Http\DownloadController;
use Paymenter\Extensions\Others\Backups\Http\RecoveryKeyController;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Runtime;
use Paymenter\Extensions\Others\Backups\Settings;

class Backups extends Page implements HasTable
{
    use InteractsWithTable;
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
                    'Full backup: the database plus every file in Paymenter\'s folder, including .env, APP_KEY, uploads and extensions. While the database and files are copied, the site shows its maintenance page (HTTP 503), up to the backup time limit. Compression and verification run after the site is back online.',
                    'Database backup: the database only. The site stays online. To restore, you also need Paymenter\'s files, from a full backup or your own copy.',
                    'Backups contain customer data and secrets (.env, APP_KEY). Keep downloads private. Saved S3 keys, site passwords and recovery keys are never included.',
                ]),
            ]),
            Section::make('How backups run')->id('how-backups-run')->collapsible()->schema([
                Text::make('Paymenter\'s cron job runs schedule:run every minute. Each minute, Backups starts what is queued: scheduled backups, Create backup, uploads, verification and deletion. If the cron job isn\'t running, nothing happens. Backups never edits your crontab or systemd units.'),
                Text::make(fn () => 'Add this line to the crontab of ' . ($this->checks()['jobs']['crontab_user'] ?? 'the user that owns Paymenter') . ' with crontab -e. If a schedule:run line is already there, replace it:'),
                $this->command(fn () => $this->schedulerLine()),
                Text::make('flock lets a full backup pause this job while it copies. Database backups work without it.'),
                Text::make('Copying needs HTTPS. Over HTTP, select the text to copy it.')->visible(fn () => !request()->isSecure()),
            ]),
            Section::make('Full backups: what has to be true')->id('full-backups')->collapsible()->schema([
                Text::make('During a full backup, Backups waits for running Paymenter cron jobs to finish and holds their lock files so new ones wait. It then puts the site into maintenance mode, waits for open page loads, checks that App URL shows the maintenance page, copies the database and files, and takes the site out of maintenance mode.'),
                Text::make('Maintenance mode stops new page loads, but a cron job that already started keeps writing. If it writes while files are copied, the database and files in the backup won\'t match. Holding the same lock file as the job prevents that.'),
                $bullets([
                    'Every Paymenter cron job uses a lock file. For schedule:run, use the line above.',
                    'No always-on queue worker. A worker started by systemd or Supervisor never stops, so it can\'t be paused. Run queue jobs from cron with their own lock, then stop the service:',
                ]),
                $this->command(fn () => $this->queueLine()),
                $bullets([
                    'No Redis or other outside storage for cache, sessions, queue or files. Data held there isn\'t in the backup. The Backups tab names the .env keys to change.',
                    'File-based maintenance mode: APP_MAINTENANCE_DRIVER=file, Paymenter\'s default.',
                    'Nothing else writes to Paymenter\'s database or files, such as another app, a sync script or manual SQL.',
                    'If your web server asks for a username and password before Paymenter loads, enter it under Settings, Site password.',
                ]),
            ]),
            Section::make('Encrypted backups in S3')->id('encryption')->collapsible()->collapsed()->schema([
                Text::make('Backups uploaded to S3 are encrypted on this server with GnuPG (AES-256) before upload. Copies on this server and downloads from this page aren\'t encrypted.'),
                Text::make('The recovery key decrypts them. Keep it in a password manager or another safe place outside this server. If this server is lost, APP_KEY and your S3 keys can\'t replace it.'),
                Text::make('A new recovery key applies to new uploads only. Earlier backups keep needing the key they were encrypted with, so keep every key you\'ve saved.'),
            ]),
            Section::make('Schedules and missed backups')->id('schedules')->collapsible()->collapsed()->schema([
                $bullets([
                    'A new or changed schedule starts at its next time. It doesn\'t run immediately. Changing one schedule doesn\'t affect the other.',
                    'If the cron job was down and a backup was missed in the last 8 days, one backup of that type runs once it\'s back. Missed backups don\'t pile up.',
                    'When a full and a database backup are due together, the full backup runs first. If it succeeds, it also counts as the database backup.',
                    'Full backups never start close to Paymenter\'s daily billing run.',
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

    // ---------------------------------------------------------------- History table

    /** Whether a request came from a schedule, by request ID. */
    private function scheduledRequests(): array
    {
        $scheduled = [];
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] === 'run' && $request['request_id']) { $scheduled[$request['request_id']] = (bool) $request['scheduled']; }
        }
        return $scheduled;
    }

    /** History rows: queued or refused backup requests, then backups. */
    private function historyRows(bool $showDeleted): array
    {
        $rows = [];
        $started = [];
        $scheduled = $this->scheduledRequests();
        $uploads = $this->uploadsToS3();
        foreach ($this->overview()['runs'] as $run) {
            if ($run['request_id']) { $started[$run['request_id']] = true; }
        }
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] !== 'run' || isset($started[$request['request_id']]) || $request['result_run_id']) { continue; }
            if (in_array($request['status'], ['complete', 'cancelled', 'satisfied'], true)) { continue; }
            $active = in_array($request['status'], self::ACTIVE, true);
            [$label, $color] = $active || in_array($request['status'], ['expired', 'skipped'], true) ? Messages::taskStatus($request['status']) : ['Didn\'t start', 'danger'];
            $detail = match ($request['status']) {
                'pending' => $this->schedulerFresh() ? 'Starts within a minute.' : 'Waiting for Paymenter\'s cron job.',
                'deferred' => 'Retrying soon. ' . Messages::error($request['error']),
                'launching' => 'Starting.', 'running' => 'Running.',
                'expired' => 'Expired: Paymenter\'s cron job didn\'t run in time.',
                default => Messages::error($request['error']),
            };
            $rows[] = ['__key' => 'request-' . $request['request_id'], 'kind' => 'request', 'request_id' => $request['request_id'],
                'created_at' => $request['created_at'], 'type' => Messages::type($request['mode'] ?: ($this->configuration()['mode'] ?? null)), 'origin' => $request['scheduled'] ? 'Scheduled' : 'Manual',
                'size' => null, 'size_label' => '—', 'status_label' => $label, 'status_color' => $color, 'detail' => $detail, 'location' => '—',
                'cancel_request' => in_array($request['status'], ['pending', 'deferred'], true) ? $request['request_id'] : null,
                'run_id' => null, 'run' => null];
        }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'deleted' && !$showDeleted) { continue; }
            [$label, $color] = Messages::runStatus($run);
            $detail = Messages::runDetail($run + ['maintenance' => $this->overview()['maintenance'] ?? false]);
            $cancel = null;
            $tasks = $this->tasksFor($run['run_id']);
            $active = array_values(array_filter($tasks, static fn ($task) => in_array($task['status'], self::ACTIVE, true)));
            if ($active) {
                $task = $active[0];
                $detail = Messages::taskProgress($task['action'], $task['status']) . ($detail ? ' ' . $detail : '');
                if (in_array($task['status'], ['pending', 'deferred'], true)) { $cancel = $task['request_id']; }
            } elseif ($run['status'] === 'ready') {
                // Only the newest finished delete matters; a later success hides an older failure.
                $latest = null;
                foreach ($tasks as $task) {
                    if ($task['action'] === 'delete' && ($latest === null || $task['created_at'] > $latest['created_at'])) { $latest = $task; }
                }
                if ($latest && $latest['status'] === 'failed') { $detail = 'The last deletion didn\'t finish. ' . Messages::error($latest['error']); }
            }
            $origin = $run['request_id'] && isset($scheduled[$run['request_id']]) ? ($scheduled[$run['request_id']] ? 'Scheduled' : 'Manual') : null;
            $rows[] = ['__key' => $run['run_id'], 'kind' => 'run', 'request_id' => null, 'created_at' => $run['created_at'],
                'type' => Messages::type($run['mode']), 'origin' => $origin, 'size' => $run['size'], 'size_label' => $run['size'] === null ? 'Unknown' : Messages::bytes($run['size']), 'status_label' => $label, 'status_color' => $color,
                'detail' => $detail, 'location' => Messages::location($run, $uploads), 'cancel_request' => $cancel,
                'run_id' => $run['run_id'], 'run' => $run];
        }
        usort($rows, static fn ($a, $b) => ($b['created_at'] ?? 0) <=> ($a['created_at'] ?? 0));
        return $rows;
    }

    private function hasDeletedRuns(): bool
    {
        return array_filter($this->overview()['runs'], static fn ($run) => $run['status'] === 'deleted') !== [];
    }

    public function table(Table $table): Table
    {
        $isRun = static fn (array $record) => $record['kind'] === 'run';
        $inS3 = static fn (array $record) => in_array($record['run']['remote'] ?? null, ['delivered', 'present'], true);
        return $table
            ->heading('Backup history')
            ->records(function (int|string $page, int|string $recordsPerPage) {
                $rows = $this->historyRows($this->showDeleted);
                $perPage = max(1, (int) $recordsPerPage);
                $page = max(1, (int) $page);
                return new LengthAwarePaginator(array_slice($rows, ($page - 1) * $perPage, $perPage), count($rows), $perPage, $page);
            })
            ->paginated([10, 25, 50])
            // No polling while a dialog is open: it would re-render the dialog, including a shown recovery key.
            ->poll(fn () => $this->hasActiveWork() && !$this->mountedActions ? '5s' : null)
            // Below sm each row stacks, so long status details and both row actions get the full width.
            ->stackedOnMobile()
            ->headerActions([
                Action::make('toggleDeleted')->label(fn () => $this->showDeleted ? 'Hide deleted' : 'Show deleted')->link()->color('gray')
                    ->visible(fn () => $this->hasDeletedRuns())
                    ->action(function () { $this->showDeleted = !$this->showDeleted; $this->resetPage(); }),
            ])
            ->columns([
                TextColumn::make('created_at')->label(fn () => 'Created (' . $this->zone() . ')')
                    ->formatStateUsing(fn ($state) => $this->time($state))
                    ->description(fn (array $record) => $this->ago($record['created_at'])),
                // Below md these move to Details so the table stays readable.
                TextColumn::make('type')->label('Type')->description(fn (array $record) => $record['origin'])->visibleFrom('md'),
                TextColumn::make('size_label')->label('Size')->visibleFrom('md'),
                TextColumn::make('status_label')->label('Status')->badge()->color(fn (array $record) => $record['status_color'])
                    ->description(fn (array $record) => $record['detail'])->wrap(),
                TextColumn::make('location')->label('Stored on')->visibleFrom('lg'),
            ])
            ->recordActions([
                Action::make('download')->label('Download')->icon('ri-download-2-line')->button()->color('gray')
                    ->url(fn (array $record) => static::downloadUrl($record['run_id']))
                    ->visible(fn (array $record) => $isRun($record) && $record['run']['download'] === 'ready'),
                Action::make('prepareDownload')->label('Download from S3')->icon('ri-download-cloud-2-line')->button()->color('gray')
                    ->visible(fn (array $record) => $isRun($record) && $record['run']['download'] === 'retrieve'
                        && !$this->activeRequest('retrieve', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('retrieve', ['run_id' => $record['run_id']], 'Download from S3 queued',
                        'Downloading from S3 to this server. ' . $this->startLine() . ' Download appears here when it\'s ready and stays available for 24 hours.')),
                Action::make('verifyFirst')->label('Verify')->icon('ri-shield-check-line')->button()->color('gray')
                    ->tooltip('Checks the backup against its checksums.')
                    ->visible(fn (array $record) => $isRun($record) && in_array($record['run']['status'], ['ready', 'damaged'], true)
                        && !in_array($record['run']['download'], ['ready', 'retrieve'], true) && !$this->activeRequest('verify', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                Action::make('cancelQueued')->label('Cancel')->icon('ri-close-line')->button()->color('gray')
                    ->visible(fn (array $record) => $record['kind'] === 'request' && $record['cancel_request'] !== null)
                    ->requiresConfirmation()->modalHeading('Cancel this backup?')->modalDescription('It hasn\'t started. Scheduled backups still run.')
                    ->modalSubmitActionLabel('Cancel backup')->modalCancelActionLabel('Keep')
                    ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                ActionGroup::make([
                    Action::make('details')->label('Details')->icon('ri-information-line')
                        ->modalHeading('Backup details')->modalSubmitAction(false)->modalCancelActionLabel('Close')
                        ->schema(fn (array $record) => $this->detailsSchema($record)),
                    Action::make('verify')->label('Verify')->icon('ri-shield-check-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['run']['status'] === 'ready'
                            && ($record['run']['download'] === 'ready' || (!$record['run']['local'] && $inS3($record)))
                            && !$this->activeRequest('verify', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                    Action::make('copy')->label('Upload to S3')->icon('ri-upload-cloud-2-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['run']['status'] === 'ready' && !$inS3($record)
                            && $this->uploadsToS3() && !$this->activeRequest('deliver', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('deliver', ['run_id' => $record['run_id']], 'Upload to S3 queued')),
                    Action::make('cancelTask')->label(fn (array $record) => 'Cancel ' . strtolower($this->pendingTaskLabel($record)))->icon('ri-close-circle-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['cancel_request'] !== null)
                        ->requiresConfirmation()->modalHeading(fn (array $record) => 'Cancel ' . strtolower($this->pendingTaskLabel($record)) . '?')->modalDescription('It hasn\'t started.')
                        ->modalSubmitActionLabel('Cancel task')->modalCancelActionLabel('Keep')
                        ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                    Action::make('delete')->label('Delete')->icon('ri-delete-bin-line')->color('danger')
                        ->visible(fn (array $record) => $isRun($record) && in_array($record['run']['status'], ['ready', 'damaged', 'delete-interrupted'], true)
                            && !$this->activeRequest('delete', $record['run_id']))
                        ->requiresConfirmation()
                        ->modalHeading(fn (array $record) => 'Delete the ' . strtolower(Messages::mode($record['run']['mode'])) . ' from ' . $this->time($record['created_at']) . '?')
                        ->modalDescription('This can\'t be undone.')
                        ->fillForm(fn (array $record) => ['location' => $this->defaultDeletionLocation($record)])
                        ->schema(fn (array $record) => [
                            // A choice appears only when the backup is in more than one place.
                            Radio::make('location')->label('Delete from')->required()->live()
                                ->options($places = array_filter([
                                    'all' => $record['run']['local'] && $inS3($record) ? 'This server and S3' : null,
                                    'local' => $record['run']['local'] ? 'This server' : null,
                                    's3' => !$record['run']['local'] || $inS3($record) ? 'S3' : null,
                                ]))
                                ->visible(count($places) > 1)
                                ->rule('in:all,local,s3'),
                            Callout::make(fn () => $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal'] ? 'Can\'t delete' : 'What will be deleted')->warning()
                                ->footer(fn () => [$this->bullets($this->deleteLines($record['run_id'], $this->currentDeletionLocation($record)))]),
                        ])
                        ->modalSubmitAction(fn (Action $action, array $record) => $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal'] ? false : $action)
                        ->modalSubmitActionLabel('Delete')
                        ->action(fn (array $record, array $data) => $this->queue('delete', ['run_id' => $record['run_id'], 'location' => $data['location'] ?? $this->defaultDeletionLocation($record), 'execute' => true], 'Deletion queued')),
                ])->label('More actions')->icon('ri-more-2-fill')->color('gray')->tooltip('More actions'),
            ])
            ->emptyStateIcon('ri-save-line')
            ->emptyStateHeading('No backups yet')
            ->emptyStateDescription(fn () => $this->setupOpen() && $this->nothingCanStart() ? 'Finish setup above to create your first backup.' : 'Create a backup or turn on a schedule.')
            ->emptyStateActions([$this->backupAction('createFirstBackup')->visible(fn () => !$this->nothingCanStart())]);
    }

    private function pendingTaskLabel(array $record): string
    {
        foreach ($this->tasksFor($record['run_id'] ?? '') as $task) {
            if ($task['request_id'] === $record['cancel_request']) { return Messages::TASKS[$task['action']] ?? 'Task'; }
        }
        return 'Task';
    }

    private function callCancel(string $requestId): void
    {
        $this->authorizeAction();
        try {
            Runtime::command(['requests', 'cancel', $requestId]);
            $this->overviewCache = null;
            Notification::make()->title('Task cancelled')->success()->send();
        } catch (\Throwable $error) {
            Notification::make()->title('Task not cancelled')->danger()->body(Messages::error($error->getMessage()))->send();
        }
    }

    private function defaultDeletionLocation(array $record): string
    {
        return $record['run']['local'] && in_array($record['run']['remote'], ['delivered', 'present'], true)
            ? 'all' : ($record['run']['local'] ? 'local' : 's3');
    }

    private function currentDeletionLocation(array $record): string
    {
        // getData() is populated only after submission. The original mounted
        // action owns the live radio state; the injected footer action does not.
        $location = $this->getMountedAction()?->getRawData()['location'] ?? $this->defaultDeletionLocation($record);
        return in_array($location, ['local', 's3', 'all'], true) ? $location : 'invalid';
    }

    private function deletePreview(string $runId, string $location = 'all'): array
    {
        $key = $runId . ':' . $location;
        if (isset($this->deletePreviews[$key])) { return $this->deletePreviews[$key]; }
        try {
            $preview = Runtime::command(['delete', $runId, '--location', $location], null, 20);
        } catch (\Throwable $error) {
            $preview = ['refusal' => $error instanceof \RuntimeException ? $error->getMessage() : 'engine-unavailable', 'local_copy' => false, 'remote_copy' => null, 'survivors' => []];
        }
        return $this->deletePreviews[$key] = $preview;
    }

    /** What a deletion removes and what stays, one fact per line. */
    private function deleteLines(string $runId, string $location = 'all'): array
    {
        $preview = $this->deletePreview($runId, $location);
        if ($preview['refusal']) { return [Messages::error($preview['refusal'])]; }
        $lines = [];
        if ($preview['local_copy']) { $lines[] = 'The backup file on this server.'; }
        if ($preview['remote_copy']) { $lines[] = 'The backup in S3 bucket ' . $preview['remote_copy']['bucket'] . '. If bucket versioning is on, old versions stay.'; }
        $lines[] = 'Another verified backup must remain in each place; otherwise nothing is deleted.';
        return $lines;
    }

    private function detailsSchema(array $record): array
    {
        if ($record['kind'] !== 'run') {
            return [
                TextEntry::make('d_task')->label('Task ID')->state($record['request_id'])->fontFamily(FontFamily::Mono)->copyable(request()->isSecure()),
                TextEntry::make('d_status')->label('Status')->state($record['status_label'] . '. ' . $record['detail']),
            ];
        }
        $run = $record['run'];
        $code = $run['error'] ?? $run['verification_error'] ?? $run['delivery_error'] ?? $run['retention_error'] ?? null;
        $format = Messages::remoteFormat($run['remote_format'] ?? null, $run['key_short'] ?? null);
        return [
            TextEntry::make('d_id')->label('Backup ID')->state($run['run_id'])->fontFamily(FontFamily::Mono)
                ->copyable(request()->isSecure())->copyMessage('Copied'),
            Grid::make(2)->schema([
                TextEntry::make('d_created')->label('Created')->state($this->time($run['created_at'], true)),
                TextEntry::make('d_type')->label('Type')->state(Messages::mode($run['mode']) . ($record['origin'] ? ', ' . strtolower($record['origin']) : '')),
                TextEntry::make('d_size')->label('Size')->state(Messages::bytes($run['size'])),
                TextEntry::make('d_location')->label('Stored on')->state($record['location']),
                TextEntry::make('d_remote_format')->label('S3 copy')->state($format . (($run['ciphertext_size'] ?? null) !== null ? ', ' . Messages::bytes($run['ciphertext_size']) : ''))->visible($format !== null),
                TextEntry::make('d_verified')->label('Last verified')->state($run['verified_at'] ? $this->time($run['verified_at'], true) . ($run['verification_source'] === 'capture' ? ' (at creation)' : '') : 'Not recorded'),
                TextEntry::make('d_status')->label('Status')->state($record['status_label']),
                TextEntry::make('d_retrieved')->label('Downloaded copy kept until')->state(fn () => $this->time($run['local_download_until'] ?? null, true))
                    ->visible((bool) ($run['local_download_until'] ?? null)),
            ]),
            TextEntry::make('d_detail')->label('Details')->state($record['detail'])->visible((bool) $record['detail']),
            TextEntry::make('d_code')->label('Error code')->state($code)->fontFamily(FontFamily::Mono)->visible((bool) $code),
        ];
    }
}
