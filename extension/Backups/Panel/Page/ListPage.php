<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Admin/Support/Panel/Page/ListPage.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Page;
use Filament\Pages\Page;
use Filament\Tables\Contracts\HasTable;
/** A List page over rows the page reads. See Concerns\ListType. */
abstract class ListPage extends Page implements PageType, HasTable
{
    use Concerns\ListType;
    protected const WIDE = true;
    protected const PAGE_CLASSES = ['domains-page-list', 'fi-resource-list-records-page'];
}
