"""Running, failed, upload-failed and waiting backups read right in the list and in Details."""
import json
import os
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')


def run(run_id, **changes):
    item = dict(run_id=run_id, request_id=None, created_at=1000.0, finished_at=1041.0, mode='database-online',
                status='ready', download='ready', remote='none', local=True, size=4096, verified_at=1041.0,
                verification_source='capture')
    item.update(changes)
    return item


@unittest.skipUnless(PHP, 'PHP CLI required')
class HistoryStates(unittest.TestCase):
    def render(self, runs, requests=(), details=None, uploads=True, settings=None):
        overview = dict(runs=runs, requests=list(requests), uploads=uploads, settings=settings or {})
        result = subprocess.run([PHP, str(ROOT / 'tests/fixtures/history-table.php'), str(ROOT)],
                                input=json.dumps(dict(overview=overview, details=details)),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        rows = {row['__key']: row for row in value['page']['items']}
        return rows, dict(zip(rows, value['classes'])), value.get('details')

    def test_running_backup_gets_its_note_and_row_edge(self):
        rows, classes, _ = self.render([run('busy', status='running', finished_at=None, local=False, size=None)])
        self.assertEqual(rows['busy']['status_label'], 'Running')
        self.assertEqual(rows['busy']['detail'], 'Copying the database. The site stays online.')
        self.assertEqual(classes['busy'], 'backups-row-running')

    def test_upload_failed_says_where_it_is_and_what_to_do(self):
        rows, classes, details = self.render([run('kept', remote='failed', delivery_error='s3-delivery-failed')], details='kept')
        self.assertEqual((rows['kept']['status_label'], rows['kept']['status_color']), ('Upload failed', 'warning'))
        self.assertEqual(rows['kept']['detail'], 'Kept on this server. Check the S3 keys in '
                         '<a class="underline" href="/admin/backups?tab=settings#s3">Settings</a>, then retry.')
        self.assertIsNone(classes['kept'])
        self.assertEqual(details['sections']['Location']['S3'][0], 'Upload failed')

    def test_failed_backup_details_lead_with_reason_and_next_step(self):
        failed = run('broken', status='failed', download='none', local=False, size=None, verified_at=None,
                     error='database-inventory-failed', finished_at=1012.0)
        _, _, details = self.render([failed], details='broken')
        wrong = details['sections']['What went wrong']
        self.assertEqual(wrong['Reason'][0], "The database couldn't be inspected.")
        self.assertEqual(wrong['Next step'][0], "Check the database user's permissions.")
        self.assertEqual(wrong['Error code'], ['database-inventory-failed', ['For support and the command line.']])
        self.assertEqual(details['sections']['Backup']['Stopped'][0], 'at 1012')
        self.assertIn('Stopped after 12 seconds.', details['line'])
        self.assertNotIn('Restore', details['sections'])

    def test_finished_backup_shows_how_long_it_took_and_how_to_restore(self):
        _, _, details = self.render([run('good')], details='good')
        self.assertEqual(details['sections']['Backup']['Finished'], ['at 1041', ['Took 41 seconds']])
        self.assertNotIn('What went wrong', details['sections'])
        restore = ' '.join(text for text, _ in details['sections']['Restore'].values())
        self.assertIn('paymenter_backups export good --output /new/folder', restore)

    def test_backup_from_an_earlier_version_says_not_recorded(self):
        _, _, details = self.render([run('old', finished_at=None)], details='old')
        self.assertEqual(details['sections']['Backup']['Finished'], ['Not recorded', []])

    def test_keep_says_what_cleanup_does_in_each_place(self):
        s3_only = run('away', local=False, remote='present', remote_checked_at=None)
        _, _, details = self.render([s3_only], details='away', settings=dict(s3_retention_policy_database='keep-all'))
        self.assertEqual(list(details['sections']['Keep']), ['S3'])
        self.assertEqual(details['sections']['Keep']['S3'][0], 'Kept. Cleanup never deletes it')
        both = run('both', remote='present', remote_checked_at=None)
        _, _, details = self.render([both], details='both', settings=dict(s3_retention_policy_database='age', s3_retention_days_database=90))
        keep = details['sections']['Keep']
        self.assertEqual(keep['This server'][0], 'Deleted after 24 newer database backups')
        self.assertEqual(keep['S3'][0], 'Deleted 90 days after it was made')
        _, _, details = self.render([both], details='both', settings=dict(automatic_retention=False))
        self.assertEqual(details['sections']['Keep']['Cleanup'][0], 'Kept until you run cleanup')

    def test_waiting_request_names_the_reason(self):
        waiting = dict(request_id='r1', action='run', status='deferred', error='job-lock-busy', created_at=2000.0,
                       mode='database-online', scheduled=True, result_run_id=None)
        rows, _, _ = self.render([], requests=[waiting])
        row = rows['request-r1']
        self.assertEqual((row['status_label'], row['origin']), ('Waiting', 'Schedule'))
        self.assertEqual(row['detail'], 'A Paymenter cron job was still running. Tries again next minute.')


if __name__ == '__main__':
    unittest.main()
