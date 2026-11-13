<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Admin/Support/Panel/Page/DetailPage.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Page;
use Filament\Pages\Page;
/** A Detail page that is not a resource page, such as a registrar account. See Concerns\DetailType. */
abstract class DetailPage extends Page implements PageType
{
    use Concerns\DetailType;
    protected const WIDE = false;
    protected const PAGE_CLASSES = ['domains-page-detail', 'fi-resource-view-record-page'];
}
