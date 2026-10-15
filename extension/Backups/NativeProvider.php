<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Console\Scheduling\Schedule;

/** ServiceProvider supplies native lazy command and schedule hooks. */
final class NativeProvider extends \Illuminate\Support\ServiceProvider
{
    public function boot(): void
    {
        $this->commands([ConfigSetCommand::class, GuardCommand::class]);
        $this->callAfterResolving(Schedule::class, function (Schedule $schedule) {
            $schedule->call(function () use ($schedule) {
                try {
                    try { BillingGuard::refresh($schedule); }
                    catch (\Throwable $guardError) { /* Capture fails closed; offline recovery still runs. */ }
                    Runtime::command(['tick', '--execute'], null, 30);
                } catch (\Throwable $error) {
                    // Never let backup configuration/recovery prevent native scheduler events.
                    \Illuminate\Support\Facades\Log::warning('Backups tick unavailable', ['code' => 'backup-tick-unavailable']);
                }
            })->name('paymenter-backups-tick')->everyMinute()->evenInMaintenanceMode();
        });
    }

}
