<?php
/** Minimal fluent stand-ins capture the real table's records callback; no database or engine mock. */
namespace Test {
    class Fluent {
        public array $args = [];
        public array $options = [];
        public static function make(...$arguments): static { $item = new static; $item->args = $arguments; return $item; }
        public function __call(string $name, array $arguments): static { $this->options[$name] = $arguments; return $this; }
    }
}
namespace Filament\Actions { class Action extends \Test\Fluent {} class ActionGroup extends \Test\Fluent {} }
namespace Filament\Tables { class Table extends \Test\Fluent {} }
namespace Filament\Tables\Columns { class TextColumn extends \Test\Fluent {} }
namespace Filament\Schemas\Components {
    class Component extends \Test\Fluent {}
    class Section extends Component {} class Flex extends Component {} class Group extends Component {} class Actions extends Component {}
    class Html extends Component {} class Text extends Component {} class Callout extends Component {} class UnorderedList extends Component {}
}
namespace Filament\Infolists\Components { class TextEntry extends \Filament\Schemas\Components\Component { public function getName() { return $this->args[0]; } } }
namespace Filament\Forms\Components { class Radio extends \Filament\Schemas\Components\Component {} }
namespace Filament\Support\Enums { enum FontFamily { case Mono; } enum Size { case Small; } enum Alignment { case Start; case End; } }
namespace Illuminate\Contracts\Support { interface Htmlable { public function toHtml(); } }
namespace Illuminate\Support {
    class HtmlString implements \Illuminate\Contracts\Support\Htmlable {
        public function __construct(private string $html = '') {}
        public function toHtml() { return $this->html; }
        public function __toString(): string { return $this->html; }
    }
}
namespace Illuminate\Pagination {
    class LengthAwarePaginator {
        public function __construct(public array $items, public int $total, public int $perPage, public int $currentPage) {}
    }
}
namespace {
    function filled($value) { return !($value === null || $value === [] || (is_string($value) && trim($value) === '')); }
    function e($value) { return htmlspecialchars((string) $value, ENT_QUOTES, 'UTF-8'); }
    function request() { return new class { public function isSecure() { return true; } }; }
    // Views render as their state word, enough to read the status line.
    function view($name, $data = []) { return new class($data) { public function __construct(private array $data) {} public function render() { return '[' . ($this->data['slot'] ?? '') . ']'; } }; }

    $root = $argv[1];
    require $root . '/extension/Backups/Messages.php';
    foreach (['HistorySearch', 'StatusTone', 'Sheet'] as $name) {
        $file = $root . '/extension/Backups/Admin/Support/' . $name . '.php';
        if (is_file($file)) { require $file; }
    }
    require $root . '/extension/Backups/Admin/Pages/Concerns/BuildsHistoryTable.php';
    require $root . '/extension/Backups/Admin/Pages/Concerns/BuildsCommands.php';
    class HistoryPage {
        use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsHistoryTable;
        use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsCommands;
        private const ACTIVE = ['pending', 'deferred', 'launching', 'running'];
        public bool $showDeleted = false;
        public array $fixture;
        private function overview(): array { return $this->fixture; }
        private function configuration(): array { return []; }
        private function formValues(): array { return ($this->fixture['settings'] ?? []) + ['retention_keep_full' => 7, 'retention_keep_database' => 24]; }
        private function uploadsToS3(): bool { return $this->fixture['uploads'] ?? true; }
        private function schedulerFresh(): bool { return true; }
        private function tasksFor(string $id): array { return []; }
        private function activeRequest(string $action, ?string $runId = null): ?array { return null; }
        private function moment(?float $timestamp): string { return $timestamp === null ? '—' : 'at ' . $timestamp; }
        private function ago(?float $timestamp): string { return $timestamp === null ? '' : 'ago ' . $timestamp; }
        private function backupAction(string $name) { return \Filament\Actions\Action::make($name); }
        public static function getUrl(array $parameters = []): string { return '/admin/backups?' . http_build_query($parameters); }
    }

    /** Sections of a schema as {title: {label: [state, below]}}; hidden sections and entries are left out. */
    function sections(array $components): array
    {
        $visible = static function ($item) {
            $hidden = $item->options['hidden'][0] ?? false;
            $shown = $item->options['visible'][0] ?? true;
            return !($hidden instanceof \Closure ? $hidden() : $hidden) && ($shown instanceof \Closure ? $shown() : $shown);
        };
        $entries = function ($value) use (&$entries, $visible) {
            $found = [];
            if ($value instanceof \Closure) { $value = $value(); }
            if (is_array($value)) { foreach ($value as $item) { $found += $entries($item); } return $found; }
            if (!$value instanceof \Test\Fluent || !$visible($value)) { return []; }
            if ($value instanceof \Filament\Infolists\Components\TextEntry) {
                $state = $value->options['state'][0] ?? null;
                $below = array_map(static fn ($text) => (string) $text->args[0], $value->options['belowContent'][0] ?? []);
                return [(string) ($value->options['label'][0] ?? $value->args[0]) => [(string) ($state instanceof \Closure ? $state() : $state), $below]];
            }
            static $texts = 0;
            if ($value instanceof \Filament\Schemas\Components\Text) { $text = $value->args[0]; return ['text:' . $texts++ => [strip_tags((string) ($text instanceof \Closure ? $text() : $text)), []]]; }
            foreach ([$value->args, $value->options['schema'] ?? []] as $children) { $found += $entries($children); }
            return $found;
        };
        $result = [];
        foreach ($components as $section) {
            if ($visible($section)) { $result[(string) $section->args[0]] = $entries($section->options['schema'] ?? []); }
        }
        return $result;
    }

    $input = json_decode(stream_get_contents(STDIN), true, flags: JSON_THROW_ON_ERROR);
    $page = new HistoryPage;
    $page->fixture = $input['overview'];
    $page->showDeleted = $input['deleted'] ?? false;
    $table = $page->table(new \Filament\Tables\Table);
    $records = $table->options['records'][0];
    $result = $records($input['page'] ?? 1, $input['perPage'] ?? 25, $input['search'] ?? null);
    // Linked notes are HTML; tests read their markup.
    $plain = array_map(static fn ($row) => array_map(static fn ($value) => $value instanceof \Illuminate\Contracts\Support\Htmlable ? $value->toHtml() : $value, $row), $result->items);
    $output = ['page' => new \Illuminate\Pagination\LengthAwarePaginator($plain, $result->total, $result->perPage, $result->currentPage), 'paging' => $table->options['paginated'][0]];
    $output['classes'] = array_map($table->options['recordClasses'][0] ?? static fn () => null, $result->items);
    if (isset($input['details'])) {
        $record = array_values(array_filter($result->items, static fn ($row) => $row['__key'] === $input['details']))[0];
        $output['details'] = (fn () => ['line' => (string) $this->detailsLine($record), 'sections' => sections($this->detailsSchema($record))])->call($page);
    }
    echo json_encode($output, JSON_THROW_ON_ERROR);
}
