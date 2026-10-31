import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from paymenter_backups.common import BackupError, State, atomic_json, digest, identity
from paymenter_backups.config import DEFAULTS, billing_fingerprint, normalize, redact, validate
from paymenter_backups.engine import capture, check_archive, external_symlinks, inventory, verify
from paymenter_backups.maintenance import MARKERS, billing_admission, cleanup, cron_lock, recover, systemd_locks
from paymenter_backups.process import group_members, process_identity, run
from paymenter_backups.requests import cancel, create, due_slots


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = pathlib.Path(self.temporary.name)
        self.app = self.base / "app"
        (self.app / "storage/framework").mkdir(parents=True)
        (self.app / ".env").write_text("APP_KEY=fixture-canary-key\nDB_PASSWORD=fixture-canary-password\n")
        (self.app / "artisan").write_text("fixture")
        self.state = State(self.app, self.base / "private")
        self.settings = dict(DEFAULTS, minimum_free_bytes=0, drain_seconds=0,
                             capture_timeout=10, no_concurrent_ddl_acknowledged=True,
                             cooperative_writers_acknowledged=True, external_state_acknowledged=True)
        self.envelope = {"app": str(self.app), "settings": self.settings,
                         "database": {"driver": "mysql", "name": "fixture", "user": "fixture", "password": "fake-canary-secret"},
                         "billing": {"daily_time": "00:00", "timezone": "UTC"},
                         "framework": {"maintenance_driver": "file", "queue_driver": "database", "session_driver": "file", "cache_driver": "file", "filesystem_driver": "local"},
                         "tools": {"tar": "/usr/bin/tar", "gzip": "/usr/bin/gzip", "php": str(self.script("php", """import pathlib,json,sys
app=pathlib.Path(sys.argv[1]).parent
payload={'status':503,'except':[],'secret':None,'redirect':None,'template':'fixture maintenance'}
(app/'storage/framework/down').write_text(json.dumps(payload))
(app/'storage/framework/maintenance.php').write_text('fixture early bootstrap')
""")), "dump": str(self.script("dump", "import sys\nsys.stdout.write('-- stable dump\\nCREATE TABLE fixture(id INT);\\n')")),
                                   "mysql": str(self.script("mysql", "import sys\nsys.stdin.read()\nprint('tables\\t0\\nroutines\\t0\\nevents\\t0\\ntriggers\\t0\\nGRANT SELECT ON fixture.* TO fixture')"))}}
        lock = self.base / "job.lock"
        lock.touch()
        self.settings["job_lock_paths"] = [str(lock)]
        cron = f"* * * * * /usr/bin/flock {lock} /usr/bin/php {self.app}/artisan schedule:run"
        self.envelope["tools"]["crontab"] = str(self.script("crontab", "print(" + repr(cron) + ")"))
        self.envelope["tools"]["systemctl"] = str(self.script("systemctl", "print('[]')"))
        now = time.time()
        atomic_json(self.state.root / "billing-guard.json", {"generated_at": now,
            "fingerprint": billing_fingerprint(self.envelope["billing"]),
            "instants": [now - 86400, now + 3600, now + 172800], "timezone": "UTC", "expression": "0 0 * * *"})
        app = self.app
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    payload = json.loads((app / MARKERS[0]).read_text())
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(payload["template"].encode())
                except FileNotFoundError:
                    self.send_response(200)
                    self.end_headers()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.envelope["framework"]["app_url"] = "http://127.0.0.1:" + str(self.server.server_port)

    def script(self, name, body):
        path = self.base / name
        path.write_text("#!" + sys.executable + "\n" + body + "\n")
        path.chmod(0o700)
        return path

    def journal(self):
        directory = self.state.run_path("owned")
        directory.mkdir(exist_ok=True)
        journal = {"run_id": "owned", "phase": "capturing", "groups": [], "owned_markers": {}, "initial_markers": {}, "service_recovered": False}
        for relative in MARKERS:
            (self.app / relative).write_text("marker")
            journal["owned_markers"][relative] = identity(self.app / relative)
        self.state.save(journal)
        return journal

    def test_full_capture_real_tar_roundtrip_preserves_secrets_and_symlink(self):
        (self.app / "native-link").symlink_to(".env")
        journal = capture(self.state, self.envelope)
        result = verify(self.state, journal["run_id"], True)
        self.assertTrue(journal["service_recovered"])
        self.assertFalse((self.app / MARKERS[0]).exists())
        self.assertEqual(result["excluded_paths"], list(MARKERS))
        with tarfile.open(journal["archive_path"], "r:gz") as outer:
            inner = outer.extractfile("./app.tar").read()
        with tarfile.open(fileobj=io.BytesIO(inner)) as app_tar:
            self.assertEqual(app_tar.extractfile("./.env").read(), (self.app / ".env").read_bytes())
            self.assertTrue(app_tar.getmember("./native-link").issym())
            self.assertNotIn("./storage/framework/down", app_tar.getnames())
        raw_journal = json.dumps(journal)
        self.assertNotIn("fake-canary-secret", raw_journal)
        self.assertNotIn("fixture-canary-key", raw_journal)

    def test_full_capture_uses_fixture_launchers_with_invalid_host_timer(self):
        host_systemctl = self.script("host-systemctl", "print(" + repr(json.dumps([
            {"next": None, "left": None, "last": 0, "passed": 0,
             "unit": "invalid.timer", "activates": ""}])) + ")")
        host_crontab = self.script("host-crontab", "raise SystemExit(2)")
        host_tools = {"/usr/bin/systemctl": host_systemctl, "/usr/bin/crontab": host_crontab}
        host_calls = []
        is_file = pathlib.Path.is_file

        def exists(path):
            return str(path) in host_tools or is_file(path)

        def read(command, *args, **kwargs):
            if command[0] in host_tools:
                host_calls.append(command)
                command = [str(host_tools[command[0]]), *command[1:]]
            return run(command, *args, **kwargs)

        with patch("pathlib.Path.is_file", autospec=True, side_effect=exists), \
                patch("paymenter_backups.maintenance.run", side_effect=read):
            with self.assertRaisesRegex(BackupError, "job-locks-unverified"):
                systemd_locks(self.app, self.settings["job_lock_paths"], {})
            self.assertTrue(host_calls)
            host_calls.clear()
            journal = capture(self.state, self.envelope)
            self.assertTrue(journal["service_recovered"])
            self.assertEqual(host_calls, [])

    def test_online_excludes_app_and_maintenance(self):
        self.settings["mode"] = "database-online"
        journal = capture(self.state, self.envelope)
        manifest = verify(self.state, journal["run_id"], True)
        self.assertEqual(set(manifest["payloads"]), {"database.sql"})
        self.assertFalse((self.app / MARKERS[0]).exists())

    def test_online_capture_io_failure_is_not_reported_as_missing_job_locks(self):
        self.settings["mode"] = "database-online"
        save = self.state.save

        def disk_full(journal):
            if journal["phase"] == "capturing":
                raise OSError(errno.ENOSPC, "No space left on device")
            return save(journal)

        with patch.object(self.state, "save", side_effect=disk_full):
            with self.assertRaisesRegex(BackupError, "^capture-failed$"):
                capture(self.state, self.envelope)
        self.assertEqual(self.state.journals()[0]["error"], "capture-failed")

    def test_online_waits_for_job_lock_and_defers_without_failed_run(self):
        from paymenter_backups.requests import path as request_path, worker
        self.settings.update(mode="database-online", lock_timeout=0)
        scheduler = pathlib.Path(self.settings["job_lock_paths"][0])
        scheduler.chmod(0o600)
        queue = self.base / "queue.lock"
        queue.touch(mode=0o600)
        self.settings["job_lock_paths"].append(str(queue))
        cron = (f"* * * * * /usr/bin/flock {scheduler} /usr/bin/php {self.app}/artisan schedule:run\n"
                f"* * * * * /usr/bin/flock {queue} /usr/bin/php {self.app}/artisan queue:work --stop-when-empty")
        self.envelope["tools"]["crontab"] = str(self.script("crontab", "print(" + repr(cron) + ")"))
        for version in (1, 2):
            for lock in (scheduler, queue):
                with self.subTest(version=version, job=lock.name):
                    self.settings["settings_version"] = version
                    before = self.state.journals()
                    request = create(self.state, arguments={"mode": "database-online"})
                    atomic_json(request_path(self.state, request["request_id"]), dict(request, status="launching"))
                    with lock.open("r+b") as held, patch("paymenter_backups.maintenance.DATABASE_LOCK_WAIT", 0):
                        fcntl.flock(held, fcntl.LOCK_EX)
                        result = worker(self.state, self.envelope, request["request_id"])
                        self.assertEqual(result["status"], "deferred")
                        self.assertEqual(result["error"], "job-lock-busy")
                        self.assertEqual(self.state.journals(), before)
                    journal = capture(self.state, self.envelope)
                    self.assertTrue(journal["local_complete"])
                    self.assertFalse((self.app / MARKERS[0]).exists())

    def test_online_completes_with_coordinated_scheduler_and_continuous_systemd_queue(self):
        from paymenter_backups.requests import path as request_path, worker
        self.settings.update(mode="database-online", lock_timeout=0)
        scheduler = pathlib.Path(self.settings["job_lock_paths"][0])
        scheduler.chmod(0o600)
        queue = self.base / "continuous-queue.lock"
        queue.touch(mode=0o600)
        php = self.script("continuous-php", "import time; print('ready', flush=True); time.sleep(120)")
        for version in (1, 2):
            for locked in (False, True):
                with self.subTest(version=version, locked=locked):
                    self.settings.update(settings_version=version,
                                         job_lock_paths=[str(scheduler)] + ([str(queue)] if locked else []))
                    command = f"/usr/bin/php {self.app}/artisan queue:work"
                    if locked:
                        command = f"/usr/bin/flock {queue} " + command
                    show = (f"Id=paymenter-worker.service\nUser={os.getuid()}\nWorkingDirectory={self.app}\n"
                            f"DynamicUser=no\nLoadState=loaded\nRestart=always\n"
                            f"ExecStart={{ path=/usr/bin/php ; argv[]={command} ; ignore_errors=no ; }}\n")
                    self.envelope["tools"]["systemctl"] = str(self.script("continuous-systemctl", f"""import json,sys
if '--user' in sys.argv or 'list-timers' in sys.argv: print('[]')
elif 'list-units' in sys.argv: print(json.dumps([{{'unit': 'paymenter-worker.service', 'active': 'active'}}]))
elif 'list-unit-files' in sys.argv: print(json.dumps([{{'unit_file': 'paymenter-worker.service', 'state': 'enabled'}}]))
else: print({show!r})"""))
                    argv = [str(php), str(self.app / "artisan"), "queue:work"]
                    if locked:
                        argv = ["/usr/bin/flock", str(queue), *argv]
                    proc = subprocess.Popen(argv, cwd=self.app, stdout=subprocess.PIPE, start_new_session=True)
                    try:
                        self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
                        self.assertEqual(proc.stdout.readline(), b"ready\n")
                        request = create(self.state, arguments={"mode": "database-online"})
                        atomic_json(request_path(self.state, request["request_id"]), dict(request, status="launching"))
                        with scheduler.open("r+b") as held, patch("paymenter_backups.maintenance.DATABASE_LOCK_WAIT", 0):
                            fcntl.flock(held, fcntl.LOCK_EX)
                            waiting = worker(self.state, self.envelope, request["request_id"])
                        self.assertEqual((waiting["status"], waiting["error"]), ("deferred", "job-lock-busy"))
                        atomic_json(request_path(self.state, request["request_id"]), dict(request, status="launching"))
                        with patch("paymenter_backups.maintenance.DATABASE_LOCK_WAIT", 0):
                            result = worker(self.state, self.envelope, request["request_id"])
                        self.assertEqual(result["status"], "complete", result)
                        self.assertTrue(self.state.journals()[-1]["local_complete"])
                        self.assertIsNone(proc.poll(), "database capture must leave the queue running")
                        self.assertFalse((self.app / MARKERS[0]).exists())
                    finally:
                        if proc.poll() is None:
                            os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=5)
                        proc.stdout.close()

    def test_online_preserves_v1_finite_queue_options_but_v2_refuses_them(self):
        self.settings.update(mode="database-online", settings_version=1)
        lock = pathlib.Path(self.settings["job_lock_paths"][0])
        lock.chmod(0o600)
        cron = (f"* * * * * /usr/bin/flock {lock} /usr/bin/php {self.app}/artisan schedule:run\n"
                f"* * * * * /usr/bin/flock {lock} /usr/bin/php {self.app}/artisan queue:work --stop-when-empty --queue=high")
        self.envelope["tools"]["crontab"] = str(self.script("legacy-options-crontab", "print(" + repr(cron) + ")"))
        self.assertTrue(capture(self.state, self.envelope)["local_complete"])
        self.settings["settings_version"] = 2
        with self.assertRaisesRegex(BackupError, "^job-coordination-unverified$"):
            capture(self.state, self.envelope)
        diagnostic = json.loads((self.state.root / "job-coordination.json").read_text())
        self.assertEqual(diagnostic["error"], "job-command-unsupported")

    def test_online_billing_window_defers_and_proven_unconfigured_hosts_still_capture(self):
        self.settings["mode"] = "database-online"
        lock = pathlib.Path(self.settings["job_lock_paths"][0])
        lock.chmod(0o600)
        crontab = self.envelope["tools"]["crontab"]
        now = time.time()
        guard_path = self.state.root / "billing-guard.json"
        guard = json.loads(guard_path.read_text())
        guard["instants"] = [now + 20, now + 172800]
        for version in (1, 2):
            with self.subTest(version=version):
                self.settings.update(settings_version=version, job_lock_paths=[str(lock)])
                self.envelope["tools"]["crontab"] = crontab
                atomic_json(guard_path, guard)
                before = self.state.journals()
                with self.assertRaisesRegex(BackupError, "billing-window-protected"):
                    capture(self.state, self.envelope)
                self.assertEqual(self.state.journals(), before)
                self.settings["job_lock_paths"] = []
                self.envelope["tools"]["crontab"] = str(self.script("no-jobs", "print('# no jobs')"))
                guard_path.unlink()
                self.assertTrue(capture(self.state, self.envelope)["local_complete"])
                from paymenter_backups.presentation import overview
                self.assertEqual(overview(self.state)["database_job_coordination"]["state"], "absent")

    def test_online_discovery_and_verification_failures_never_bypass_known_locks(self):
        from paymenter_backups.requests import path as request_path, worker
        self.settings.update(mode="database-online", lock_timeout=0)
        lock = pathlib.Path(self.settings["job_lock_paths"][0])
        lock.chmod(0o600)
        original_crontab = self.envelope["tools"]["crontab"]
        for version in (1, 2):
            for failure in ("discovery", "unsafe-lock"):
                with self.subTest(version=version, failure=failure):
                    self.settings.update(settings_version=version, job_lock_paths=[str(lock)])
                    self.envelope["tools"]["crontab"] = (str(self.script("broken-crontab", "raise SystemExit(2)"))
                                                           if failure == "discovery" else original_crontab)
                    lock.chmod(0o600 if failure == "discovery" else 0o400)
                    request = create(self.state, arguments={"mode": "database-online"})
                    atomic_json(request_path(self.state, request["request_id"]), dict(request, status="launching"))
                    with lock.open("rb") as held:
                        fcntl.flock(held, fcntl.LOCK_EX)
                        result = worker(self.state, self.envelope, request["request_id"])
                    self.assertEqual((result["status"], result["error"]), ("deferred", "job-coordination-unverified"))
                    self.assertEqual(self.state.journals(), [])
                    self.assertIn("job_wait_since", json.loads(request_path(self.state, request["request_id"]).read_text()))
        # An empty override cannot make failed discovery proof of absent setup.
        self.settings["job_lock_paths"] = []
        self.envelope["tools"]["crontab"] = str(self.script("broken-crontab", "raise SystemExit(2)"))
        with self.assertRaisesRegex(BackupError, "job-coordination-unverified"):
            capture(self.state, self.envelope)

    def test_online_outlasts_a_normal_fifty_second_queue_run(self):
        # Real queue process and flock: a short mocked wait misses minute-boundary starvation.
        self.settings.update(mode="database-online", settings_version=2)
        scheduler = pathlib.Path(self.settings["job_lock_paths"][0])
        scheduler.chmod(0o600)
        queue = self.base / "queue.lock"
        queue.touch(mode=0o600)
        cron = (f"* * * * * /usr/bin/flock -n {scheduler} /usr/bin/php {self.app}/artisan schedule:run\n"
                f"* * * * * /usr/bin/flock -n {queue} /usr/bin/php {self.app}/artisan queue:work --stop-when-empty --max-time=50")
        self.envelope["tools"]["crontab"] = str(self.script("crontab", "print(" + repr(cron) + ")"))
        self.settings["job_lock_paths"].append(str(queue))
        done = self.base / "queue-finished"
        php = self.script("queue-php", f"import pathlib,time\nprint('ready', flush=True)\ntime.sleep(50)\npathlib.Path({str(done)!r}).touch()")
        proc = subprocess.Popen(["/usr/bin/flock", "-n", str(queue), str(php), str(self.app / "artisan"),
                                 "queue:work", "--stop-when-empty", "--max-time=50"], stdout=subprocess.PIPE,
                                start_new_session=True)
        try:
            self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
            self.assertEqual(proc.stdout.readline(), b"ready\n")
            journal = capture(self.state, self.envelope)
            self.assertTrue(journal["local_complete"])
            self.assertTrue(done.exists())
            self.assertEqual(proc.wait(timeout=5), 0)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            proc.stdout.close()

    def test_online_billing_reserves_the_longer_queue_wait(self):
        self.settings.update(mode="database-online", capture_timeout=300, lock_timeout=15, billing_margin_seconds=0)
        now = time.time()
        guard_path = self.state.root / "billing-guard.json"
        guard = json.loads(guard_path.read_text())
        guard["instants"] = [now + 370, now + 172800]
        atomic_json(guard_path, guard)
        with self.assertRaisesRegex(BackupError, "billing-window-protected"):
            capture(self.state, self.envelope)
        self.assertEqual(self.state.journals(), [])

    def test_waiting_flock_launcher_is_not_a_writer_but_uncoordinated_php_is(self):
        from paymenter_backups.maintenance import coordination
        self.settings.update(mode="database-online", settings_version=2)
        lock = pathlib.Path(self.settings["job_lock_paths"][0])
        lock.chmod(0o600)
        php = self.script("sleeping-php", "import time; print('ready', flush=True); time.sleep(20)")
        # Spawn the launcher only once Backups owns the lock, immediately before its writer scan.
        from paymenter_backups.process import app_workers
        processes = []
        def scan(app, *args, **kwargs):
            if not processes:
                waiter = subprocess.Popen(["/usr/bin/flock", str(lock), str(php), str(self.app / "artisan"), "queue:work"],
                                          stdout=subprocess.PIPE, start_new_session=True)
                processes.append(waiter)
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    if any("->" in line and str(waiter.pid) in line.split()
                           for line in pathlib.Path("/proc/locks").read_text().splitlines()):
                        break
                    time.sleep(0.01)
            return app_workers(app, *args, **kwargs)
        try:
            with patch("paymenter_backups.maintenance.app_workers", side_effect=scan):
                self.assertTrue(capture(self.state, self.envelope)["local_complete"])
            self.assertTrue(select.select([processes[0].stdout], [], [], 5)[0])
            self.assertEqual(processes[0].stdout.readline(), b"ready\n")
            # Stop the cooperating process, then start a real writer without any lock.
            os.killpg(processes[0].pid, signal.SIGTERM)
            processes[0].wait(timeout=5)
            writer = subprocess.Popen([str(php), str(self.app / "artisan"), "queue:work"],
                                      stdout=subprocess.PIPE, start_new_session=True)
            processes.append(writer)
            self.assertTrue(select.select([writer.stdout], [], [], 5)[0])
            self.assertEqual(writer.stdout.readline(), b"ready\n")
            with self.assertRaisesRegex(BackupError, "job-workers-running"):
                with coordination(self.state, self.envelope):
                    self.fail("a real writer was ignored")
        finally:
            for proc in processes:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
                proc.stdout.close()

    def test_online_inventory_and_dump_share_one_deadline(self):
        self.settings.update(mode="database-online", capture_timeout=1.5)
        mysql = pathlib.Path(self.envelope["tools"]["mysql"])
        mysql.write_text(mysql.read_text().replace("import sys", "import time; time.sleep(0.9); import sys"))
        dump = pathlib.Path(self.envelope["tools"]["dump"])
        dump.write_text(dump.read_text().replace("import sys", "import time; time.sleep(0.9); import sys"))
        with self.assertRaisesRegex(BackupError, "child-timeout|capture-timeout"):
            capture(self.state, self.envelope)
        self.assertFalse((self.app / MARKERS[0]).exists())
        with open(self.settings["job_lock_paths"][0], "r+b") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_operator_replacement_same_bytes_refuses_release(self):
        journal = self.journal()
        path = self.app / MARKERS[0]
        replacement = path.with_name("replacement")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        with self.assertRaisesRegex(BackupError, "maintenance-ownership-uncertain"):
            cleanup(self.state, journal)
        self.assertTrue(path.exists())
        self.assertTrue((self.app / MARKERS[1]).exists())

    def test_recovery_removes_down_then_bootstrap_and_is_idempotent(self):
        journal = self.journal()
        result = recover(self.state, execute=True)
        self.assertEqual(result[0]["action"], "recovered")
        self.assertFalse((self.app / MARKERS[0]).exists())
        self.assertFalse((self.app / MARKERS[1]).exists())
        self.assertEqual(recover(self.state, execute=True), [])

    def test_preexisting_safe_maintenance_is_preserved_in_archive(self):
        run([self.envelope["tools"]["php"], str(self.app / "artisan")], 2)
        originals = {relative: identity(self.app / relative) for relative in MARKERS}
        journal = capture(self.state, self.envelope)
        self.assertEqual({relative: identity(self.app / relative) for relative in MARKERS}, originals)
        self.assertEqual(verify(self.state, journal["run_id"], True)["excluded_paths"], [])

    def test_scheduled_initial_maintenance_skips(self):
        run([self.envelope["tools"]["php"], str(self.app / "artisan")], 2)
        with self.assertRaisesRegex(BackupError, "initial-maintenance-scheduled-skip"):
            capture(self.state, self.envelope, scheduled=True)

    def test_failed_dump_restores_service_retains_failure(self):
        self.envelope["tools"]["dump"] = str(self.script("faildump", "import sys;sys.exit(1)"))
        with self.assertRaisesRegex(BackupError, "child-failed"):
            capture(self.state, self.envelope)
        self.assertFalse((self.app / MARKERS[0]).exists())
        self.assertEqual(self.state.journals()[0]["phase"], "failed")
        self.assertFalse(list(self.state.root.rglob("client.cnf")))

    def test_billing_minute_and_stale_guard_refuse(self):
        now = time.time()
        guard_path = self.state.root / "billing-guard.json"
        guard = json.loads(guard_path.read_text())
        guard["instants"] = [now - 20, now + 172800]
        atomic_json(guard_path, guard)
        with self.assertRaisesRegex(BackupError, "billing-window-protected"):
            billing_admission(self.state, self.envelope, now)
        guard["generated_at"] -= 1000
        atomic_json(guard_path, guard)
        with self.assertRaisesRegex(BackupError, "billing-guard-stale"):
            billing_admission(self.state, self.envelope, now)

    def test_inventory_visible_nonzero_and_errors_refuse(self):
        defaults = self.state.root / "dummy.cnf"
        defaults.write_text("")
        for output, code in (("tables\\t0\\nroutines\\t1\\nevents\\t0\\ntriggers\\t0\\nGRANT SELECT", "unsupported-db-objects"), ("broken", "database-inventory-failed")):
            tool = self.script("inv", "print(" + repr(output.replace("\\t", "\t").replace("\\n", "\n")) + ")")
            self.envelope["tools"]["mysql"] = str(tool)
            with self.assertRaisesRegex(BackupError, code):
                inventory(self.envelope, defaults, 2, None)

    def test_inventory_reports_unsupported_driver_separately_from_missing_name(self):
        defaults = self.state.root / "dummy.cnf"
        defaults.write_text("")
        original = dict(self.envelope["database"])
        self.envelope["database"] = {**original, "driver": "pgsql"}
        with self.assertRaisesRegex(BackupError, "database-driver-unsupported"):
            inventory(self.envelope, defaults, 2, None)
        self.envelope["database"] = {**original, "driver": "mariadb", "name": ""}
        with self.assertRaisesRegex(BackupError, "unsupported-database"):
            inventory(self.envelope, defaults, 2, None)
        self.envelope["database"] = original

    def test_inventory_accepts_mariadb_driver(self):
        defaults = self.state.root / "dummy.cnf"
        defaults.write_text("")
        self.envelope["database"]["driver"] = "mariadb"
        self.envelope["tools"]["mysql"] = str(self.script(
            "mariadb-inventory", "print('tables\\t0\\nroutines\\t0\\nevents\\t0\\ntriggers\\t0\\nGRANT SELECT')"))
        result = inventory(self.envelope, defaults, 2, None)
        self.assertEqual(result["counts"], {"tables": 0, "routines": 0, "events": 0, "triggers": 0})

    def test_inventory_omits_mariadb_authentication_hashes(self):
        defaults = self.state.root / "dummy.cnf"
        defaults.write_text("")
        output = "tables\t0\nroutines\t0\nevents\t0\ntriggers\t0\nGRANT SELECT ON fixture.* TO 'user'@'host' IDENTIFIED VIA mysql_native_password USING 'canary-authentication-hash'"
        self.envelope["tools"]["mysql"] = str(self.script("inv-grants", "print(" + repr(output) + ")"))
        result = inventory(self.envelope, defaults, 2, None)
        self.assertNotIn("canary-authentication-hash", json.dumps(result))
        self.assertIn("GRANT SELECT ON fixture.*", result["current_user_grants"][0])

    def test_cron_exact_paths_and_actual_command(self):
        lock = self.settings["job_lock_paths"][0]
        good = f"* * * * * flock {lock} php {self.app}/artisan schedule:run"
        self.assertEqual(cron_lock(good, self.app, [lock]), lock)
        for bad in (good.replace(lock, lock + "-other"), good.replace("flock", "echo flock"), good.replace("php", "true ; php"), good + " ; true"):
            with self.assertRaisesRegex(BackupError, "job-locks-unverified"):
                cron_lock(bad, self.app, [lock])

    def test_atime_change_does_not_change_marker_ownership(self):
        path = self.app / "marker"
        path.write_text("value")
        os.utime(path, (1, 2))
        self.assertEqual(identity(path), identity(path))

    def test_acknowledgement_preserves_operator_files(self):
        self.journal()
        before = (self.app / MARKERS[0]).read_bytes()
        result = recover(self.state, run_id="owned", acknowledge=True)
        self.assertEqual(result[0]["action"], "acknowledged")
        self.assertEqual((self.app / MARKERS[0]).read_bytes(), before)

    def test_archive_corruption_refuses(self):
        self.settings["mode"] = "database-online"
        journal = capture(self.state, self.envelope)
        with open(journal["archive_path"], "ab") as stream:
            stream.write(b"corruption")
        with self.assertRaisesRegex(BackupError, "archive-checksum-mismatch"):
            verify(self.state, journal["run_id"])

    def test_truncated_gzip_trailer_refuses_even_with_complete_tar_members(self):
        self.settings["mode"] = "database-online"
        journal = capture(self.state, self.envelope)
        manifest = verify(self.state, journal["run_id"], True)
        archive = pathlib.Path(journal["archive_path"])
        archive.write_bytes(archive.read_bytes()[:-8])
        with self.assertRaisesRegex(BackupError, "archive-unreadable"):
            check_archive(archive, manifest)

    def test_private_binding_and_outside_app(self):
        with self.assertRaisesRegex(BackupError, "private-root-inside-app"):
            State(self.app, self.app / "private")
        alternate = self.base / "another-app"
        alternate.mkdir()
        with self.assertRaisesRegex(BackupError, "installation-mismatch"):
            State(alternate, self.state.root)

    def test_external_symlink_refuses(self):
        (self.app / "bad-link").symlink_to(self.base)
        with self.assertRaisesRegex(BackupError, "external-symlink-target"):
            external_symlinks(self.app)

    def test_budget_monitor_stops_output_and_recovers(self):
        self.settings["local_byte_budget"] = 15000
        self.envelope["tools"]["dump"] = str(self.script("bigdump", "import sys,time\nfor i in range(1000):\n sys.stdout.write('x'*10000);sys.stdout.flush();time.sleep(.005)"))
        with self.assertRaisesRegex(BackupError, "local-byte-budget-exceeded|child-output-limit"):
            capture(self.state, self.envelope)
        self.assertFalse((self.app / MARKERS[0]).exists())

    def test_request_cancel_and_durable_slot(self):
        first = create(self.state, slot="2026-01-01T02:00@UTC")
        second = create(self.state, slot="2026-01-01T02:00@UTC")
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(cancel(self.state, first["request_id"])["status"], "cancelled")

    def test_recovery_and_retention_preview_create_no_lock_or_state_files(self):
        from paymenter_backups.storage import retention
        before = {str(path.relative_to(self.state.root)) for path in self.state.root.rglob("*")}
        self.assertEqual(recover(self.state), [])
        self.assertEqual(retention(self.state, self.envelope)["delete"], [])
        after = {str(path.relative_to(self.state.root)) for path in self.state.root.rglob("*")}
        self.assertEqual(before, after)


class PureTests(unittest.TestCase):
    def test_dst_gap_skips_and_repeat_runs_once(self):
        import datetime
        settings = dict(DEFAULTS, schedule_timezone="America/New_York", schedule_time="02:30", request_max_age=86400)
        gap_end = datetime.datetime(2026, 3, 9, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertFalse(any("2026-03-08" in slot for slot, _ in due_slots(settings, gap_end)))
        settings["schedule_time"] = "01:30"
        repeat_end = datetime.datetime(2026, 11, 2, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(sum("2026-11-01" in slot for slot, _ in due_slots(settings, repeat_end)), 1)

    def test_native_strings_are_normalized_and_secrets_redacted(self):
        settings = dict(DEFAULTS, capture_timeout="90", enabled="0", schedule_weekdays="[0,3]")
        normalize(settings)
        self.assertEqual(settings["capture_timeout"], 90)
        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["schedule_weekdays"], [0, 3])
        self.assertNotIn("canary", json.dumps(redact({"credentials": {"secret_key": "canary"}, "database": {"password": "canary"}})))
        with self.assertRaisesRegex(BackupError, "tool-path-invalid"):
            validate({"settings": settings, "tools": {"python": "python3"}})


if __name__ == "__main__":
    unittest.main()
