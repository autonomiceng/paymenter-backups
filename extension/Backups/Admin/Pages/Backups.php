<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages;

use Filament\Pages\Page;
use Illuminate\Support\Facades\Auth;
use Paymenter\Extensions\Others\Backups\Runtime;

class Backups extends Page
{
    protected static string|\BackedEnum|null $navigationIcon = 'ri-save-line';
    protected static string|\UnitEnum|null $navigationGroup = 'System';
    protected string $view = 'backups::admin';
    public string $accessKey = '';
    public string $secretKey = '';
    public string $sessionToken = '';
    public string $probeOrigin = '';
    public string $probeUsername = '';
    public string $probePassword = '';
    public string $runId = '';
    public string $message = '';
    public array $backupStatus = [];

    public static function canAccess(): bool
    {
        return Auth::user()?->hasPermission('admin.extensions.update') ?? false;
    }

    private function authorizeAction(): void { abort_unless(static::canAccess(), 403); }

    public function dehydrate(): void { $this->accessKey = ''; $this->secretKey = ''; $this->sessionToken = ''; $this->probeOrigin = ''; $this->probeUsername = ''; $this->probePassword = ''; }

    public function mount(): void { $this->authorizeAction(); $this->refreshStatus(); }

    public function refreshStatus(): void
    {
        $this->authorizeAction();
        try { $this->backupStatus = Runtime::command(['status']); }
        catch (\Throwable $error) { $this->backupStatus = []; $this->message = 'Private storage is unavailable to this web pool. Use the CLI; check the pool UID and open_basedir.'; }
    }

    public function saveCredentials(): void
    {
        $this->authorizeAction();
        try {
            Runtime::credentials(['access_key' => $this->accessKey, 'secret_key' => $this->secretKey, 'session_token' => $this->sessionToken]);
            $this->message = 'Credentials saved in the private encrypted file.';
        } catch (\Throwable $error) {
            $this->message = $error->getMessage() === 'credentials-unreadable' ? 'Credentials unreadable. Resolve the key or installation binding using the CLI before replacing this file.' : 'Credentials could not be saved. Use the CLI to check private storage and configuration.';
        } finally {
            $this->accessKey = ''; $this->secretKey = ''; $this->sessionToken = '';
        }
    }

    public function saveProbeCredentials(): void
    {
        $this->authorizeAction();
        try {
            Runtime::credentials(['origin' => $this->probeOrigin, 'username' => $this->probeUsername, 'password' => $this->probePassword], true);
            $this->message = 'Maintenance probe credentials saved in the private encrypted file.';
        } catch (\Throwable $error) {
            $this->message = $error->getMessage() === 'credentials-unreadable' ? 'Credentials unreadable. Resolve the key or installation binding using the CLI before replacing this file.' : 'Probe credentials could not be saved. Check the configured HTTPS origin and use the CLI to inspect private storage.';
        } finally { $this->probeOrigin = ''; $this->probeUsername = ''; $this->probePassword = ''; }
    }

    public function clearProbeCredentials(): void
    {
        $this->authorizeAction();
        try {
            Runtime::credentials(['clear' => true], true);
            $this->message = 'Maintenance probe credentials cleared.';
        } catch (\Throwable $error) { $this->message = 'Probe credentials could not be cleared. Use the CLI to inspect private storage.'; }
        finally { $this->probeOrigin = ''; $this->probeUsername = ''; $this->probePassword = ''; }
    }

    /** Durable spool only. PHP-FPM must never launch a capture child. */
    public function requestAction(string $action): void
    {
        $this->authorizeAction();
        abort_unless(in_array($action, ['run', 'verify', 'deliver', 'retention', 'destination-test'], true), 422);
        if (in_array($action, ['verify', 'deliver'], true) && !preg_match('/^[A-Za-z0-9_-]{1,100}$/D', $this->runId)) { $this->message = 'Enter a run ID from status.'; return; }
        try {
            $arguments = in_array($action, ['verify', 'deliver'], true) ? ['run_id' => $this->runId] : [];
            if ($action === 'retention') { $arguments['execute'] = true; }
            Runtime::command(['requests', 'create', '--action', $action], $arguments);
            $this->message = 'Request saved. The next scheduler tick will process it.';
        } catch (\Throwable $error) { $this->message = 'Request unavailable. Use the CLI to inspect configuration and private storage.'; }
        $this->refreshStatus();
    }
}
