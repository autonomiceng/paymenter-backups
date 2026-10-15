"""Synthetic archives only: no capture, Paymenter, SDK, credentials or network."""
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "extension/Backups/runtime/verify_recovery.py"
SPEC = importlib.util.spec_from_file_location("standalone_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def hashed(data):
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class RecoveryVerifierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="standalone-recovery-test-")
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name)
        self.run = "20261014T120000Z-test"
        self.installation = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.prefix = "backups/" + self.installation + "/"
        self.key = {"scheme": "pbkdf2-hmac-sha256-v1", "salt": "a" * 64,
                    "iterations": 600000, "value": "b" * 64}
        self.payloads = {"database.sql": b"-- synthetic SQL\nSELECT 1;\n"}
        self.manifest = {"schema_version": 1, "run_id": self.run, "installation_uuid": self.installation,
                         "mode": "database-online", "created_at": 12345.5,
                         "guarantee": "transactional-database-only", "database_inventory": {
                             "state": "visible-zero", "counts": {name: 0 for name in ("tables", "routines", "events", "triggers")},
                             "current_user_grants": ["GRANT SELECT ON fixture.* TO fixture"],
                             "grants_redaction": "Authentication omitted", "scope": "Visible only"},
                         "excluded_paths": [], "external_state": "External state excluded",
                         "payloads": {name: hashed(data) for name, data in self.payloads.items()}}
        self.publish()

    def make_tar(self, members=None, embedded=None, tail=b""):
        stream = io.BytesIO()
        if members is None:
            members = list(self.payloads.items())
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            if embedded is None:
                embedded = {key: value for key, value in self.manifest.items() if key != "archive"}
            for name, data in members + [("manifest.json", encoded(embedded))]:
                item = tarfile.TarInfo("./" + name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        return gzip.compress(stream.getvalue() + tail, mtime=0)

    def publish(self, archive=None):
        original = {key: value for key, value in self.manifest.items() if key != "archive"}
        archive = archive if archive is not None else self.make_tar(embedded=original)
        self.manifest["archive"] = hashed(archive)
        archive_ciphertext = b"synthetic encrypted archive transport"
        recovery_ciphertext = b"synthetic encrypted recovery transport"
        archive_object = dict(hashed(archive_ciphertext), name=self.prefix + self.run + ".tar.gz.gpg")
        recovery_object = dict(hashed(recovery_ciphertext), name=self.prefix + self.run + ".recovery.json.gpg")
        self.descriptor = {"version": 1, "format": verifier.FORMAT, "installation_uuid": self.installation,
                           "run_id": self.run, "key": self.key,
                           "objects": {"archive": archive_object, "recovery": recovery_object}}
        self.recovery = {"version": 1, "format": verifier.FORMAT, "installation_uuid": self.installation,
                         "run_id": self.run, "key": self.key,
                         "scope": {"endpoint": "", "region": "fixture-region", "bucket": "fixture-bucket",
                                   "prefix": self.prefix, "path_style": True},
                         "archive": archive_object, "manifest": self.manifest}
        self.args = types.SimpleNamespace(descriptor=self.base / "descriptor.json",
                                          archive_ciphertext=self.base / "archive.gpg",
                                          recovery_ciphertext=self.base / "recovery.gpg",
                                          recovery=self.base / "recovery.json", archive=self.base / "backup.tar.gz")
        self.args.archive.write_bytes(archive)
        self.args.archive_ciphertext.write_bytes(archive_ciphertext)
        self.args.recovery_ciphertext.write_bytes(recovery_ciphertext)
        self.write_documents()

    def write_documents(self):
        self.args.descriptor.write_bytes(encoded(self.descriptor))
        self.args.recovery.write_bytes(encoded(self.recovery))

    def assert_refused(self, code=None):
        with self.assertRaises(verifier.VerificationError) as caught:
            verifier.verify(self.args)
        if code:
            self.assertEqual(str(caught.exception), code)

    def test_database_and_full_synthetic_archives_verify(self):
        self.assertEqual(verifier.verify(self.args)["verification"], "plaintext-content-verified")
        self.payloads["app.tar"] = b"synthetic application payload (not a capture)"
        self.manifest.update(mode="full-quiesced", guarantee="database-and-full-app-checkpoint",
                             payloads={name: hashed(data) for name, data in self.payloads.items()})
        self.publish()
        self.assertEqual(verifier.verify(self.args)["mode"], "full-quiesced")

    def test_copied_script_runs_without_original_repository_or_sdk(self):
        copied = self.base / "verify_recovery.py"
        shutil.copyfile(SCRIPT, copied)
        command = [sys.executable, "-I", str(copied)]
        for flag in ("descriptor", "archive-ciphertext", "recovery-ciphertext", "recovery", "archive"):
            command += ["--" + flag, str(getattr(self.args, flag.replace("-", "_")))]
        result = subprocess.run(command, cwd=self.base, capture_output=True, text=True, timeout=10,
                                env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verification"], "plaintext-content-verified")
        self.assertTrue(json.loads(result.stdout)["gpg_completion_required"])

    def test_transport_only_is_explicitly_untrusted(self):
        self.args.recovery = self.args.archive = None
        result = verifier.verify(self.args)
        self.assertEqual(result["verification"], "untrusted-transport-only")
        self.assertNotIn("verified", result)

    def test_recovery_document_can_be_bound_before_archive_decryption(self):
        self.args.archive = None
        result = verifier.verify(self.args)
        self.assertEqual(result["verification"], "recovery-document-bound")
        self.assertTrue(result["gpg_completion_required"])

    def test_each_downloaded_ciphertext_must_match_exact_digest_and_size(self):
        for field in ("archive_ciphertext", "recovery_ciphertext"):
            with self.subTest(field=field):
                path = getattr(self.args, field)
                before = path.read_bytes()
                path.write_bytes(b"X" + before[1:])
                self.assert_refused("recovery-object-mismatch")
                path.write_bytes(before + b"X")
                self.assert_refused("recovery-object-mismatch")
                path.write_bytes(before)

    def test_descriptor_recovery_exact_schema_names_and_fingerprint(self):
        for target, field, value in (("descriptor", "extra", "forbidden"),
                                     ("descriptor", "version", True),
                                     ("recovery", "extra", "forbidden"),
                                     ("recovery", "run_id", "different-run")):
            with self.subTest(target=target, field=field):
                saved = copy.deepcopy(getattr(self, target))
                getattr(self, target)[field] = value
                self.write_documents()
                self.assert_refused()
                setattr(self, target, saved)
        self.descriptor["objects"]["archive"]["name"] = "../" + self.prefix + self.run + ".tar.gz.gpg"
        self.write_documents()
        self.assert_refused()

    def test_scope_and_archive_bindings_cannot_be_swapped(self):
        for change in (lambda: self.recovery["archive"].update(sha256="c" * 64),
                       lambda: self.recovery["key"].update(value="c" * 64),
                       lambda: self.recovery["scope"].update(prefix="other/")):
            self.publish()
            # Separate documents share no mutable object identities on disk.
            self.recovery = copy.deepcopy(self.recovery)
            change()
            self.write_documents()
            self.assert_refused()

    def test_schema_is_bounded_canonical_and_rejects_duplicate_keys(self):
        original = self.args.descriptor.read_bytes()
        for data in (original + b" ", b" " * (verifier.DESCRIPTOR_LIMIT + 1),
                     original.replace(b'"version":1', b'"version":1,"version":1'),
                     original.replace(b'"version":1', b'"version":NaN')):
            self.args.descriptor.write_bytes(data)
            self.assert_refused()
        self.args.descriptor.write_bytes(original)
        self.args.recovery.write_bytes(b" " * (verifier.DOCUMENT_LIMIT + 1))
        self.assert_refused()

    def test_plaintext_outer_digest_and_size_are_required(self):
        self.args.archive.write_bytes(self.args.archive.read_bytes() + b"extra")
        self.assert_refused("recovery-object-mismatch")

    def test_gzip_truncation_and_crc_checked_after_tar_end(self):
        original = self.args.archive.read_bytes()
        changed = bytearray(original)
        changed[-8] ^= 1
        for archive in (original[:-1], bytes(changed)):
            with self.subTest(size=len(archive)):
                self.publish(archive)
                with self.assertRaises((EOFError, OSError, verifier.VerificationError)):
                    verifier.verify(self.args)

    def test_hidden_tar_content_after_end_is_rejected(self):
        self.publish(self.make_tar(tail=b"hidden member"))
        self.assert_refused("archive-layout-invalid")

    def test_unexpected_duplicate_traversal_and_missing_payloads(self):
        data = self.payloads["database.sql"]
        for members in ([("extra", b"x"), ("database.sql", data)],
                        [("database.sql", data), ("database.sql", data)],
                        [("../database.sql", data)], []):
            self.publish(self.make_tar(members=members, embedded={k: v for k, v in self.manifest.items() if k != "archive"}))
            self.assert_refused("archive-layout-invalid")

    def test_embedded_manifest_and_payload_hashes_must_match(self):
        embedded = {k: v for k, v in self.manifest.items() if k != "archive"}
        embedded = dict(embedded, external_state="changed")
        self.publish(self.make_tar(embedded=embedded))
        self.assert_refused("archive-manifest-mismatch")
        self.publish(self.make_tar(members=[("database.sql", b"X" * len(self.payloads["database.sql"]))],
                                   embedded={k: v for k, v in self.manifest.items() if k != "archive"}))
        self.assert_refused("archive-payload-mismatch")

    def test_links_and_unbounded_padding_are_rejected(self):
        raw = bytearray(gzip.decompress(self.args.archive.read_bytes()))
        item = tarfile.TarInfo("./database.sql")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/passwd"
        raw[512:1024] = item.tobuf(format=tarfile.GNU_FORMAT)
        self.publish(gzip.compress(raw, mtime=0))
        self.assert_refused("archive-layout-invalid")
        self.publish(self.make_tar(tail=b"\0" * (2 * verifier.MANIFEST_LIMIT + 1)))
        self.assert_refused("archive-layout-invalid")

    def test_final_symlink_and_file_change_refuse(self):
        target = self.base / "real-archive"
        self.args.archive.rename(target)
        self.args.archive.symlink_to(target)
        with self.assertRaises(OSError):
            verifier.verify(self.args)
        path = self.args.recovery
        with self.assertRaises(verifier.VerificationError) as caught:
            with verifier.file_reader(path):
                path.write_bytes(path.read_bytes() + b" ")
        self.assertEqual(str(caught.exception), "recovery-file-changed")


class OfflineWheelTests(unittest.TestCase):
    def test_offline_wheelhouse_never_contacts_metadata_or_falls_back(self):
        spec = importlib.util.spec_from_file_location("release_builder", ROOT / "tools/build_release.py")
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(builder.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            wheel = pathlib.Path(directory) / "python_dateutil-2.9.0.post0-py2.py3-none-any.whl"
            contents = b"synthetic wheel bytes"
            wheel.write_bytes(contents)
            permitted = {hashlib.sha256(contents).hexdigest()}
            self.assertEqual(builder.wheel_contents("python-dateutil", "2.9.0.post0", permitted, directory), contents)
            wheel.write_bytes(b"corrupt")
            with self.assertRaises(RuntimeError):
                builder.wheel_contents("python-dateutil", "2.9.0.post0", permitted, directory)
            with self.assertRaises(RuntimeError):
                builder.wheel_contents("missing", "1.0", permitted, directory)


if __name__ == "__main__":
    unittest.main()
