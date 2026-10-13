<x-filament-panels::page>
    <p>Configure schedule and non-secret options in the native Extensions editor. Actions below are durable requests processed by the scheduler.</p>
    @if ($message)<p role="status">{{ $message }}</p>@endif
    <form wire:submit="saveCredentials" autocomplete="off">
        <label>Access key <input type="password" wire:model="accessKey" autocomplete="new-password" /></label>
        <label>Secret key <input type="password" wire:model="secretKey" autocomplete="new-password" /></label>
        <label>Session token (optional) <input type="password" wire:model="sessionToken" autocomplete="new-password" /></label>
        <x-filament::button type="submit">Save credentials</x-filament::button>
    </form>
    <form wire:submit="saveProbeCredentials" autocomplete="off">
        <p>Optional HTTP Basic credentials for the maintenance probe. Enter the HTTPS origin matching Paymenter's configured application URL.</p>
        <label>HTTPS origin <input type="text" wire:model="probeOrigin" placeholder="https://billing.example.com" autocomplete="off" /></label>
        <label>Probe username <input type="password" wire:model="probeUsername" autocomplete="new-password" /></label>
        <label>Probe password <input type="password" wire:model="probePassword" autocomplete="new-password" /></label>
        <x-filament::button type="submit">Save probe credentials</x-filament::button>
        <x-filament::button type="button" wire:click="clearProbeCredentials" wire:confirm="Clear the maintenance probe credentials?">Clear probe credentials</x-filament::button>
    </form>
    <div>
        <x-filament::button wire:click="requestAction('run')">Request backup</x-filament::button>
        <x-filament::button wire:click="requestAction('destination-test')">Test destination</x-filament::button>
        <x-filament::button wire:click="requestAction('retention')" wire:confirm="Apply configured retention to owned verified backups?">Apply retention</x-filament::button>
    </div>
    <label>Run ID <input type="text" wire:model="runId" /></label>
    <div>
        <x-filament::button wire:click="requestAction('verify')">Verify run</x-filament::button>
        <x-filament::button wire:click="requestAction('deliver')">Retry delivery</x-filament::button>
        <x-filament::button wire:click="refreshStatus">Refresh status</x-filament::button>
    </div>
    <pre>{{ json_encode($backupStatus, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) }}</pre>
</x-filament-panels::page>
