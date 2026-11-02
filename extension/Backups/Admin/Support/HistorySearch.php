<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Support;

/** Search the complete visible history before the table takes a page. */
final class HistorySearch
{
    public static function filter(array $rows, ?string $search): array
    {
        $terms = preg_split('/\s+/u', mb_strtolower(trim($search ?? ''), 'UTF-8'), -1, PREG_SPLIT_NO_EMPTY);
        if (!$terms) { return $rows; }
        return array_values(array_filter($rows, static function (array $row) use ($terms): bool {
            $values = array_map(static fn ($key) => strip_tags((string) ($row[$key] ?? '')),
                ['run_id', 'request_id', 'type', 'origin', 'status_label', 'detail', 'location', 'size_label']);
            $text = mb_strtolower(html_entity_decode(implode(' ', $values), ENT_QUOTES | ENT_HTML5, 'UTF-8'), 'UTF-8');
            foreach ($terms as $term) {
                if (!str_contains($text, $term)) { return false; }
            }
            return true;
        }));
    }
}
