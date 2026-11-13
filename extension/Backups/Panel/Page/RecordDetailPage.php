<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Admin/Support/Panel/Page/RecordDetailPage.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Page;
use Filament\Resources\Pages\Page;
/** A resource record's Detail page, such as a monitored domain. See Concerns\DetailType. */
abstract class RecordDetailPage extends Page implements PageType
{
    use Concerns\DetailType;
    use Concerns\ForRecord;
    protected const WIDE = false;
    protected const PAGE_CLASSES = ['domains-page-detail', 'fi-resource-view-record-page'];
}
