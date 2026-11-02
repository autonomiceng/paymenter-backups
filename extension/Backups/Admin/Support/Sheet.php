<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Support;

use Filament\Infolists\Components\TextEntry;
use Filament\Schemas\Components\{Actions, Component, Flex, Group, Section};

/** Plain Filament schemas using the shared skin's existing sheet selectors.
 * The historical domains-* CSS names do not require the Domains extension. */
final class Sheet
{
    public static function make(array $sections): Group
    {
        return Group::make($sections)->extraAttributes(['class' => 'domains-sheet']);
    }

    public static function section(string $title): Section
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
}
