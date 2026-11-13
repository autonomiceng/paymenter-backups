<?php
// Synced from Domains da04a18ba33f05f38dd67607d36d9bfcf3510206 Domains/Admin/Support/Panel/Page/Concerns/ForRecord.php by tools/panel_sync.py. Change it in Domains and run the script again.
// MIT, Copyright (c) 2026 Autonomic Engineering.
declare(strict_types=1);
namespace Paymenter\Extensions\Others\Backups\Panel\Page\Concerns;
use Filament\Resources\Pages\Concerns\InteractsWithRecord;
/** A resource page about one record: it loads the record and checks the resource's view permission on every request,
 * as Filament's own view page does. */
trait ForRecord
{
    use InteractsWithRecord;
    public function mount(int|string $record): void { $this->record = $this->resolveRecord($record); $this->authorizeAccess(); }
    public function hydrate(): void { $this->authorizeAccess(); }
    protected function authorizeAccess(): void { abort_unless(static::getResource()::canView($this->getRecord()), 403); }
}
