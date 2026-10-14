"""S3 protocol fixture: real pinned SDK requests, streamed reads and multipart."""
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sdk_path = os.environ.get("PAYMENTER_BACKUPS_TEST_SDK")
if sdk_path:
    sys.path.insert(0, sdk_path)
try:
    import boto3
    from botocore.config import Config
except ImportError:
    boto3 = None

from paymenter_backups.common import BackupError, read_json
from paymenter_backups.config import DEFAULTS
from paymenter_backups.storage import clean_environment, client, deliver, destination_test, prefix, retention, upload, whole_hash


@unittest.skipUnless(boto3, "Install requirements-s3.lock SDK for protocol tests")
class S3ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.objects = {}
        self.parts = {}
        self.trace = []
        self.failure_once = False
        self.no_such_upload = False
        self.fail_archive_delete = False
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def answer(self, code, body=b"", headers=None):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            def data(self):
                # SDK compatibility setting when_required produces fixed length
                # uploads to this ordinary S3-compatible protocol fixture.
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))
            def split(self):
                parsed = urllib.parse.urlsplit(self.path)
                return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            def do_PUT(self):
                key, query = self.split()
                fixture.trace.append(("PUT", key, query, dict(self.headers)))
                data = self.data()
                if fixture.failure_once:
                    fixture.failure_once = False
                    return self.answer(503, b"<Error><Code>SlowDown</Code></Error>")
                if "uploadId" in query:
                    fixture.parts[int(query["partNumber"][0])] = data
                else:
                    fixture.objects[key] = data
                self.answer(200, headers={"ETag": '"' + hashlib.md5(data).hexdigest() + '"'})
            def do_POST(self):
                key, query = self.split()
                fixture.trace.append(("POST", key, query, dict(self.headers)))
                self.data()
                if "uploads" in query:
                    fixture.parts = {}
                    return self.answer(200, b"<InitiateMultipartUploadResult><Bucket>fixture</Bucket><Key>fixture</Key><UploadId>fixture-upload</UploadId></InitiateMultipartUploadResult>")
                fixture.objects[key] = b"".join(fixture.parts[number] for number in sorted(fixture.parts))
                self.answer(200, b"<CompleteMultipartUploadResult><Location>fixture</Location><Bucket>fixture</Bucket><Key>fixture</Key><ETag>multipart-not-a-hash</ETag></CompleteMultipartUploadResult>")
            def do_GET(self):
                key, _ = self.split()
                fixture.trace.append(("GET", key, {}, dict(self.headers)))
                if key not in fixture.objects:
                    return self.answer(404, b"<Error><Code>NoSuchKey</Code></Error>")
                self.answer(200, fixture.objects[key], {"ETag": "multipart-not-a-hash"})
            def do_DELETE(self):
                key, query = self.split()
                fixture.trace.append(("DELETE", key, query, dict(self.headers)))
                if "uploadId" in query and fixture.no_such_upload:
                    return self.answer(404, b"<Error><Code>NoSuchUpload</Code></Error>")
                if fixture.fail_archive_delete and key.endswith(".tar.gz"):
                    return self.answer(500, b"<Error><Code>InternalError</Code></Error>")
                fixture.objects.pop(key, None)
                self.answer(204)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        # Tests bind the real SDK to a disposable loopback HTTP fixture. Product
        # config refuses HTTP offsite destinations and uses verified TLS.
        self.s3 = boto3.session.Session().client("s3", endpoint_url="http://127.0.0.1:" + str(self.server.server_port),
                aws_access_key_id="fake-access", aws_secret_access_key="fake-secret", region_name="us-east-1",
                config=Config(proxies={}, retries={"total_max_attempts": 3, "mode": "standard"},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required", s3={"addressing_style": "path"}))
        self.addCleanup(self.s3.close)

    def capture_fixture(self):
        import test_engine
        from paymenter_backups.engine import capture
        fixture = test_engine.Fixture("test_online_excludes_app_and_maintenance")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.settings["mode"] = "database-online"
        journal = capture(fixture.state, fixture.envelope)
        fixture.settings.update(destination="s3", s3_bucket="fixture", s3_prefix="owned")
        return fixture, journal

    def test_delivery_manifest_last_and_idempotency_repairs_missing_manifest(self):
        fixture, journal = self.capture_fixture()
        with patch("paymenter_backups.storage.client", return_value=self.s3):
            deliver(fixture.state, fixture.envelope, journal["run_id"])
            puts = [key for verb, key, _, _ in self.trace if verb == "PUT"]
            self.assertTrue(puts[-2].endswith(".manifest.json"))
            self.assertTrue(puts[-1].endswith("lastmanifest.json"))
            self.trace.clear()
            self.assertTrue(deliver(fixture.state, fixture.envelope, journal["run_id"])["already_delivered"])
            self.assertFalse(any(verb == "PUT" for verb, _, _, _ in self.trace))
            manifest_key = "/fixture/" + prefix(fixture.state, fixture.envelope) + journal["run_id"] + ".manifest.json"
            del self.objects[manifest_key]
            self.assertTrue(deliver(fixture.state, fixture.envelope, journal["run_id"])["remote_delivered"])
            self.assertIn(manifest_key, self.objects)

    def test_completed_multipart_stale_journal_retry_and_failure_keeps_local(self):
        fixture, journal = self.capture_fixture()
        journal["multipart"] = {"bucket": "fixture", "key": prefix(fixture.state, fixture.envelope) + journal["run_id"] + ".tar.gz", "upload_id": "already-completed"}
        fixture.state.save(journal)
        self.no_such_upload = True
        with patch("paymenter_backups.storage.client", return_value=self.s3):
            self.assertTrue(deliver(fixture.state, fixture.envelope, journal["run_id"])["remote_delivered"])
        self.assertNotIn("multipart", read_json(fixture.state.run_path(journal["run_id"]) / "journal.json"))
        with patch("paymenter_backups.storage.client", return_value=self.s3), patch("paymenter_backups.storage.whole_hash", return_value=("corrupt", 0)):
            with self.assertRaisesRegex(BackupError, "s3-readback-mismatch"):
                deliver(fixture.state, fixture.envelope, journal["run_id"])
        self.assertTrue(pathlib.Path(journal["archive_path"]).exists())
        self.assertTrue(pathlib.Path(journal["manifest_path"]).exists())

    def test_retention_owned_scope_last_good_and_lastmanifest_do_not_rewind(self):
        from paymenter_backups.engine import capture
        fixture, first = self.capture_fixture()
        fixture.settings["destination"] = "local"
        second = capture(fixture.state, fixture.envelope)
        fixture.settings["destination"] = "s3"
        fixture.settings["retention_keep"] = 1
        unrelated = "/fixture/unrelated/customer.tar.gz"
        self.objects[unrelated] = b"preserve"
        with patch("paymenter_backups.storage.client", return_value=self.s3):
            deliver(fixture.state, fixture.envelope, second["run_id"])
            deliver(fixture.state, fixture.envelope, first["run_id"])
            lastkey = "/fixture/" + prefix(fixture.state, fixture.envelope) + "lastmanifest.json"
            self.assertEqual(json.loads(self.objects[lastkey])["run_id"], second["run_id"])
            planned = retention(fixture.state, fixture.envelope)
            self.assertEqual(planned["delete"], [first["run_id"]])
            retention(fixture.state, fixture.envelope, True)
            self.assertIn(unrelated, self.objects)
            self.assertTrue(pathlib.Path(second["archive_path"]).exists())
            self.assertFalse(pathlib.Path(first["archive_path"]).exists())
            self.assertEqual(json.loads(self.objects[lastkey])["run_id"], second["run_id"])

    def test_remote_retention_failure_after_marker_delete_resumes(self):
        from paymenter_backups.engine import capture
        fixture, first = self.capture_fixture()
        fixture.settings["destination"] = "local"
        second = capture(fixture.state, fixture.envelope)
        fixture.settings.update(destination="s3", retention_keep=1)
        with patch("paymenter_backups.storage.client", return_value=self.s3):
            deliver(fixture.state, fixture.envelope, first["run_id"])
            deliver(fixture.state, fixture.envelope, second["run_id"])
            self.fail_archive_delete = True
            with self.assertRaisesRegex(BackupError, "retention-remote-delete-failed"):
                retention(fixture.state, fixture.envelope, True)
            journal = read_json(fixture.state.run_path(first["run_id"]) / "journal.json")
            self.assertTrue(journal["retention_delete_intent"]["remote_verified"])
            self.assertTrue(pathlib.Path(first["archive_path"]).exists())
            self.fail_archive_delete = False
            retention(fixture.state, fixture.envelope, True)
            self.assertFalse(pathlib.Path(first["archive_path"]).exists())
            self.assertTrue(pathlib.Path(second["archive_path"]).exists())

    def test_local_retention_failure_between_unlinks_resumes(self):
        from paymenter_backups.engine import capture
        fixture, first = self.capture_fixture()
        fixture.settings.update(destination="local", retention_keep=1)
        second = capture(fixture.state, fixture.envelope)
        unlink = os.unlink
        def fault(path, *args, dir_fd=None, **kwargs):
            # Removal is relative to the verified directory descriptor.
            if dir_fd is not None and path == pathlib.Path(first["archive_path"]).name:
                raise OSError("disposable fault")
            return unlink(path, *args, dir_fd=dir_fd, **kwargs)
        with patch("os.unlink", fault):
            with self.assertRaisesRegex(BackupError, "retention-local-delete-failed"):
                retention(fixture.state, fixture.envelope, True)
        self.assertFalse(pathlib.Path(first["manifest_path"]).exists())
        self.assertTrue(pathlib.Path(first["archive_path"]).exists())
        surviving = pathlib.Path(first["archive_path"])
        original = surviving.read_bytes()
        surviving.write_bytes(original + b"operator replacement")
        with self.assertRaisesRegex(BackupError, "retention-local-verification-failed"):
            retention(fixture.state, fixture.envelope, True)
        self.assertTrue(surviving.exists())
        surviving.write_bytes(original)
        retention(fixture.state, fixture.envelope, True)
        self.assertFalse(pathlib.Path(first["archive_path"]).exists())
        self.assertTrue(pathlib.Path(second["archive_path"]).exists())

    def test_multipart_whole_get_verification_and_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "payload"
            data = os.urandom(9 * 1024 * 1024)
            path.write_bytes(data)
            self.failure_once = True
            upload(self.s3, "fixture", "installation/owned.tar.gz", path)
            self.assertEqual(whole_hash(self.s3, "fixture", "installation/owned.tar.gz"), (hashlib.sha256(data).hexdigest(), len(data)))
            self.assertGreaterEqual(sum(verb == "PUT" for verb, _, _, _ in self.trace), 3)
            self.assertTrue(any(verb == "GET" for verb, _, _, _ in self.trace))
            self.assertTrue(any(verb == "POST" and "uploads" in query for verb, _, query, _ in self.trace))
            self.assertFalse(any("x-amz-checksum-crc32" in {key.lower() for key in headers} for _, _, _, headers in self.trace))

    def test_destination_probe_scoped_and_cleanup(self):
        class State:
            uuid = "fixture-installation"
        envelope = {"settings": dict(DEFAULTS, destination="s3", s3_bucket="fixture", s3_prefix="owned")}
        self.objects["/fixture/unrelated/customer.txt"] = b"preserve"
        with patch("paymenter_backups.storage.client", return_value=self.s3):
            self.assertTrue(destination_test(State(), envelope)["verified"])
        self.assertEqual(self.objects, {"/fixture/unrelated/customer.txt": b"preserve"})
        self.assertTrue(all(key.startswith("/fixture/owned/fixture-installation/probes/") for _, key, _, _ in self.trace))

    def test_explicit_client_ignores_hostile_environment(self):
        hostile = {"AWS_PROFILE": "untrusted", "AWS_ENDPOINT_URL_S3": "http://evil.invalid", "AWS_ACCESS_KEY_ID": "bad-access",
                   "AWS_SECRET_ACCESS_KEY": "bad-secret", "AWS_CA_BUNDLE": "/tmp/bad-ca", "SSLKEYLOGFILE": "/tmp/bad-keylog",
                   "SSL_CERT_FILE": "/tmp/bad-ca", "HTTPS_PROXY": "http://evil.invalid", "PYTHONPATH": "/tmp/untrusted"}
        envelope = {"settings": dict(DEFAULTS, s3_endpoint="https://fixture.invalid", s3_region="eu-west-1", s3_path_style=True),
                    "credentials": {"access_key": "explicit-access", "secret_key": "explicit-secret"}}
        with patch.dict(os.environ, hostile):
            actual = client(envelope)
            try:
                self.assertEqual(actual.meta.endpoint_url, "https://fixture.invalid")
                self.assertEqual(actual._request_signer._credentials.access_key, "explicit-access")
                self.assertEqual(actual.meta.config.proxies, {})
                self.assertEqual(actual.meta.config.request_checksum_calculation, "when_required")
                self.assertTrue(all(name not in os.environ for name in hostile))
            finally:
                actual.close()


if __name__ == "__main__":
    unittest.main()
