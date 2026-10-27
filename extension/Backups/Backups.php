<?php
namespace Paymenter\Extensions\Others\Backups;

use App\Attributes\ExtensionMeta;
use App\Classes\Extension\Extension;
use Illuminate\Console\Scheduling\Schedule;

#[ExtensionMeta(name: 'Backups', description: 'Scheduled database and full backups for Paymenter, on this server and in encrypted S3 storage, with verified downloads and recovery.', version: '0.4.3', author: 'Autonomic Engineering')]
class Backups extends Extension
{
    // Settings are edited on the Backups page; this native page owns lifecycle only.
    // Its one read-only entry points there; Filament derives the "Backup settings" heading from the name.
    // Paymenter registers the page only while the extension is enabled.
    public function getConfig($values = [])
    {
        $route = Admin\Pages\Backups::getRouteName();
        return [['name' => 'backup_settings', 'type' => 'placeholder',
            'label' => Messages::settingsPointer(\Illuminate\Support\Facades\Route::has($route) ? route($route) : null)]];
    }

    public function installed()
    {
        $record = \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->first();
        if (!$record) { return; } // Native upload precedes the separate install action.
        // Rows written by 0.3 keep their legacy meaning until the Backups page saves settings once.
        $fresh = !$record->settings()->exists();
        foreach (Settings::DEFAULTS as $key => $value) {
            if ($key === 'settings_version') { $value = $fresh ? 2 : 1; }
            $record->settings()->firstOrCreate(['key' => $key], ['value' => is_array($value) ? json_encode($value) : (is_bool($value) ? ($value ? '1' : '0') : (string) $value), 'type' => 'string', 'encrypted' => false]);
        }
    }

    public function boot()
    {
        \Illuminate\Support\Facades\View::addNamespace('backups', __DIR__ . '/resources/views');
        if (!app()->runningInConsole()) { return; }
        (new NativeProvider(app()))->boot();
    }

    public function updated($record) {}

    /** Advisory after an explicit Backups settings repair, never a lifecycle Save. */
    public function configurationUpdated($record)
    {
        try {
            Runtime::command(['init']);
            Runtime::command(['config', 'validate']);
            PrivateStore::atomic(Runtime::privateRoot() . '/configuration-advisory.json', ['schema_version' => 1, 'checked_at' => time(), 'valid' => true]);
        } catch (\Throwable $error) {
            try { PrivateStore::atomic(Runtime::privateRoot() . '/configuration-advisory.json', ['schema_version' => 1, 'checked_at' => time(), 'valid' => false, 'error' => 'config-invalid']); }
            catch (\Throwable $ignored) { /* Read-only pool: CLI remains available. */ }
        }
    }

    // Native uninstall owns only its record/settings; private state and remote data remain.
    public function uninstalled() {}
}
