<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Filament\Actions\Action;
use Filament\Actions\ActionGroup;
use Filament\Forms\Components\Radio;
use Filament\Infolists\Components\TextEntry;
use Filament\Notifications\Notification;
use Filament\Schemas\Components\Callout;
use Filament\Schemas\Components\Grid;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Enums\Size;
use Filament\Tables\Columns\TextColumn;
use Filament\Tables\Table;
use Illuminate\Support\HtmlString;
use Illuminate\Pagination\LengthAwarePaginator;
use Paymenter\Extensions\Others\Backups\Messages;
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
        foreach ($this->overview()['runs'] as $run) {
            if ($run['request_id']) { $started[$run['request_id']] = true; }
        }
        foreach ($this->overview()['requests'] as $request) {
            if ($request['action'] !== 'run' || isset($started[$request['request_id']]) || $request['result_run_id']) { continue; }
            if (in_array($request['status'], ['complete', 'cancelled', 'satisfied'], true)) { continue; }
            $active = in_array($request['status'], self::ACTIVE, true);
            [$label, $color] = $active || in_array($request['status'], ['expired', 'skipped'], true) ? Messages::taskStatus($request['status']) : ['Didn\'t start', 'danger'];
            $schedulerFresh = $this->schedulerFresh();
            $detail = match ($request['status']) {
                'pending' => $schedulerFresh ? 'Starts within a minute.' : $this->schedulerWaitLine(),
                'deferred' => $schedulerFresh ? 'Retrying soon. ' . Messages::error($request['error']) : $this->schedulerWaitLine(),
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

    private function schedulerWaitLine(): HtmlString
    {
        $url = e(static::getUrl(['tab' => 'backups']) . '#setup');
        return new HtmlString('Waiting for Paymenter\'s scheduled tasks. Set them up on <a class="underline" href="' . $url . '">the Backups tab</a>.');
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
