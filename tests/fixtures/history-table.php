<?php
/** Minimal fluent stand-ins capture the real table's records callback; no database or engine mock. */
namespace Test {
    class Fluent {
        public array $options = [];
        public static function make(...$arguments): static { return new static; }
        public function __call(string $name, array $arguments): static { $this->options[$name] = $arguments; return $this; }
    }
}
namespace Filament\Actions { class Action extends \Test\Fluent {} class ActionGroup extends \Test\Fluent {} }
namespace Filament\Tables { class Table extends \Test\Fluent {} }
namespace Filament\Tables\Columns { class TextColumn extends \Test\Fluent {} }
namespace Illuminate\Pagination {
    class LengthAwarePaginator {
        public function __construct(public array $items, public int $total, public int $perPage, public int $currentPage) {}
    }
}
namespace {
    $root = $argv[1];
    require $root . '/extension/Backups/Messages.php';
    foreach (['HistorySearch', 'StatusTone'] as $name) {
        $file = $root . '/extension/Backups/Admin/Support/' . $name . '.php';
        if (is_file($file)) { require $file; }
    }
    require $root . '/extension/Backups/Admin/Pages/Concerns/BuildsHistoryTable.php';
    class HistoryPage {
        use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsHistoryTable;
        private const ACTIVE = ['pending', 'deferred', 'launching', 'running'];
        public bool $showDeleted = false;
        public array $fixture;
        private function overview(): array { return $this->fixture; }
        private function uploadsToS3(): bool { return true; }
        private function tasksFor(string $id): array { return []; }
        private function backupAction(string $name) { return \Filament\Actions\Action::make($name); }
    }
    $input = json_decode(stream_get_contents(STDIN), true, flags: JSON_THROW_ON_ERROR);
    $page = new HistoryPage;
    $page->fixture = $input['overview'];
    $page->showDeleted = $input['deleted'] ?? false;
    $table = $page->table(new \Filament\Tables\Table);
    $records = $table->options['records'][0];
    $result = $records($input['page'] ?? 1, $input['perPage'] ?? 25, $input['search'] ?? null);
    echo json_encode(['page' => $result, 'paging' => $table->options['paginated'][0]], JSON_THROW_ON_ERROR);
}
