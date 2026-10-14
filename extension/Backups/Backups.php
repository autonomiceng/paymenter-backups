<?php
namespace Paymenter\Extensions\Others\Backups;

use App\Attributes\ExtensionMeta;
use App\Classes\Extension\Extension;
use Illuminate\Console\Scheduling\Schedule;

#[ExtensionMeta(name: 'Backups', description: 'Database and file backups for Paymenter on this server and in S3, with verified downloads and terminal recovery.', version: '0.3.0', author: 'Autonomic Engineering')]
class Backups extends Extension
{
    public function getConfig($values = []) { return Settings::fields(); }

    public function installed()
    {
        $record = \App\Models\Extension::where('extension', 'Backups')->where('type', 'other')->first();
        if (!$record) { return; } // Native upload precedes the separate install action.
        foreach (Settings::DEFAULTS as $key => $value) {
            $record->settings()->firstOrCreate(['key' => $key], ['value' => is_array($value) ? json_encode($value) : (is_bool($value) ? ($value ? '1' : '0') : (string) $value), 'type' => 'string', 'encrypted' => false]);
        }
    }

    public function boot()
    {
        \Illuminate\Support\Facades\View::addNamespace('backups', __DIR__ . '/resources/views');
        if (!app()->runningInConsole()) { return; }
        (new NativeProvider(app()))->boot();
    }

    public function updated($record)
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
