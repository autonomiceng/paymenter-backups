"""Existing host launchers are inspected; this extension never manages them."""
import json
import os
import pathlib
import shlex
import tempfile
import unittest
from unittest.mock import patch

from paymenter_backups.common import BackupError
from paymenter_backups.maintenance import systemd_locks, web_origin


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = pathlib.Path(temporary.name) / "paymenter"
        self.app.mkdir()
        self.lock = str(pathlib.Path(temporary.name) / "jobs.lock")
        self.invocations = []
        self.command = f"/usr/bin/flock {self.lock} /usr/bin/php {self.app}/artisan schedule:run"
        self.owner = str(os.getuid())
        self.unit = "paymenter-scheduler.service"

    def response(self, command, _timeout):
        self.invocations.append(command)
        self.assertFalse(any(word in command for word in ("enable", "disable", "start", "stop", "restart", "edit")))
        if "list-timers" in command:
            return json.dumps([{"unit": "paymenter-scheduler.timer", "activates": self.unit}]).encode()
        if "list-units" in command or "list-unit-files" in command:
            return b'' if '--plain' in command else b'[]'
        return (f"Id={self.unit}\nUser={self.owner}\nWorkingDirectory={self.app}\nDynamicUser=no\nLoadState=loaded\n"
                f"ExecStart={{ path=/usr/bin/flock ; argv[]={self.command} ; ignore_errors=no ; }}\n").encode()

    def test_existing_locked_systemd_timer_is_read_only_and_verified(self):
        with patch("paymenter_backups.maintenance.run", side_effect=self.response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), {self.lock})
        self.assertTrue(self.invocations)

    def test_systemd_paths_keep_literal_percent_and_backslashes(self):
        self.app = self.app.with_name(r"paymenter\%site")
        self.lock = str(self.app.parent / r'jobs\%archive.lock')
        self.command = shlex.join(['/usr/bin/flock', self.lock, '/hosting%php/php', str(self.app / 'artisan'), 'schedule:run'])
        with patch('paymenter_backups.maintenance.run', side_effect=self.response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {'systemctl': '/usr/bin/true'}), {self.lock})

    def test_unlocked_or_shell_wrapped_own_timer_refuses(self):
        for command in (f"/usr/bin/php {self.app}/artisan schedule:run", f"/bin/sh -c /usr/bin/flock {self.lock} /usr/bin/php {self.app}/artisan schedule:run"):
            with self.subTest(command=command):
                self.command = command
                with patch("paymenter_backups.maintenance.run", side_effect=self.response), self.assertRaisesRegex(BackupError, "job-locks-unverified"):
                    systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"})

    def test_other_unix_users_timer_is_not_claimed(self):
        self.owner = str(os.getuid() + 1)
        with patch("paymenter_backups.maintenance.run", side_effect=self.response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), set())

    def test_unavailable_systemd_is_not_required_on_cron_hosts(self):
        with patch("paymenter_backups.maintenance.run", side_effect=BackupError("child-failed")):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), set())

    def test_timer_service_names_cannot_be_used_as_command_options(self):
        self.unit = "--start-malicious.service"
        with patch("paymenter_backups.maintenance.run", side_effect=self.response), self.assertRaisesRegex(BackupError, "job-locks-unverified"):
            systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"})

    def test_invalid_timer_target_detail_is_bounded_and_uses_repr(self):
        for value in ("", [], ["scheduler.service"], False, 1, {}, "--option.service", "x" * 300, "bad\nname.service"):
            with self.subTest(value=value):
                with patch("paymenter_backups.maintenance.run", return_value=json.dumps([
                        {"unit": "orphan.timer", "activates": value}]).encode()), \
                        self.assertRaises(BackupError) as caught:
                    systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"})
                self.assertEqual(caught.exception.code, "job-locks-unverified")
                self.assertEqual(caught.exception.detail["activates"], repr(value)[:80])

    def test_missing_timer_target_is_diagnosed_and_refused(self):
        with patch("paymenter_backups.maintenance.run", return_value=b'[{"unit":"orphan.timer"}]'), \
                self.assertRaises(BackupError) as caught:
            systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"})
        self.assertEqual(caught.exception.code, "job-locks-unverified")
        self.assertEqual(caught.exception.detail["activates"], "None")

    def test_masked_timer_without_trigger_is_accepted_on_systemd_255(self):
        # A masked timer with no Triggers has a null ACTIVATES cell.
        timers = b'[{"next":null,"left":null,"last":0,"passed":0,"unit":"fwupd-refresh.timer","activates":null}]'
        def response(command, timeout):
            return timers if "list-timers" in command else b'[]'
        with patch("paymenter_backups.maintenance.run", side_effect=response) as read:
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), set())
        self.assertTrue(read.called)
        self.assertFalse(any("show" in call.args[0] for call in read.call_args_list))

    def test_null_timer_target_does_not_skip_real_timer_lock_verification(self):
        def response(command, timeout):
            if "list-timers" in command:
                self.invocations.append(command)
                return json.dumps([
                    {"unit": "masked.timer", "activates": None},
                    {"unit": "paymenter-scheduler.timer", "activates": self.unit},
                ]).encode()
            return self.response(command, timeout)
        with patch("paymenter_backups.maintenance.run", side_effect=response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), {self.lock})
        shows = [command for command in self.invocations if "show" in command]
        self.assertTrue(shows)
        for command in shows:
            self.assertEqual(command[command.index("--") + 1:], [self.unit])

    def test_older_systemd_plain_timer_table_is_supported(self):
        def response(command, timeout):
            if "list-timers" in command:
                self.invocations.append(command)
                return ("Wed 2026-10-14 02:00:00 UTC 1h left Wed 2026-10-14 01:00:00 UTC 1min ago paymenter-scheduler.timer " + self.unit + "\n").encode()
            return self.response(command, timeout)
        with patch("paymenter_backups.maintenance.run", side_effect=response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), {self.lock})
        fallback = [command for command in self.invocations if "list-timers" in command and "--plain" in command]
        self.assertTrue(fallback)
        self.assertTrue(all("--no-legend" in command for command in fallback))

    def test_unrelated_dynamic_user_does_not_require_user_lookup(self):
        self.owner = "unresolvable-unrelated-dynamic-user"
        self.command = "/usr/bin/true"
        with patch("paymenter_backups.maintenance.run", side_effect=self.response), patch("paymenter_backups.maintenance.pwd.getpwnam", side_effect=KeyError) as lookup:
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), set())
            lookup.assert_not_called()

    def test_timer_properties_are_requested_in_one_bounded_batch(self):
        units = [f"paymenter-{index}.service" for index in range(64)]
        def response(command, _timeout):
            self.invocations.append(command)
            if "list-timers" in command:
                return json.dumps([{"activates": unit} for unit in units]).encode()
            if "list-units" in command or "list-unit-files" in command:
                return b'[]'
            self.assertIn("--", command)
            self.assertEqual(command[command.index("--") + 1:], sorted(units))
            return "\n\n".join(f"Id={unit}\nUser={self.owner}\nWorkingDirectory={self.app}\nDynamicUser=no\nLoadState=loaded\nExecStart={{ path=/usr/bin/flock ; argv[]={self.command} ; ignore_errors=no ; }}" for unit in units).encode()
        with patch("paymenter_backups.maintenance.run", side_effect=response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), {self.lock})
        system_shows = [command for command in self.invocations if "--system" in command and "show" in command]
        self.assertEqual(len(system_shows), 1)

    def test_valid_escaped_unit_name_is_passed_after_option_separator(self):
        self.unit = r"paymenter\x2dscheduler.service"
        with patch("paymenter_backups.maintenance.run", side_effect=self.response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), {self.lock})
        for command in self.invocations:
            if "show" in command:
                self.assertEqual(command[command.index("--") + 1:], [self.unit])

    def test_unlocked_shell_cd_scheduler_is_not_skipped_as_unrelated(self):
        self.command = f"/bin/sh -c 'cd {self.app} && /usr/bin/php artisan schedule:run'"
        def response(command, timeout):
            return self.response(command, timeout).replace(("WorkingDirectory=" + str(self.app) + "\n").encode(), b"WorkingDirectory=/\n")
        with patch("paymenter_backups.maintenance.run", side_effect=response), self.assertRaisesRegex(BackupError, "job-locks-unverified"):
            systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"})

    def test_plain_timer_fallback_requests_full_untruncated_names(self):
        def response(command, timeout):
            if "list-timers" in command:
                self.invocations.append(command)
                return ("- - - - paymenter-scheduler.timer " + self.unit + "\n").encode()
            return self.response(command, timeout)
        with patch("paymenter_backups.maintenance.run", side_effect=response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), {self.lock})
        fallback = [command for command in self.invocations if "--plain" in command]
        self.assertTrue(fallback)
        self.assertTrue(all("--full" in command for command in fallback))

    def test_show_block_without_matching_unit_identity_refuses(self):
        def response(command, timeout):
            result = self.response(command, timeout)
            return result if "list-timers" in command else result.replace(("Id=" + self.unit + "\n").encode(), b"")
        with patch("paymenter_backups.maintenance.run", side_effect=response), self.assertRaisesRegex(BackupError, "job-locks-unverified"):
            systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"})

    def test_unsupported_json_option_retries_plain_table(self):
        def response(command, timeout):
            if "--output=json" in command:
                self.invocations.append(command)
                raise BackupError("child-failed")
            if "list-timers" in command:
                self.invocations.append(command)
                return ("- - - - paymenter-scheduler.timer " + self.unit + "\n").encode()
            return self.response(command, timeout)
        with patch("paymenter_backups.maintenance.run", side_effect=response):
            self.assertEqual(systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"}), {self.lock})

    def test_malformed_plain_timer_metadata_refuses(self):
        with patch("paymenter_backups.maintenance.run", return_value=b"unparseable timer metadata\n"), self.assertRaisesRegex(BackupError, "job-locks-unverified"):
            systemd_locks(self.app, [self.lock], {"systemctl": "/usr/bin/true"})

    def test_web_origin_preserves_ipv6_brackets_and_http_default_port(self):
        self.assertEqual(web_origin("http://[0:0:0:0:0:0:0:1]:80/base", allow_http=True), "http://[::1]")
        self.assertEqual(web_origin("http://example.com:443/base", allow_http=True), "http://example.com:443")
        self.assertEqual(web_origin("https://EXAMPLE.com:443/base"), "https://example.com")
        for url in ("https://example.com.", "https://éxample.com", "https://user@example.com", "https://example.com?query"):
            with self.subTest(url=url), self.assertRaisesRegex(BackupError, "maintenance-probe-origin-invalid"):
                web_origin(url, allow_http=True)
