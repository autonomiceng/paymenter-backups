<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Presentation/Tone.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Presentation;
/** The five status tones. A page never picks one: a State gives it for its reader, and the mark comes from the tone
 * (a skin draws it from data-domains-tone). Adding a tone or mark needs Ilya. */
enum Tone: string
{
    /** Solid green, check: done or healthy. */
    case Ok = 'ok';
    /** Hollow grey, minus: stopped on purpose, or nothing to do. */
    case Off = 'off';
    /** Hollow cyan, node: we or the registrar are working on it. */
    case Progress = 'prog';
    /** Solid amber, "!": this reader must do something. */
    case Attention = 'warn';
    /** Solid red, x: it broke. */
    case Failed = 'err';
}
