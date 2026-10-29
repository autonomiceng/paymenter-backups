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
        span = re.fullmatch(r'<span style="([^"]*)">(.*)</span>', rendered['html'], re.S)
        self.assertIsNotNone(span, rendered['html'])
        self.assertEqual(span[2], COMMAND.replace('&', '&amp;').replace('>', '&gt;'))
        attributes = rendered['attributes']
        self.assertTrue(attributes['data-backups-command'])
        self.assertFalse(splits_words([attributes['style'], span[1]], attributes['class']), attributes)
        self.assertEqual(declarations(span[1])['white-space'], 'pre')
        self.assertEqual(declarations(attributes['style'])['overflow-x'], 'auto')
        # Scrolled content stays reachable from the keyboard.
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


if __name__ == '__main__':
    unittest.main()
