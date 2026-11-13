{{-- Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/resources/views/components/panel/head-title.blade.php by tools/panel_sync.py. Change it in Domains and run the script again. --}}
{{-- MIT, Copyright (c) 2026 Autonomic Engineering. A page title (Panel\Head) with its Demo or Test tag. --}}
<span class="domains-panel-head-title" data-piece="head-title">{{ $domain ? \Paymenter\Extensions\Others\Backups\Panel\Presentation\DomainName::html($title) : $title }}</span>{{ \Paymenter\Extensions\Others\Backups\Panel\Support\DemoData::label($demo, $test, inline: true) }}
