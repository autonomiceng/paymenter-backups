<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Actions\Action;
use Filament\Forms\Components\Checkbox;
use Filament\Forms\Components\CheckboxList;
use Filament\Forms\Components\Select;
use Filament\Forms\Components\TextInput;
use Filament\Forms\Components\Textarea;
use Filament\Forms\Components\Toggle;
use Filament\Schemas\Components\Actions;
use Filament\Schemas\Components\EmbeddedSchema;
use Filament\Schemas\Components\Flex;
use Filament\Schemas\Components\Form;
use Filament\Schemas\Components\Grid;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Icon;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\Utilities\Get;
use Filament\Schemas\Schema;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Enums\FontWeight;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Admin\Support\Sheet;
use Paymenter\Extensions\Others\Backups\PrivateStore;
use Paymenter\Extensions\Others\Backups\Settings;

trait BuildsSettingsForm
{
    public function settingsForm(Schema $schema): Schema
    {
        // Private properties are rebuilt on each Livewire request. Read the
        // current native rows before deciding which repair fields validate.
        $this->formValues();
        $positive = static fn (string $key) => in_array($key, Settings::POSITIVE, true) ? 1 : 0;
        $integer = fn (string $key, string $label, ?string $suffix = null, string|\Closure|null $help = null) => TextInput::make($key)->label($label)
            ->integer()->required()->minValue($positive($key))->suffix($suffix)->helperText($help);
        $gib = fn (string $key, string $label, string $help, bool $positive) => TextInput::make($key)->label($label)->suffix('GiB')->helperText($help)
            ->numeric()->required()->inputMode('decimal')->step(0.01)
            ->rules(['numeric', $positive ? 'gt:0' : 'min:0', 'max:1000000', 'decimal:0,2'])
            ->validationMessages(['gt' => 'Enter more than 0.', 'decimal' => 'Use at most two decimals.']);
        $path = static fn (string $key, string $label, string|\Closure|null $help = null, string|\Closure|null $placeholder = null) => TextInput::make($key)->label($label)
            ->helperText($help)->placeholder($placeholder)->rules(['nullable', 'string', 'regex:/^\/[^\x00]*$/'])
            ->validationMessages(['regex' => 'Enter a full path starting with /.']);
        // Plain Laravel rules: Filament evaluates closures in rules() as its own utilities.
        $lineList = static fn (string $key, string $label, string $help) => Textarea::make($key)->label($label)->helperText($help)->rows(3)
            ->rules(['nullable', 'string', 'regex:' . Settings::PATH_LINES])
            ->validationMessages(['regex' => 'Put one full path on each line, starting with /.']);
        $time = static fn (string $key) => TextInput::make($key)->label('At')->type('time')->required()->rules(['regex:' . Settings::TIME])
            ->validationMessages(['regex' => 'Enter a time as HH:MM.']);
        $days = static fn (string $key, string $schedule) => CheckboxList::make($key)->label('On these days')->options(array_map('strval', Messages::WEEKDAYS))->columns(['default' => 2])
            ->required(fn (Get $get) => $get($schedule) === 'weekly')
            ->rules(['array'])->nestedRecursiveRules(['in:0,1,2,3,4,5,6'])
            ->validationMessages(['required' => 'Choose at least one day.'])
            ->visibleJs("\$get('{$schedule}') === 'weekly'");
        $keep = fn (string $key, string $label) => $integer($key, $label, 'backups', 'The newest verified backup is always kept.');
        $s3Policy = fn (string $type, string $label) => [
            Select::make('s3_retention_policy_' . $type)->label($label)->options(Settings::SELECTS['s3_retention_policy_' . $type])
                ->required()->selectablePlaceholder(false),
            $integer('s3_retention_keep_' . $type, 'Backups to keep in S3', 'backups')
                ->visibleJs("\$get('s3_retention_policy_{$type}') === 'count'"),
            $integer('s3_retention_days_' . $type, 'Days to keep in S3', 'days', 'The newest backup in S3 is always kept.')
                ->visibleJs("\$get('s3_retention_policy_{$type}') === 'age'"),
        ];
        $tool = fn (string $key, string $label) => $path($key, $label, null, fn () => $this->detectedTool($key));
        $zones = array_combine(\DateTimeZone::listIdentifiers(), \DateTimeZone::listIdentifiers());

        return $schema->components([
            Form::make([Sheet::split([
                Sheet::section('Public web folders need correction')->visible(fn () => in_array('extra_docroots', $this->malformedKeys, true))
                    ->description('The saved list is damaged. Enter the complete list, confirm it, and save it by itself first.')
                    ->schema(Sheet::contentRows([
                        $lineList('extra_docroots', 'Other public web folders', 'One full path per line. Leave empty only if this account has no other public web folders.'),
                        Checkbox::make('confirm_public_directories')->label('I have listed all public web folders for this account.')
                            ->accepted()->validationMessages(['accepted' => 'Confirm the complete list before saving.']),
                    ])),
                Sheet::section('First-time setup')->id('first-time-setup')
                    ->description('Choose where Backups keeps its private data. You can\'t change this after the first save.')
                    ->visible(fn () => !$this->initialized())
                    ->schema(Sheet::contentRows([
                        $path('private_root', 'Data folder', 'Holds backup history, the installation ID and encrypted keys. Must be outside Paymenter and any public web folder.', PrivateStore::defaultPath(realpath(base_path())))
                            ->visible(fn () => !$this->initialized()),
                        $lineList('extra_docroots', 'Other public web folders', 'Document roots of other sites on this account, one per line. Backups are never stored inside them.')
                            ->visible(fn () => !$this->initialized() && !in_array('extra_docroots', $this->malformedKeys, true)),
                    ])),

                Sheet::section('Schedule')->id('schedule')->description('A new or changed schedule starts at its next time.')->schema(Sheet::contentRows([
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        Group::make()->columns(1)->extraAttributes(['style' => 'align-content: start; align-self: start; min-width: 0'])->schema([
                            Text::make('Full backups')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            Text::make('The database and all Paymenter files, except logs, cache and sessions. Cron jobs pause while it copies. The site stays online.'),
                            Select::make('full_schedule')->label('Frequency')->options(Settings::SELECTS['full_schedule'])->required()->selectablePlaceholder(false),
                            $days('full_weekdays', 'full_schedule'),
                            $time('full_time')->helperText(fn () => $this->billingSentence())->visibleJs("\$get('full_schedule') !== 'off'"),
                            Flex::make([
                                Text::make('Full backups aren\'t ready yet.')->color('warning'),
                                Actions::make([$this->link('scheduleFullSetup', 'See what\'s needed', static::getUrl(['tab' => 'backups']) . '#setup')]),
                            ])->verticallyAlignCenter()->visible(fn () => $this->initialized() && $this->overviewError === null && $this->blockers('full-quiesced') !== []),
                        ]),
                        Group::make()->columns(1)->extraAttributes(['style' => 'align-content: start; align-self: start; min-width: 0'])->schema([
                            Text::make('Database backups')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            Text::make('The database only. The site stays online. To restore, you also need Paymenter\'s files.'),
                            Select::make('database_schedule')->label('Frequency')->options(Settings::SELECTS['database_schedule'])->required()->selectablePlaceholder(false),
                            Grid::make(2)->schema([
                                Select::make('database_every_hours')->label('Interval')->options(Settings::SELECTS['database_every_hours'])->required()->selectablePlaceholder(false)
                                    ->rules(['in:' . implode(',', Settings::EVERY_HOURS)]),
                                TextInput::make('database_minute')->label('At minute')->integer()->required()->minValue(0)->maxValue(59),
                            ])->visibleJs("\$get('database_schedule') === 'hourly'"),
                            // Computed in the browser, so changing these fields needs no round trip.
                            Text::make(self::HOURLY_SENTENCE_JS)->js()->visibleJs("\$get('database_schedule') === 'hourly'"),
                            $days('database_weekdays', 'database_schedule'),
                            $time('database_time')->visibleJs("['daily', 'weekly'].includes(\$get('database_schedule'))"),
                        ]),
                    ]),
                    Select::make('schedule_timezone')->label('Time zone')->options($zones)->searchable()->required()->rule('timezone')
                        ->helperText('Schedules run in this zone. UTC has no daylight-saving gaps.'),
                ])),

                Sheet::section('This server')->id('storage')->schema(Sheet::contentRows([
                    Toggle::make('keep_local')->label('Keep backups here')
                        ->helperText('When off, each backup is deleted here once its S3 copy is verified.')
                        ->accepted(fn (Get $get) => !$get('upload_s3'))
                        ->validationMessages(['accepted' => 'Keep backups on this server, upload them to S3, or both.']),
                    $path('local_path', 'Backup folder', 'In a private folder outside Paymenter. Leave empty to use the default. Changing it affects new backups only.',
                        fn () => $this->storage()['default_local_path'] ?? (($root = $this->safePrivateRoot()) ? $root . '/backups' : null)),
                    Text::make(fn () => $this->legacyDirectorySentence())->visible(fn () => $this->legacyDirectorySentence() !== null),
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        $gib('local_byte_budget', 'Space limit', 'New backups stop at this size.', true),
                        $gib('minimum_free_bytes', 'Keep free', 'Backups stop if free disk space would drop below this.', false),
                    ]),
                    Grid::make(['default' => 1, 'md' => 2])->schema([
                        $keep('retention_keep_full', 'Full backups to keep'),
                        $keep('retention_keep_database', 'Database backups to keep'),
                    ]),
                ])),

                Sheet::section('S3')->id('s3')->schema(Sheet::contentRows([
                    Toggle::make('upload_s3')->label('Upload to S3')->helperText('AWS S3 or any S3-compatible storage.'),
                    Group::make([
                        Group::make()->columns(['default' => 1, 'md' => 2])->schema([
                            Text::make('Bucket')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            TextInput::make('s3_endpoint')->label('Endpoint')->placeholder('https://s3.example.com')
                                ->helperText('Leave empty for AWS. For other providers, enter their S3 API URL, for example https://s3.example.com.')
                                ->rules(['nullable', 'regex:/^https:\/\/[^\s\/?#@]+(?:\/[^\s?#]*)?$/D'])->validationMessages(['regex' => 'Enter the provider\'s https:// API URL, without a username or query.']),
                            TextInput::make('s3_region')->label('Region')->placeholder('us-east-1'),
                            TextInput::make('s3_bucket')->label('Bucket')->helperText('Bucket name, for example my-backups. It must already exist.')
                                ->required(fn (Get $get) => (bool) $get('upload_s3'))->validationMessages(['required' => 'Enter the bucket name.']),
                            TextInput::make('s3_prefix')->label('Folder in bucket')->required()
                                ->helperText(fn () => 'Backups go to FOLDER/' . ($this->overview()['installation_uuid'] ?? 'INSTALLATION-ID') . '/. Changing it doesn\'t move existing backups.')
                                ->rules(['string', 'regex:' . Settings::S3_PREFIX])
                                ->validationMessages(['regex' => 'The folder can\'t contain "//", ".", ".." or "\\".']),
                            Toggle::make('s3_path_style')->label('Path-style addressing')->helperText('Turn on for MinIO and some other S3-compatible providers.')->inline(false),
                        ]),
                        Group::make()->columns(1)->schema([
                            Text::make('Access keys')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            Text::make('Stored encrypted on this server. Never shown again.'),
                            EmbeddedSchema::make('s3KeysForm'),
                        ]),
                        Group::make()->id('encryption')->columns(1)->schema([
                            Text::make('Encryption')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            Toggle::make('s3_encryption')->label('Encrypt backups in S3')
                                ->helperText(fn () => $this->encryptionHelp()),
                            EmbeddedSchema::make('encryptionForm')
                                ->visible(fn () => $this->keyState() === 'none')->visibleJs("\$get('s3_encryption')"),
                            Group::make([
                                Text::make(fn () => $this->recoveryKeySentence()),
                                Actions::make(fn () => [...$this->recoveryKeyActions(), $this->rotateRecoveryKeyAction(), $this->discardRecoveryKeyAction()]),
                            ])->dense()->visible(fn () => $this->keyState() !== 'none')->visibleJs("\$get('s3_encryption')"),
                            Checkbox::make('plaintext_offsite_acknowledged')->label('I understand backups in S3 won\'t be encrypted')
                                ->helperText('They contain customer data, .env and APP_KEY. Give these keys access to this bucket only.')
                                ->accepted(fn (Get $get) => (bool) $get('upload_s3') && !$get('s3_encryption'))
                                ->validationMessages(['accepted' => 'Confirm this, or turn encryption on.'])
                                ->visibleJs("! \$get('s3_encryption')"),
                        ]),
                        Group::make()->columns(['default' => 1, 'md' => 2])->schema([
                            Text::make('Keep in S3')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            Grid::make(1)->schema($s3Policy('full', 'Full backups')),
                            Grid::make(1)->schema($s3Policy('database', 'Database backups')),
                        ]),
                    ])->visibleJs("\$get('upload_s3')"),
                ])),

                Sheet::section('Cleanup')->id('cleanup')->schema(Sheet::contentRows([
                    Toggle::make('automatic_retention')->label('Delete old backups')
                        ->helperText('After each backup, deletes backups past Keep.'),
                ])),

                Sheet::section('Full backups need')->id('full-backup-requirements')->schema(Sheet::contentRows([
                    Checkbox::make('full_host_assumptions_acknowledged')->label('Nothing but Paymenter writes to its database or files.')
                        ->helperText('Only Paymenter\'s cron jobs pause during a full backup. Writes from other programs can leave a broken backup.'),
                    Checkbox::make('no_concurrent_ddl_acknowledged')->label('No updates, extension installs or migrations while a backup runs.')
                        ->helperText('A change to the database during a backup can make its copy unusable. This applies to both backup types.')
                        ->extraFieldWrapperAttributes(['id' => 'before-backups-run']),
                ])),

                Sheet::section('Advanced')->id('advanced')->description('Binary paths, time limits and folders. Most sites don\'t need to change these.')
                    ->collapsible()->collapsed()->schema(Sheet::contentRows([
                        Group::make()->columns(['default' => 1, 'md' => 2])->schema([
                            Text::make('Binary paths')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            Text::make('An empty path uses the binary Backups detects.')->columnSpanFull(),
                            $tool('python', 'Python 3'), $tool('php', 'PHP'), $tool('dump', 'mysqldump or mariadb-dump'),
                            $tool('mysql', 'mysql or mariadb client'), $tool('tar', 'tar'), $tool('gzip', 'gzip'), $tool('gpg', 'GnuPG (gpg)'),
                        ]),
                        Group::make()->columns(['default' => 1, 'md' => 2])->schema([
                            Text::make('Time limits')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            $integer('capture_timeout', 'Backup time limit', 'seconds', 'Limits the database export, extension checks and file copy while cron jobs are paused.'),
                            $integer('lock_timeout', 'Wait for running cron jobs', 'seconds', 'If a Paymenter cron job is running, wait this long before trying again next minute.'),
                            $integer('billing_margin_seconds', 'Billing safety margin', 'seconds', 'Extra time kept clear before and after Paymenter\'s daily billing run.'),
                            $integer('request_max_age', 'Queued task expiry', 'seconds', 'Tasks that haven\'t started after this long are dropped.'),
                            $integer('s3_timeout', 'S3 timeout', 'seconds', 'Per request.'),
                            $integer('s3_retries', 'S3 retries', 'attempts', 'Total attempts per request, including the first.'),
                        ]),
                        Group::make()->columns(1)->schema([
                            Text::make('Lock files')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            $lineList('job_lock_paths', 'Lock files (override)', 'Leave empty to use the lock files your cron jobs use. One full path per line.'),
                        ]),
                        Group::make()->columns(['default' => 1, 'md' => 2])->visible(fn () => $this->initialized())->schema([
                            Text::make('Folders')->weight(FontWeight::SemiBold)->columnSpanFull(),
                            $path('private_root', 'Data folder', 'Holds backup history, the installation ID and encrypted keys. Can\'t be changed after setup.')
                                ->disabled()->visible(fn () => $this->initialized()),
                            $lineList('extra_docroots', 'Other public web folders', 'Document roots of other sites on this account, one per line. Backups are never stored inside them.')
                                ->visible(fn () => $this->initialized() && !in_array('extra_docroots', $this->malformedKeys, true)),
                        ]),
                    ])),
            ], [
                Sheet::section('Full backup checks')->id('full-backup-checks')
                    ->schema(Sheet::contentRows(fn () => $this->detectedChecks())),
            ])])->livewireSubmitHandler('saveSettings')->footer([
                Actions::make([Action::make('saveSettings')->label(fn () => $this->initialized() ? 'Save' : 'Save and finish setup')->submit('saveSettings')->keyBindings(['mod+s'])]),
            ]),
        ])->statePath('settings');
    }

    private function detectedTool(string $tool): ?string
    {
        $checked = $this->checks()['tools'][$tool] ?? null;
        if (!is_array($checked)) { return null; }
        return is_string($checked['resolved'] ?? null) && $checked['resolved'] !== '' ? $checked['resolved'] : 'Not found';
    }

    private function legacyDirectorySentence(): ?string
    {
        $legacy = $this->storage()['legacy_default_path'] ?? null;
        if (!is_string($legacy) || $legacy === '') { return null; }
        $stored = (string) ($this->formValues()['local_path'] ?? '');
        if ($stored === $legacy) { return 'This is the old default folder. Clear the field to use the new default for new backups.'; }
        return $stored === '' ? 'Earlier backups stay in ' . $legacy . '.' : null;
    }

    /** Detected jobs and stores, each with its fix. */
    private function detectedChecks(): array
    {
        $jobs = $this->checks()['jobs'] ?? null;
        $rows = [];
        $row = function (string $tone, string $title, array $lines = []) {
            $icon = match ($tone) { 'success' => 'ri-checkbox-circle-line', 'gray' => 'ri-question-line', default => 'ri-error-warning-line' };
            return Flex::make([Icon::make($icon)->color($tone)->grow(false), Group::make([Text::make($title), ...$lines])->grow()->extraAttributes(['style' => 'min-width: 0'])])->verticallyAlignStart();
        };
        if (!is_array($jobs)) {
            $rows[] = $row('gray', 'Cron jobs haven\'t been checked yet. The check runs from Paymenter\'s cron job.');
        } else {
            $user = $jobs['crontab_user'] ?? 'the site user';
            $state = $jobs['state'] ?? 'unknown';
            if ($state === 'failed') {
                $rows[] = $row('warning', 'Couldn\'t read the crontab or systemd units for ' . $user . '.');
            } elseif ($state === 'stale') {
                $rows[] = $row('gray', 'Cron jobs were last checked ' . $this->ago($jobs['checked_at'] ?? null) . '. The check refreshes while Paymenter\'s cron job runs.');
            } elseif ($state !== 'ok') {
                $rows[] = $row('gray', 'Cron jobs haven\'t been checked yet. The check runs from Paymenter\'s cron job.');
            }
            $hasScheduler = false;
            foreach ($jobs['items'] ?? [] as $job) {
                if (!is_array($job)) { continue; }
                $kind = ($job['kind'] ?? null) === 'queue-worker' ? 'Queue worker' : 'Cron job';
                $hasScheduler = $hasScheduler || ($job['kind'] ?? null) === 'scheduler';
                $source = ($job['source'] ?? null) === 'systemd' ? ' (systemd ' . ($job['label'] ?? 'unit') . ')' : '';
                $command = filled($job['command'] ?? null) ? [Text::make((string) $job['command'])->fontFamily(FontFamily::Mono)->extraAttributes(['tabindex' => '0', 'style' => 'display: block; min-width: 0; width: 100%; overflow-wrap: normal; overflow-x: auto; max-width: 100%; white-space: nowrap'])] : [];
                $fix = $kind === 'Queue worker' ? $this->queueLine() : $this->schedulerLine();
                $systemd = ($job['source'] ?? null) === 'systemd' && filled($job['label'] ?? null);
                $rows[] = match ($job['coordination'] ?? null) {
                    'coordinated' => $row('success', $kind . $source . ' uses lock file ' . ($job['lock'] ?? '') . '.', $command),
                    'continuous' => $row('warning', $kind . $source . ' runs all the time, so full backups can\'t pause it.', [...$command,
                        ...($systemd ? [Text::make('Stop and disable it:'), $this->command('systemctl disable --now ' . $job['label'])] : []),
                        Text::make('Run queue jobs from cron instead:'), $this->command($this->queueLine())]),
                    'unsupported' => $row('warning', $kind . $source . ': Backups can\'t read this line.', [...$command, Text::make('Replace it with:'), $this->command($fix)]),
                    default => $row('warning', $kind . $source . ' has no lock file.', [...$command, Text::make('Replace it with:'), $this->command($fix)]),
                };
            }
            if (!$hasScheduler && in_array($jobs['state'] ?? null, ['ok', 'stale'], true)) {
                $rows[] = $row('warning', 'No cron job runs Paymenter\'s scheduler. Add this line to the crontab of ' . $user . ':', [$this->command($this->schedulerLine())]);
            }
        }
        foreach ($this->checks()['stores'] ?? [] as $store) {
            if (!is_array($store) || ($store['kind'] ?? null) === 'maintenance') { continue; }
            $name = Messages::STORES[$store['kind'] ?? ''] ?? 'Storage';
            $driver = (string) ($store['driver'] ?? 'not detected');
            $rows[] = match ($store['supported'] ?? null) {
                true => $row('success', $name . ': ' . $driver),
                false => $row('warning', $name . ': ' . $driver . '. Full backups need local storage. Change it in .env:',
                    filled($store['env'] ?? null) && filled($store['suggest'] ?? null) ? [$this->command($store['env'] . '=' . $store['suggest'])] : []),
                default => $row('gray', $name . ': ' . $driver . '. Backups can\'t tell whether this keeps data outside Paymenter.'),
            };
        }
        return $rows;
    }
}
