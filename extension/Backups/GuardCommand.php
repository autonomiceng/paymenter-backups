<?php
namespace Paymenter\Extensions\Others\Backups;

use Illuminate\Console\Command;
use Illuminate\Console\Scheduling\Schedule;

final class GuardCommand extends Command
{
    protected $signature = 'backups:guard';
    protected $description = 'Refresh the observed native billing schedule guard';

    public function handle(Schedule $schedule): int
    {
        try {
            $guard = BillingGuard::refresh($schedule);
            $this->line(json_encode($guard, JSON_UNESCAPED_SLASHES));
            return self::SUCCESS;
        } catch (\Throwable $error) {
            $this->error('billing-guard-unavailable');
            return self::FAILURE;
        }
    }
}
