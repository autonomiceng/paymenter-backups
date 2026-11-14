"""Check the instants represented by the staff schedule summary across DST."""
import calendar
from datetime import datetime, time, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PHP = os.environ.get('PAYMENTER_BACKUPS_TEST_PHP') or shutil.which('php')
PROGRAM = r'''
require $argv[1] . '/extension/Backups/Messages.php';
require $argv[1] . '/extension/Backups/Panel/Presentation/StaffTime.php';
require $argv[1] . '/extension/Backups/Admin/Pages/Concerns/BuildsPageContent.php';
function config($key) { return 'America/Los_Angeles'; }
$page = new class {
    use Paymenter\Extensions\Others\Backups\Admin\Pages\Concerns\BuildsPageContent;
    private const KINDS = ['database-online' => 'database'];
    private function formValues(): array {
        return ['schedule_timezone' => 'UTC', 'database_schedule' => 'weekly',
            'database_time' => '07:30', 'database_weekdays' => [6, 0]];
    }
    public function summary(int $at): string { return $this->scheduleRecurrence('database-online', $at); }
};
echo json_encode($page->summary((int) $argv[2]), JSON_THROW_ON_ERROR);
'''


@unittest.skipUnless(PHP, 'PHP required for the schedule summary')
class ScheduleRecurrenceTests(unittest.TestCase):
    def test_weekly_summary_represents_each_staff_run_across_spring_dst(self):
        staff_zone = ZoneInfo('America/Los_Angeles')
        # The first pair straddles the clock change; the second has a stable offset.
        for sunday in (8, 15):
            with self.subTest(sunday=sunday):
                first = datetime(2026, 3, sunday, 7, 30, tzinfo=ZoneInfo('UTC'))
                expected = [first, first + timedelta(days=1)]
                result = subprocess.run([PHP, '-r', PROGRAM, str(ROOT), str(int(first.timestamp()))],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = json.loads(result.stdout)
                # Read weekday/time data, without pinning the surrounding prose or time format.
                days = {index for index, name in enumerate(calendar.day_name) if name in summary}
                clock = re.search(r'(\d{1,2}):(\d{2})(?:\s*([AP]M))?', summary)
                hour, minute = map(int, clock.group(1, 2))
                if clock.group(3):
                    hour = hour % 12 + (12 if clock.group(3) == 'PM' else 0)
                zone_name = summary.rsplit(' ', 1)[-1]
                # An abbreviation denotes the staff wall clock, including its DST rules.
                zone = staff_zone if zone_name in {'PST', 'PDT'} else ZoneInfo(zone_name)
                start = first.astimezone(zone).date()
                shown = []
                for offset in range(8):
                    day = start + timedelta(days=offset)
                    if day.weekday() in days:
                        run = datetime.combine(day, time(hour, minute), tzinfo=zone)
                        if run >= first:
                            shown.append(run.astimezone(staff_zone))
                self.assertEqual([int(run.timestamp()) for run in shown[:2]],
                                 [int(run.timestamp()) for run in expected], summary)


if __name__ == '__main__':
    unittest.main()
