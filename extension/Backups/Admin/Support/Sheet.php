<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Support;

use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\{Actions, Component, Flex, Group, Html, Section};
use Illuminate\Contracts\Support\Htmlable;
use Illuminate\Support\HtmlString;

/** Plain Filament schemas using the shared skin's existing sheet selectors.
 * The historical domains-* CSS names do not require the Domains extension. */
final class Sheet
{
    public static function make(array $sections): Group
    {
        return Group::make($sections)->extraAttributes(['class' => 'domains-sheet']);
    }

    public static function section(string|\Closure $title): Section
    {
        return Section::make($title)->extraAttributes(['class' => 'domains-sec']);
    }

    public static function row(TextEntry $entry, array $actions = []): Flex
    {
        $children = [$entry->inlineLabel()->grow()];
        if ($actions) { $children[] = Actions::make($actions)->grow(false); }
        $row = Flex::make($children)->from('md')->extraAttributes(['class' => 'domains-row']);
        return $actions ? $row : $row->hiddenWhenAllChildComponentsHidden();
    }

    public static function rows(array $entries, array $actions = []): array
    {
        return array_map(static fn (TextEntry $entry) => self::row($entry, $actions[$entry->getName()] ?? []), $entries);
    }

    /** Native form fields and prose keep their own semantics inside padded sheet rows. */
    public static function contentRows(array|\Closure $components): \Closure
    {
        return static fn () => array_map(static fn (Component $component) => Flex::make([$component->grow()])
            ->hiddenWhenAllChildComponentsHidden()->visibleJs($component->getVisibleJs())->hiddenJs($component->getHiddenJs())
            ->from('md')->extraAttributes(['class' => 'domains-row']), $components instanceof \Closure ? $components() : $components);
    }

    /** The sheet with a rail beside it; the rail drops below the sheet on narrow screens. */
    public static function split(array $sheet, array $rail): Group
    {
        return Group::make([
            Group::make($sheet)->columnSpan(['default' => 1, 'xl' => 2])->extraAttributes(['class' => 'domains-sheet']),
            Group::make($rail)->columnSpan(1)->extraAttributes(['class' => 'domains-rail']),
        ])->columns(['default' => 1, 'xl' => 3])->columnSpanFull()->extraAttributes(['class' => 'domains-split']);
    }

    /** A state word with its mark. Plain Filament shows the same word as a badge. */
    public static function state(TextEntry $entry, ?string $color): TextEntry
    {
        return $entry->badge()->color($color)->extraEntryWrapperAttributes(['data-beco-tone' => StatusTone::forColor($color)]);
    }

    /** The status line under a title: mark, state word, then one sentence. */
    public static function headLine(string $color, string $label, string|Htmlable|null $step = null): HtmlString
    {
        $badge = view('filament::components.badge', ['color' => $color, 'size' => 'lg', 'slot' => new HtmlString(e($label))])->render();
        $step = $step instanceof Htmlable ? $step->toHtml() : e((string) $step);
        return new HtmlString('<span class="domains-head-line"><span class="domains-head-state" data-beco-tone="' . e(StatusTone::forColor($color) ?? '') . '">'
            . $badge . '</span>' . ($step !== '' ? ' <span class="domains-head-step">' . $step . '</span>' : '') . '</span>');
    }

    /**
     * Marked steps, each {label, color, value?, detail?}: a list to screen readers, plain lines without a skin.
     * The trace joins steps with a line; checks stand alone.
     */
    public static function steps(array $steps, bool $trace = true): Group
    {
        return Group::make(array_map(static fn (array $step) => Group::make([
            Html::make(new HtmlString(view('backups::partials.step', $step)->render())),
        ])->extraAttributes(['class' => 'domains-trace-item', 'role' => 'listitem', 'data-beco-tone' => StatusTone::forColor($step['color']) ?? 'off']), $steps))
            ->extraAttributes(['class' => 'domains-trace' . ($trace ? '' : ' backups-checks'), 'role' => 'list']);
    }
}
