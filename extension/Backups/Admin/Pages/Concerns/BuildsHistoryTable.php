<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Actions\Action;
use Filament\Actions\ActionGroup;
use Filament\Forms\Components\Radio;
use Filament\Infolists\Components\TextEntry;
use Filament\Notifications\Notification;
use Filament\Schemas\Components\Actions;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\Group;
use Filament\Schemas\Components\Text;
use Filament\Support\Enums\Alignment;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Enums\Size;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Table;
use Illuminate\Pagination\LengthAwarePaginator;
use Paymenter\Extensions\Others\Backups\Messages;
use Illuminate\Support\HtmlString;
use Paymenter\Extensions\Others\Backups\Admin\Support\{HistorySearch, Sheet, StatusTone};
use Paymenter\Extensions\Others\Backups\Runtime;

trait BuildsHistoryTable
{
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
        $running = false;
        foreach ($this->overview()['runs'] as $run) {
            if ($run['request_id']) { $started[$run['request_id']] = true; }
            $running = $running || $run['status'] === 'running';
        }
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] !== 'run' || isset($started[$request['request_id']]) || $request['result_run_id']) { continue; }
            if (in_array($request['status'], ['complete', 'cancelled', 'satisfied'], true)) { continue; }
            $active = in_array($request['status'], self::ACTIVE, true);
            [$label, $color] = $active || in_array($request['status'], ['expired', 'skipped'], true) ? Messages::taskStatus($request['status']) : ['Didn\'t start', 'danger'];
            $schedulerFresh = $this->schedulerFresh();
            $detail = match ($request['status']) {
                'pending' => $schedulerFresh ? ($running ? 'Starts after the running backup.' : 'Starts within a minute.') : $this->schedulerWaitLine(),
                'deferred' => $schedulerFresh ? Messages::error($request['error']) : $this->schedulerWaitLine(),
                'launching' => 'Starting.', 'running' => 'Running.',
                'expired' => 'Expired: Paymenter\'s cron job didn\'t run in time.',
                default => Messages::error($request['error']),
            };
            $rows[] = ['__key' => 'request-' . $request['request_id'], 'kind' => 'request', 'request_id' => $request['request_id'],
                'created_at' => $request['created_at'], 'type' => Messages::type($request['mode'] ?: ($this->configuration()['mode'] ?? null)), 'origin' => $request['scheduled'] ? 'Schedule' : 'Staff',
                'size' => null, 'size_label' => '—', 'status_label' => $label, 'status_color' => $color, 'detail' => $detail, 'location' => '—',
                'cancel_request' => in_array($request['status'], ['pending', 'deferred'], true) ? $request['request_id'] : null,
                'run_id' => null, 'run' => null];
        }
        foreach ($this->overview()['runs'] as $run) {
            if ($run['status'] === 'deleted' && !$showDeleted) { continue; }
            [$label, $color] = Messages::runStatus($run);
            $detail = Messages::runDetail($run + ['maintenance' => $this->overview()['maintenance'] ?? false, 'uploads' => $uploads]);
            $cancel = null;
            $tasks = $this->tasksFor($run['run_id']);
            $active = array_values(array_filter($tasks, static fn ($task) => in_array($task['status'], self::ACTIVE, true)));
            if ($active) {
                $task = $active[0];
                if ($task['action'] === 'deliver' && $task['status'] === 'deferred' && $task['error']) {
                    $detail = Messages::error($task['error']);
                }
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
            // "Settings" in an upload failure's next step links to the S3 settings.
            if ($run['remote'] === 'failed' && is_string($detail) && str_contains($detail, ' in Settings,')) {
                $detail = new HtmlString(str_replace(' in Settings,', ' in <a class="underline" href="' . e($this->settingsUrl('s3')) . '">Settings</a>,', e($detail)));
            }
            $origin = $run['request_id'] && isset($scheduled[$run['request_id']]) ? ($scheduled[$run['request_id']] ? 'Schedule' : 'Staff') : null;
            $rows[] = ['__key' => $run['run_id'], 'kind' => 'run', 'request_id' => null, 'created_at' => $run['created_at'],
                'type' => Messages::type($run['mode']), 'origin' => $origin, 'size' => $run['size'], 'size_label' => Messages::bytes($run['size']), 'status_label' => $label, 'status_color' => $color,
                'detail' => $detail, 'location' => Messages::location($run), 'cancel_request' => $cancel,
                'run_id' => $run['run_id'], 'run' => $run];
        }
        usort($rows, static fn ($a, $b) => ($b['created_at'] ?? 0) <=> ($a['created_at'] ?? 0));
        return $rows;
    }

    private function schedulerWaitLine(): \Illuminate\Support\HtmlString
    {
        $url = static::getUrl(['tab' => 'backups']) . '#checks';
        return Messages::schedulerWaitLine($url);
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
            ->description(fn () => $this->hasActiveWork() ? 'Updates every 5 seconds while a backup runs' : null)
            ->records(function (int|string $page, int|string $recordsPerPage, ?string $search, ?string $sortColumn = null, ?string $sortDirection = null) {
                $rows = HistorySearch::filter($this->historyRows($this->showDeleted), $search);
                // History is newest first; Started sorts oldest first only when asked.
                if ($sortColumn === 'created_at' && $sortDirection === 'asc') { $rows = array_reverse($rows); }
                $perPage = max(1, (int) $recordsPerPage);
                $page = max(1, (int) $page);
                return new LengthAwarePaginator(array_slice($rows, ($page - 1) * $perPage, $perPage), count($rows), $perPage, $page);
            })
            ->paginated([25, 50, 100])->defaultPaginationPageOption(25)
            ->searchable()->searchPlaceholder('Search backup ID')
            ->recordClasses(fn (array $record) => ($record['run']['status'] ?? null) === 'running' ? 'backups-row-running' : null)
            ->columnManagerTriggerAction(fn (Action $action) => $action->label('Columns')->button())
            // Merge, so the skin's shared table class set for every table stays.
            // On phones the rows stay rows: the table scrolls sideways inside its card, never the page.
            ->extraAttributes(['class' => 'beco-own-table backups-history'], merge: true)
            // No polling while a dialog is open: it would re-render the dialog, including a shown recovery key.
            ->poll(fn () => $this->hasActiveWork() && !$this->mountedActions ? '5s' : null)
            ->headerActions([
                Action::make('toggleDeleted')->label(fn () => $this->showDeleted ? 'Hide deleted' : 'Show deleted')->link()->color('gray')
                    ->visible(fn () => $this->hasDeletedRuns())
                    ->action(function () { $this->showDeleted = !$this->showDeleted; $this->resetPage(); }),
            ])
            ->columns([
                TextColumn::make('created_at')->label('Started')->sortable()
                    ->formatStateUsing(fn ($state) => $this->moment($state)),
                // Every column shows on phones, so the kind of backup stays in view.
                TextColumn::make('type')->label('Type')->toggleable(),
                TextColumn::make('status_label')->label('Status')->badge()->color(fn (array $record) => $record['status_color'])
                    ->extraCellAttributes(fn (array $record) => ['data-beco-tone' => StatusTone::forLabel($record['status_label'])])->toggleable(),
                TextColumn::make('detail')->label('Note')->wrap()->toggleable(),
                TextColumn::make('size_label')->label('Size')->alignEnd()->extraCellAttributes(['class' => 'tabular-nums'])->toggleable(),
                TextColumn::make('location')->label('Location')->toggleable(),
                TextColumn::make('origin')->label('Started by')->toggleable(isToggledHiddenByDefault: true),
            ])
            ->recordActions([
                Action::make('download')->label('Download')->icon('ri-download-2-line')->button()->color('gray')
                    ->url(fn (array $record) => static::downloadUrl($record['run_id']))
                    ->visible(fn (array $record) => $isRun($record) && $record['run']['download'] === 'ready'),
                Action::make('prepareDownload')->label('Get from S3')->icon('ri-download-cloud-2-line')->button()->color('gray')
                    ->visible(fn (array $record) => $isRun($record) && $record['run']['download'] === 'retrieve'
                        && !$this->activeRequest('retrieve', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('retrieve', ['run_id' => $record['run_id']], 'Get from S3 queued',
                        'Copying it from S3 to this server. ' . $this->startLine() . ' Download appears here when it\'s ready and stays available for 24 hours.')),
                Action::make('retryUpload')->label('Upload to S3')->icon('ri-upload-cloud-2-line')->button()->color('gray')
                    ->visible(fn (array $record) => $isRun($record) && ($record['run']['remote'] ?? null) === 'failed' && $record['run']['status'] === 'ready'
                        && $this->uploadsToS3() && !$this->activeRequest('deliver', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('deliver', ['run_id' => $record['run_id']], 'Upload to S3 queued')),
                Action::make('verifyFirst')->label('Verify')->icon('ri-shield-check-line')->button()->color('gray')
                    ->tooltip('Checks the backup against its checksums.')
                    ->visible(fn (array $record) => $isRun($record) && self::verifiesOnRow($record['run']) && !$this->activeRequest('verify', $record['run_id']))
                    ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                Action::make('cancelQueued')->label('Cancel')->icon('ri-close-line')->button()->color('gray')
                    ->visible(fn (array $record) => $record['kind'] === 'request' && $record['cancel_request'] !== null)
                    ->requiresConfirmation()->modalHeading('Cancel this backup?')->modalDescription('It hasn\'t started. Scheduled backups still run.')
                    ->modalSubmitActionLabel('Cancel backup')->modalCancelActionLabel('Keep')
                    ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                ActionGroup::make([
                    Action::make('details')->label('Details')->icon('ri-information-line')
                        ->modalHeading(fn (array $record) => $isRun($record) ? Messages::mode($record['run']['mode']) : 'Queued backup')
                        ->modalDescription(fn (array $record) => $this->detailsLine($record))
                        ->modalWidth('2xl')->modalSubmitAction(false)->modalCancelActionLabel('Close')
                        ->extraAttributes(['x-on:click.capture' => 'close()'])
                        ->extraModalFooterActions(fn (array $record) => [
                            Action::make('detailsDownload')->label('Download')->icon('ri-download-2-line')->color('gray')
                                ->visible($isRun($record) && $record['run']['download'] === 'ready')
                                ->url($isRun($record) ? static::downloadUrl($record['run_id']) : null),
                        ])
                        ->extraModalWindowAttributes(['class' => 'backups-details'])
                        ->schema(fn (array $record) => [Sheet::make($this->detailsSchema($record))]),
                    Action::make('verify')->label('Verify')->icon('ri-shield-check-line')
                        ->visible(fn (array $record) => $isRun($record) && self::verifiesInMenu($record['run']) && !$this->activeRequest('verify', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('verify', ['run_id' => $record['run_id']], 'Verification queued')),
                    Action::make('copy')->label('Upload to S3')->icon('ri-upload-cloud-2-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['run']['status'] === 'ready' && !$inS3($record)
                            && $record['run']['remote'] !== 'failed' && $this->uploadsToS3() && !$this->activeRequest('deliver', $record['run_id']))
                        ->action(fn (array $record) => $this->queue('deliver', ['run_id' => $record['run_id']], 'Upload to S3 queued')),
                    Action::make('cancelTask')->label(fn (array $record) => 'Cancel ' . strtolower($this->pendingTaskLabel($record)))->icon('ri-close-circle-line')
                        ->visible(fn (array $record) => $isRun($record) && $record['cancel_request'] !== null)
                        ->requiresConfirmation()->modalHeading(fn (array $record) => 'Cancel ' . strtolower($this->pendingTaskLabel($record)) . '?')->modalDescription('It hasn\'t started.')
                        ->modalSubmitActionLabel('Cancel task')->modalCancelActionLabel('Keep')
                        ->action(fn (array $record) => $this->callCancel($record['cancel_request'])),
                    Action::make('delete')->label('Delete')->icon('ri-delete-bin-line')->color('danger')
                        ->visible(fn (array $record) => $isRun($record) && in_array($record['run']['status'], ['ready', 'damaged', 'delete-interrupted'], true)
                            && !$this->activeRequest('delete', $record['run_id']))
                        // A plain form dialog, not Filament's centered confirmation with its warning icon.
                        ->modalWidth('lg')->modalAlignment(Alignment::Start)->modalFooterActionsAlignment(Alignment::End)
                        ->modalHeading('Delete this backup?')
                        ->modalDescription('This can\'t be undone.')
                        ->fillForm(fn (array $record) => ['location' => $this->defaultDeletionLocation($record)])
                        ->schema(fn (array $record) => [
                            Group::make(Sheet::rows([
                                TextEntry::make('delete_backup')->label('Backup')->state(new HtmlString(e(Messages::type($record['run']['mode']) . ', ') . $this->moment($record['created_at']))),
                                TextEntry::make('delete_size')->label('Size')->state(Messages::bytes($record['run']['size'])),
                            ]))->extraAttributes(['class' => 'backups-facts']),
                            // A choice appears only when the backup is in more than one place.
                            Radio::make('location')->label('Delete from')->inline()->required()->live()
                                ->options($places = array_filter([
                                    'all' => $record['run']['local'] && $inS3($record) ? 'This server and S3' : null,
                                    'local' => $record['run']['local'] ? 'This server' : null,
                                    's3' => !$record['run']['local'] || $inS3($record) ? 'S3' : null,
                                ]))
                                ->visible(count($places) > 1)
                                ->rule('in:all,local,s3'),
                            Callout::make('Can\'t delete')->danger()
                                ->footer(fn () => [$this->bullets($this->deleteLines($record['run_id'], $this->currentDeletionLocation($record)))])
                                ->visible(fn () => (bool) $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal']),
                            Group::make(fn () => [$this->bullets($this->deleteLines($record['run_id'], $this->currentDeletionLocation($record)))])
                                ->hidden(fn () => (bool) $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal']),
                        ])
                        ->modalSubmitAction(fn (Action $action, array $record) => $this->deletePreview($record['run_id'], $this->currentDeletionLocation($record))['refusal'] ? false : $action)
                        ->modalSubmitActionLabel('Delete backup')->modalCancelActionLabel('Keep')
                        ->action(fn (array $record, array $data) => $this->queue('delete', ['run_id' => $record['run_id'], 'location' => $data['location'] ?? $this->defaultDeletionLocation($record), 'execute' => true], 'Deletion queued')),
                ])->label('More actions')->icon('ri-more-2-fill')->color('gray')->tooltip('More actions'),
            ])
            ->emptyStateIcon('ri-save-line')
            ->emptyStateHeading(fn () => filled($this->getTableSearch()) ? 'No matching backups' : 'No backups yet')
            ->emptyStateDescription(fn () => filled($this->getTableSearch()) ? 'Try another backup ID, type, status or location.' : ($this->setupOpen() && $this->nothingCanStart() ? 'Finish setup above to create your first backup.' : 'Create a backup or turn on a schedule.'))
            ->emptyStateActions([$this->backupAction('createFirstBackup')->visible(fn () => !filled($this->getTableSearch()) && !$this->nothingCanStart())]);
    }

    /** Ready or damaged copies without Download or Get from S3 use row Verify. */
    private static function verifiesOnRow(array $run): bool
    {
        return in_array($run['status'], ['ready', 'damaged'], true) && !in_array($run['download'], ['ready', 'retrieve'], true);
    }

    /** The remaining ready or damaged copies use menu Verify, never both. */
    private static function verifiesInMenu(array $run): bool
    {
        return in_array($run['status'], ['ready', 'damaged'], true) && !self::verifiesOnRow($run);
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
        if ($preview['local_copy']) { $lines[] = 'The file on this server.'; }
        if ($preview['remote_copy']) { $lines[] = 'The copy in S3 bucket ' . $preview['remote_copy']['bucket'] . '. If versioning is on, old versions stay.'; }
        $lines[] = 'Another verified backup stays in each place. If none would, nothing is deleted.';
        return $lines;
    }

    /** The status line under a backup's title. */
    private function detailsLine(array $record): HtmlString
    {
        $run = $record['run'];
        if ($run === null) { return Sheet::headLine($record['status_color'], $record['status_label'], $record['detail']); }
        $took = $this->took($run);
        $line = match (true) {
            $record['status_label'] === 'Verified' && $run['verified_at'] => new HtmlString(e($run['verification_source'] === 'capture' ? 'Checked when it was made, ' : 'Checked ') . $this->moment($run['verified_at']) . '.'),
            $run['status'] === 'failed' && $took !== null => 'Stopped after ' . $took . '.',
            default => $record['detail'],
        };
        return Sheet::headLine($record['status_color'], $record['status_label'], $line);
    }

    /** "41 seconds" between start and finish, or null when the finish wasn't recorded. */
    /** What cleanup does to this backup in each place it is stored. S3 has its own policy per type. */
    private function keepRows(array $run): array
    {
        $values = $this->formValues();
        $kind = $run['mode'] === 'full-quiesced' ? 'full' : 'database';
        $type = strtolower(Messages::type($run['mode']));
        $below = static fn (?string $text) => $text === null ? [] : [Text::make($text)];
        $rows = [];
        // Off only stops the automatic run: Run cleanup still applies each place's rule below.
        if (!($values['automatic_retention'] ?? true)) {
            $rows[] = Sheet::row(TextEntry::make('d_keep')->label('Cleanup')->state('Automatic cleanup is off')
                ->belowContent($below('The rules below apply only when you run cleanup.')),
                [$this->link('detailsKeep', 'Change', $this->settingsUrl('cleanup'))]);
        }
        $local = (int) ($values['retention_keep_' . $kind] ?? 0);
        if ($run['local']) {
            $rows[] = Sheet::row(TextEntry::make('d_keep_local')->label('This server')->state('Deleted after ' . $local . ' newer ' . $type . ' backups')
                ->belowContent($below('Or delete it yourself from the row menu.')),
                [$this->link('detailsKeepLocal', 'Change', $this->settingsUrl('storage'))]);
        }
        if (in_array($run['remote'], ['delivered', 'present'], true)) {
            $policy = $values['s3_retention_policy_' . $kind] ?? 'local-count';
            $state = match ($policy) {
                'count' => 'Deleted after ' . (int) ($values['s3_retention_keep_' . $kind] ?? 0) . ' newer ' . $type . ' backups',
                'age' => 'Deleted ' . (int) ($values['s3_retention_days_' . $kind] ?? 0) . ' days after it was made',
                'keep-all' => 'Kept. Cleanup never deletes it',
                default => 'Deleted after ' . $local . ' newer ' . $type . ' backups',
            };
            $rows[] = Sheet::row(TextEntry::make('d_keep_s3')->label('S3')->state($state)
                ->belowContent($below(($policy === 'keep-all' ? '' : 'The newest backup in S3 is always kept. ') . 'Bucket lifecycle rules can also delete it.')),
                [$this->link('detailsKeepS3', 'Change', $this->settingsUrl('s3'))]);
        }
        return $rows;
    }

    private function took(array $run): ?string
    {
        if (($run['finished_at'] ?? null) === null || $run['created_at'] === null) { return null; }
        return Messages::duration($run['finished_at'] - $run['created_at']);
    }

    private function detailsSchema(array $record): array
    {
        if ($record['kind'] !== 'run') {
            return [Sheet::section('Backup')->schema(Sheet::rows([
                TextEntry::make('d_task')->label('Task ID')->state($record['request_id'])->fontFamily(FontFamily::Mono)->copyable(request()->isSecure()),
                TextEntry::make('d_type')->label('Type')->state($record['type']),
                TextEntry::make('d_origin')->label('Started by')->state($record['origin']),
                TextEntry::make('d_created')->label('Queued')->state($this->moment($record['created_at'])),
            ]))];
        }
        $run = $record['run'];
        $code = $run['error'] ?? $run['verification_error'] ?? $run['delivery_error'] ?? $run['retention_error'] ?? null;
        [$reason, $next] = Messages::reasonAndNext($code);
        $action = $code ? (Messages::readinessItem($code, [])['action'] ?? null) : null;
        $stopped = in_array($run['status'], ['failed', 'interrupted', 'attention'], true);
        $took = $this->took($run);
        $format = Messages::remoteFormat($run['remote_format'] ?? null, $run['key_short'] ?? null);
        $s3 = match (true) {
            in_array($run['remote'], ['delivered', 'present'], true) => ['Uploaded', trim(($format ?? '') . (($run['ciphertext_size'] ?? null) !== null ? ', ' . Messages::bytes($run['ciphertext_size']) : '') . '.'
                . ($run['remote_checked_at'] ? ' Checked ' . $this->ago($run['remote_checked_at']) . '.' : ''), ' .')],
            $run['remote'] === 'failed' => ['Upload failed', Messages::error($run['delivery_error'] ?: 's3-delivery-failed')],
            $run['remote'] !== 'none' => [Messages::location($run), Messages::runDetail($run)],
            $this->uploadsToS3() => ['Not uploaded yet', null],
            default => ['Off', 'Optional. Add a bucket to keep a copy off this server.'],
        };
        $below = static fn (?string $text) => $text ? [Text::make($text)] : [];
        return [
            Sheet::section('What went wrong')->schema([
                Sheet::row(TextEntry::make('d_reason')->label('Reason')->state($reason)),
                Sheet::row(TextEntry::make('d_next')->label('Next step')->state($next)->visible($next !== null),
                    $action && $action['kind'] === 'settings' ? [$this->link('detailsFix', $action['label'], $this->settingsUrl($action['target'] ?? 'settings'))] : []),
                Sheet::row(TextEntry::make('d_code')->label('Error code')->state($code)->fontFamily(FontFamily::Mono)->belowContent($below('For support and the command line.'))),
            ])->visible($code !== null),
            Sheet::section('Backup')->schema(Sheet::rows([
                TextEntry::make('d_id')->label('Backup ID')->state($run['run_id'])->fontFamily(FontFamily::Mono)
                    ->copyable(request()->isSecure())->copyMessage('Copied')
                    ->suffixAction(Action::make('copyBackupId')->label('Copy')->icon('ri-file-copy-line')
                        ->visible(request()->isSecure())
                        ->alpineClickHandler('window.navigator.clipboard.writeText(' . \Illuminate\Support\Js::from($run['run_id']) . ')')),
                Sheet::hint(TextEntry::make('d_type')->label('Type'), Messages::TYPE_HINTS[$run['mode']] ?? '')->state(Messages::type($run['mode'])),
                TextEntry::make('d_origin')->label('Started by')->state($record['origin'] ?? 'Not recorded'),
                TextEntry::make('d_created')->label('Started')->state($this->moment($run['created_at'])),
                TextEntry::make('d_finished')->label($stopped ? 'Stopped' : 'Finished')
                    ->state(($run['finished_at'] ?? null) !== null ? $this->moment($run['finished_at']) : 'Not recorded')
                    ->belowContent($below($took !== null && !$stopped ? 'Took ' . $took : null))
                    ->visible($run['status'] !== 'running'),
                TextEntry::make('d_size')->label('Size')->state($run['size'] === null && $stopped ? 'None' : Messages::bytes($run['size']))
                    ->belowContent($below($run['size'] === null && $stopped ? 'Nothing was kept.' : null)),
            ])),
            Sheet::section('Location')->schema(Sheet::rows([
                TextEntry::make('d_local')->label('This server')->state($run['local'] ? 'Stored' : 'Not stored')
                    ->belowContent($below($run['local'] ? 'Not encrypted. It holds customer data, .env and APP_KEY, so keep downloads private.' : null)),
                TextEntry::make('d_retrieved')->label('Copy from S3 kept until')->state(fn () => $this->moment($run['local_download_until'] ?? null))
                    ->visible((bool) ($run['local_download_until'] ?? null)),
                TextEntry::make('d_s3')->label('S3')->state($s3[0])->belowContent($below($s3[1])),
            ], ['d_s3' => $run['remote'] === 'none' && !$this->uploadsToS3() ? [$this->link('detailsSetUpS3', 'Set up S3', $this->settingsUrl('s3'))] : []]))->visible(!$stopped),
            Sheet::section('Check')->schema(Sheet::rows([
                TextEntry::make('d_verified')->label('Last verified')->state($run['verified_at'] ? $this->moment($run['verified_at']) : 'Not verified yet')
                    ->belowContent($below($run['verified_at'] && $run['verification_source'] === 'capture'
                        ? 'When it was made. Verify checks the file against its checksums again.' : 'Verify checks the file against its checksums.')),
            ], ['d_verified' => [Action::make('detailsVerify')->label('Verify')->button()->outlined()->size(Size::Small)->color('gray')
                ->visible((self::verifiesOnRow($run) || self::verifiesInMenu($run)) && !$this->activeRequest('verify', $run['run_id']))
                ->action(fn () => $this->queue('verify', ['run_id' => $run['run_id']], 'Verification queued'))]]))->visible($run['status'] === 'ready' || $run['status'] === 'damaged'),
            Sheet::section('Restore')->schema(Sheet::contentRows([
                Text::make('Restore into a new folder and a new database, never over the running site. As the site user, this copies the backup out:'),
                $this->command('paymenter_backups export ' . self::shell($run['run_id']) . ' --output /new/folder'),
                Actions::make([$this->link('detailsRestoreSteps', 'Full restore steps', static::getUrl(['tab' => 'help']) . '#restoring')]),
            ]))->visible($run['status'] === 'ready'),
            Sheet::section('Keep')->schema(fn () => $this->keepRows($run))->visible($run['status'] === 'ready'),
        ];
    }
}
