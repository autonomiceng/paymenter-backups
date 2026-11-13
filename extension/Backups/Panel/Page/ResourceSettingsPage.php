<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Admin/Support/Panel/Page/ResourceSettingsPage.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Page;
use Filament\Resources\Pages\Page;
/** A Settings page that lives under a resource, such as Domain monitoring settings. See Concerns\SettingsType. */
abstract class ResourceSettingsPage extends Page implements PageType
{
    use Concerns\SettingsType;
    protected const WIDE = false;
    protected const PAGE_CLASSES = ['domains-page-settings'];
}
