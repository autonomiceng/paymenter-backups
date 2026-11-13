<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Presentation/Reader.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Presentation;
/** Who reads a page. Both read the same words; only a state's tone and the detail under it can differ. */
enum Reader
{
    case Staff;
    case Customer;
}
