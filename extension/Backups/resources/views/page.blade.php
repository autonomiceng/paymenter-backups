<x-filament-panels::page>
    {{-- Rendered once per page. Otherwise the history table renders it inside the Backups tab. --}}
    <x-filament-actions::modals />
    {{ $this->content }}
</x-filament-panels::page>
