"""Which Sheet class the native sheet rows check proves: installed package or checkout source."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Stand-in for Paymenter's extensions/ autoloading of an installed package directory.
DRIVER = r'''
[, $app, $script, $installed] = $argv;
if ($installed !== '') {
    $sheetInstalled = $installed;
    spl_autoload_register(function ($class) use ($installed) {
        $file = $installed . '/Admin/Support/Sheet.php';
        if ($class === 'Paymenter\Extensions\Others\Backups\Admin\Support\Sheet' && is_file($file)) { require $file; }
    });
}
$argv = [$script, $app];
require $script;
echo 'rows checked';
'''


@unittest.skipUnless(os.environ.get('PHP_BINARY') and os.environ.get('PAYMENTER_TEST_APP'),
                     'PHP_BINARY and PAYMENTER_TEST_APP required')
class SheetRowsSource(unittest.TestCase):
    def run_rows(self, installed=''):
        return subprocess.run([os.environ['PHP_BINARY'], '-r', DRIVER, os.environ['PAYMENTER_TEST_APP'],
                               str(ROOT / 'tests/integration/sheet_rows.php'), str(installed)],
                              capture_output=True, text=True, timeout=60)

    def test_standalone_run_checks_checkout_source(self):
        result = self.run_rows()
        self.assertEqual((result.returncode, result.stdout), (0, 'rows checked'), result.stderr)

    def test_installed_run_fails_when_package_lacks_sheet(self):
        with tempfile.TemporaryDirectory(prefix='sheet-rows-') as scratch:
            result = self.run_rows(Path(scratch) / 'Backups')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('installed Sheet class did not load', result.stdout + result.stderr)

    def test_installed_run_checks_packaged_copy(self):
        with tempfile.TemporaryDirectory(prefix='sheet-rows-') as scratch:
            installed = Path(scratch) / 'Backups'
            (installed / 'Admin/Support').mkdir(parents=True)
            shutil.copy2(ROOT / 'extension/Backups/Admin/Support/Sheet.php', installed / 'Admin/Support/Sheet.php')
            result = self.run_rows(installed)
        self.assertEqual((result.returncode, result.stdout), (0, 'rows checked'), result.stderr)
