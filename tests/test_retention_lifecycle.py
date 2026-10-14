"""Independent copy lifecycle checks through real archives and engine interfaces.

No deployed Paymenter, native scheduler, provider account or external network is used here.
"""
import copy
import datetime
import fcntl
import gzip
import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import test_admin_contracts as archive_fixture
import test_engine as capture_fixture
from paymenter_backups import artifacts, storage, requests
from paymenter_backups import engine
from paymenter_backups import cli
from paymenter_backups.common import BackupError, State, atomic_json, read_json
from paymenter_backups.config import DEFAULTS, normalize
from paymenter_backups.engine import destination
from paymenter_backups.presentation import overview


class MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class ObjectStore:
    def __init__(self):
        self.objects = {}
        self.trace = []
        self.fail_put = False
        self.fail_delete_key = None
        self.bytes_read = {}

    def put_object(self, Bucket, Key, Body, **_):
        self.trace.append(("put", Bucket, Key))
        if self.fail_put:
            raise OSError("controlled upload failure")
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else bytes(Body)
        return {}

    def get_object(self, Bucket, Key):
        self.trace.append(("get", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise MissingObject()
        store = self

        class Body(io.BytesIO):
            def read(self, size=-1):
                data = super().read(size)
                store.bytes_read[Key] = store.bytes_read.get(Key, 0) + len(data)
                return data

        data = self.objects[(Bucket, Key)]
        return {"Body": Body(data), "ContentLength": len(data)}

    def head_object(self, Bucket, Key):
        self.trace.append(("head", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise MissingObject()
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.trace.append(("delete", Bucket, Key))
        if Key == self.fail_delete_key:
            raise OSError("controlled interruption after manifest deletion")
        self.objects.pop((Bucket, Key), None)
        return {}


class RetentionLifecycle(unittest.TestCase):
    def setUp(self):
        self.fixture = archive_fixture.AdminArtifactContracts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.envelope = {"settings": dict(DEFAULTS, destination="s3", s3_encryption=False, s3_bucket="fixture",
            s3_endpoint="https://first.invalid", plaintext_offsite_acknowledged=True,
            minimum_free_bytes=0, local_byte_budget=1024**3, retention_keep=1,
            s3_retention_policy="keep-all")}
        self.store = ObjectStore()
        patched = patch.object(storage, "client", return_value=self.store)
        patched.start()
        self.addCleanup(patched.stop)

    def backup(self, run_id, created_at, deliver=True):
        archive, manifest = self.fixture.backup(run_id, created_at=created_at)
        if deliver:
            storage.deliver(self.state, self.envelope, run_id)
        return archive, manifest

    def item(self, run_id):
        return artifacts.journal(self.state, run_id)

    def object(self, run_id, suffix=".tar.gz"):
        return ("fixture", storage.prefix(self.state, self.envelope) + run_id + suffix)

    def test_new_directory_and_legacy_binding_identity_remain_distinct(self):
        self.assertEqual(self.state.default_destination(), self.state.root / "backups")
        binding = read_json(self.state.binding)
        binding.pop("default_local_directory")
        atomic_json(self.state.binding, binding)
        before = self.state.binding.read_bytes()
        legacy = State(self.state.app, self.state.root)
        self.assertEqual(destination(legacy, self.envelope), self.state.root / "artifacts")
        self.assertEqual(legacy.uuid, self.state.uuid)
        self.assertEqual(self.state.binding.read_bytes(), before)

    def test_legacy_policy_does_not_silently_activate_age_expiry(self):
        settings = dict(DEFAULTS, retention_keep="11")
        normalize(settings)
        self.assertEqual(settings["s3_retention_policy"], "local-count")
        self.assertEqual(settings["retention_keep"], 11)

    def test_unbound_legacy_multipart_refuses_before_remote_work_or_journal_write(self):
        archive, _ = self.backup("legacy-multipart", 10, deliver=False)
        journal = self.item("legacy-multipart")
        journal["multipart"] = {"bucket": "fixture", "key": self.object("legacy-multipart")[1],
            "upload_id": "legacy-provider-upload"}
        journal_path = self.state.run_path("legacy-multipart") / "journal.json"
        atomic_json(journal_path, journal)
        before = journal_path.read_bytes()
        source = archive.read_bytes()
        with patch.object(storage, "client") as client:
            with self.assertRaisesRegex(BackupError, "multipart-location-unbound"):
                storage.deliver(self.state, self.envelope, "legacy-multipart")
            client.assert_not_called()
        self.assertEqual(self.store.trace, [])
        self.assertEqual(journal_path.read_bytes(), before)
        self.assertEqual(archive.read_bytes(), source)

    def test_usage_keeps_counting_old_external_directories_after_settings_change(self):
        first = self.fixture.base / "first-external-directory"
        second = self.fixture.base / "second-external-directory"
        first.mkdir(mode=0o700)
        second.mkdir(mode=0o700)
        self.fixture.directory = first
        self.backup("older", 10, deliver=False)
        self.fixture.directory = second
        self.backup("newer", 20, deliver=False)
        self.envelope["settings"]["local_path"] = str(second)
        expected = engine.usage(self.state.root) + engine.usage(first) + engine.usage(second)
        self.assertEqual(engine.storage_usage(self.state, self.envelope["settings"]), expected)

    def test_local_count_keeps_remote_only_history_and_objects(self):
        older, _ = self.backup("older", 10)
        newer, _ = self.backup("newer", 20)
        objects = copy.deepcopy(self.store.objects)
        storage.retention(self.state, self.envelope, execute=True)
        self.assertFalse(older.exists())
        self.assertTrue(newer.exists())
        self.assertEqual(self.store.objects, objects)
        self.assertFalse(self.item("older")["local_complete"])
        self.assertTrue(self.item("older")["remote_delivered"])
        entry = next(x for x in overview(self.state, self.envelope)["runs"] if x["run_id"] == "older")
        self.assertNotEqual(entry["status"], "deleted")
        self.assertFalse(entry["local"])
        self.assertNotEqual(entry["remote"], "none")

    def test_s3_age_expiry_preserves_local_download_bytes(self):
        self.envelope["settings"].update(retention_keep=3, s3_retention_policy="age", s3_retention_days=30)
        older, _ = self.backup("older", time.time() - 60 * 86400)
        self.backup("newer", time.time())
        expected = older.read_bytes()
        storage.retention(self.state, self.envelope, execute=True)
        self.assertNotIn(self.object("older"), self.store.objects)
        self.assertIn(self.object("newer"), self.store.objects)
        self.assertEqual(older.read_bytes(), expected)
        with tempfile.TemporaryFile() as output:
            artifacts.stream(self.state, "older", output.fileno())
            output.seek(0)
            output.readline()
            self.assertEqual(output.read(), expected)

    def test_changed_endpoint_refuses_remote_operations_with_same_bucket_and_keys(self):
        self.backup("older", 10)
        self.backup("newer", 20)
        before = copy.deepcopy(self.store.objects)
        self.envelope["settings"]["s3_endpoint"] = "https://second.invalid"
        self.store.trace.clear()
        with self.assertRaises(BackupError):
            storage.delete_run(self.state, self.envelope, "older", location="s3")
        with self.assertRaises(BackupError):
            storage.deliver(self.state, self.envelope, "older")
        self.assertEqual(self.store.objects, before)
        self.assertEqual(self.store.trace, [])

    def test_local_delete_remains_available_when_remote_scope_changes(self):
        older, _ = self.backup("older", 10)
        self.backup("newer", 20)
        remote = copy.deepcopy(self.store.objects)
        self.envelope["settings"]["s3_endpoint"] = "https://second.invalid"
        storage.delete_run(self.state, self.envelope, "older", location="local")
        self.assertFalse(older.exists())
        self.assertEqual(self.store.objects, remote)
        self.assertTrue(self.item("older")["remote_delivered"])

    def test_unbound_legacy_remote_cannot_authorize_destruction(self):
        self.backup("older", 10)
        self.backup("newer", 20)
        item = self.item("older")
        item["remote"].pop("scope")
        self.state.save(item)
        self.store.trace.clear()
        with self.assertRaises(BackupError):
            storage.delete_run(self.state, self.envelope, "older", location="s3")
        self.assertEqual(self.store.trace, [])

    def test_failed_upload_source_survives_independent_local_retention(self):
        older, _ = self.backup("failed", 10, deliver=False)
        self.store.fail_put = True
        with self.assertRaises(BackupError):
            storage.deliver(self.state, self.envelope, "failed")
        self.store.fail_put = False
        self.backup("newer", 20)
        storage.retention(self.state, self.envelope, execute=True)
        self.assertTrue(older.exists())
        self.assertTrue(self.item("failed")["local_complete"])

    def test_local_delete_requires_a_verified_local_survivor(self):
        target, _ = self.backup("target", 10)
        survivor, _ = self.backup("survivor", 20)
        survivor.write_bytes(b"damaged local copy; its S3 copy is still valid")
        with self.assertRaises(BackupError):
            storage.delete_run(self.state, self.envelope, "target", location="local")
        self.assertTrue(target.exists())
        self.assertIn(self.object("survivor"), self.store.objects)

    def test_remote_delete_requires_a_verified_remote_survivor(self):
        self.backup("target", 10)
        self.backup("survivor", 20)
        damaged = bytearray(self.store.objects[self.object("survivor")])
        damaged[len(damaged) // 2] ^= 1
        self.store.objects[self.object("survivor")] = bytes(damaged)
        with self.assertRaises(BackupError):
            storage.delete_run(self.state, self.envelope, "target", location="s3")
        self.assertIn(self.object("target"), self.store.objects)
        self.assertTrue(self.item("target")["local_complete"])

    def test_interrupted_s3_only_delete_keeps_local_download_available(self):
        target, _ = self.backup("target", 10)
        self.backup("survivor", 20)
        expected = target.read_bytes()
        self.store.fail_delete_key = self.object("target")[1]
        with self.assertRaises(BackupError):
            storage.delete_run(self.state, self.envelope, "target", location="s3")
        self.assertEqual(target.read_bytes(), expected)
        with tempfile.TemporaryFile() as output:
            artifacts.stream(self.state, "target", output.fileno())
            output.seek(0)
            output.readline()
            self.assertEqual(output.read(), expected)

    def test_damaged_newest_local_copy_does_not_block_safe_retention(self):
        oldest, _ = self.backup("oldest", 10)
        good, _ = self.backup("good", 20)
        damaged, _ = self.backup("damaged", 30)
        damaged.write_bytes(b"damaged latest local archive")
        storage.retention(self.state, self.envelope, execute=True)
        self.assertFalse(oldest.exists())
        self.assertTrue(good.exists())
        self.assertTrue(damaged.exists())
        self.assertIn(self.object("damaged"), self.store.objects)

    def test_lifecycle_missing_copy_does_not_block_other_s3_expiry(self):
        now = time.time()
        self.envelope["settings"].update(retention_keep=3, s3_retention_policy="age", s3_retention_days=30)
        self.backup("old", now - 90 * 86400)
        self.backup("good", now - 10 * 86400)
        missing, _ = self.backup("missing", now - 86400)
        expected = missing.read_bytes()
        del self.store.objects[self.object("missing")]
        del self.store.objects[self.object("missing", ".manifest.json")]
        storage.retention(self.state, self.envelope, execute=True)
        self.assertNotIn(self.object("old"), self.store.objects)
        self.assertIn(self.object("good"), self.store.objects)
        self.assertEqual(self.item("missing")["remote_observation"]["status"], "missing")
        self.assertEqual(missing.read_bytes(), expected)
        pointer = ("fixture", storage.prefix(self.state, self.envelope) + "lastmanifest.json")
        self.assertEqual(json.loads(self.store.objects[pointer])["run_id"], "good")

    def test_remote_retrieval_recreates_exact_bytes_with_download_lease(self):
        older, _ = self.backup("older", 10)
        self.backup("newer", 20)
        expected = older.read_bytes()
        storage.retention(self.state, self.envelope, execute=True)
        storage.retrieve(self.state, self.envelope, "older")
        self.assertEqual(older.read_bytes(), expected)
        self.assertEqual(older.stat().st_mode & 0o777, 0o600)
        self.assertGreaterEqual(self.item("older")["local_download_until"], time.time() + 23 * 3600)
        storage.retention(self.state, self.envelope, execute=True)
        self.assertTrue(older.exists())

    def test_retrieval_never_overwrites_an_unrecorded_local_replacement(self):
        older, _ = self.backup("older", 10)
        self.backup("newer", 20)
        storage.retention(self.state, self.envelope, execute=True)
        older.write_bytes(b"operator replacement to preserve")
        older.chmod(0o600)
        with self.assertRaises(BackupError):
            storage.retrieve(self.state, self.envelope, "older")
        self.assertEqual(older.read_bytes(), b"operator replacement to preserve")

    def test_retrieval_cannot_follow_replaced_directory_into_document_root(self):
        older, _ = self.backup("older", 10)
        self.backup("newer", 20)
        storage.retention(self.state, self.envelope, execute=True)
        original_directory = older.parent
        exposed = self.state.app / "public" / "replaced-backups"
        exposed.mkdir(mode=0o700)
        moved = original_directory.with_name("preserved-original-directory")
        replaced = False

        def replace_after_location_check(_envelope):
            nonlocal replaced
            if not replaced:
                replaced = True
                original_directory.rename(moved)
                original_directory.symlink_to(exposed, target_is_directory=True)
            return self.store

        with patch.object(storage, "client", side_effect=replace_after_location_check):
            with self.assertRaises(BackupError):
                storage.retrieve(self.state, self.envelope, "older")
        self.assertTrue(replaced)
        self.assertEqual(list(exposed.iterdir()), [], "Retrieval wrote archive bytes through a replaced parent into the document root")

    def test_retrieval_bounds_oversized_remote_manifest_before_publication(self):
        older, _ = self.backup("older", 10)
        self.backup("newer", 20)
        storage.retention(self.state, self.envelope, execute=True)
        manifest_key = self.object("older", ".manifest.json")
        self.store.objects[manifest_key] = b"x" * (4 * 1024**2)
        self.store.bytes_read.clear()
        with self.assertRaises(BackupError):
            storage.retrieve(self.state, self.envelope, "older")
        self.assertLessEqual(self.store.bytes_read[manifest_key[1]], 2 * 1024**2)
        self.assertFalse(older.exists())
        self.assertFalse(self.item("older")["local_complete"])

    def test_preview_does_not_transfer_remote_archives(self):
        self.backup("older", 10)
        self.backup("newer", 20)
        self.store.trace.clear()
        storage.retention(self.state, self.envelope)
        storage.delete_plan(self.state, self.envelope, "older", location="s3")
        storage.retrieve_plan(self.state, self.envelope, "older")
        self.assertEqual(self.store.trace, [])

    def test_enabling_schedule_does_not_backfill_earlier_slots(self):
        instant = datetime.datetime(2026, 10, 14, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="02:00")
        with patch.object(requests.time, "time", return_value=instant), patch.object(requests, "detach", return_value=99999999) as launcher:
            requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        self.assertEqual(requests.list_requests(self.state), [])
        launcher.assert_not_called()

    def test_cancel_after_scheduled_creation_is_never_overwritten_by_tick(self):
        enabled_at = datetime.datetime(2026, 10, 1, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
        tick_at = enabled_at + 2 * 3600
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="02:00")
        requests.sync_schedule(self.state, self.envelope["settings"], enabled_at)
        original_create = requests.create

        def cancel_created(*args, **kwargs):
            item = original_create(*args, **kwargs)
            requests.cancel(self.state, item["request_id"])
            return item

        with patch.object(requests.time, "time", return_value=tick_at), \
                patch.object(requests, "create", side_effect=cancel_created), \
                patch.object(requests, "detach") as detach:
            requests.tick(self.state, self.envelope, ["fixture-worker"])
        self.assertEqual([item["status"] for item in requests.list_requests(self.state)], ["cancelled"])
        detach.assert_not_called()

    def test_tick_refreshes_stale_configuration_without_resetting_newer_epoch(self):
        saved_at = datetime.datetime(2026, 10, 14, 1, 55, tzinfo=datetime.timezone.utc).timestamp()
        stale = copy.deepcopy(self.envelope)
        current = copy.deepcopy(self.envelope)
        stale["settings"].update(enabled=False, mode="database-online", schedule_time="03:00")
        current["settings"].update(enabled=True, mode="database-online", schedule_time="02:00")
        epoch = requests.sync_schedule(self.state, current["settings"], saved_at)
        with patch.object(requests.time, "time", return_value=saved_at + 5 * 60), \
                patch.object(requests, "detach", return_value=99999999):
            requests.tick(self.state, stale, ["fixture-worker"], envelope_loader=lambda: current)
        self.assertEqual(read_json(self.state.root / "schedule.json"), epoch)
        self.assertEqual([item["slot_at"] for item in requests.list_requests(self.state)], [saved_at + 5 * 60])

    def test_private_lock_wait_has_a_finite_deadline(self):
        with self.state.lock("tick.lock"):
            started = time.monotonic()
            with self.assertRaisesRegex(BackupError, "busy"):
                with self.state.lock("tick.lock", timeout=0.15):
                    self.fail("An exclusively held lock was acquired")
            elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.14)
        self.assertLess(elapsed, 2)

    def test_schedule_save_waits_for_brief_tick_and_then_succeeds(self):
        with self.state.lock("tick.lock") as held:
            def release():
                time.sleep(0.15)
                fcntl.flock(held, fcntl.LOCK_UN)
            releaser = threading.Thread(target=release)
            releaser.start()
            try:
                started = time.monotonic()
                epoch = requests.sync_schedule(self.state, self.envelope["settings"])
                elapsed = time.monotonic() - started
            finally:
                releaser.join(timeout=2)
        self.assertFalse(releaser.is_alive())
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 2)
        self.assertEqual(read_json(self.state.root / "schedule.json"), epoch)
        self.assertEqual(requests.list_requests(self.state), [])

    def test_scheduler_outage_still_catches_up_unchanged_schedule(self):
        before = datetime.datetime(2026, 10, 14, 1, 55, tzinfo=datetime.timezone.utc).timestamp()
        after = before + 65 * 60
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="02:00")
        with patch.object(requests, "detach", return_value=99999999):
            with patch.object(requests.time, "time", return_value=before):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
            self.assertEqual(requests.list_requests(self.state), [])
            with patch.object(requests.time, "time", return_value=after):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        items = requests.list_requests(self.state)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["slot_at"], before + 5 * 60)

    def test_changing_schedule_time_does_not_backfill(self):
        instant = datetime.datetime(2026, 10, 14, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="11:00")
        with patch.object(requests.time, "time", return_value=instant), patch.object(requests, "detach", return_value=99999999):
            requests.tick(self.state, self.envelope, ["unused-fixture-command"])
            self.envelope["settings"]["schedule_time"] = "02:00"
            requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        self.assertEqual(requests.list_requests(self.state), [])

    def test_enabling_at_exact_scheduled_minute_waits_for_next_future_slot(self):
        boundary = datetime.datetime(2026, 10, 14, 2, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="02:00")
        requests.sync_schedule(self.state, self.envelope["settings"], boundary)
        with patch.object(requests, "detach", return_value=99999999) as detach:
            for instant in (boundary, boundary + 60):
                with patch.object(requests.time, "time", return_value=instant):
                    requests.tick(self.state, self.envelope, ["fixture-worker"])
            self.assertEqual(requests.list_requests(self.state), [])
            detach.assert_not_called()
            with patch.object(requests.time, "time", return_value=boundary + 86400):
                requests.tick(self.state, self.envelope, ["fixture-worker"])
            self.assertEqual(detach.call_count, 1)
        self.assertEqual([item["slot_at"] for item in requests.list_requests(self.state)], [boundary + 86400])

    def test_editing_at_exact_scheduled_minute_waits_for_next_future_slot(self):
        boundary = datetime.datetime(2026, 10, 14, 2, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="03:00")
        requests.sync_schedule(self.state, self.envelope["settings"], boundary - 60)
        self.envelope["settings"]["schedule_time"] = "02:00"
        requests.sync_schedule(self.state, self.envelope["settings"], boundary)
        with patch.object(requests, "detach", return_value=99999999) as detach:
            for instant in (boundary, boundary + 60):
                with patch.object(requests.time, "time", return_value=instant):
                    requests.tick(self.state, self.envelope, ["fixture-worker"])
            self.assertEqual(requests.list_requests(self.state), [])
            detach.assert_not_called()
            with patch.object(requests.time, "time", return_value=boundary + 86400):
                requests.tick(self.state, self.envelope, ["fixture-worker"])
            self.assertEqual(detach.call_count, 1)
        self.assertEqual([item["slot_at"] for item in requests.list_requests(self.state)], [boundary + 86400])

    def test_schedule_epoch_does_not_extend_task_expiry(self):
        instant = datetime.datetime(2026, 10, 14, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        settings = dict(DEFAULTS, enabled=True, mode="database-online", schedule_time="02:00", request_max_age=3600)
        self.assertEqual(list(requests.due_slots(settings, now=instant, since=instant - 7 * 86400)), [])

    def test_outage_coalesces_multiple_missed_slots_into_one_fresh_task(self):
        before = datetime.datetime(2026, 10, 1, 1, 55, tzinfo=datetime.timezone.utc).timestamp()
        after = datetime.datetime(2026, 10, 8, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="02:00")
        with patch.object(requests, "detach", return_value=99999999) as launcher:
            with patch.object(requests.time, "time", return_value=before):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
            with patch.object(requests.time, "time", return_value=after):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        items = requests.list_requests(self.state)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["created_at"], after)
        self.assertEqual(items[0]["status"], "launching")
        self.assertEqual(launcher.call_count, 1)

    def test_weekly_outage_recovery_is_fresh_despite_older_source_slot(self):
        before = datetime.datetime(2026, 10, 1, 1, 55, tzinfo=datetime.timezone.utc).timestamp()
        after = datetime.datetime(2026, 10, 16, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_frequency="weekly",
            schedule_weekdays=[0], schedule_time="02:00", request_max_age=3600)
        with patch.object(requests, "detach", return_value=99999999):
            with patch.object(requests.time, "time", return_value=before):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
            with patch.object(requests.time, "time", return_value=after):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        items = requests.list_requests(self.state)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["created_at"], after)
        self.assertEqual(items[0]["status"], "launching")

    def test_disabled_schedule_cancels_waiting_scheduled_task(self):
        before = datetime.datetime(2026, 10, 14, 1, 55, tzinfo=datetime.timezone.utc).timestamp()
        due = before + 5 * 60
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_time="02:00")
        with patch.object(requests, "detach", return_value=99999999):
            with patch.object(requests.time, "time", return_value=before):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
            with patch.object(requests.time, "time", return_value=due):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        item = requests.list_requests(self.state)[0]
        item.update(status="pending", worker=None)
        atomic_json(requests.path(self.state, item["request_id"]), item)
        self.envelope["settings"]["enabled"] = False
        with patch.object(requests.time, "time", return_value=due + 60), patch.object(requests, "detach") as launcher:
            requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        self.assertEqual(requests.list_requests(self.state)[0]["status"], "cancelled")
        launcher.assert_not_called()

    def test_queued_outage_task_expires_from_creation_time(self):
        before = datetime.datetime(2026, 10, 1, 1, 55, tzinfo=datetime.timezone.utc).timestamp()
        after = datetime.datetime(2026, 10, 16, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.envelope["settings"].update(enabled=True, mode="database-online", schedule_frequency="weekly",
            schedule_weekdays=[0], schedule_time="02:00", request_max_age=3600)
        with patch.object(requests, "detach", return_value=99999999):
            with patch.object(requests.time, "time", return_value=before):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
            with patch.object(requests.time, "time", return_value=after):
                requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        item = requests.list_requests(self.state)[0]
        item.update(status="pending", worker=None)
        atomic_json(requests.path(self.state, item["request_id"]), item)
        with patch.object(requests.time, "time", return_value=after + 3601), patch.object(requests, "detach") as launcher:
            requests.tick(self.state, self.envelope, ["unused-fixture-command"])
        self.assertEqual(requests.list_requests(self.state)[0]["status"], "expired")
        launcher.assert_not_called()

    def test_keep_all_retention_does_not_read_archives_when_local_policy_keeps_them(self):
        self.envelope["settings"]["retention_keep"] = 3
        self.backup("older", 10)
        self.backup("newer", 20)
        self.store.bytes_read.clear()
        storage.retention(self.state, self.envelope, execute=True)
        archive_bytes = sum(count for key, count in self.store.bytes_read.items() if key.endswith(".tar.gz"))
        self.assertEqual(archive_bytes, 0)

    def test_retention_checks_each_s3_archive_at_most_once_per_attempt(self):
        self.envelope["settings"].update(retention_keep=4, s3_retention_policy="count", s3_retention_keep=1)
        for index, run_id in enumerate(("first", "second", "third", "latest"), 1):
            self.backup(run_id, index * 10)
        sizes = {key: len(data) for (bucket, key), data in self.store.objects.items() if key.endswith(".tar.gz")}
        self.store.bytes_read.clear()
        storage.retention(self.state, self.envelope, execute=True)
        for key, size in sizes.items():
            self.assertGreater(self.store.bytes_read.get(key, 0), 0)
            self.assertLessEqual(self.store.bytes_read[key], size, "Retention downloaded the same archive repeatedly")
        self.assertIn(self.object("latest"), self.store.objects)
        self.assertNotIn(self.object("first"), self.store.objects)

    def test_retrieval_request_is_async_until_its_explicit_worker_runs(self):
        older, _ = self.backup("older", 10)
        self.backup("newer", 20)
        expected = older.read_bytes()
        storage.retention(self.state, self.envelope, execute=True)
        item = requests.create(self.state, "retrieve", {"run_id": "older", "actor": 7})
        self.assertFalse(older.exists())
        self.assertEqual(item["status"], "pending")
        item["status"] = "launching"
        atomic_json(requests.path(self.state, item["request_id"]), item)
        result = requests.worker(self.state, self.envelope, item["request_id"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(older.read_bytes(), expected)


class AdmissionChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = capture_fixture.Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_saved_basic_credentials_refuse_before_entering_maintenance(self):
        credentials = {"origin": "https://first.invalid", "username": "fake-user", "password": "fake-password"}
        for url, expected in (("http://first.invalid", "maintenance-probe-http-saved"),
                ("https://second.invalid", "maintenance-probe-origin-mismatch"),
                ("https://[::ffff:127.0.0.1]", "maintenance-probe-origin-invalid"),
                ("https://é.invalid", "maintenance-probe-origin-invalid")):
            with self.subTest(app_url=url):
                envelope = copy.deepcopy(self.fixture.envelope)
                envelope["maintenance_probe_credentials"] = credentials
                envelope["framework"]["app_url"] = url
                self.assertIn(expected, engine.plan(self.fixture.state, envelope)["admission_errors"])
                with patch.object(engine, "run", wraps=engine.run) as processes:
                    with self.assertRaisesRegex(BackupError, expected):
                        engine.capture(self.fixture.state, envelope)
                self.assertFalse(any("down" in call.args[0] for call in processes.call_args_list))
                self.assertFalse((self.fixture.app / "storage/framework/down").exists())
                self.assertEqual(self.fixture.state.journals(), [])

    def test_database_only_capture_is_not_blocked_by_unused_basic_credentials(self):
        envelope = copy.deepcopy(self.fixture.envelope)
        envelope["settings"]["mode"] = "database-online"
        envelope["maintenance_probe_credentials"] = {"origin": "https://first.invalid", "username": "fake-user", "password": "fake-password"}
        self.assertFalse(any(code.startswith("maintenance-probe-") for code in engine.plan(self.fixture.state, envelope)["admission_errors"]))

    def test_busy_scheduler_refuses_configuration_before_any_native_write(self):
        args = types.SimpleNamespace(php=self.fixture.envelope["tools"]["php"])
        started = time.monotonic()
        with self.fixture.state.lock("tick.lock"), \
                patch.object(cli, "bridge", side_effect=lambda *_args: copy.deepcopy(self.fixture.envelope)), \
                patch.object(cli.subprocess, "run") as native_writer:
            with self.assertRaisesRegex(BackupError, "busy"):
                cli.set_config(args, self.fixture.state, {"settings": {"enabled": True, "mode": "database-online"}})
            native_writer.assert_not_called()
        self.assertGreaterEqual(time.monotonic() - started, 9.9)
        self.assertLess(time.monotonic() - started, 12)

    def test_credentials_rechecked_against_locked_origin_before_any_save(self):
        initial = copy.deepcopy(self.fixture.envelope)
        locked = copy.deepcopy(self.fixture.envelope)
        initial["framework"]["app_url"] = "https://first.invalid"
        locked["framework"]["app_url"] = "https://second.invalid"
        credentials = {"origin": "https://first.invalid", "username": "fake-user", "password": "fake-password"}
        args = types.SimpleNamespace(php=self.fixture.envelope["tools"]["php"])
        with patch.object(cli, "bridge", side_effect=[initial, locked]) as bridge, \
                patch.object(cli.subprocess, "run") as native_writer:
            with self.assertRaisesRegex(BackupError, "maintenance-probe-origin-mismatch"):
                cli.set_config(args, self.fixture.state, {"maintenance_probe_credentials": credentials})
            self.assertEqual(bridge.call_count, 2)
            native_writer.assert_not_called()

    def test_failed_packaging_removes_owned_temporary_payload_but_keeps_journal(self):
        envelope = copy.deepcopy(self.fixture.envelope)
        envelope["settings"]["mode"] = "database-online"
        original_run = engine.run

        def fail_packaging(command, *args, **kwargs):
            if any(str(value).startswith("--use-compress-program=") for value in command):
                target = pathlib.Path(command[command.index("-cf") + 1])
                target.write_bytes(b"controlled incomplete packaging")
                target.chmod(0o600)
                raise BackupError("controlled-packaging-failure")
            return original_run(command, *args, **kwargs)

        with patch.object(engine, "run", side_effect=fail_packaging):
            with self.assertRaises(BackupError):
                engine.capture(self.fixture.state, envelope)
        journals = self.fixture.state.journals()
        self.assertEqual(len(journals), 1)
        self.assertEqual(journals[0]["phase"], "failed")
        self.assertFalse((self.fixture.state.run_path(journals[0]["run_id"]) / "payload").exists())
        self.assertEqual(list(self.fixture.state.root.rglob("*.partial")), [])

    def test_automatic_retention_can_clear_budget_before_a_new_capture(self):
        envelope = copy.deepcopy(self.fixture.envelope)
        envelope["settings"]["mode"] = "database-online"
        original_dump = envelope["tools"]["dump"]
        source = self.fixture.base / "large-fixture.sql"
        source.write_bytes(b"-- controlled fixture payload\n" + os.urandom(200000).hex().encode())
        envelope["tools"]["dump"] = str(self.fixture.script("large-dump", "import pathlib,sys\nsys.stdout.buffer.write(pathlib.Path(" + repr(str(source)) + ").read_bytes())"))
        first = engine.capture(self.fixture.state, envelope)
        second = engine.capture(self.fixture.state, envelope)
        envelope["tools"]["dump"] = original_dump
        envelope["settings"].update(retention_keep=1, automatic_retention=True,
            local_byte_budget=engine.usage(self.fixture.state.root) - 1)
        third = engine.capture(self.fixture.state, envelope)
        self.assertTrue(third["local_complete"])
        self.assertFalse(pathlib.Path(first["archive_path"]).exists())
        self.assertTrue(pathlib.Path(second["archive_path"]).exists())


class ArchiveStreamChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = archive_fixture.AdminArtifactContracts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.archive, manifest_path = self.fixture.backup("streamed")
        self.manifest = read_json(manifest_path)

    def test_corrupt_gzip_member_after_tar_eof_is_not_ignored(self):
        trailing = bytearray(gzip.compress(b"controlled trailing member"))
        trailing[-8] ^= 1
        with self.archive.open("ab") as output:
            output.write(trailing)
        with self.assertRaises(BackupError):
            engine.check_archive(self.archive, self.manifest)

    def test_trailing_decompression_is_bounded_after_tar_eof(self):
        with self.archive.open("ab") as output:
            output.write(gzip.compress(b"x" * (3 * 1024**2)))
        with self.assertRaisesRegex(BackupError, "archive-layout-invalid"):
            engine.check_archive(self.archive, self.manifest)

    def test_duplicate_embedded_manifest_is_rejected(self):
        with tarfile.open(self.archive, "r:gz") as original:
            payload = original.extractfile("database.sql").read()
            embedded = original.extractfile("manifest.json").read()
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, data in (("database.sql", payload), ("manifest.json", embedded), ("manifest.json", embedded)):
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        with self.assertRaisesRegex(BackupError, "archive-layout-invalid"):
            engine.check_archive(self.archive, self.manifest)
