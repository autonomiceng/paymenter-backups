<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\Actions;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\Component;
use Filament\Schemas\Components\EmbeddedSchema;
use Filament\Schemas\Components\EmbeddedTable;
use Filament\Schemas\Components\Fieldset;
use Filament\Schemas\Components\Flex;
use Filament\Schemas\Components\Grid;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Icon;
use Filament\Schemas\Components\Section;
use Filament\Schemas\Components\Tabs;
use Filament\Schemas\Components\Tabs\Tab;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Schema;
use Filament\Support\Enums\FontWeight;
use Filament\Support\Enums\Size;
use Paymenter\Extensions\Others\Backups\Messages;
use Paymenter\Extensions\Others\Backups\Settings;

trait BuildsPageContent
{
    /** One setup item: icon, title, one line, exact fixes and at most one action. */
    private function setupItem(array $item, string $prefix): Component
    {
        $tone = $item['tone'];
        $children = [Group::make([
            Text::make($item['title'])->weight(FontWeight::SemiBold)->color('gray'),
            Text::make($item['text']),
        ])->gap(false)];
        foreach ($item['fixes'] as $fix) {
            if (filled($fix['lead'] ?? null)) { $children[] = Text::make($fix['lead']); }
            $children[] = $this->command($fix['code']);
        }
        $action = $item['action'];
        if ($action) {
            $name = $prefix . preg_replace('/[^A-Za-z0-9]/', '', ucwords($item['code'], '-'));
            $actions = match ($action['kind']) {
                'settings' => [$this->link($name, $action['label'], $this->settingsUrl($action['target'] ?? 'settings'))],
                'help' => [$this->link($name, $action['label'], static::getUrl(['tab' => 'help']) . '#' . ($action['target'] ?? 'help'))],
                'test-storage' => $this->schedulerFresh() ? [$this->testStorageAction()->button()->size(Size::Small)] : [],
                'recovery-key' => $this->recoveryKeyActions(),
                default => [],
            };
            if ($actions) { $children[] = Actions::make($actions); }
        }
        return Flex::make([
            Icon::make(match ($tone) { 'danger' => 'ri-alert-line', 'info' => 'ri-information-line', default => 'ri-error-warning-line' })
                ->color(match ($tone) { 'danger' => 'danger', 'info' => 'info', default => 'warning' })->grow(false),
            Group::make($children)->dense()->grow(),
        ])->verticallyAlignStart();
    }

    public function content(Schema $schema): Schema
    {
        return $schema->components([
            Callout::make('Settings save needs attention')
                ->warning()
                ->description(fn () => Messages::withWriterCause(
                    Messages::error($this->overviewError), $this->overviewErrorCause
                ))
                ->visible(fn () => $this->overview() && $this->overviewError !== null
                    && $this->overviewErrorCause !== null && $this->privateRootProblem() === null),
            Callout::make('Backups can\'t start')
                ->danger()
                ->description(fn () => Messages::error($this->overviewError) . ' To see details, run these as the site user:')
                ->footer([Group::make([$this->command(fn () => $this->terminalFunction()), $this->command('paymenter_backups overview --offline')])])
                ->visible(fn () => $this->overview() && $this->overviewError !== null
                    && $this->overviewErrorCause === null && $this->privateRootProblem() === null),
            Tabs::make('Backups')
                ->contained(false)
                ->persistTabInQueryString('tab')
                ->tabs([
                    Tab::make('Backups')->id('backups')->schema([
                        $this->installationSection(),
                        $this->setupSection(),
                        ...$this->alertCallouts(),
                        $this->overviewSection(),
                        EmbeddedTable::make(),
                    ]),
                    Tab::make('Settings')->id('settings')->schema([
                        EmbeddedSchema::make('settingsForm'),
                    ]),
                    Tab::make('Help')->id('help')->schema($this->helpSections()),
                ]),
        ]);
    }

    private function setupSection(): Section
    {
        return Section::make('Setup')->id('setup')
            ->schema(function () {
                $groups = $this->setupGroups();
                $blocks = [];
                if ($groups['all']) {
                    $blocks[] = Fieldset::make('All backups')->columns(1)
                        ->schema(array_map(fn ($item) => $this->setupItem($item, 'setupAll'), $groups['all']));
                }
                foreach (self::KIND_LABELS as $mode => $label) {
                    [$icon, $color, $line] = $this->typeStatus($mode);
                    $blocks[] = Fieldset::make($label)->columns(1)->schema([
                        Flex::make([Icon::make($icon)->color($color)->grow(false), Text::make($line)->grow()])->verticallyAlignStart(),
                        ...array_map(fn ($item) => $this->setupItem($item, 'setup' . ucfirst(self::KINDS[$mode])), $groups[$mode]),
                    ]);
                }
                return $blocks;
            })
            ->visible(fn () => $this->setupOpen());
    }

    private function installationSection(): Section
    {
        return Section::make(Messages::INSTALLATION_TITLE)->schema(function () {
            $checks = $this->configuration()['installation']['checks'] ?? [];
            $rows = [];
            foreach (array_keys(Messages::INSTALLATION_CHECKS) as $key) {
                $check = $checks[$key] ?? ['state' => 'missing', 'code' => 'engine-unavailable'];
                $rows[] = Text::make(Messages::installationCheck($key, $check));
            }
            return $rows;
        })->visible(fn () => isset($this->configuration()['installation']));
    }

    /** Transient conditions, one callout each. */
    private function alertCallouts(): array
    {
        $pending = fn () => (int) ($this->overview()['s3_pending']['count'] ?? 0);
        return [
            Text::make('Database backups are running without job locks and can overlap Paymenter jobs.')
                ->visible(fn () => ($this->overview()['database_job_coordination']['state'] ?? null) === 'absent'),
            Callout::make(Messages::JOB_COORDINATION_UNVERIFIED)->warning()
                ->description(fn () => Messages::error($this->overview()['database_job_coordination']['error'] ?? null))
                ->visible(fn () => ($this->overview()['database_job_coordination']['state'] ?? null) === 'unverified'),
            Callout::make('Database backups have waited a day for Paymenter jobs')->warning()
                ->description('Check scheduler and queue jobs and their lock setup on this tab. Waiting backups retry automatically. If none is waiting, queue a database backup. Backups cannot safely stop a job that may be changing data or guess which locks protect it.')
                ->visible(fn () => (bool) ($this->overview()['job_wait_overdue'] ?? false)),
            Callout::make('Backups in S3 aren\'t encrypted')->info()
                ->description('Save settings to turn on encryption for new uploads. Backups already in S3 stay as they are.')
                ->actions([$this->link('alertEncryption', 'Go to Storage settings', $this->settingsUrl('storage'))->button()])
                ->visible(fn () => $this->overviewError === null && $this->settingsVersion() < 2 && $this->uploadsToS3()
                    && !($this->encryption()['enabled'] ?? true)),
            Callout::make(fn () => $pending() . ($pending() === 1 ? ' backup isn\'t' : ' backups aren\'t') . ' uploaded to S3 yet')->warning()
                ->description('They\'re kept on this server. Use Upload to S3 in each row\'s menu.')
                ->visible(fn () => $pending() > 0),
            Callout::make('Backup space is full')->danger()
                ->description(fn () => 'Backups on this server reached the ' . Messages::bytes($this->configuration()['space']['budget'] ?? null) . ' limit. New backups stop until you delete old ones or raise the limit.')
                ->actions([$this->link('alertSpace', 'Go to Storage settings', $this->settingsUrl('storage'))->button()])
                ->visible(fn () => ($this->configuration()['space']['state'] ?? null) === 'full'),
            Callout::make('The site is in maintenance mode')->info()
                ->description('Queued tasks wait until it\'s back online.')
                ->visible(fn () => (bool) ($this->overview()['maintenance'] ?? false)),
            Callout::make(fn () => 'Cleanup skipped ' . $this->skippedCount() . ($this->skippedCount() === 1 ? ' backup' : ' backups'))->warning()
                ->description('They weren\'t deleted. Their rows say why.')
                ->visible(fn () => $this->skippedCount() > 0),
        ];
    }

    private function skippedCount(): int { return (int) ($this->configuration()['retention']['last_apply']['skipped_count'] ?? 0); }

    /** Schedule summary for a mode from the overview, or the legacy single schedule. */
    private function scheduleFor(string $mode): array
    {
        $schedules = $this->configuration()['schedules'] ?? null;
        if (is_array($schedules) && is_array($schedules[self::KINDS[$mode]] ?? null)) { return $schedules[self::KINDS[$mode]]; }
        $legacy = $this->configuration()['schedule'] ?? [];
        $selected = ($this->configuration()['mode'] ?? 'full-quiesced') === $mode && ($legacy['enabled'] ?? false);
        return ['frequency' => $selected ? ($legacy['frequency'] ?? 'daily') : 'off', 'next_at' => $selected ? ($legacy['next_at'] ?? null) : null];
    }

    /** Newest verified backup of a mode: the engine's pointer, or the newest verified row. */
    private function lastGood(string $mode): ?array
    {
        $pointer = $this->overview()['last_good'][self::KINDS[$mode]] ?? null;
        if ($pointer !== null) { return $this->run($pointer); }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['mode'] === $mode && $run['status'] === 'ready' && ($run['download'] === 'ready' || in_array($run['remote'], ['delivered', 'present'], true))) { return $run; }
        }
        return null;
    }

    private function storageTestState(): string
    {
        $test = $this->latestRequest('destination-test');
        if (!$test) { return 'none'; }
        if ($test['status'] === 'complete') {
            // The engine says whether the test still proves the current bucket, format and recovery key.
            if (array_key_exists('result_verified', $test)) { return $test['result_verified'] === true ? 'passed' : 'stale'; }
            $current = $this->configuration()['destination']['id'] ?? null;
            return $current !== null && ($test['result_destination_id'] ?? null) === $current ? 'passed' : 'stale';
        }
        return in_array($test['status'], self::ACTIVE, true) ? 'queued' : $test['status'];
    }

    private function overviewSection(): Section
    {
        $next = function (string $mode) {
            $schedule = $this->scheduleFor($mode);
            if (($schedule['frequency'] ?? 'off') === 'off') { return 'Off'; }
            return ($schedule['next_at'] ?? null) ? $this->time($schedule['next_at'], true) : 'None in the next 8 days';
        };
        return Section::make('Overview')
            ->headerActions([$this->testStorageAction(), $this->applyRetentionAction()])
            ->schema([
                Grid::make(['default' => 1, 'md' => 2])->schema([
                    TextEntry::make('overview_last_full')->label('Last full backup')
                        ->state(fn () => ($run = $this->lastGood('full-quiesced')) ? $this->when($run['created_at']) : 'None yet'),
                    TextEntry::make('overview_last_database')->label('Last database backup')
                        ->state(fn () => ($run = $this->lastGood('database-online')) ? $this->when($run['created_at']) : 'None yet'),
                    TextEntry::make('overview_next_full')->label('Next full backup')->state(fn () => $next('full-quiesced')),
                    TextEntry::make('overview_next_database')->label('Next database backup')->state(fn () => $next('database-online')),
                    TextEntry::make('overview_last_s3')->label('Last upload to S3')->state(function () {
                        $run = $this->run($this->overview()['last_good']['s3'] ?? null);
                        if (!$run) { return 'None yet'; }
                        return $this->time($run['created_at'], true) . ($run['remote_checked_at'] ? ', checked ' . $this->ago($run['remote_checked_at']) : ', not checked yet');
                    })->visible(fn () => $this->uploadsToS3()),
                    TextEntry::make('overview_storage')->label('Stored on')->state(function () {
                        $storage = $this->storage();
                        if (!($this->configuration()['valid'] ?? false)) { return 'Fix your settings to see this'; }
                        $places = [];
                        if ($storage['keep_local'] ?? true) { $places[] = 'This server'; }
                        if ($storage['upload_s3'] ?? false) { $places[] = 'S3 (' . ($storage['s3_bucket'] ?: 'no bucket') . ')' . ($this->savedS3Keys() ? '' : ', keys missing'); }
                        return implode(', ', $places) ?: 'This server';
                    }),
                    TextEntry::make('overview_encryption')->label('S3 encryption')->state(function () {
                        if (!($this->encryption()['enabled'] ?? false)) { return 'Off'; }
                        $key = $this->keyInfo();
                        return match ($this->keyState()) {
                            'none' => 'On, no recovery key yet',
                            'pending' => 'On, recovery key ' . ($this->pendingKeyShort() ?? '') . ' not confirmed',
                            default => 'On, recovery key ' . $key['short'] . ($this->pendingKeyShort() !== null ? '; new key ' . $this->pendingKeyShort() . ' not confirmed' : ''),
                        };
                    })->visible(fn () => $this->uploadsToS3()),
                    TextEntry::make('overview_space')->label('Space used')->state(function () {
                        $space = $this->configuration()['space'] ?? null;
                        if (!$space || $space['used'] === null) { return 'Nothing stored yet'; }
                        $count = count(array_filter($this->overview()['runs'], static fn ($run) => $run['local']));
                        $used = Messages::bytes($space['used']) . ' of ' . Messages::bytes($space['budget']);
                        if (($space['state'] ?? null) === 'full') { return $used . '. Full.'; }
                        return $used . ($count === 0 ? ' (no backups stored here)' : ', ' . $count . ' ' . ($count === 1 ? 'backup' : 'backups'));
                    }),
                    TextEntry::make('overview_scheduler')->label('Cron job')->state(function () {
                        $last = $this->lastTick();
                        if (!$last) { return 'Never ran'; }
                        return $this->schedulerFresh() ? 'Ran ' . $this->ago($last) : 'Not running (last run ' . $this->ago($last) . ')';
                    })->color(fn () => $this->schedulerFresh() ? null : 'warning'),
                    TextEntry::make('overview_test')->label('Storage test')->state(function () {
                        $test = $this->latestRequest('destination-test');
                        return match ($this->storageTestState()) {
                            'none' => 'Not tested',
                            'passed' => 'Passed ' . $this->ago($test['finished_at']),
                            'stale' => 'Settings changed since the last test',
                            'failed' => 'Failed ' . $this->ago($test['finished_at']) . '. ' . Messages::error($test['error']),
                            'cancelled' => 'Last test cancelled',
                            'expired' => 'Last test expired before it ran',
                            default => 'Queued',
                        };
                    })->color(fn () => $this->storageTestState() === 'failed' ? 'danger' : null),
                ]),
            ]);
    }
}
