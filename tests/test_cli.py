import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_engine
from paymenter_backups.common import BackupError, read_json
from paymenter_backups.engine import capture, export
from paymenter_backups.cli import dispatch, parser
from paymenter_backups.process import group_members


def process_cmdline(pid):
    try:
        return pathlib.Path(f"/proc/{pid}/cmdline").read_text(errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return ""


class CLITests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_engine.Fixture("test_online_excludes_app_and_maintenance")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = pathlib.Path(__file__).resolve().parents[1]
        self.command = [sys.executable, "-I", str(self.root / "bin/paymenter-backups"),
            "--app", str(self.fixture.app), "--private-root", str(self.fixture.state.root),
            "--php", "/does/not/exist", "--bridge", "/does/not/exist"]

    def cli(self, *arguments, input=None, command=None):
        result = subprocess.run((command or self.command) + list(arguments),
                    input=json.dumps(input).encode() if input is not None else None,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def test_independent_status_list_verify_and_recover_without_php_database(self):
        self.fixture.settings["mode"] = "database-online"
        journal = capture(self.fixture.state, self.fixture.envelope)
        self.assertTrue(self.cli("verify", journal["run_id"])["verified"])
        self.assertEqual(self.cli("list")[0]["run_id"], journal["run_id"])
        self.assertFalse(self.cli("status")["unresolved"])
        self.assertEqual(self.cli("recover", "--execute"), [])

    def start_slow_capture(self):
        fixture = self.fixture
        fixture.envelope["tools"]["dump"] = str(fixture.script("slowdump", "import sys,time\nprint('-- dump',flush=True)\ntime.sleep(60)"))
        fixture.settings["capture_timeout"] = 60
        envelope_path = fixture.base / "envelope.json"
        envelope_path.write_text(json.dumps(fixture.envelope))
        script = "import sys,json;sys.path.insert(0,sys.argv[1]);from paymenter_backups.common import State;from paymenter_backups.engine import capture;capture(State(sys.argv[2],sys.argv[3]),json.load(open(sys.argv[4])))"
        capturer = subprocess.Popen([sys.executable, "-I", "-c", script, str(self.root / "src"), str(fixture.app), str(fixture.state.root), str(envelope_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        def stop():
            if capturer.poll() is None:
                capturer.kill()
            capturer.wait()
        self.addCleanup(stop)
        deadline = time.time() + 5
        journal = None
        while time.time() < deadline:
            journals = fixture.state.journals()
            if journals and journals[0]["phase"] == "capturing" and any("slowdump" in process_cmdline(group["pid"]) for group in journals[0]["groups"]):
                journal = journals[0]
                break
            time.sleep(.02)
        self.assertIsNotNone(journal)
        return capturer, journal

    def test_capturer_sigterm_keeps_service_online_and_records_interruption(self):
        capturer, journal = self.start_slow_capture()
        capturer.send_signal(signal.SIGTERM)
        capturer.wait(timeout=6)
        final = self.fixture.state.journals()[0]
        self.assertTrue(final["service_recovered"])
        self.assertEqual(final["error"], "capture-interrupted")
        self.assertFalse((self.fixture.app / "storage/framework/down").exists())
        self.assertFalse(any(group_members(group) for group in journal["groups"]))

    def test_actual_capturer_sigkill_needs_no_service_recovery(self):
        fixture = self.fixture
        capturer, journal = self.start_slow_capture()
        capturer.kill()
        capturer.wait()
        deadline = time.time() + 3
        while any(group_members(group) for group in journal["groups"]) and time.time() < deadline:
            time.sleep(.02)
        self.assertFalse((fixture.app / "storage/framework/down").exists())
        self.assertEqual(self.cli("recover"), [])
        self.assertEqual(self.cli("recover", "--auto"), [])
        self.assertFalse((fixture.app / "storage/framework/down").exists())
        self.assertFalse(self.cli("status")["unresolved"])

    def test_config_and_schedule_writes_stdin_and_safe_show(self):
        fixture = self.fixture
        from paymenter_backups.common import atomic_json
        atomic_json(fixture.state.root / 'scheduler.json', {'last_tick_at': time.time()})
        atomic_json(fixture.state.root / 'incidents.json', {'schema_version': 1,
            'installation_uuid': fixture.state.uuid, 'checked_at': time.time(), 'active': {}})
        native_settings = fixture.base / "settings.json"
        native_settings.write_text("{}")
        fixture.envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(fixture.state.root))
        envelope_path = fixture.base / "envelope.json"
        envelope_path.write_text(json.dumps(fixture.envelope))
        bridge = fixture.base / "bridge.py"
        bridge.write_text("import json,pathlib,sys\nenv=json.load(open(" + repr(str(envelope_path)) + "))\nenv['settings'].update(json.load(open(" + repr(str(native_settings)) + ")))\nprint(json.dumps(env))\n")
        (fixture.app / "artisan").write_text("import json,pathlib,sys\np=pathlib.Path(" + repr(str(native_settings)) + ")\nd=json.loads(p.read_text());d.update(json.load(sys.stdin));p.write_text(json.dumps(d))\nprint(json.dumps({'ok':True,'receipt':{},'snapshot':d}))\n")
        command = self.command[:-4] + ["--php", sys.executable, "--bridge", str(bridge)]
        self.assertTrue(self.cli("config", "set", input={"settings": {"capture_timeout": 20}}, command=command)["updated"])
        self.assertEqual(json.loads(native_settings.read_text())["capture_timeout"], 20)
        # The fixture guard includes a billing instant one hour from now. Keep
        # this CLI roundtrip outside it at every UTC time the suite is run.
        schedule_time = time.strftime("%H:%M", time.gmtime(time.time() + 12 * 3600))
        self.cli("schedule", "set", input={"schedule_time": schedule_time, "schedule_weekdays": [0, 2]}, command=command)
        self.cli("schedule", "enable", command=command)
        self.assertTrue(self.cli("schedule", "show", command=command)["enabled"])
        # A failing reserve check must never trap staff with schedules On.
        saved = self.cli('config', 'set', input={'settings': {'minimum_free_bytes': 2**62}}, command=command)
        self.assertTrue(saved['updated'])
        self.assertEqual(saved['local_storage_error'], 'disk-reserve-exceeded')
        self.cli("schedule", "disable", command=command)
        self.assertFalse(self.cli("schedule", "show", command=command)["enabled"])
        shown = json.dumps(self.cli("config", "show", command=command))
        self.assertNotIn("fake-canary-secret", shown)

    def test_export_canonical_parent_symlink_refuses_before_writing(self):
        fixture = self.fixture
        fixture.settings["mode"] = "database-online"
        journal = capture(fixture.state, fixture.envelope)
        (fixture.app / "public").mkdir()
        link = fixture.base / "public-alias"
        link.symlink_to(fixture.app / "public", target_is_directory=True)
        with self.assertRaisesRegex(BackupError, "export-destination-unsafe"):
            export(fixture.state, journal["run_id"], link / "kit")
        self.assertFalse((fixture.app / "public/kit").exists())

    def test_tar_options_cannot_exclude_app_key(self):
        with patch.dict(os.environ, {"TAR_OPTIONS": "--exclude=.env", "GZIP": "--bad-flag", "MYSQL_PWD": "bad-env-secret"}):
            journal = capture(self.fixture.state, self.fixture.envelope)
        import tarfile, io
        with tarfile.open(journal["archive_path"], "r:gz") as archive:
            inner = archive.extractfile("./app.tar").read()
        with tarfile.open(fileobj=io.BytesIO(inner)) as archive:
            self.assertIn(b"APP_KEY=", archive.extractfile("./.env").read())

    def test_readonly_fresh_plan_configshow_validate_and_status_do_not_initialize(self):
        fixture = self.fixture
        fresh = fixture.base / "fresh-private"
        envelope = dict(fixture.envelope, schema_version=1, owner_uid=os.getuid(), private_root=str(fresh))
        bridge = fixture.base / "fresh-bridge.py"
        bridge.write_text("import json\nprint(" + repr(json.dumps(envelope)) + ")\n")
        command = [sys.executable, "-I", str(self.root / "bin/paymenter-backups"), "--app", str(fixture.app), "--private-root", str(fresh), "--php", sys.executable, "--bridge", str(bridge)]
        self.assertFalse(self.cli("status", command=command)["initialized"])
        self.assertIn("installation-not-initialized", self.cli("run", command=command)["admission_errors"])
        self.cli("config", "show", command=command)
        self.cli("config", "validate", command=command)
        self.assertFalse(fresh.exists())
        self.assertTrue(self.cli("init", command=command)["initialized"])
        self.assertTrue((fresh / "installation.json").exists())

    def test_fresh_docroot_policy_refuses_private_root_probe_and_export(self):
        fixture = self.fixture
        public = fixture.base / "public_html"
        public.mkdir()
        from paymenter_backups.common import State
        with self.assertRaisesRegex(BackupError, "private-root-inside-docroot"):
            State(fixture.app, public / "private", extra_docroots=[str(public)])
        self.assertFalse((public / "private").exists())
        fixture.settings["extra_docroots"] = [str(public)]
        fixture.envelope.update(schema_version=1, owner_uid=os.getuid(), private_root=str(fixture.state.root))
        fixture.settings["local_path"] = str(public / "artifacts")
        args = parser().parse_args(["--app", str(fixture.app), "--private-root", str(fixture.state.root), "destination-test"])
        with patch("paymenter_backups.cli.load", return_value=fixture.envelope):
            with self.assertRaisesRegex(BackupError, "destination-inside-docroot"):
                dispatch(args)
        self.assertFalse((public / "artifacts").exists())
        fixture.settings["local_path"] = ""
        fixture.settings["mode"] = "database-online"
        journal = capture(fixture.state, fixture.envelope)
        with self.assertRaisesRegex(BackupError, "export-destination-unsafe"):
            export(State(fixture.app, fixture.state.root, initialize=False), journal["run_id"], public / "kit")

    def test_schedule_overlap_is_rejected_before_native_write(self):
        from paymenter_backups.maintenance import schedule_admission
        from paymenter_backups.common import atomic_json
        from paymenter_backups.config import billing_fingerprint
        import datetime
        fixture = self.fixture
        fixture.settings.update(enabled=True, schedule_time="04:00")
        today = datetime.datetime.now(datetime.timezone.utc)
        billing = today.replace(hour=4, minute=0, second=0, microsecond=0).timestamp()
        while billing < time.time():
            billing += 86400
        atomic_json(fixture.state.root / "billing-guard.json", {"generated_at": time.time(), "fingerprint": billing_fingerprint(fixture.envelope["billing"]), "instants": [billing + day * 86400 for day in range(8)]})
        with self.assertRaisesRegex(BackupError, "schedule-overlaps-billing"):
            schedule_admission(fixture.state, fixture.envelope)

    def test_automatic_retention_after_successful_publication(self):
        fixture = self.fixture
        fixture.settings.update(mode="database-online", retention_keep=1)
        args = parser().parse_args(["--app", str(fixture.app), "--private-root", str(fixture.state.root), "run", "--execute"])
        with patch("paymenter_backups.cli.bridge", return_value=fixture.envelope), patch("paymenter_backups.cli.load", return_value=fixture.envelope):
            first = dispatch(args)
            second = dispatch(args)
        self.assertTrue(pathlib.Path(second["archive_path"]).exists())
        self.assertFalse(pathlib.Path(first["archive_path"]).exists())
        self.assertEqual(second["retention"]["delete"], [first["run_id"]])



class ProcessCmdlineTests(unittest.TestCase):
    def test_a_process_that_exits_mid_read_reads_as_empty(self):
        for error in (FileNotFoundError, ProcessLookupError):
            with self.subTest(error=error.__name__), patch.object(pathlib.Path, "read_text", side_effect=error):
                self.assertEqual(process_cmdline(os.getpid()), "")

    def test_a_live_process_returns_its_command_line(self):
        self.assertEqual(process_cmdline(os.getpid()), "\0".join(sys.orig_argv) + "\0")

if __name__ == "__main__":
    unittest.main()
