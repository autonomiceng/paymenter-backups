"""Admin history, download, verification, per-backup delete and config check."""
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_engine
from paymenter_backups import artifacts
from paymenter_backups.cli import dispatch, parser
from paymenter_backups.common import BackupError, atomic_json, read_json
from paymenter_backups.config import redact
from paymenter_backups.engine import capture, verify
from paymenter_backups.presentation import overview
from paymenter_backups.requests import create, next_slot, worker
from paymenter_backups.storage import delete_plan, delete_run, retention

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AdminWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_engine.Fixture("test_online_excludes_app_and_maintenance")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.envelope = self.fixture.envelope
        self.fixture.settings["mode"] = "database-online"

    def backup(self, payload_bytes=None):
        if payload_bytes:
            # Random bytes keep the gzip archive larger than one stream chunk.
            self.envelope["tools"]["dump"] = str(self.fixture.script("bigdump", "import os,sys\nsys.stdout.buffer.write(os.urandom(%d))" % payload_bytes))
        journal = capture(self.state, self.envelope)
        time.sleep(0.01)
        return journal

    def journal(self, run_id):
        return read_json(self.state.run_path(run_id) / "journal.json")

    def stream_bytes(self, run_id, **kwargs):
        reader, writer = os.pipe()
        chunks = []
        try:
            import threading
            def drain():
                for chunk in iter(lambda: os.read(reader, 1 << 20), b""):
                    chunks.append(chunk)
            thread = threading.Thread(target=drain)
            thread.start()
            try:
                artifacts.stream(self.state, run_id, writer, **kwargs)
            finally:
                os.close(writer)
                thread.join()
        finally:
            os.close(reader)
        data = b"".join(chunks)
        header, _, body = data.partition(b"\n")
        return json.loads(header), body

    def cli(self, *arguments, input_bytes=None, check=True):
        command = [sys.executable, "-I", str(ROOT / "bin/paymenter-backups"), "--app", str(self.fixture.app),
                   "--private-root", str(self.state.root), "--php", "/does/not/exist", "--bridge", "/does/not/exist"]
        result = subprocess.run(command + list(arguments), input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result

    def test_capture_records_identity_and_download_streams_exact_verified_bytes(self):
        journal = self.backup(3 * 1024 * 1024)
        recorded = self.journal(journal["run_id"])["verification"]
        self.assertEqual(recorded["source"], "capture")
        self.assertEqual(recorded["identity"]["inode"], os.stat(journal["archive_path"]).st_ino)
        header, body = self.stream_bytes(journal["run_id"])
        archive = pathlib.Path(journal["archive_path"]).read_bytes()
        self.assertEqual(body, archive)
        self.assertEqual(header["size"], len(archive))
        self.assertEqual(header["filename"], "paymenter-backup-" + journal["run_id"] + ".tar.gz")
        # The public helper command streams the same bytes through a pipe and audits the actor.
        result = self.cli("artifact-stream", journal["run_id"], "--actor", "7")
        self.assertEqual(result.stdout.partition(b"\n")[2], archive)
        audit = [json.loads(line) for line in (self.state.root / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(audit[-1], {"at": audit[-1]["at"], "event": "download", "run_id": journal["run_id"], "actor": 7, "outcome": "complete", "bytes": len(archive)})

    def test_changed_archive_requires_new_verification_before_download(self):
        journal = self.backup()
        archive = pathlib.Path(journal["archive_path"])
        os.utime(archive, ns=(time.time_ns(), time.time_ns() - 10**9))
        self.assertEqual(artifacts.download_state(self.state, self.journal(journal["run_id"])), "verify")
        with self.assertRaisesRegex(BackupError, "verification-required"):
            self.stream_bytes(journal["run_id"])
        failed = self.cli("artifact-stream", journal["run_id"], check=False)
        self.assertEqual((failed.returncode, failed.stdout), (1, b""))
        self.assertEqual(json.loads(failed.stderr)["error"], "verification-required")
        listed = json.loads(self.cli("overview", "--offline").stdout)
        self.assertEqual((listed["runs"][0]["status"], listed["runs"][0]["download"]), ("ready", "verify"))
        self.assertNotIn(str(self.state.root), json.dumps(listed))
        # Terminal verification without --record changes nothing; --record restores download.
        self.cli("verify", journal["run_id"])
        self.assertEqual(artifacts.download_state(self.state, self.journal(journal["run_id"])), "verify")
        self.assertTrue(json.loads(self.cli("verify", journal["run_id"], "--record").stdout)["verified"])
        self.assertEqual(artifacts.download_state(self.state, self.journal(journal["run_id"])), "ready")
        os.utime(archive, ns=(time.time_ns(), time.time_ns() - 5 * 10**9))
        # A queued verification records the new identity and download is ready again.
        request = create(self.state, "verify", {"run_id": journal["run_id"]})
        self.launch(request)
        self.assertEqual(read_json(self.state.root / "requests" / (request["request_id"] + ".json"))["status"], "complete")
        self.assertEqual(self.journal(journal["run_id"])["verification"]["source"], "verify")
        self.assertEqual(artifacts.download_state(self.state, self.journal(journal["run_id"])), "ready")

    def launch(self, request):
        item = read_json(self.state.root / "requests" / (request["request_id"] + ".json"))
        item["status"] = "launching"
        atomic_json(self.state.root / "requests" / (request["request_id"] + ".json"), item)
        return worker(self.state, self.envelope, request["request_id"])

    def test_stream_withholds_final_chunk_when_bytes_differ_from_record(self):
        journal = self.backup(3 * 1024 * 1024)
        archive = pathlib.Path(journal["archive_path"])
        data = bytearray(archive.read_bytes())
        data[-100] ^= 0xFF
        archive.write_bytes(bytes(data))
        # Simulate a stale record that still matches the descriptor identity.
        with patch.object(artifacts, "verification_current", return_value=True):
            with self.assertRaisesRegex(BackupError, "archive-changed-during-download"):
                self.stream_bytes(journal["run_id"])
            reader, writer = os.pipe()
            received = b""
            try:
                with self.assertRaises(BackupError):
                    import threading
                    chunks = []
                    thread = threading.Thread(target=lambda: chunks.extend(iter(lambda: os.read(reader, 1 << 20), b"")))
                    thread.start()
                    try:
                        artifacts.stream(self.state, journal["run_id"], writer)
                    finally:
                        os.close(writer)
                        thread.join()
                received = b"".join(chunks).partition(b"\n")[2]
            finally:
                os.close(reader)
        self.assertLess(len(received), len(data))
        self.assertEqual(received, bytes(data[:len(received)]))

    def test_resolution_refuses_symlink_hardlink_mode_and_foreign_locations(self):
        journal = self.backup()
        run_id = journal["run_id"]
        archive = pathlib.Path(journal["archive_path"])
        original = archive.read_bytes()
        outside = self.fixture.base / "outside.tar.gz"
        outside.write_bytes(original)
        outside.chmod(0o600)
        archive.unlink()
        archive.symlink_to(outside)
        with self.assertRaisesRegex(BackupError, "artifact-unsafe"):
            with artifacts.open_archive(self.state, run_id):
                pass
        archive.unlink()
        os.link(outside, archive)
        with self.assertRaisesRegex(BackupError, "artifact-unsafe"):
            with artifacts.open_archive(self.state, run_id):
                pass
        archive.unlink()
        archive.write_bytes(original)
        archive.chmod(0o644)
        with self.assertRaisesRegex(BackupError, "artifact-unsafe"):
            verify(self.state, run_id)
        archive.chmod(0o600)
        verify(self.state, run_id)
        public = self.fixture.app / "public"
        public.mkdir()
        for forged in (str(public / (run_id + ".tar.gz")), str(archive.parent) + "/../artifacts/" + run_id + ".tar.gz",
                       str(archive.parent / "other.tar.gz"), "relative/" + run_id + ".tar.gz"):
            item = self.journal(run_id)
            item["archive_path"] = forged
            if forged.startswith(str(public)):
                item["manifest_path"] = str(public / (run_id + ".manifest.json"))
            self.state.save(item)
            with self.assertRaisesRegex(BackupError, "artifact-location-invalid"):
                with artifacts.open_archive(self.state, run_id):
                    pass
            self.state.save(journal)
        # A linked artifact directory is not canonical.
        linked = self.fixture.base / "linked-artifacts"
        linked.symlink_to(archive.parent)
        item = dict(journal, archive_path=str(linked / archive.name), manifest_path=str(linked / (run_id + ".manifest.json")))
        self.state.save(item)
        with self.assertRaisesRegex(BackupError, "artifact-location-invalid"):
            artifacts.stream(self.state, run_id, os.open(os.devnull, os.O_WRONLY))

    def test_queued_verification_records_damage_without_inventing_success(self):
        journal = self.backup()
        archive = pathlib.Path(journal["archive_path"])
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0xFF
        archive.write_bytes(bytes(data))
        request = create(self.state, "verify", {"run_id": journal["run_id"]})
        self.assertEqual(self.launch(request)["status"], "failed")
        item = self.journal(journal["run_id"])
        self.assertNotIn("verification", item)
        self.assertEqual(item["verification_error"]["code"], "archive-checksum-mismatch")
        summary = overview(self.state)
        self.assertEqual(summary["runs"][0]["status"], "damaged")
        self.assertEqual(summary["runs"][0]["download"], "verify")

    def test_overview_whitelists_presentation_fields(self):
        self.fixture.settings["mode"] = "full-quiesced"
        first = capture(self.state, self.envelope)
        failing = dict(self.envelope, tools=dict(self.envelope["tools"], dump=str(self.fixture.script("faildump", "import sys\nsys.exit(3)"))))
        with self.assertRaises(BackupError):
            capture(self.state, failing)
        create(self.state, "verify", {"run_id": first["run_id"], "actor": 3})
        offline = overview(self.state)
        text = json.dumps(offline)
        for forbidden in (str(self.state.root), str(self.fixture.app), "fake-canary-secret", "GRANT", "payload",
                          "archive_path", "manifest_path", '"pid"', "boot_id", "owned_markers", read_json(first["manifest_path"])["archive"]["sha256"]):
            self.assertNotIn(forbidden, text)
        statuses = [item["status"] for item in offline["runs"]]
        self.assertEqual(sorted(statuses), ["failed", "ready"])
        ready = next(item for item in offline["runs"] if item["status"] == "ready")
        self.assertEqual((ready["mode"], ready["download"], ready["verification_source"]), ("full-quiesced", "ready", "capture"))
        self.assertEqual(ready["size"], os.stat(first["archive_path"]).st_size)
        failed = next(item for item in offline["runs"] if item["status"] == "failed")
        self.assertEqual(failed["error"], "child-failed")
        self.assertEqual(offline["requests"][0]["action"], "verify")
        self.assertEqual(offline["requests"][0]["actor"], 3)
        self.assertFalse(offline["configuration"]["available"])
        # Configuration readiness adds booleans and nonsecret settings only.
        self.envelope["credentials"] = {"access_key": "ACCESS-CANARY", "secret_key": "SECRET-CANARY"}
        self.envelope["maintenance_probe_credentials"] = {"origin": "https://billing.example.invalid", "username": "USER-CANARY", "password": "PASSWORD-CANARY"}
        configured = overview(self.state, self.envelope)
        text = json.dumps(configured)
        self.assertNotIn("CANARY", text)
        self.assertTrue(configured["configuration"]["saved_keys"]["s3"])
        self.assertEqual(configured["configuration"]["saved_keys"]["site_origin"], "https://billing.example.invalid")
        # The CLI redacts sensitive key names in all output; the overview must survive it intact.
        self.assertEqual(redact(configured), configured)
        self.assertIn("database-online", configured["configuration"]["admission"])

    def test_delete_preview_refuses_last_backup_and_conflicts(self):
        only = self.backup()
        self.assertEqual(delete_plan(self.state, self.envelope, only["run_id"])["refusal"], "delete-last-backup")
        with self.assertRaisesRegex(BackupError, "delete-last-backup"):
            delete_run(self.state, self.envelope, only["run_id"])
        self.assertTrue(pathlib.Path(only["archive_path"]).exists())
        newer = self.backup()
        with self.assertRaisesRegex(BackupError, "delete-confirmation-required"):
            create(self.state, "delete", {"run_id": only["run_id"]})
        create(self.state, "deliver", {"run_id": only["run_id"]})
        self.assertEqual(delete_plan(self.state, self.envelope, only["run_id"])["refusal"], "delete-conflict")
        with self.assertRaisesRegex(BackupError, "delete-conflict"):
            create(self.state, "delete", {"run_id": only["run_id"], "execute": True})
        self.assertIsNone(delete_plan(self.state, self.envelope, newer["run_id"])["refusal"])

    def test_delete_request_removes_only_target_after_full_survivor_verification(self):
        older, newer = self.backup(), self.backup()
        keep = self.fixture.settings["retention_keep"]
        request = create(self.state, "delete", {"run_id": older["run_id"], "execute": True, "actor": 1})
        again = create(self.state, "delete", {"run_id": older["run_id"], "execute": True, "actor": 2})
        self.assertEqual(request["request_id"], again["request_id"])
        self.assertEqual(self.launch(request)["status"], "complete")
        self.assertFalse(pathlib.Path(older["archive_path"]).exists())
        self.assertFalse(pathlib.Path(older["manifest_path"]).exists())
        self.assertTrue(pathlib.Path(newer["archive_path"]).exists())
        item = self.journal(older["run_id"])
        self.assertEqual((item["retained_deleted"], item["deletion"]["reason"], item["delete_intent"]["survivor"]), (True, "manual", newer["run_id"]))
        self.assertEqual(self.journal(newer["run_id"])["verification"]["source"], "verify")
        self.assertEqual(self.fixture.settings["retention_keep"], keep)
        self.assertEqual([entry["status"] for entry in overview(self.state)["runs"]], ["ready", "deleted"])

    def test_delete_refuses_when_no_survivor_passes_full_verification(self):
        older, newer = self.backup(), self.backup()
        survivor = pathlib.Path(newer["archive_path"])
        data = bytearray(survivor.read_bytes())
        data[len(data) // 2] ^= 0xFF
        survivor.write_bytes(bytes(data))
        with self.assertRaisesRegex(BackupError, "delete-no-verified-survivor"):
            delete_run(self.state, self.envelope, older["run_id"])
        self.assertTrue(pathlib.Path(older["archive_path"]).exists())
        self.assertNotIn("delete_intent", self.journal(older["run_id"]))
        self.assertEqual(self.journal(newer["run_id"])["verification_error"]["code"], "archive-checksum-mismatch")

    def test_interrupted_delete_persists_intent_and_resumes(self):
        older, newer = self.backup(), self.backup()
        unlink = os.unlink
        def fault(path, *args, dir_fd=None, **kwargs):
            # Removal is relative to the verified directory descriptor.
            if dir_fd is not None and path == pathlib.Path(older["archive_path"]).name:
                raise OSError("disposable fault")
            return unlink(path, *args, dir_fd=dir_fd, **kwargs)
        with patch("os.unlink", fault):
            with self.assertRaisesRegex(BackupError, "delete-local-delete-failed"):
                delete_run(self.state, self.envelope, older["run_id"])
        item = self.journal(older["run_id"])
        self.assertIn("delete_intent", item)
        self.assertFalse(pathlib.Path(older["manifest_path"]).exists())
        self.assertEqual(overview(self.state)["runs"][1]["status"], "delete-interrupted")
        # Retention neither verifies nor counts a backup with a manual delete intent.
        self.assertEqual(retention(self.state, self.envelope)["keep"], [newer["run_id"]])
        request = create(self.state, "delete", {"run_id": older["run_id"], "execute": True})
        self.assertEqual(self.launch(request)["status"], "complete")
        self.assertFalse(pathlib.Path(older["archive_path"]).exists())
        self.assertTrue(self.journal(older["run_id"])["retained_deleted"])

    def test_delete_keeps_a_file_replaced_after_its_check(self):
        older, _newer = self.backup(), self.backup()
        archive = pathlib.Path(older["archive_path"])
        replacement = archive.read_bytes()
        original_stat = artifacts.stat_owned
        def replace_after_check(location, name, seen):
            original_stat(location, name, seen)
            if name == archive.name:
                # Same name, same content, different file: not the one that was checked.
                archive.unlink()
                archive.write_bytes(replacement)
                archive.chmod(0o600)
        with patch.object(artifacts, "stat_owned", replace_after_check):
            with self.assertRaisesRegex(BackupError, "delete-local-verification-failed"):
                delete_run(self.state, self.envelope, older["run_id"])
        self.assertTrue(archive.exists())
        self.assertIn("delete_intent", self.journal(older["run_id"]))

    def test_busy_run_lock_defers_delete_and_verify(self):
        older, _ = self.backup(), self.backup()
        request = create(self.state, "delete", {"run_id": older["run_id"], "execute": True})
        with self.state.lock():
            descriptor = os.open(self.state.root / "run.lock", os.O_RDWR)
            try:
                import fcntl
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            pid = os.fork()
            if pid == 0:
                try:
                    result = self.launch(request)
                    os._exit(0 if result["status"] == "deferred" and result["error"] == "busy" else 1)
                except BaseException:
                    os._exit(2)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertTrue(pathlib.Path(older["archive_path"]).exists())

    def test_run_request_mode_and_duplicate_suppression(self):
        self.fixture.settings["mode"] = "full-quiesced"
        first = create(self.state, "run", {"mode": "database-online", "actor": 1})
        self.assertEqual(create(self.state, "run", {"mode": "database-online", "actor": 4})["request_id"], first["request_id"])
        self.assertNotEqual(create(self.state, "run", {})["request_id"], first["request_id"])
        for invalid in ({"mode": "full"}, {"mode": "database-online", "unknown": 1}, {"actor": "admin"}):
            with self.assertRaisesRegex(BackupError, "request-arguments-invalid"):
                create(self.state, "run", invalid)
        self.assertEqual(self.launch(first)["status"], "complete")
        journal = next(item for item in self.state.journals() if item.get("request_id") == first["request_id"])
        self.assertEqual(journal["mode"], "database-online")
        self.assertFalse((self.fixture.app / "storage/framework/down").exists())

    def test_config_check_validates_without_writing(self):
        args = parser().parse_args(["--app", str(self.fixture.app), "--private-root", str(self.state.root), "config", "check"])
        before = sorted(str(path) for path in self.state.root.rglob("*"))
        def check(settings):
            with patch("paymenter_backups.cli.bridge", return_value=json.loads(json.dumps(self.envelope))), \
                    patch("paymenter_backups.cli.stdin_json", return_value={"settings": settings}):
                return dispatch(args)
        self.assertEqual(check({"retention_keep": 3, "schedule_weekdays": [0, 6]}), {"valid": True})
        for invalid in ({"retention_keep": 0}, {"capture_timeout": "0"}, {"schedule_weekdays": [7]},
                        {"enabled": True, "schedule_frequency": "weekly", "schedule_weekdays": []},
                        {"job_lock_paths": ["relative.lock"]}, {"unknown": 1}):
            with self.assertRaisesRegex(BackupError, "config-invalid"):
                check(invalid)
        with self.assertRaisesRegex(BackupError, "plaintext-offsite-acknowledgement-required"):
            check({"destination": "s3", "s3_bucket": "fixture"})
        with self.assertRaisesRegex(BackupError, "s3-tls-required"):
            check({"destination": "s3", "s3_bucket": "fixture", "plaintext_offsite_acknowledged": True, "s3_endpoint": "http://fixture.invalid"})
        self.assertEqual(sorted(str(path) for path in self.state.root.rglob("*")), before)

    def test_native_null_text_settings_normalize_to_empty(self):
        from paymenter_backups.config import DEFAULTS, normalize, validate
        settings = dict(DEFAULTS, local_path=None, s3_endpoint=None, s3_bucket=None)
        normalize(settings)
        self.assertEqual((settings["local_path"], settings["s3_endpoint"], settings["s3_bucket"]), ("", "", ""))
        validate(dict(self.envelope, settings=settings))
        with self.assertRaisesRegex(BackupError, "config-invalid"):
            normalize(dict(DEFAULTS, s3_bucket=["not", "text"]))

    def test_tick_records_scheduler_check_in(self):
        args = parser().parse_args(["--app", str(self.fixture.app), "--private-root", str(self.state.root), "tick", "--execute"])
        self.assertIsNone(overview(self.state)["scheduler"]["last_tick_at"])
        with patch("paymenter_backups.cli.load", return_value=self.envelope):
            self.assertEqual(dispatch(args), {"launched": []})
        self.assertAlmostEqual(overview(self.state)["scheduler"]["last_tick_at"], time.time(), delta=5)

    def test_next_slot_matches_schedule_rules(self):
        settings = dict(self.fixture.settings, schedule_time="02:00", schedule_timezone="UTC", schedule_frequency="weekly", schedule_weekdays=[2])
        monday = 1767571200  # 2026-01-05T00:00:00Z
        self.assertEqual(next_slot(settings, monday), monday + 2 * 86400 + 7200)
        settings.update(schedule_frequency="daily")
        self.assertEqual(next_slot(settings, monday + 7200), monday + 86400 + 7200)
        # 02:30 does not exist in New York on 2026-03-08; the next slot is the following day.
        settings.update(schedule_time="02:30", schedule_timezone="America/New_York")
        gap_day = 1772946000  # 2026-03-08T05:00:00Z
        self.assertEqual(next_slot(settings, gap_day), 1773037800)


if __name__ == "__main__":
    unittest.main()
