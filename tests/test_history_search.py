"""The native table searches every eligible row before it slices a page."""
import json
import os
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')


@unittest.skipUnless(PHP, 'PHP CLI required')
class HistorySearch(unittest.TestCase):
    def table(self, search=None, page=1, deleted=False):
        runs = [dict(run_id=f'backup-{i:02}', request_id=None, created_at=i, mode='database-online',
                     status='ready', download='ready', remote='delivered', local=True, size=4096)
                for i in range(60)]
        runs[2].update(status='damaged', download='verify', verification_error='archive-changed')
        runs[3].update(status='deleted', deletion_reason='manual')
        result = subprocess.run([PHP, str(ROOT / 'tests/fixtures/history-table.php'),
                                 os.environ.get('BACKUPS_SOURCE_ROOT', str(ROOT))],
                                input=json.dumps(dict(overview=dict(runs=runs, requests=[]),
                                                      search=search, page=page, deleted=deleted)),
                                text=True, capture_output=True, check=True, timeout=5)
        return json.loads(result.stdout)

    def test_match_beyond_first_page_is_found_and_total_is_filtered(self):
        result = self.table('BACKUP-02')
        self.assertEqual(result['page']['total'], 1)
        self.assertEqual([row['run_id'] for row in result['page']['items']], ['backup-02'])
        self.assertTrue(result['page']['items'][0]['detail'])  # Failure reason survives search.

    def test_terms_match_displayed_fields_and_preserve_newest_first(self):
        result = self.table('database verified S3')
        self.assertEqual(result['page']['total'], 58)
        self.assertEqual(len(result['page']['items']), 25)
        self.assertEqual(result['page']['items'][0]['run_id'], 'backup-59')
        self.assertEqual(self.table('database verified S3', page=3)['page']['items'][-1]['run_id'], 'backup-00')
        self.assertEqual(self.table('not-a-backup')['page']['total'], 0)
        self.assertEqual(self.table(' \t ')['page']['total'], 59)
        self.assertEqual(result['paging'], [25, 50, 100])

    def test_search_never_reintroduces_hidden_deleted_rows(self):
        self.assertEqual(self.table('backup-03')['page']['total'], 0)
        self.assertEqual(self.table('backup-03', deleted=True)['page']['total'], 1)


if __name__ == '__main__':
    unittest.main()
