<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Presentation/DomainName.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Presentation;
use Illuminate\Support\HtmlString;
/** A domain name that wraps after dots and hyphens, not inside a label. Staff tables keep names on one line instead. */
final class DomainName
{
    /** Escaped name with a line break opportunity after each dot and hyphen. */
    public static function html(string $name): HtmlString
    {
        return new HtmlString(str_replace(['.', '-'], ['.<wbr>', '-<wbr>'], e($name)));
    }
}
