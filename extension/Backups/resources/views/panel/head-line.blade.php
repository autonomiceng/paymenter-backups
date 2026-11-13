{{-- Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/resources/views/components/panel/head-line.blade.php by tools/panel_sync.py. Change it in Domains and run the script again. --}}
{{-- MIT, Copyright (c) 2026 Autonomic Engineering. The one line under a page title (Panel\Head): the record's state with its mark, facts, then one link. Spans only, since Filament puts it in a paragraph. --}}
<span class="domains-panel-head-line" data-piece="head-line">@if ($word !== null)<span class="domains-panel-st" data-domains-tone="{{ $tone->value }}" data-domains-record="{{ $record }}">{{ $word }}</span>@endif
@foreach ($facts as $item) <span class="domains-panel-head-fact">{{ $item['label'] }} @include('backups::panel.fact', ['fact' => $item['fact']])</span>@endforeach
@if ($link) <a class="domains-panel-link" href="{{ $link['url'] }}">{{ $link['label'] }}</a>@endif</span>
