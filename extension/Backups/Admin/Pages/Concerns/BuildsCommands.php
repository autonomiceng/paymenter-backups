<?php
namespace Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns;

use Carbon\CarbonImmutable;
use Filament\Actions\Action;
use Filament\Schemas\Components\Text;
use Filament\Schemas\Components\UnorderedList;
use Filament\Support\Enums\FontFamily;
use Filament\Support\Enums\Size;
use Paymenter\Extensions\Others\Backups\Runtime;
use Paymenter\Extensions\Others\Backups\Settings;

trait BuildsCommands
{
    private static function shell(string $word): string
    {
        return preg_match('~^[A-Za-z0-9_@%+=:,./-]+$~D', $word) ? $word : escapeshellarg($word);
    }

    /** Cron parses percent before the shell, even inside quoted arguments. */
    private static function cronShell(string $word): string
    {
        return implode('\\%', array_map([self::class, 'shell'], explode('%', $word)));
    }

    private function safePrivateRoot(): ?string
    {
        try { return Runtime::privateRoot(); } catch (\Throwable $error) { return null; }
    }

    private function binary(string $tool, string $fallback): string
    {
        $checked = $this->checks()['tools'][$tool] ?? [];
        $configured = $this->formValues()[$tool] ?? '';
        return $configured ?: (is_string($checked['resolved'] ?? null) ? $checked['resolved'] : $fallback);
    }

    /** Cron line for Paymenter's scheduler, from the engine. The fallback mirrors its format. */
    private function schedulerLine(): string
    {
        $line = $this->checks()['jobs']['suggestions']['scheduler'] ?? null;
        if (is_string($line) && $line !== '') { return $line; }
        $script = Runtime::guardedSchedulerPath();
        if (base_path() === '/app' && $script !== '') {
            foreach ($this->checks()['jobs']['items'] ?? [] as $job) {
                $command = $job['command'] ?? null;
                $lock = $job['lock'] ?? null;
                if (($job['kind'] ?? null) === 'scheduler' && ($job['coordination'] ?? null) === 'coordinated'
                    && is_string($lock) && $lock !== ''
                    && is_string($command) && $command === '/usr/bin/flock -n ' . self::shell($lock) . ' /usr/local/bin/php ' . self::shell($script)) {
                    return '* * * * * /usr/bin/flock -n ' . self::cronShell($lock) . ' /usr/local/bin/php ' . self::cronShell($script) . ' >/dev/null 2>&1';
                }
            }
        }
        $root = $this->safePrivateRoot() ?? '/path/to/paymenter-backups-private';
        return '* * * * * /usr/bin/flock -n ' . self::cronShell($root . '/scheduler.lock') . ' ' . self::cronShell($this->binary('php', PHP_BINDIR . '/php')) . ' ' . self::cronShell(base_path('artisan')) . ' schedule:run >/dev/null 2>&1';
    }

    private function queueLine(): string
    {
        $line = $this->checks()['jobs']['suggestions']['queue'] ?? null;
        if (is_string($line) && $line !== '') { return $line; }
        $root = $this->safePrivateRoot() ?? '/path/to/paymenter-backups-private';
        return '* * * * * /usr/bin/flock -n ' . self::cronShell($root . '/queue.lock') . ' ' . self::cronShell($this->binary('php', PHP_BINDIR . '/php')) . ' ' . self::cronShell(base_path('artisan')) . ' queue:work --stop-when-empty --max-time=50 >/dev/null 2>&1';
    }

    /** Paymenter's daily billing time and the window full backups keep clear, as clock times. */
    private function billingWindow(): ?array
    {
        $billing = $this->checks()['billing'] ?? $this->configuration()['billing'] ?? null;
        if (!is_array($billing) || !is_string($billing['time'] ?? null) || !preg_match('/^\d{2}:\d{2}$/', $billing['time'])) { return null; }
        $zone = is_string($billing['timezone'] ?? null) && $billing['timezone'] !== '' ? $billing['timezone'] : 'UTC';
        if (is_string($billing['window_before'] ?? null) && preg_match(Settings::TIME, $billing['window_before'])
            && is_string($billing['window_after'] ?? null) && preg_match(Settings::TIME, $billing['window_after'])) {
            return ['time' => $billing['time'], 'timezone' => $zone, 'start' => $billing['window_before'], 'end' => $billing['window_after']];
        }
        $stored = $this->formValues();
        $margin = (int) ($stored['billing_margin_seconds'] ?? 120);
        $before = (int) $stored['lock_timeout'] + (int) $stored['drain_seconds'] + max(1, (int) $stored['capture_timeout']) + 30 + $margin;
        $after = 60 + $margin;
        try { $at = CarbonImmutable::createFromFormat('H:i', $billing['time'], $zone); } catch (\Throwable $error) { return null; }
        return ['time' => $billing['time'], 'timezone' => $zone, 'start' => $at->subSeconds($before)->format('H:i'), 'end' => $at->addSeconds($after)->format('H:i')];
    }

    private function billingSentence(): ?string
    {
        $window = $this->billingWindow();
        return $window ? 'Paymenter bills daily at ' . $window['time'] . ' ' . $window['timezone'] . '. Full backups won\'t start between ' . $window['start'] . ' and ' . $window['end'] . '.' : null;
    }

    /** Exact escaped text, copyable over HTTPS with click, Enter or Space. */
    private function command(string|\Closure $command): Text
    {
        $plain = static fn () => $command instanceof \Closure ? (string) $command() : $command;
        // Keep meaningful whitespace inside its own span: preserving whitespace
        // on the native Text wrapper would also show the template's indentation.
        return Text::make(static fn () => new \Illuminate\Support\HtmlString('<span style="white-space: pre">' . e($plain()) . '</span>'))->fontFamily(FontFamily::Mono)
            ->copyable(fn () => request()->isSecure())->copyableState($plain)->copyMessage('Copied')
            ->tooltip(fn () => request()->isSecure() ? 'Copy' : null)
            // Keep command lines intact and scroll long paths inside the theme's input frame.
            ->extraAttributes(['data-backups-command' => true, 'tabindex' => '0', 'class' => 'fi-input-wrp w-full px-4 py-2', 'style' => 'overflow-wrap: normal; overflow-x: auto; max-width: 100%']);
    }

    /** A bulleted list. Items are blocks so each bullet sits on the first line of a wrapped item. */
    private function bullets(array $lines): UnorderedList
    {
        return UnorderedList::make(array_map(static fn ($line) => Text::make($line)->extraAttributes(['style' => 'display: block']), $lines))->columns(1);
    }

    private function settingsUrl(string $anchor = 'settings'): string
    {
        return static::getUrl(['tab' => 'settings']) . ($anchor !== 'settings' ? '#' . $anchor : '');
    }

    private function link(string $name, string $label, string $url): Action
    {
        return Action::make($name)->label($label)->url($url)->button()->size(Size::Small)->color('gray');
    }
}
