<x-filament-panels::page>
    {{-- Rendered once per page. Otherwise the history table renders it inside the Backups tab. Out of the page's flow,
         so its empty box adds no gap above the tabs. --}}
    <div style="position: absolute"><x-filament-actions::modals /></div>
    {{ $this->content }}
</x-filament-panels::page>
