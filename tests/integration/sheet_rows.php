<?php
/** Exercise the real Filament visibility filter without a database.
 * Standalone runs test the checkout Sheet class. A caller that sets $sheetInstalled to the
 * installed extension directory requires Paymenter's autoloader to supply that copy. */
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
if (isset($sheetInstalled)) {
    // No checkout fallback: a package without Sheet.php must fail here.
    $sheetFile = $sheetInstalled . '/Admin/Support/Sheet.php';
    if (!class_exists(Sheet::class)) {
        throw new RuntimeException('installed Sheet class did not load from ' . $sheetFile);
    }
} else {
    $sheetFile = dirname(__DIR__, 2) . '/extension/Backups/Admin/Support/Sheet.php';
    if (!class_exists(Sheet::class, false)) {
        require_once $sheetFile;
    }
}
$sheetLoaded = (new ReflectionClass(Sheet::class))->getFileName();
if (realpath($sheetLoaded) !== realpath($sheetFile)) {
    throw new RuntimeException('Sheet class loaded from ' . $sheetLoaded . ', expected ' . $sheetFile);
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
