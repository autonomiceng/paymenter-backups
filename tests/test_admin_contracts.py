"""Admin contract tests using real private files and tar archives.

These tests never boot Paymenter, launch a scheduler or contact a provider.
They check administrator operations through the engine API.
"""
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from paymenter_backups import artifacts, storage
from paymenter_backups import cli
from paymenter_backups.common import BackupError, State, atomic_json, read_json
from paymenter_backups.config import DEFAULTS, normalize, validate
from paymenter_backups.engine import verify
from paymenter_backups.presentation import overview
from paymenter_backups.requests import cancel, create, path as request_path, worker
from paymenter_backups.storage import delete_plan, delete_run, prefix, retention


class AdminArtifactContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="backup-admin-test-")
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.app = self.base / "app"
        (self.app / "public").mkdir(parents=True)
        self.state = State(self.app, self.base / "private")
        self.directory = self.state.root / "artifacts"
        self.directory.mkdir(mode=0o700)
        self.envelope = {"settings": dict(DEFAULTS)}

    def backup(self, run_id, payload=None, created_at=1):
        payload = payload or b"CREATE TABLE fixture(id INT);\n"
        manifest = {
            "schema_version": 1, "installation_uuid": self.state.uuid,
            "run_id": run_id, "mode": "database-online", "created_at": created_at,
            "payloads": {"database.sql": {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}},
        }
        archive = self.directory / (run_id + ".tar.gz")
        with tarfile.open(archive, "w:gz") as output:
            for name, data in (("database.sql", payload), ("manifest.json", json.dumps(manifest).encode())):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        archive.chmod(0o600)
        manifest["archive"] = {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size}
        manifest_path = self.directory / (run_id + ".manifest.json")
        atomic_json(manifest_path, manifest)
        self.state.run_path(run_id).mkdir(mode=0o700)
        self.state.save({"run_id": run_id, "installation_uuid": self.state.uuid,
                         "created_at": created_at, "phase": "complete", "local_complete": True,
                         "service_recovered": True, "remote_delivered": False,
                         "archive_path": str(archive), "manifest_path": str(manifest_path)})
        with self.state.lock():
            verify(self.state, run_id, record=True)
        return archive, manifest_path

    def test_stream_returns_exact_archive_and_safe_header(self):
        archive, _ = self.backup("owned")
        with tempfile.TemporaryFile() as output:
            result = artifacts.stream(self.state, "owned", output.fileno())
            output.seek(0)
            header = json.loads(output.readline())
            body = output.read()
        self.assertEqual(body, archive.read_bytes())
        self.assertEqual(header["size"], len(body))
        self.assertEqual(header["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(header["filename"], "paymenter-backup-owned.tar.gz")
        self.assertEqual(result["run_id"], "owned")
        self.assertNotIn(str(self.base), json.dumps(header))

    def test_stream_requires_current_full_verification_before_any_output(self):
        archive, _ = self.backup("owned")
        archive.touch()
        with tempfile.TemporaryFile() as output:
            with self.assertRaisesRegex(BackupError, "verification-required"):
                artifacts.stream(self.state, "owned", output.fileno())
            self.assertEqual(output.tell(), 0)

    def test_stream_withholds_final_chunk_when_open_archive_changes(self):
        archive, _ = self.backup("owned", os.urandom(3 * artifacts.CHUNK))
        delivered = io.BytesIO()
        changed = False

        def mutate_during_transfer(_descriptor, data):
            nonlocal changed
            delivered.write(data)
            if not changed and len(data) >= artifacts.CHUNK:
                with archive.open("r+b") as modifying:
                    modifying.seek(-1, os.SEEK_END)
                    original = modifying.read(1)
                    modifying.seek(-1, os.SEEK_END)
                    modifying.write(bytes([original[0] ^ 1]))
                    modifying.flush()
                    os.fsync(modifying.fileno())
                changed = True

        with patch.object(artifacts, "write_all", side_effect=mutate_during_transfer):
            with self.assertRaisesRegex(BackupError, "archive-changed-during-download"):
                artifacts.stream(self.state, "owned", -1)
        self.assertTrue(changed)
        delivered.seek(0)
        header = json.loads(delivered.readline())
        self.assertLess(len(delivered.read()), header["size"])

    def test_archive_and_manifest_require_exact_0600(self):
        archive, manifest = self.backup("owned")
        for target in (archive, manifest):
            for mode in (0o400, 0o700, 0o644):
                with self.subTest(file=target.name, mode=oct(mode)):
                    target.chmod(mode)
                    try:
                        with self.assertRaises(BackupError):
                            with artifacts.open_archive(self.state, "owned"):
                                pass
                    finally:
                        target.chmod(0o600)

    def test_hardlink_symlink_and_other_installation_refuse(self):
        archive, _ = self.backup("owned")
        extra = self.base / "hardlink"
        os.link(archive, extra)
        with self.assertRaisesRegex(BackupError, "artifact-unsafe"):
            with artifacts.open_archive(self.state, "owned"):
                pass
        extra.unlink()
        original = archive.with_suffix(".original")
        archive.rename(original)
        archive.symlink_to(original)
        with self.assertRaisesRegex(BackupError, "artifact-unsafe"):
            with artifacts.open_archive(self.state, "owned"):
                pass
        archive.unlink()
        original.rename(archive)
        item = artifacts.journal(self.state, "owned")
        item["installation_uuid"] = "a-different-installation"
        self.state.save(item)
        with self.assertRaisesRegex(BackupError, "artifact-identity-mismatch"):
            with artifacts.open_archive(self.state, "owned"):
                pass

    def test_run_ids_never_select_paths(self):
        self.backup("owned")
        for run_id in ("../owned", "/owned", "owned/../../public", "owned\x00", ""):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(BackupError, "invalid-run-id"):
                with artifacts.open_archive(self.state, run_id):
                    pass
        with self.assertRaisesRegex(BackupError, "run-not-found"):
            with artifacts.open_archive(self.state, "missing"):
                pass

    def test_parent_symlink_swap_after_location_check_refuses(self):
        self.backup("owned")
        inside_app = self.app / "artifacts"
        shutil.copytree(self.directory, inside_app)
        inside_app.chmod(0o700)
        original_locations = artifacts.locations
        swapped = False

        def swap_parent_after_check(state, item):
            nonlocal swapped
            result = original_locations(state, item)
            if not swapped:
                state.root.rename(self.base / "private-original")
                state.root.symlink_to(self.app, target_is_directory=True)
                swapped = True
            return result

        with patch.object(artifacts, "locations", side_effect=swap_parent_after_check):
            with self.assertRaises(BackupError):
                with artifacts.open_archive(self.state, "owned"):
                    pass
        self.assertTrue(swapped)

    def test_manifest_mutation_during_read_refuses(self):
        _, manifest = self.backup("owned")
        read = artifacts.os.read
        changed = False

        def change_after_read(descriptor, size):
            nonlocal changed
            data = read(descriptor, size)
            if data and not changed:
                with manifest.open("ab") as output:
                    output.write(b" ")
                    output.flush()
                    os.fsync(output.fileno())
                changed = True
            return data

        with patch.object(artifacts.os, "read", side_effect=change_after_read):
            with self.assertRaises(BackupError):
                with artifacts.open_archive(self.state, "owned"):
                    pass
        self.assertTrue(changed)

    def test_manifest_digest_must_be_hexadecimal(self):
        _, manifest_path = self.backup("owned")
        manifest = read_json(manifest_path)
        manifest["archive"]["sha256"] = "z" * 64
        atomic_json(manifest_path, manifest)
        with self.assertRaisesRegex(BackupError, "artifact-manifest-invalid"):
            with artifacts.open_archive(self.state, "owned"):
                pass

    def test_delete_preview_then_execution_preserves_last_good_and_history(self):
        first, _ = self.backup("first", created_at=1)
        survivor, _ = self.backup("survivor", created_at=2)
        preview = delete_plan(self.state, self.envelope, "first")
        self.assertIsNone(preview["refusal"])
        self.assertTrue(first.exists())
        result = delete_run(self.state, self.envelope, "first")
        self.assertTrue(result["deleted"])
        self.assertFalse(first.exists())
        self.assertTrue(survivor.exists())
        self.assertTrue(verify(self.state, "survivor")["verified"])
        item = artifacts.journal(self.state, "first")
        self.assertTrue(item["retained_deleted"])
        self.assertEqual(item["deletion"]["reason"], "manual")
        with self.assertRaisesRegex(BackupError, "delete-last-backup"):
            delete_run(self.state, self.envelope, "survivor")
        self.assertTrue(survivor.exists())

    def test_native_retention_keeps_working_after_manual_delete_refactor(self):
        first, _ = self.backup("first", created_at=1)
        latest, _ = self.backup("latest", created_at=2)
        self.envelope["settings"]["retention_keep"] = 1
        result = retention(self.state, self.envelope, execute=True)
        self.assertEqual(result["delete"], ["first"])
        self.assertFalse(first.exists())
        self.assertTrue(latest.exists())
        self.assertTrue(verify(self.state, "latest")["verified"])

    def test_delete_refuses_when_only_survivor_is_corrupt(self):
        first, _ = self.backup("first", created_at=1)
        survivor, _ = self.backup("survivor", created_at=2)
        with survivor.open("ab") as output:
            output.write(b"corruption")
        with self.assertRaisesRegex(BackupError, "delete-no-verified-survivor"):
            delete_run(self.state, self.envelope, "first")
        self.assertTrue(first.exists())
        self.assertNotIn("delete_intent", artifacts.journal(self.state, "first"))

    def test_delete_preview_excludes_known_damaged_survivors(self):
        good, _ = self.backup("good", created_at=1)
        damaged, _ = self.backup("damaged", created_at=2)
        with damaged.open("ab") as output:
            output.write(b"known damage")
        with self.state.lock():
            with self.assertRaises(BackupError):
                verify(self.state, "damaged", record=True)
        item = artifacts.journal(self.state, "damaged")
        self.assertNotIn("verification", item)
        self.assertIn("verification_error", item)
        preview = delete_plan(self.state, self.envelope, "good")
        self.assertNotIn("damaged", preview["survivors"])
        self.assertEqual(preview["refusal"], "delete-last-backup")
        self.assertTrue(good.exists())

    def test_delete_damaged_target_is_allowed_when_verified_survivor_exists(self):
        good, _ = self.backup("good", created_at=1)
        damaged, _ = self.backup("damaged", created_at=2)
        with damaged.open("ab") as output:
            output.write(b"known damage")
        with self.state.lock():
            with self.assertRaises(BackupError):
                verify(self.state, "damaged", record=True)
        preview = delete_plan(self.state, self.envelope, "damaged")
        self.assertIsNone(preview["refusal"])
        self.assertIn("good", preview["survivors"])
        result = delete_run(self.state, self.envelope, "damaged")
        self.assertTrue(result["deleted"])
        self.assertFalse(damaged.exists())
        self.assertTrue(good.exists())
        self.assertTrue(verify(self.state, "good")["verified"])

    def test_delete_retry_rechecks_survivor_after_partial_failure(self):
        first, first_manifest = self.backup("first", created_at=1)
        survivor, _ = self.backup("survivor", created_at=2)
        unlink = os.unlink

        def fail_archive_once(path, *args, **kwargs):
            if os.fspath(path) in (str(first), first.name):
                raise OSError("fixture failure after manifest removal")
            return unlink(path, *args, **kwargs)

        with patch.object(os, "unlink", fail_archive_once):
            with self.assertRaisesRegex(BackupError, "delete-local-delete-failed"):
                delete_run(self.state, self.envelope, "first")
        self.assertFalse(first_manifest.exists())
        self.assertTrue(first.exists())
        self.assertIn("delete_intent", artifacts.journal(self.state, "first"))
        with survivor.open("ab") as output:
            output.write(b"corruption after first deletion attempt")
        with self.assertRaisesRegex(BackupError, "delete-no-verified-survivor"):
            delete_run(self.state, self.envelope, "first")
        self.assertTrue(first.exists())

    def test_delete_refuses_public_archive_mode(self):
        first, _ = self.backup("first", created_at=1)
        self.backup("survivor", created_at=2)
        first.chmod(0o644)
        with self.assertRaises(BackupError):
            delete_run(self.state, self.envelope, "first")
        self.assertTrue(first.exists())

    def test_delete_keeps_same_inode_archive_modified_after_its_check(self):
        archive, _ = self.backup("first", created_at=1)
        self.backup("survivor", created_at=2)
        original_stat = artifacts.stat_owned
        before = archive.stat()
        changed = False

        def mutate_after_check(location, name, seen):
            nonlocal changed
            original_stat(location, name, seen)
            if name == archive.name and not changed:
                changed = True
                with archive.open("r+b") as output:
                    first_byte = output.read(1)
                    output.seek(0)
                    output.write(bytes([first_byte[0] ^ 1]))
                    output.flush()
                    os.fsync(output.fileno())
                os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))

        with patch.object(artifacts, "stat_owned", mutate_after_check):
            with self.assertRaisesRegex(BackupError, "delete-local-verification-failed"):
                delete_run(self.state, self.envelope, "first")
        self.assertTrue(changed)
        self.assertTrue(archive.exists())
        self.assertEqual(archive.stat().st_ino, before.st_ino)
        self.assertEqual(archive.stat().st_size, before.st_size)
        self.assertIn("delete_intent", artifacts.journal(self.state, "first"))

    def test_delete_cannot_follow_replaced_parent_into_application(self):
        self.backup("first", created_at=1)
        self.backup("survivor", created_at=2)
        inside_app = self.app / "artifacts"
        shutil.copytree(self.directory, inside_app)
        inside_app.chmod(0o700)
        shutil.copytree(self.state.root / "runs", self.app / "runs")
        original_locations = artifacts.locations
        target_checks = 0

        def swap_before_local_removal(state, item):
            nonlocal target_checks
            result = original_locations(state, item)
            if item["run_id"] == "first":
                target_checks += 1
                if target_checks == 2:
                    state.root.rename(self.base / "private-original")
                    state.root.symlink_to(self.app, target_is_directory=True)
            return result

        with patch.object(artifacts, "locations", side_effect=swap_before_local_removal):
            try:
                delete_run(self.state, self.envelope, "first")
            except BackupError:
                pass
        self.assertGreaterEqual(target_checks, 2)
        self.assertTrue((inside_app / "first.tar.gz").exists(), "Deletion followed a replaced parent into the application")
        self.assertTrue((inside_app / "first.manifest.json").exists())

    def test_queued_verify_is_durable_deduplicated_and_cancelled_without_work(self):
        archive, _ = self.backup("owned")
        before = archive.read_bytes()
        first = create(self.state, "verify", {"run_id": "owned", "actor": 1})
        repeated = create(self.state, "verify", {"run_id": "owned", "actor": 2})
        self.assertEqual(first["request_id"], repeated["request_id"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(archive.read_bytes(), before)
        result = cancel(self.state, first["request_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(read_json(request_path(self.state, first["request_id"]))["status"], "cancelled")
        self.assertEqual(archive.read_bytes(), before)

    def test_queued_delete_conflicts_with_existing_verify(self):
        archive, _ = self.backup("first")
        self.backup("survivor", created_at=2)
        create(self.state, "verify", {"run_id": "first"})
        with self.assertRaisesRegex(BackupError, "delete-conflict"):
            create(self.state, "delete", {"run_id": "first", "execute": True})
        self.assertTrue(archive.exists())

    def launch_exact_request(self, item):
        item["status"] = "launching"
        atomic_json(request_path(self.state, item["request_id"]), item)
        return worker(self.state, self.envelope, item["request_id"])

    def test_queued_delete_worker_completes_only_after_files_removed(self):
        first, _ = self.backup("first")
        survivor, _ = self.backup("survivor", created_at=2)
        item = create(self.state, "delete", {"run_id": "first", "execute": True, "actor": 1})
        self.assertTrue(first.exists())
        result = self.launch_exact_request(item)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(first.exists())
        self.assertTrue(survivor.exists())
        projection = overview(self.state)
        self.assertEqual(projection["requests"][0]["status"], "complete")
        entries = {item["run_id"]: item for item in projection["runs"]}
        self.assertEqual(entries["first"]["status"], "deleted")

    def test_queued_delete_last_copy_records_failure_and_preserves_archive(self):
        archive, _ = self.backup("owned")
        item = create(self.state, "delete", {"run_id": "owned", "execute": True})
        result = self.launch_exact_request(item)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "delete-last-backup")
        self.assertTrue(archive.exists())
        self.assertEqual(overview(self.state)["requests"][0]["error"], "delete-last-backup")

    def test_overview_never_projects_private_journal_or_request_payloads(self):
        self.backup("owned")
        item = artifacts.journal(self.state, "owned")
        item.update(grants="PRIVATE-GRANT-CANARY", groups=[{"secret": "PRIVATE-GROUP-CANARY"}],
                    error="PRIVATE-ERROR-CANARY" + str(self.base))
        self.state.save(item)
        task = create(self.state, "verify", {"run_id": "owned"})
        task.update(status="complete", result={"run_id": "owned", "archive_path": str(self.base),
                                             "credentials": "PRIVATE-RESULT-CANARY"})
        atomic_json(request_path(self.state, task["request_id"]), task)
        projection = overview(self.state)
        rendered = json.dumps(projection)
        self.assertNotIn(str(self.base), rendered)
        self.assertNotIn("CANARY", rendered)
        self.assertEqual(projection["runs"][0]["error"], "unknown")
        self.assertNotIn("result", projection["requests"][0])

    def remote_objects(self, *run_ids):
        self.envelope["settings"].update(destination="s3", s3_bucket="fixture-only-bucket")
        base = prefix(self.state, self.envelope)
        objects = {}
        for run_id in run_ids:
            item = artifacts.journal(self.state, run_id)
            item.update(remote_delivered=True, remote={"bucket": "fixture-only-bucket",
                        "archive_key": base + run_id + ".tar.gz", "manifest_key": base + run_id + ".manifest.json"})
            self.state.save(item)
            objects[item["remote"]["archive_key"]] = pathlib.Path(item["archive_path"]).read_bytes()
            objects[item["remote"]["manifest_key"]] = json.dumps(read_json(item["manifest_path"]), sort_keys=True, indent=2).encode() + b"\n"
        return base, objects

    def test_delete_keeps_latest_remote_copy(self):
        self.backup("first", created_at=1)
        latest, _ = self.backup("latest", created_at=2)
        self.remote_objects("first", "latest")
        self.assertEqual(delete_plan(self.state, self.envelope, "latest")["refusal"], "delete-latest-remote-copy")
        with patch.object(storage, "client") as client:
            with self.assertRaisesRegex(BackupError, "delete-latest-remote-copy"):
                delete_run(self.state, self.envelope, "latest")
            client.assert_not_called()
        self.assertTrue(latest.exists())

    def test_delete_refuses_changed_remote_scope_before_any_removal(self):
        first, _ = self.backup("first", created_at=1)
        self.backup("survivor", created_at=2)
        self.remote_objects("first")
        self.envelope["settings"]["s3_bucket"] = "different-fixture-bucket"
        with patch.object(storage, "client") as client:
            with self.assertRaisesRegex(BackupError, "delete-remote-scope-changed"):
                delete_run(self.state, self.envelope, "first")
            client.assert_not_called()
        self.assertTrue(first.exists())

    def test_delete_removes_only_verified_owned_remote_objects_manifest_first(self):
        first, _ = self.backup("first", created_at=1)
        latest, _ = self.backup("latest", created_at=2)
        base, objects = self.remote_objects("first", "latest")
        marker = base + "lastmanifest.json"
        foreign = "another-installation/foreign.tar.gz"
        objects.update({marker: b"latest marker", foreign: b"foreign object"})
        trace = []

        class Remote:
            def get_object(self, *, Bucket, Key):
                self_bucket = Bucket
                if self_bucket != "fixture-only-bucket":
                    raise AssertionError("wrong bucket")
                return {"Body": io.BytesIO(objects[Key])}

            def delete_object(self, *, Bucket, Key):
                if Bucket != "fixture-only-bucket":
                    raise AssertionError("wrong bucket")
                trace.append(Key)
                del objects[Key]

        with patch.object(storage, "client", return_value=Remote()):
            result = delete_run(self.state, self.envelope, "first")
        self.assertTrue(result["deleted"])
        self.assertEqual(trace, [base + "first.manifest.json", base + "first.tar.gz"])
        self.assertFalse(first.exists())
        self.assertTrue(latest.exists())
        self.assertIn(base + "latest.tar.gz", objects)
        self.assertIn(base + "latest.manifest.json", objects)
        self.assertEqual(objects[marker], b"latest marker")
        self.assertEqual(objects[foreign], b"foreign object")


class AdminConfigContracts(unittest.TestCase):
    def envelope(self):
        settings = dict(DEFAULTS, schedule_weekdays=[0], job_lock_paths=[], extra_docroots=[])
        return {"settings": settings, "tools": {name: sys.executable for name in ("python", "php", "dump", "mysql", "tar", "gzip")}}

    def test_schedule_weekdays_requires_a_list_in_all_modes(self):
        for days in ({}, {"monday": 0}, None, "0", 0, True):
            with self.subTest(days=days):
                envelope = self.envelope()
                envelope["settings"]["schedule_weekdays"] = days
                with self.assertRaisesRegex(BackupError, "config-invalid"):
                    normalize(envelope["settings"])
                    validate(envelope)

    def test_integer_limits_refuse_fractional_numbers_before_normalization(self):
        keys = [key for key, value in DEFAULTS.items() if type(value) is int]
        for key in keys:
            for value in (1.5, True):
                with self.subTest(key=key, value=value):
                    envelope = self.envelope()
                    envelope["settings"][key] = value
                    with self.assertRaisesRegex(BackupError, "config-invalid"):
                        normalize(envelope["settings"])
                        validate(envelope)

    def test_config_check_refuses_output_inside_app_or_public_root_before_writes(self):
        with tempfile.TemporaryDirectory(prefix="backup-config-test-") as temporary:
            base = pathlib.Path(temporary)
            app = base / "app"
            (app / "public").mkdir(parents=True)
            other_public = base / "other-public"
            other_public.mkdir()
            state = State(app, base / "private")
            envelope = self.envelope()
            envelope["app"] = str(app)
            envelope["settings"]["extra_docroots"] = [str(other_public)]
            args = SimpleNamespace(app=str(app), private_root=str(state.root))
            for output in (app / "backups", app / "public" / "backups", other_public / "backups"):
                with self.subTest(output=output):
                    with patch.object(cli, "bridge", return_value=envelope):
                        with self.assertRaises(BackupError):
                            cli.check_config(args, state, {"settings": {"local_path": str(output)}})
                    self.assertFalse(output.exists())


@unittest.skipUnless(shutil.which("php"), "PHP is needed for the native presentation utility")
class AdminPresentationContracts(unittest.TestCase):
    def test_storage_budget_and_archive_sizes_preserve_integer_zeroes(self):
        source = pathlib.Path(__file__).resolve().parents[1] / "extension" / "Backups" / "Messages.php"
        program = r'''
require $argv[1];
$result = [];
foreach ([0, 1024, 1572864, 10485760, 20971520, 104857600, 10737418240] as $bytes) {
    $result[] = Paymenter\Extensions\Others\Backups\Messages::bytes($bytes);
}
echo json_encode($result, JSON_THROW_ON_ERROR);
'''
        result = subprocess.run([shutil.which("php"), "-r", program, str(source)], check=True, capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), ["0 bytes", "1 KiB", "1.5 MiB", "10 MiB", "20 MiB", "100 MiB", "10 GiB"])


if __name__ == "__main__":
    unittest.main()
