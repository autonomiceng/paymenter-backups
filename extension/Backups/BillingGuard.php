<?php
namespace Paymenter\Extensions\Others\Backups;

use App\Console\Commands\CronJob;
use Cron\CronExpression;
use Illuminate\Console\Scheduling\Schedule;
use RuntimeException;

final class BillingGuard
{
    public static function refresh(Schedule $schedule): array
    {
        // Query native rows directly, independent of the SettingsProvider cache.
        $rows = \App\Models\Setting::whereNull('settingable_type')->whereIn('key', ['cronjob_time', 'timezone'])->pluck('value', 'key')->all();
        if (!isset($rows['cronjob_time'])) { throw new RuntimeException('billing-settings-unreadable'); }
        $billing = ['daily_time' => $rows['cronjob_time'], 'timezone' => $rows['timezone'] ?? config('app.timezone', 'UTC')];
        $matches = array_filter($schedule->events(), static fn ($event) => isset($event->command) && str_contains($event->command, app(CronJob::class)->getName()));
        if (count($matches) !== 1) { throw new RuntimeException('billing-event-unverified'); }
        $event = array_values($matches)[0];
        $timezone = $event->timezone instanceof \DateTimeZone ? $event->timezone->getName() : ($event->timezone ?: date_default_timezone_get());
        $cron = new CronExpression($event->expression);
        $now = time();
        $recentMargin = max(180, (int) (\App\Models\Extension::where('extension', 'Backups')->first()?->settings()->where('key', 'billing_margin_seconds')->value('value') ?? 120));
        $cursor = new \DateTimeImmutable('@' . ($now - $recentMargin - 60));
        $instants = [];
        for ($i = 0; $i < 4096; $i++) {
            $next = $cron->getNextRunDate($cursor, 0, false, $timezone);
            if ($next->getTimestamp() > $now + 8 * 24 * 3600) { break; }
            $instants[] = $next->getTimestamp();
            $cursor = new \DateTimeImmutable('@' . $next->getTimestamp());
        }
        if (!$instants) { throw new RuntimeException('billing-event-unverified'); }
        $guard = ['schema_version' => 1, 'generated_at' => $now, 'fingerprint' => hash('sha256', json_encode($billing, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES)), 'expression' => $event->expression, 'timezone' => $timezone, 'instants' => $instants];
        PrivateStore::atomic(Runtime::privateRoot() . '/billing-guard.json', $guard);
        return $guard;
    }
}
