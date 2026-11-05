"""Setup commands stay whole on narrow screens: words like php never split across lines."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')
COMMAND = '* * * * * /usr/bin/flock -n /private/scheduler.lock /usr/local/bin/php /opt/paymenter/scheduler.php >/dev/null 2>&1'

# Minimal framework stand-ins: Text records the HTML and attributes the page renders.
SCRIPT = r'''
namespace Filament\Support\Enums {
    enum FontFamily { case Mono; }
}
namespace Filament\Schemas\Components {
    final class Text {
        public array $attributes = [];
        private function __construct(public \Closure|string $content) {}
        public static function make(\Closure|string $content): static { return new static($content); }
        public function extraAttributes(array $attributes): static { $this->attributes = $attributes; return $this; }
        public function __call(string $name, array $arguments): static { return $this; }
    }
}
namespace Illuminate\Support {
    final class HtmlString { public function __construct(private string $value) {} public function __toString(): string { return $this->value; } }
}
namespace {
    function e($value) { return htmlspecialchars((string) $value, ENT_QUOTES, 'UTF-8'); }
    function request() { return new class { public function isSecure() { return true; } }; }
    require $argv[1] . '/extension/Backups/Admin/Pages/Concerns/BuildsCommands.php';
    final class CommandPage { use \Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsCommands { command as public; } }
    $text = (new CommandPage)->command($argv[2]);
    echo json_encode(['html' => (string) ($text->content)(), 'attributes' => $text->attributes], JSON_THROW_ON_ERROR);
}
'''


def declarations(style):
    return {name.strip(): value.strip() for name, _, value in
            (part.partition(':') for part in style.split(';') if part.strip())}


def splits_words(styles, classes=''):
    """True when the CSS lets a line break inside a word such as php."""
    merged = {}
    for style in styles:
        merged.update(declarations(style))
    return ('break-words' in classes.split() or 'break-all' in classes.split()
            or merged.get('overflow-wrap') in ('anywhere', 'break-word')
            or merged.get('word-wrap') == 'break-word'
            or merged.get('word-break') in ('break-all', 'break-word'))


@unittest.skipUnless(PHP, 'PHP CLI required for the command block')
class CommandBlockWrap(unittest.TestCase):
    def render(self):
        result = subprocess.run([PHP, '-r', SCRIPT, str(ROOT), COMMAND],
                                capture_output=True, text=True, check=True, timeout=5)
        return json.loads(result.stdout)

    def test_command_block_keeps_words_whole_and_scrolls(self):
        rendered = self.render()
        span = re.fullmatch(r'<span style="([^"]*)">(.*?)</span><span class="backups-copy" aria-hidden="true"[^>]*>Copy</span>', rendered['html'], re.S)
        self.assertIsNotNone(span, rendered['html'])
        self.assertEqual(span[2], COMMAND.replace('&', '&amp;').replace('>', '&gt;'))
        attributes = rendered['attributes']
        self.assertTrue(attributes['data-backups-command'])
        self.assertFalse(splits_words([attributes['style'], span[1]], attributes['class']), attributes)
        self.assertEqual(declarations(span[1])['white-space'], 'pre')
        # The command scrolls inside the frame so the Copy word stays visible.
        self.assertEqual(declarations(span[1])['overflow-x'], 'auto')
        # The native copyable wrapper stays reachable from the keyboard.
        self.assertEqual(attributes['tabindex'], '0')

    def test_detected_job_command_keeps_words_whole_and_scrolls(self):
        source = (ROOT / 'extension/Backups/Admin/Pages/Concerns/BuildsSettingsForm.php').read_text()
        line = re.search(r"Text::make\(\(string\) \$job\['command'\]\).*?->extraAttributes\(\[(.*?)\]\)", source)
        self.assertIsNotNone(line, 'detected job command row not found')
        style = re.search(r"'style' => '([^']*)'", line[1])[1]
        self.assertFalse(splits_words([style]), style)
        self.assertEqual(declarations(style)['white-space'], 'nowrap')
        self.assertEqual(declarations(style)['overflow-x'], 'auto')
        self.assertIn("'tabindex' => '0'", line[1])

    def test_native_scrolling_check_matches_rendered_command_markup(self):
        rendered = self.render()
        source = (ROOT / 'tests/integration/native_checks.php').read_text()
        check = re.search(r"check\(([^\n]+), 'mobile history or command scrolling missing'\);", source)
        self.assertIsNotNone(check, 'native command scrolling check not found')
        needles = re.findall(r"str_contains\(\$queue->html\(\), '([^']+)'\)", check[1])
        self.assertEqual(len(needles), 2)
        # History stacking is unrelated; exercise the exact command assertion
        # against the real BuildsCommands output, without booting a database.
        markup = 'fi-ta-table-stacked-on-mobile ' + rendered['html']
        self.assertTrue(all(needle in markup for needle in needles),
                        f'Native check strings {needles!r} do not match rendered markup {markup!r}')

    def test_recovery_contract_matches_rendered_command_span_style(self):
        rendered = self.render()
        source = (ROOT / 'tests/integration/admin_contracts.php').read_text()
        style = re.search(r"\$valueNode->getAttribute\('style'\) === '([^']+)'", source)
        self.assertIsNotNone(style, 'recovery command span style check not found')
        self.assertTrue(rendered['html'].startswith(f'<span style="{style[1]}">'),
                        f'Recovery check style {style[1]!r} does not match rendered markup {rendered["html"]!r}')


if __name__ == '__main__':
    unittest.main()
