<?php
/** Exercise the real Filament visibility filter without a database or installed extension. */
use Filament\Actions\Action;
use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Concerns\InteractsWithSchemas;
use Filament\Schemas\Contracts\HasSchemas;
use Filament\Schemas\Schema;
use Paymenter\Extensions\Others\Backups\Admin\Support\Sheet;

if (!class_exists(Schema::class)) {
    require $argv[1] . '/vendor/autoload.php';
    Illuminate\Container\Container::setInstance(new Illuminate\Container\Container);
}
if (!class_exists(Sheet::class)) {
    require_once dirname(__DIR__, 2) . '/extension/Backups/Admin/Support/Sheet.php';
}

$sheetLivewire = new class extends Livewire\Component implements HasSchemas {
    use InteractsWithSchemas;
};
foreach ([false, true] as $uploadS3) {
    $sheetRows = Sheet::rows([
        TextEntry::make('overview_storage')->state('This server'),
        TextEntry::make('overview_last_s3')->visible(fn () => $uploadS3),
        TextEntry::make('overview_encryption')->visible(fn () => $uploadS3),
        TextEntry::make('action_entry')->hidden(),
    ], ['action_entry' => [Action::make('download')->hidden()]]);
    $sheetSchema = Schema::make($sheetLivewire)->components($sheetRows);
    $expectedRows = $uploadS3 ? $sheetRows : [$sheetRows[0], $sheetRows[3]];
    if ($sheetSchema->getComponents() !== $expectedRows) {
        throw new RuntimeException('S3 ' . ($uploadS3 ? 'on: visible rows changed' : 'off: empty overview rows remain'));
    }
}
