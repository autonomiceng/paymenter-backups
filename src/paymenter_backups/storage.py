"""Explicit S3 configuration and complete streamed object verification."""
import hashlib
import contextlib
import json
import os
import pathlib
import re
import stat
import time
import uuid

from . import artifacts, encryption, remote_artifacts
from .common import BackupError, atomic_json, read_json
from .engine import verify


def clean_environment():
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith(("AWS_", "BOTO_", "PYTHON", "OPENSSL_")) or upper in {
            "SSLKEYLOGFILE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        }:
            os.environ.pop(name, None)


def client(envelope):
    clean_environment()
    credentials = envelope.get("credentials", {})
    if not credentials.get("access_key") or not credentials.get("secret_key"):
        raise BackupError("s3-credentials-required")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise BackupError("isolated-s3-sdk-missing") from None
    settings = envelope["settings"]
    config = Config(region_name=settings["s3_region"],
                    connect_timeout=settings["s3_timeout"], read_timeout=settings["s3_timeout"],
                    retries={"mode": "standard", "total_max_attempts": settings["s3_retries"]},
                    proxies={}, s3={"addressing_style": "path" if settings["s3_path_style"] else "virtual"},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required")
    try:
        return boto3.session.Session().client("s3", config=config,
                    endpoint_url=settings["s3_endpoint"] or None, region_name=settings["s3_region"],
                    aws_access_key_id=credentials["access_key"], aws_secret_access_key=credentials["secret_key"],
                    aws_session_token=credentials.get("session_token") or None, verify=True)
    except Exception:
        raise BackupError("s3-client-failed") from None


def prefix(state, envelope):
    base = envelope["settings"]["s3_prefix"].strip("/")
    if any(part in ("", ".", "..") for part in base.split("/")) or "\\" in base:
        raise BackupError("s3-prefix-invalid")
    return base + "/" + state.uuid + "/"


def remote_scope(state, envelope):
    settings = envelope["settings"]
    return {"endpoint": settings["s3_endpoint"].rstrip("/"), "region": settings["s3_region"],
            "bucket": settings["s3_bucket"], "prefix": prefix(state, envelope), "path_style": bool(settings["s3_path_style"])}


def destination_id(state, envelope):
    value = remote_scope(state, envelope) if envelope["settings"]["destination"] == "s3" else {"local": envelope["settings"].get("local_path") or str(state.default_destination())}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:32]


def scope_matches(state, envelope, item):
    if envelope["settings"]["destination"] != "s3":
        return False
    remote = item.get("remote") or item.get("delivery_intent") or {}
    scope = remote_scope(state, envelope)
    return remote_artifacts.owned(remote, scope, item["run_id"])


def owned_manifest(state, item):
    manifest = item.get("manifest")
    if not isinstance(manifest, dict):
        intent = item.get("delete_intent") or item.get("retention_delete_intent") or {}
        manifest = intent.get("manifest")
    if not isinstance(manifest, dict):
        manifest = artifacts.read_manifest(state, item, artifacts.locations(state, item))
    artifacts.validate_manifest(state, item, manifest)
    return manifest


def remote_readiness(state, envelope, item):
    if not item.get("remote_delivered"):
        return "none"
    if not (item.get("remote") or {}).get("scope"):
        return "unbound"
    if envelope is not None and not scope_matches(state, envelope, item):
        return "scope-changed"
    observation = item.get("remote_observation") or {}
    return observation.get("status", "delivered")


def observe_remote(state, envelope, item):
    """Cheap pair presence and bounded manifest proof; archive bytes stay unread."""
    remote = item.get("remote") or item["delivery_intent"]
    if remote_artifacts.format_of(remote) == encryption.FORMAT:
        return remote_artifacts.observe(state, envelope, item)
    manifest = owned_manifest(state, item)
    s3 = client(envelope)
    expected = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    result = "present"
    try:
        for key, length in ((remote["archive_key"], manifest["archive"]["size"]), (remote["manifest_key"], len(expected))):
            try:
                size = s3.head_object(Bucket=remote["bucket"], Key=key)["ContentLength"]
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                    result = "missing"
                    break
                raise
            if size != length:
                result = "damaged"
                break
        if result == "present" and hash_or_missing(s3, remote["bucket"], remote["manifest_key"], len(expected)) != (hashlib.sha256(expected).hexdigest(), len(expected)):
            result = "damaged"
        item["remote_observation"] = {"status": result, "checked_at": time.time()}
        if result != "present":
            item.pop("remote_verification", None)
        state.save(item)
        return result
    except Exception as error:
        code = error.code if isinstance(error, BackupError) else "remote-check-failed"
        item["remote_observation"] = {"status": "unknown", "checked_at": time.time(), "error": code}
        state.save(item)
        raise BackupError(code) from None


def check_remote_once(state, envelope, item, checked):
    key = item["run_id"]
    if key in checked:
        if checked[key] is not None:
            raise BackupError(checked[key])
        return
    try:
        with checked_remote(state, envelope, item, record=True):
            pass
        checked[key] = None
    except BackupError as error:
        checked[key] = error.code
        raise


class RemoteReader:
    """Hash and bound each remote read, including bytes left after tar EOF."""
    def __init__(self, s3, remote, archive):
        self.body = s3.get_object(Bucket=remote["bucket"], Key=remote["archive_key"])["Body"]
        self.expected = archive
        self.hasher = hashlib.sha256()
        self.size = 0

    def read(self, size=-1):
        maximum = min(1024 * 1024, self.expected["size"] - self.size + 1)
        data = self.body.read(maximum if size < 0 else min(size, maximum))
        self.size += len(data)
        if self.size > self.expected["size"]:
            raise BackupError("remote-archive-size-mismatch")
        self.hasher.update(data)
        return data

    def tell(self):
        return self.size

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if kind is None:
                for _ in iter(lambda: self.read(1024 * 1024), b""):
                    pass
                if (self.size, self.hasher.hexdigest()) != (self.expected["size"], self.expected["sha256"]):
                    raise BackupError("remote-archive-checksum-mismatch")
        finally:
            self.body.close()


@contextlib.contextmanager
def checked_remote(state, envelope, item, record=False, directory=None, directory_fd=None):
    """Bounded private spool, complete object/manifest/archive check, no publication."""
    from .engine import DiskReserve, check_archive, output_limit, space
    if remote_artifacts.format_of(item.get("remote") or item.get("delivery_intent") or {}) == encryption.FORMAT:
        with remote_artifacts.checked_remote(state, envelope, item, record, directory, directory_fd) as result:
            yield result
        return
    if not scope_matches(state, envelope, item):
        raise BackupError("remote-scope-changed" if (item.get("remote") or {}).get("scope") else "remote-location-unbound")
    manifest = owned_manifest(state, item)
    remote = item.get("remote") or item["delivery_intent"]
    s3 = client(envelope)
    temporary = pathlib.Path(directory) / (".remote-" + uuid.uuid4().hex + ".partial") if directory else None
    temporary_fd = None
    status = "unknown"
    try:
        expected = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
        manifest_hash = hash_or_missing(s3, remote["bucket"], remote["manifest_key"], artifacts.MANIFEST_LIMIT)
        if manifest_hash != (hashlib.sha256(expected).hexdigest(), len(expected)):
            status = "missing" if manifest_hash is None else "damaged"
            raise BackupError("remote-copy-" + status)
        if not directory:
            try:
                check_archive(lambda: RemoteReader(s3, remote, manifest["archive"]), manifest)
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                    status = "missing"
                    raise BackupError("remote-copy-missing") from None
                status = "damaged"
                raise
            status = "delivered"
            if record:
                item["remote_observation"] = {"status": status, "checked_at": time.time()}
                item["remote_verification"] = {"verified_at": time.time(), "source": "s3-verify", "sha256": manifest["archive"]["sha256"], "scope": remote["scope"]}
                state.save(item)
            yield None, manifest
            return
        available = output_limit(state, envelope["settings"], pathlib.Path(directory))
        if manifest["archive"]["size"] > available:
            raise BackupError("local-byte-budget-exceeded")
        try:
            body = s3.get_object(Bucket=remote["bucket"], Key=remote["archive_key"])["Body"]
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                status = "missing"
                raise BackupError("remote-copy-missing") from None
            raise
        hasher = hashlib.sha256()
        size = 0
        try:
            if directory_fd is None:
                raise BackupError("artifact-location-invalid")
            item["retrieve_work"] = {"temporary": temporary.name, "scope": remote["scope"], "identity": None}
            state.save(item)
            temporary_fd = os.open(temporary.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
            item["retrieve_work"]["identity"] = artifacts.identity(os.fstat(temporary_fd))
            state.save(item)
            reserve = DiskReserve(state, envelope["settings"], pathlib.Path(directory))
            with os.fdopen(os.dup(temporary_fd), "wb") as output:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > manifest["archive"]["size"] or size > available:
                        raise BackupError("remote-archive-size-mismatch")
                    output.write(chunk)
                    output.flush()
                    reserve.check(size)
                    hasher.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            reserve.check(size, force=True)
        finally:
            body.close()
        if (hasher.hexdigest(), size) != (manifest["archive"]["sha256"], manifest["archive"]["size"]):
            status = "damaged"
            raise BackupError("remote-archive-checksum-mismatch")
        def reader():
            os.lseek(temporary_fd, 0, os.SEEK_SET)
            return os.fdopen(os.dup(temporary_fd), "rb")
        check_archive(reader, manifest)
        checked_identity = artifacts.identity(os.fstat(temporary_fd))
        if artifacts.identity(os.stat(temporary.name, dir_fd=directory_fd, follow_symlinks=False)) != checked_identity:
            raise BackupError("retrieve-spool-changed")
        item["retrieve_work"]["verified_identity"] = checked_identity
        state.save(item)
        space(state, envelope["settings"])
        status = "delivered"
        if record:
            item["remote_observation"] = {"status": status, "checked_at": time.time()}
            item["remote_verification"] = {"verified_at": time.time(), "source": "s3-verify", "sha256": manifest["archive"]["sha256"], "scope": remote["scope"]}
            state.save(item)
        yield temporary, manifest
    except BackupError as error:
        if record:
            item["remote_observation"] = {"status": status if status != "unknown" else "unknown", "checked_at": time.time(), "error": error.code}
            item.pop("remote_verification", None)
            state.save(item)
        raise
    except Exception:
        if record:
            item["remote_observation"] = {"status": "unknown", "checked_at": time.time(), "error": "remote-check-failed"}
            item.pop("remote_verification", None)
            state.save(item)
        raise BackupError("remote-check-failed") from None
    finally:
        work_done = temporary is None
        if temporary:
            if directory_fd is not None:
                try:
                    named = os.stat(temporary.name, dir_fd=directory_fd, follow_symlinks=False)
                    if temporary_fd is not None and artifacts.identity(named) == artifacts.identity(os.fstat(temporary_fd)):
                        os.unlink(temporary.name, dir_fd=directory_fd)
                        work_done = True
                except FileNotFoundError:
                    work_done = True
        if temporary_fd is not None:
            os.close(temporary_fd)
        if work_done and directory is not None and item.get("retrieve_work", {}).get("temporary") == temporary.name:
            item.pop("retrieve_work", None)
            state.save(item)


def whole_hash(s3, bucket, key, limit=None):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    result = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(min(1024 * 1024, limit - size + 1) if limit is not None else 1024 * 1024)
            if not chunk:
                break
            result.update(chunk)
            size += len(chunk)
            if limit is not None and size > limit:
                raise BackupError("remote-object-too-large")
    finally:
        body.close()
    return result.hexdigest(), size


def upload(s3, bucket, key, path, journal=None, state=None):
    opener = path if callable(path) else (lambda: open(path, "rb"))
    with opener() as stream:
        size = os.fstat(stream.fileno()).st_size
        if size <= 8 * 1024**2:
            s3.put_object(Bucket=bucket, Key=key, Body=stream, ContentLength=size)
            return
        # Explicit multipart journal ensures failures remain discoverable.
        upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
        if journal is not None:
            journal["multipart"] = {"bucket": bucket, "key": key, "upload_id": upload_id,
                                     "scope": journal["delivery_intent"]["scope"],
                                     "format": remote_artifacts.format_of(journal["delivery_intent"]),
                                     **({"key_fingerprint": journal["delivery_intent"]["key_fingerprint"]} if remote_artifacts.format_of(journal["delivery_intent"]) == encryption.FORMAT else {})}
            state.save(journal)
        parts = []
        try:
            number = 1
            part_bytes = max(8 * 1024**2, (size + 9998) // 9999)
            while True:
                chunk = stream.read(part_bytes)
                if not chunk:
                    break
                result = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id,
                                        PartNumber=number, Body=chunk, ContentLength=len(chunk))
                parts.append({"PartNumber": number, "ETag": result["ETag"]})
                number += 1
            s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
            if journal is not None:
                journal.pop("multipart", None)
                state.save(journal)
        except Exception:
            try:
                s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                if journal is not None:
                    journal.pop("multipart", None)
                    state.save(journal)
            except Exception:
                pass
            raise


def recover_legacy_delivery_source(state, item):
    """Explicit delivery may recover a checked source from a legacy removal intent."""
    intent = item.get("retention_delete_intent")
    if not intent or item.get("copy_state_version") == 2 or item.get("retained_deleted"):
        return
    from .engine import check_archive
    manifest = intent.get("manifest")
    artifacts.validate_manifest(state, item, manifest)
    location = artifacts.locations(state, item)
    with artifacts.open_owned(location, location[1].name) as (descriptor, info):
        artifact = artifacts.Artifact(item, manifest, descriptor, info)
        if info.st_size != manifest["archive"]["size"] or artifact.digest() != manifest["archive"]["sha256"]:
            raise BackupError("legacy-local-source-damaged")
        check_archive(artifact.reader, manifest)
        if not artifact.unchanged():
            raise BackupError("legacy-local-source-damaged")
    repaired = False
    try:
        if artifacts.read_manifest(state, item, location) != manifest:
            raise BackupError("legacy-local-source-damaged")
    except BackupError as error:
        if error.code != "artifact-missing":
            raise
        directory_fd = artifacts.open_directory(location[0])
        name = ".legacy-manifest-" + uuid.uuid4().hex
        try:
            current = os.fstat(directory_fd)
            if (current.st_dev, current.st_ino) != location.key:
                raise BackupError("artifact-location-invalid")
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            with os.fdopen(descriptor, "wb") as output:
                output.write(json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(name, location[2].name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
            except FileExistsError:
                raise BackupError("legacy-manifest-replaced") from None
            os.fsync(directory_fd)
            repaired = True
        finally:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)
    item.update({"manifest": manifest, "copy_state_version": 2, "local_complete": True,
                 "legacy_retention_recovery": {"at": time.time(), "metadata_repaired": repaired}})
    item.pop("retention_delete_intent", None)
    state.save(item)


def deliver(state, envelope, run_id, envelope_loader=None, expected_scope=None):
    """Delivery and optional staging removal share one run-lock attempt.

    Refresh key selection under the lock. Staging cleanup always requires a
    complete current proof of the recorded format, including decryption.
    """
    with state.lock():
        if envelope_loader is not None:
            envelope = envelope_loader()
        if expected_scope is not None and (envelope['settings']['destination'] != 's3'
                or expected_scope != remote_scope(state, envelope)):
            raise BackupError('delivery-destination-changed')
        item = artifacts.journal(state, run_id)
        if envelope['settings']['destination'] == 's3' and item.get('remote_delivered') and scope_matches(state, envelope, item):
            # Remote-only retries cannot require a local source.
            try:
                with checked_remote(state, envelope, item, record=True):
                    pass
            except BackupError:
                if not item.get('local_complete') or item.get('staging_removal_intent'):
                    raise
            else:
                staging_removal(state, envelope, item, remote_proven=True)
                return {'run_id': run_id, 'remote_delivered': True, 'already_delivered': True,
                        'local_complete': item.get('local_complete', False)}
        result = deliver_payload(state, envelope, run_id)
        item = artifacts.journal(state, run_id)
        if result.get('remote_delivered'):
            staging_removal(state, envelope, item)
            result['local_complete'] = item.get('local_complete', False)
        return result


def deliver_payload(state, envelope, run_id):
    """Publish the frozen recorded format; caller holds run.lock."""
    item = artifacts.journal(state, run_id)
    if envelope['settings']['destination'] != 's3':
        return _deliver_plaintext(state, envelope, run_id, locked=True)
    remote = item.get('remote') or item.get('delivery_intent')
    if item.get('multipart') and not item['multipart'].get('scope'):
        raise BackupError('multipart-location-unbound')
    format = remote_artifacts.format_of(remote) if remote else (
        remote_artifacts.multipart_format(item['multipart']) if item.get('multipart') else
        encryption.FORMAT if encryption.enabled(envelope) else remote_artifacts.PLAINTEXT)
    if format == encryption.FORMAT:
        manifest = owned_manifest(state, item) if item.get('remote_delivered') else verify(state, run_id, show=True)
        item = artifacts.journal(state, run_id)
        try:
            return remote_artifacts.deliver(state, envelope, item, manifest)
        except BackupError as error:
            item['delivery_error'] = error.code
            state.save(item)
            raise
    if not item.get('remote_delivered') and (encryption.enabled(envelope) or not envelope['settings'].get('plaintext_offsite_acknowledged')):
        raise BackupError('legacy-plaintext-delivery-blocked')
    return _deliver_plaintext(state, envelope, run_id, locked=True)


def _deliver_plaintext(state, envelope, run_id, locked=False):
    with contextlib.nullcontext() if locked else state.lock():
        source = artifacts.journal(state, run_id)
        old = source.get("multipart")
        if old and envelope["settings"]["destination"] == "s3":
            if not old.get("scope"):
                raise BackupError("multipart-location-unbound")
            if (old["scope"] != remote_scope(state, envelope) or old.get("bucket") != envelope["settings"]["s3_bucket"]
                    or old.get("key") != prefix(state, envelope) + run_id + ".tar.gz"):
                raise BackupError("multipart-destination-mismatch")
        recover_legacy_delivery_source(state, source)
        manifest = verify(state, run_id, show=True)
        journal = artifacts.journal(state, run_id)
        if envelope["settings"]["destination"] != "s3":
            return {"run_id": run_id, "local_complete": True, "remote_delivered": False}
        bucket = envelope["settings"]["s3_bucket"]
        base = prefix(state, envelope)
        archive_key, manifest_key = base + run_id + ".tar.gz", base + run_id + ".manifest.json"
        encoded = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
        target = {"bucket": bucket, "archive_key": archive_key, "manifest_key": manifest_key,
                  "scope": remote_scope(state, envelope)}
        recorded = journal.get("delivery_intent") or journal.get("remote")
        if recorded:
            if any(recorded.get(key) != target[key] for key in ("bucket", "archive_key", "manifest_key")) or (recorded.get("scope") and recorded["scope"] != target["scope"]):
                raise BackupError("delivery-destination-changed")
            if not recorded.get("scope"):
                journal["legacy_remote_location_unrecorded"] = True
        s3 = client(envelope)
        journal["manifest"] = manifest
        journal["delivery_intent"] = target
        state.save(journal)
        try:
            # Retried delivery first disposes of explicitly recorded unfinished work.
            old = journal.get("multipart")
            if old:
                if old.get("scope") != target["scope"] or old["bucket"] != bucket or old["key"] != archive_key:
                    raise BackupError("multipart-destination-mismatch")
                try:
                    s3.abort_multipart_upload(Bucket=bucket, Key=archive_key, UploadId=old["upload_id"])
                except Exception as error:
                    if getattr(error, "response", {}).get("Error", {}).get("Code") != "NoSuchUpload":
                        raise
                journal.pop("multipart")
                state.save(journal)
            if journal.get("remote_delivered") and journal.get("remote", {}).get("bucket") == bucket and journal["remote"].get("archive_key") == archive_key:
                try:
                    verified = whole_hash(s3, bucket, archive_key, limit=manifest["archive"]["size"]) == (manifest["archive"]["sha256"], manifest["archive"]["size"])
                    verified = verified and whole_hash(s3, bucket, manifest_key, limit=len(encoded)) == (hashlib.sha256(encoded).hexdigest(), len(encoded))
                    if verified:
                        journal.update({"remote": dict(target, delivered_at=time.time()), "remote_observation": {"status": "delivered", "checked_at": time.time()},
                                        "remote_verification": {"verified_at": time.time(), "source": "s3-delivery", "sha256": manifest["archive"]["sha256"], "scope": target["scope"]}})
                        journal.pop("delivery_intent", None)
                        state.save(journal)
                        return {"run_id": run_id, "remote_delivered": True, "already_delivered": True}
                except Exception:
                    pass
            with artifacts.open_archive(state, run_id, journal) as artifact:
                # Readback below compares the remote object with this manifest.
                if artifact.manifest != manifest:
                    raise BackupError("archive-changed-during-delivery")
                upload(s3, bucket, archive_key, artifact.reader, journal, state)
            if whole_hash(s3, bucket, archive_key, limit=manifest["archive"]["size"]) != (manifest["archive"]["sha256"], manifest["archive"]["size"]):
                raise BackupError("s3-readback-mismatch")
            s3.put_object(Bucket=bucket, Key=manifest_key, Body=encoded, ContentLength=len(encoded))
            if whole_hash(s3, bucket, manifest_key, limit=len(encoded)) != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
                raise BackupError("s3-manifest-readback-mismatch")
            newer = [item for item in state.journals() if item.get("remote_delivered") and not item.get("retained_deleted")
                     and item["created_at"] > journal["created_at"] and item.get("remote", {}).get("bucket") == bucket
                     and item["remote"].get("manifest_key") == base + item["run_id"] + ".manifest.json"]
            if not newer:
                publish_pointer(state, s3, dict(journal, remote=target))
            journal.update({"remote_delivered": True, "delivery_error": None,
                            "remote": dict(target, delivered_at=time.time()),
                            "remote_observation": {"status": "delivered", "checked_at": time.time()},
                            "remote_verification": {"verified_at": time.time(), "source": "s3-delivery", "sha256": manifest["archive"]["sha256"], "scope": target["scope"]}})
            journal.pop("delivery_intent", None)
            journal.pop("remote_pruned", None)
            state.save(journal)
            return {"run_id": run_id, "remote_delivered": True}
        except Exception as error:
            journal["delivery_error"] = error.code if isinstance(error, BackupError) else "s3-delivery-failed"
            journal["remote_observation"] = {"status": "unknown", "checked_at": time.time(), "error": journal["delivery_error"]}
            state.save(journal)
            raise BackupError(journal["delivery_error"]) from None


def staging_removal(state, envelope, item, remote_proven=False):
    """The same-run remote exception applies only to staging/expired retrieval.

    Intent precedes the manifest-first descriptor-relative unlinks. Every retry
    obtains a new remote proof; neither remote_delivered nor HEAD authorizes it.
    Caller holds run.lock; checked_remote is the encryption proof seam.
    """
    settings = envelope['settings']
    if settings.get('settings_version', 1) != 2 or settings['keep_local'] or not settings['upload_s3']:
        return False
    if item.get('local_download_until', 0) > time.time():
        return False
    if not item.get('local_complete') and not item.get('staging_removal_intent'):
        return False
    if artifacts.deleting(item) or item.get('local_delete_intent') or item.get('remote_delete_intent'):
        raise BackupError('staging-removal-conflict')
    if not item.get('remote_delivered') or not scope_matches(state, envelope, item):
        raise BackupError('staging-removal-remote-proof-required')
    if not remote_proven:
        with checked_remote(state, envelope, item, record=True):
            pass
    manifest = owned_manifest(state, item)
    intent = item.get('staging_removal_intent')
    # Verify present files before creating an intent; an absent file only counts
    # as a resumed unlink when a previous durable intent established ownership.
    check_local(state, item, manifest, intent, 'staging-removal')
    if not intent:
        intent = {'at': time.time(), 'manifest': manifest, 'scope': remote_scope(state, envelope)}
        item['staging_removal_intent'] = intent
        state.save(item)
    elif intent.get('scope') != remote_scope(state, envelope) or intent.get('manifest') != manifest:
        raise BackupError('staging-removal-remote-proof-required')
    try:
        remove_local(state, item, manifest, intent, 'staging-removal')
        item.update(local_complete=False, staging_removed_at=time.time(),
                    local_pruned={'at': time.time(), 'reason': 'staging-removal'})
        item.pop('verification', None)
        item.pop('staging_removal_intent', None)
        item.pop('staging_removal_error', None)
        # Keep remote ownership/manifest and run identity; this is not Delete.
        finish_copy_state(state, item)
        return True
    except BackupError as error:
        item['staging_removal_error'] = error.code
        state.save(item)
        raise


def destination_test(state, envelope, envelope_loader=None, locked=False):
    with contextlib.nullcontext() if locked else state.lock():
        if envelope_loader is not None:
            envelope = envelope_loader()
        return _destination_test(state, envelope)


def _destination_test(state, envelope):
    if envelope["settings"]["destination"] == "local":
        from .engine import destination
        directory = destination(state, envelope)
        probe = directory / (".probe-" + uuid.uuid4().hex)
        try:
            atomic_json(probe, {"installation_uuid": state.uuid})
            if read_json(probe)["installation_uuid"] != state.uuid:
                raise BackupError("destination-probe-failed")
        finally:
            probe.unlink(missing_ok=True)
        return {"destination": "local", "destination_id": destination_id(state, envelope), "verified": True}
    if encryption.enabled(envelope):
        return remote_artifacts.destination_test(state, envelope)
    s3 = client(envelope)
    bucket = envelope["settings"]["s3_bucket"]
    key = prefix(state, envelope) + "probes/" + uuid.uuid4().hex
    payload = os.urandom(1024)
    failure = None
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentLength=len(payload))
        if whole_hash(s3, bucket, key) != (hashlib.sha256(payload).hexdigest(), len(payload)):
            raise BackupError("destination-readback-mismatch")
    except Exception as error:
        failure = error.code if isinstance(error, BackupError) else "destination-test-failed"
    finally:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:
            failure = "destination-probe-cleanup-failed"
    if failure:
        raise BackupError(failure)
    return {"destination": "s3", "destination_id": destination_id(state, envelope), "verified": True, "format": remote_artifacts.PLAINTEXT}


def pointer_bytes(state, item):
    remote = item["remote"]
    value = {"installation_uuid": state.uuid, "run_id": item["run_id"], "manifest_key": remote["manifest_key"]}
    if remote_artifacts.format_of(remote) == encryption.FORMAT:
        value.update(format=encryption.FORMAT, descriptor_sha256=remote["descriptor_digest"]["sha256"])
        return remote_artifacts.encode(value)
    return json.dumps(value).encode()


def publish_pointer(state, s3, item):
    """Read, compare and verify discovery metadata under the caller's run lock."""
    remote = item['remote']
    bucket = remote['bucket']
    key = remote['scope']['prefix'] + 'lastmanifest.json'
    try:
        body = s3.get_object(Bucket=bucket, Key=key)['Body']
    except Exception as error:
        if getattr(error, 'response', {}).get('Error', {}).get('Code') not in ('NoSuchKey', '404', 'NotFound'):
            raise
    else:
        try:
            data = body.read(65537)
        finally:
            body.close()
        try:
            if len(data) > 65536:
                raise ValueError()
            current = json.loads(data)
            if current['installation_uuid'] != state.uuid:
                raise ValueError()
            previous = artifacts.journal(state, current['run_id'])
            previous_remote = previous.get('remote') or previous.get('delivery_intent') or {}
            if (previous_remote.get('scope') != remote['scope']
                    or current['manifest_key'] != previous_remote.get('manifest_key')):
                raise ValueError()
            gone = ((previous.get('remote_observation') or {}).get('status') in ('missing', 'damaged', 'none')
                    or previous.get('remote_delete_intent') or previous.get('remote_pruned')
                    or previous.get('retained_deleted'))
            if previous['created_at'] > item['created_at'] and not gone:
                return
        except (BackupError, KeyError, TypeError, ValueError, OSError):
            raise BackupError('s3-pointer-invalid') from None
    value = pointer_bytes(state, item)
    s3.put_object(Bucket=bucket, Key=key, Body=value, ContentLength=len(value))
    if whole_hash(s3, bucket, key, limit=len(value)) != (hashlib.sha256(value).hexdigest(), len(value)):
        raise BackupError('s3-pointer-readback-mismatch')


def latest_remote_run(state, envelope, complete, mode=None):
    """Newest run delivered under the configured bucket and installation prefix."""
    if envelope["settings"]["destination"] != "s3":
        return None
    current_prefix = prefix(state, envelope)
    if mode is not None:
        complete = [item for item in complete if item.get("mode") == mode]
    return next((item["run_id"] for item in complete if item.get("remote_delivered")
                 and scope_matches(state, envelope, item)
                 and (item.get("remote_observation") or {}).get("status") not in ("missing", "damaged")), None)


def remove_remote(state, s3, bucket, base, item, manifest, intent_key, failure, preverified=False):
    """Verify owned remote objects match the manifest or are gone, then delete them."""
    remote = item.get("remote") or item["delivery_intent"]
    if remote_artifacts.format_of(remote) == encryption.FORMAT:
        return remote_artifacts.remove(state, s3, bucket, base, item, intent_key, failure, preverified)
    expected = {"bucket": bucket, "archive_key": base + item["run_id"] + ".tar.gz", "manifest_key": base + item["run_id"] + ".manifest.json"}
    if any(remote.get(key) != value for key, value in expected.items()):
        raise BackupError(failure + "-ownership-mismatch")
    intent = item[intent_key]
    try:
        if preverified:
            intent["remote_verified"] = True
            state.save(item)
            s3.delete_object(Bucket=bucket, Key=remote["manifest_key"])
            s3.delete_object(Bucket=bucket, Key=remote["archive_key"])
            return
        archive_hash = hash_or_missing(s3, bucket, remote["archive_key"], manifest["archive"]["size"])
        if archive_hash is not None and archive_hash != (manifest["archive"]["sha256"], manifest["archive"]["size"]):
            raise BackupError(failure + "-remote-verification-failed")
        encoded = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
        manifest_hash = hash_or_missing(s3, bucket, remote["manifest_key"], len(encoded))
        if manifest_hash is not None and manifest_hash != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
            raise BackupError(failure + "-remote-verification-failed")
        if not intent.get("remote_verified") and (archive_hash is None or manifest_hash is None) and not item.get("delivery_intent"):
            raise BackupError(failure + "-remote-verification-failed")
        intent["remote_verified"] = True
        state.save(item)
        # Remove manifest first so incomplete removal never advertises recovery.
        s3.delete_object(Bucket=bucket, Key=remote["manifest_key"])
        s3.delete_object(Bucket=bucket, Key=remote["archive_key"])
    except BackupError:
        raise
    except Exception:
        raise BackupError(failure + "-remote-delete-failed") from None


def check_local(state, item, manifest, intent, failure, check_content=True):
    """Check owned local files through the verified directory; absent files need intent.

    Returns the location and the full stat identity of each file present, so
    removal can refuse a file replaced after this check.
    """
    try:
        location = artifacts.locations(state, item)
    except BackupError:
        raise BackupError(failure + "-ownership-mismatch") from None
    seen = {}
    for name, kind in ((location[2].name, "manifest"), (location[1].name, "archive")):
        try:
            if kind == "manifest":
                try:
                    stored = json.loads(artifacts.read_owned(location, name, seen=seen))
                except ValueError:
                    stored = None
                if stored != manifest:
                    raise BackupError(failure + "-local-verification-failed")
            elif check_content and artifacts.digest_owned(location, name, seen=seen) != (manifest["archive"]["size"], manifest["archive"]["sha256"]):
                raise BackupError(failure + "-local-verification-failed")
            elif not check_content:
                artifacts.stat_owned(location, name, seen)
        except BackupError as error:
            if error.code == "artifact-missing":
                if not intent:
                    raise BackupError(failure + "-local-verification-failed") from None
                continue
            if error.code.startswith(failure):
                raise
            if error.code == "artifact-changed":
                raise BackupError(failure + "-local-verification-failed") from None
            raise BackupError(failure + "-ownership-mismatch") from None
    return location, seen


def remove_local(state, item, manifest, intent, failure, check_content=True):
    location, seen = check_local(state, item, manifest, intent, failure, check_content)
    try:
        # Manifest first so an interrupted removal never advertises a restore point.
        artifacts.remove_owned(location, [location[2].name, location[1].name], seen)
    except BackupError as error:
        if error.code == "artifact-changed":
            raise BackupError(failure + "-local-verification-failed") from None
        raise BackupError(failure + "-ownership-mismatch") from None
    except OSError:
        raise BackupError(failure + "-local-delete-failed") from None


def available_runs(state):
    return sorted((item for item in state.journals() if not item.get("retained_deleted")
                   and (item.get("local_complete") or item.get("remote_delivered") or item.get("local_delete_intent") or item.get("remote_delete_intent"))),
                  key=lambda item: item["created_at"], reverse=True)


def recorded_mode(state, item):
    """Only agreement with the owned manifest can establish a retention kind."""
    mode = item.get('mode')
    if mode not in ('full-quiesced', 'database-online'):
        return None
    try:
        return mode if owned_manifest(state, item).get('mode') == mode else None
    except BackupError:
        return None


def local_survivor(state, items, excluded, checked=None, mode=None):
    if mode is not None:
        items = [item for item in items if recorded_mode(state, item) == mode]
    checked = {} if checked is None else checked
    for item in items:
        if item["run_id"] in excluded or not item.get("local_complete") or artifacts.deleting(item) or item.get("local_delete_intent"):
            continue
        try:
            key = item["run_id"]
            if key not in checked:
                try:
                    verify(state, key, record=True)
                    checked[key] = None
                except BackupError as error:
                    checked[key] = error.code
                    raise
            if checked[key] is not None:
                raise BackupError(checked[key])
            return item["run_id"]
        except BackupError:
            continue
    raise BackupError("delete-no-verified-local-survivor")


def remote_survivor(state, envelope, items, excluded, checked=None, mode=None):
    if mode is not None:
        items = [item for item in items if recorded_mode(state, item) == mode]
    checked = {} if checked is None else checked
    for item in items:
        if item["run_id"] in excluded or not item.get("remote_delivered") or artifacts.deleting(item) or item.get("remote_delete_intent"):
            continue
        if not scope_matches(state, envelope, item):
            continue
        try:
            check_remote_once(state, envelope, item, checked)
            return item["run_id"]
        except BackupError:
            continue
    raise BackupError("delete-no-verified-s3-survivor")


def finish_copy_state(state, item):
    item["copy_state_version"] = 2
    if not item.get("local_complete") and not item.get("remote_delivered") and not item.get("delivery_intent"):
        item["retained_deleted"] = True
    else:
        item.pop("retained_deleted", None)
    state.save(item)


def remove_copy(state, envelope, item, location, reason, request_id=None, remote_checked=False):
    """Journal one location's removal before I/O; never removes the other copy."""
    manifest = owned_manifest(state, item)
    key = location + "_delete_intent" if location == "local" else "remote_delete_intent"
    intent = item.get(key)
    if not intent:
        intent = {"at": time.time(), "manifest": manifest, "reason": reason, "request_id": request_id}
        item[key] = intent
        item["manifest"] = manifest
        item["copy_state_version"] = 2
        if reason == "retention":
            item["retention_delete_intent"] = intent
        state.save(item)
    if location == "s3":
        if not scope_matches(state, envelope, item):
            raise BackupError(reason + "-remote-scope-changed")
        if remote_artifacts.format_of(item.get("remote") or item.get("delivery_intent") or {}) == encryption.FORMAT and not intent.get("remote_verified"):
            if not remote_checked:
                if item.get("delivery_intent") and not item.get("remote_delivered"):
                    remote_artifacts.check_pending_source(state, envelope, item, manifest)
                else:
                    with checked_remote(state, envelope, item, record=True):
                        pass
            intent["remote_verified"] = True
            intent["verification_level"] = "restore"
            state.save(item)
        remove_remote(state, client(envelope), envelope["settings"]["s3_bucket"], prefix(state, envelope), item, manifest, key, reason, preverified=remote_checked)
        item.update({"remote_delivered": False, "remote_pruned": {"at": time.time(), "reason": reason}})
        item["remote_observation"] = {"status": "none", "checked_at": time.time()}
        item.pop("remote_verification", None)
        item.pop("delivery_intent", None)
    else:
        remove_local(state, item, manifest, intent, reason, check_content=reason == "retention")
        item.update({"local_complete": False, "local_pruned": {"at": time.time(), "reason": reason}})
        item.pop("verification", None)
    item.pop(key, None)
    item.pop("retention_delete_intent", None)
    item["deletion"] = {"reason": "manual" if reason == "delete" else reason, "at": time.time(), "location": location, "request_id": request_id}
    finish_copy_state(state, item)


def _retention(state, envelope, execute=False, locked=False, local_only=False, mode=None):
    with contextlib.nullcontext() if locked else state.lock(create=execute):
        settings = envelope["settings"]
        keep = settings["retention_keep"]
        available = available_runs(state)
        latest = latest_remote_run(state, envelope, available, mode)
        if execute and not local_only:
            for item in available:
                if item['run_id'] == latest and item.get('retention_delete_intent') and not artifacts.deleting(item):
                    item.pop('retention_delete_intent')
                    if (item.get('remote_delete_intent') or {}).get('reason') == 'retention':
                        item.pop('remote_delete_intent')
                    state.save(item)
        items = [item for item in available if not artifacts.deleting(item) and not item.get("staging_removal_intent")
                 and (mode is None or recorded_mode(state, item) == mode)]
        skipped = []
        damaged = []
        checked = {}
        checked_local = {}
        healthy = []
        for item in items:
            if item.get('local_complete') and not item.get('local_delete_intent'):
                try:
                    # A render may reuse a full check bound to the current file
                    # identity and manifest digest. Applying retention always
                    # rechecks the bytes under the run lock before deleting.
                    if execute or artifacts.download_state(state, item) != 'ready':
                        verify(state, item['run_id'], record=execute)
                    checked_local[item['run_id']] = None
                except (BackupError, OSError) as error:
                    code = error.code if isinstance(error, BackupError) else 'retention-local-verification-failed'
                    checked_local[item['run_id']] = code
                    damaged.append({'run_id': item['run_id'], 'code': code})
                    skipped.append({'run_id': item['run_id'], 'location': 'local', 'code': code})
                    continue
            healthy.append(item)
        items = healthy
        if execute and not local_only:
            for item in items:
                if not item.get("remote_delivered"):
                    continue
                if not scope_matches(state, envelope, item):
                    skipped.append({"run_id": item["run_id"], "location": "s3", "code": "retention-remote-scope-changed"})
                    continue
                try:
                    observation = observe_remote(state, envelope, item)
                    if observation in ("missing", "damaged"):
                        skipped.append({"run_id": item["run_id"], "location": "s3", "code": "remote-copy-" + observation})
                except BackupError as error:
                    skipped.append({"run_id": item["run_id"], "location": "s3", "code": error.code})
                    item["retention_error"] = {"code": error.code, "location": "s3", "at": time.time()}
                    state.save(item)
        local = [item for item in items if item.get("local_complete") or item.get("local_delete_intent")]
        remote = [item for item in items if (item.get("remote_delivered") or item.get("remote_delete_intent"))
                  and scope_matches(state, envelope, item)
                  and ((item.get("remote_observation") or {}).get("status") not in ("missing", "damaged") or item.get("remote_delete_intent"))]
        policy = settings.get("s3_retention_policy", "local-count")
        remote_keep = keep if policy == "local-count" else settings.get("s3_retention_keep", 30)
        remote_old = remote[remote_keep:] if policy in ("local-count", "count") else ([item for item in remote if item["created_at"] < time.time() - settings.get("s3_retention_days", 90) * 86400] if policy == "age" else [])
        # Previews rely on durable observations and perform no provider reads.
        local_candidates = [item for item in local if (item in local[keep:] or item.get("local_delete_intent"))
                            and item.get("local_download_until", 0) <= time.time()
                            and not item.get("delivery_intent")
                            and not ((settings["destination"] == "s3" or item.get("delivery_required"))
                                     and not item.get("remote_delivered"))]
        remote_candidates = [item for item in remote if (item in remote_old or item.get("remote_delete_intent"))
                             and item is not remote[0] and scope_matches(state, envelope, item)] if remote else []
        if local_only:
            remote_candidates = []
        if settings.get("settings_version", 1) == 2 and not settings["keep_local"]:
            local_candidates = []
        result = {"keep": [item["run_id"] for item in local[:keep]],
                  "local_delete": [item["run_id"] for item in local_candidates],
                  "remote_delete": [item["run_id"] for item in remote_candidates],
                  "delete": list(dict.fromkeys(item["run_id"] for item in remote_candidates + local_candidates)),
                  "s3_policy": policy, "skipped": skipped, "damaged": damaged, "protected": [],
                  "removed": {"local": [], "s3": []}, "execute_required": not execute}
        if not execute:
            return result
        protected_local = set()
        if local_candidates:
            for _ in range(keep):
                try:
                    protected_local.add(local_survivor(state, available_runs(state), protected_local, checked_local, mode))
                except BackupError:
                    break
        # Presence observations repair lifecycle-stale discovery. Full archive
        # proof is reserved for removal candidates and the survivor they rely on.
        good_remote = next((item for item in remote if not item.get("remote_delete_intent")
                            and (item.get("remote_observation") or {}).get("status") in ("delivered", "present")), None)
        if not local_only and settings["destination"] == "s3" and any(scope_matches(state, envelope, item) for item in items):
            s3 = client(envelope)
            pointer = prefix(state, envelope) + "lastmanifest.json"
            try:
                overall = next((run for run in available_runs(state) if run.get("remote_delivered") and scope_matches(state, envelope, run) and not artifacts.deleting(run) and (run.get("remote_observation") or {}).get("status") in ("delivered", "present")), None)
                if overall:
                    publish_pointer(state, s3, overall)
                elif not any(item.get("remote_delivered") and scope_matches(state, envelope, item)
                             and (item.get("remote_observation") or {}).get("status") == "unknown" for item in available_runs(state)):
                    s3.delete_object(Bucket=settings["s3_bucket"], Key=pointer)
            except Exception:
                skipped.append({"run_id": None, "location": "s3", "code": "retention-pointer-update-failed"})
                # Keep remote candidates if discovery cannot be updated safely.
                remote_candidates = []
        # Remote first when both policies remove a copy: failure preserves local source.
        failed_remote = set()
        for item in remote_candidates:
            try:
                if good_remote and item["run_id"] == good_remote["run_id"]:
                    result["protected"].append({"run_id": item["run_id"], "location": "s3", "code": "retention-last-good-s3-copy"})
                    continue
                remote_survivor(state, envelope, available_runs(state), {item["run_id"]}, checked, mode)
                if not item.get("remote_delete_intent"):
                    check_remote_once(state, envelope, item, checked)
                remove_copy(state, envelope, item, "s3", "retention", remote_checked=item["run_id"] in checked and checked[item["run_id"]] is None)
                result["removed"]["s3"].append(item["run_id"])
                item.pop("retention_error", None)
                state.save(item)
            except BackupError as error:
                failed_remote.add(item["run_id"])
                skipped.append({"run_id": item["run_id"], "location": "s3", "code": error.code})
                item["retention_error"] = {"code": error.code, "location": "s3", "at": time.time()}
                state.save(item)
        for item in local_candidates:
            item = artifacts.journal(state, item["run_id"])
            if item["run_id"] in protected_local or item["run_id"] in failed_remote:
                if item["run_id"] in protected_local:
                    result["protected"].append({"run_id": item["run_id"], "location": "local", "code": "retention-last-good-local-copy"})
                continue
            try:
                local_survivor(state, available_runs(state), {item["run_id"]}, checked_local, mode)
                # Removing only the local copy of an S3 backup requires current
                # full remote verification; expired pairs can be removed together.
                if settings["destination"] == "s3" and item.get("remote_delivered"):
                    check_remote_once(state, envelope, item, checked)
                remove_copy(state, envelope, item, "local", "retention")
                result["removed"]["local"].append(item["run_id"])
                item.pop("retention_error", None)
                state.save(item)
            except BackupError as error:
                skipped.append({"run_id": item["run_id"], "location": "local", "code": error.code})
                item["retention_error"] = {"code": error.code, "location": "local", "at": time.time()}
                state.save(item)
        if not local_only and settings["destination"] == "s3" and any(scope_matches(state, envelope, item) for item in items):
            refreshed = [item for item in available_runs(state) if item.get("remote_delivered") and scope_matches(state, envelope, item)
                         and not item.get("remote_delete_intent") and (item.get("remote_observation") or {}).get("status") in ("delivered", "present")]
            try:
                if refreshed:
                    publish_pointer(state, s3, refreshed[0])
            except Exception:
                skipped.append({"run_id": None, "location": "s3", "code": "retention-pointer-update-failed"})
        atomic_json(state.root / "retention.json", {"at": time.time(), "skipped_count": len(skipped),
                                                  "local_removed": len(result["removed"]["local"]),
                                                  "s3_removed": len(result["removed"]["s3"])})
        return result


def retention(state, envelope, execute=False, locked=False, local_only=False, envelope_loader=None):
    if envelope_loader is not None:
        with contextlib.nullcontext() if locked else state.lock(create=execute):
            current = envelope_loader()
            state.apply_policy(current['settings'])
            return retention(state, current, execute, True, local_only)
    if envelope['settings'].get('settings_version', 1) == 1:
        return _retention(state, envelope, execute, locked, local_only)
    with contextlib.nullcontext() if locked else state.lock(create=execute):
        results = {}
        staging = []
        skipped = []
        if not envelope['settings']['keep_local'] and envelope['settings']['upload_s3']:
            for item in available_runs(state):
                if not item.get('local_complete') or not item.get('remote_delivered') or item.get('local_download_until', 0) > time.time():
                    continue
                if recorded_mode(state, item) is None:
                    continue
                if execute:
                    try:
                        if staging_removal(state, envelope, item):
                            staging.append(item['run_id'])
                    except BackupError as error:
                        skipped.append({'run_id': item['run_id'], 'location': 'local', 'code': error.code})
                elif scope_matches(state, envelope, item) and not artifacts.deleting(item):
                    staging.append(item['run_id'])
        for kind, mode in (('full', 'full-quiesced'), ('database', 'database-online')):
            settings = dict(envelope['settings'], retention_keep=envelope['settings']['retention_keep_' + kind])
            for suffix in ('policy', 'keep', 'days'):
                settings['s3_retention_' + suffix] = settings['s3_retention_' + suffix + '_' + kind]
            results[kind] = _retention(state, dict(envelope, settings=settings), execute, True, local_only, mode)
        result = {key: list(dict.fromkeys(value for part in results.values() for value in part[key]))
                  for key in ('keep', 'local_delete', 'remote_delete', 'delete')}
        result['local_delete'] = list(dict.fromkeys(staging + result['local_delete']))
        result['delete'] = list(dict.fromkeys(staging + result['delete']))
        result.update(by_kind=results, staging_removal=staging,
                      damaged=[item for part in results.values() for item in part['damaged']],
                      skipped=skipped + [item for part in results.values() for item in part['skipped']],
                      protected=[item for part in results.values() for item in part['protected']],
                      removed={location: list(dict.fromkeys((staging if execute and location == 'local' else []) + [run for part in results.values() for run in part['removed'][location]])) for location in ('local', 's3')},
                      execute_required=not execute)
        for item in available_runs(state):
            if recorded_mode(state, item) is None:
                result['skipped'].append({'run_id': item['run_id'], 'location': None, 'code': 'retention-mode-unknown'})
        if execute:
            atomic_json(state.root / 'retention.json', {'at': time.time(), 'skipped_count': len(result['skipped']),
                        'local_removed': len(result['removed']['local']), 's3_removed': len(result['removed']['s3'])})
        return result


ACTIVE_REQUESTS = ("pending", "deferred", "launching", "running")


def delete_plan(state, envelope, run_id, request_id=None, location="all"):
    from .requests import list_requests
    if location not in ("local", "s3", "all"):
        raise BackupError("delete-location-invalid")
    item = artifacts.journal(state, run_id)
    items = available_runs(state)
    if envelope["settings"].get("settings_version", 1) == 2:
        if recorded_mode(state, item) is None:
            return {"run_id": run_id, "refusal": "delete-mode-unknown", "execute_required": True}
        items = [other for other in items if recorded_mode(state, other) == item["mode"]]
    target_local = location in ("local", "all") and bool(item.get("local_complete") or item.get("local_delete_intent"))
    target_remote = location in ("s3", "all") and bool(item.get("remote_delivered") or item.get("remote_delete_intent") or item.get("delivery_intent"))
    refusal = "artifact-deleted" if item.get("retained_deleted") else None
    if not refusal and not target_local and not target_remote:
        refusal = "run-incomplete"
    remote = None
    if target_remote:
        remote = {"bucket": (item.get("remote") or item.get("delivery_intent") or {}).get("bucket"), "in_configured_scope": scope_matches(state, envelope, item)}
        if not refusal and not remote["in_configured_scope"]:
            refusal = "delete-remote-scope-changed" if (item.get("remote") or item.get("delivery_intent") or {}).get("scope") else "delete-remote-location-unbound"
    locals_ = [other["run_id"] for other in items if other["run_id"] != run_id and other.get("local_complete") and not other.get("verification_error") and not artifacts.deleting(other)]
    remotes = [other["run_id"] for other in items if other["run_id"] != run_id and other.get("remote_delivered") and scope_matches(state, envelope, other) and not artifacts.deleting(other)]
    if not refusal and target_local and not locals_:
        refusal = "delete-last-backup"
    if not refusal and target_remote and not remotes:
        refusal = "delete-last-s3-backup"
    if not refusal and target_remote and run_id == latest_remote_run(state, envelope, items):
        refusal = "delete-latest-remote-copy"
    if not refusal:
        for request in list_requests(state):
            if request.get("request_id") != request_id and request.get("status") in ACTIVE_REQUESTS and request.get("arguments", {}).get("run_id") == run_id:
                refusal = "delete-conflict"
                break
    return {"run_id": run_id, "location": location, "local_copy": target_local, "remote_copy": remote,
            "survivors": locals_ if target_local else remotes, "local_survivors": locals_, "remote_survivors": remotes,
            "resuming": bool(artifacts.deleting(item) or item.get("local_delete_intent") or item.get("remote_delete_intent")),
            "refusal": refusal, "execute_required": True}


def delete_run(state, envelope, run_id, request_id=None, location="all", envelope_loader=None):
    with state.lock():
        if envelope_loader is not None:
            envelope = envelope_loader()
            state.apply_policy(envelope['settings'])
        item = artifacts.journal(state, run_id)
        if item.get("retained_deleted"):
            return {"run_id": run_id, "deleted": True, "already_deleted": True}
        preview = delete_plan(state, envelope, run_id, request_id, location)
        if preview["refusal"]:
            raise BackupError(preview["refusal"])
        survivor = None
        mode = item.get("mode") if envelope["settings"].get("settings_version", 1) == 2 else None
        if preview["local_copy"]:
            try:
                survivor = local_survivor(state, available_runs(state), {run_id}, mode=mode)
            except BackupError:
                raise BackupError("delete-no-verified-survivor") from None
        if preview["remote_copy"]:
            remote_survivor(state, envelope, available_runs(state), {run_id}, mode=mode)
        manifest = owned_manifest(state, item)
        if not artifacts.deleting(item):
            item["delete_intent"] = {"at": time.time(), "manifest": manifest, "location": location, "request_id": request_id, "survivor": survivor}
            state.save(item)
        elif item["delete_intent"].get("location", "all") != location:
            raise BackupError("delete-location-changed")
        if preview["remote_copy"]:
            remove_copy(state, envelope, item, "s3", "delete", request_id)
        if preview["local_copy"]:
            remove_copy(state, envelope, item, "local", "delete", request_id)
        # Preserve the completed receipt; only an unfinished intent blocks use.
        item["delete_intent"]["completed"] = True
        finish_copy_state(state, item)
        return {"run_id": run_id, "location": location, "deleted": bool(item.get("retained_deleted")), "survivor": survivor}


def retrieve_plan(state, envelope, run_id):
    item = artifacts.journal(state, run_id)
    refusal = None
    if item.get("retained_deleted"):
        refusal = "artifact-deleted"
    elif item.get("local_complete"):
        refusal = "retrieve-local-copy-exists"
    elif not item.get("remote_delivered"):
        refusal = "remote-copy-unavailable"
    elif not scope_matches(state, envelope, item):
        refusal = "remote-scope-changed" if (item.get("remote") or {}).get("scope") else "remote-location-unbound"
    return {"run_id": run_id, "refusal": refusal, "resuming": bool(item.get("retrieve_intent")), "execute_required": True}


def complete_retrieval(state, item, manifest):
    item.update({"local_complete": True, "local_download_until": time.time() + 86400, "manifest": manifest})
    item.pop("local_pruned", None)
    item.pop("verification_error", None)
    item.pop("retrieve_intent", None)
    item.pop("retrieve_work", None)
    state.save(item)
    verify(state, item["run_id"], record=True)
    return {"run_id": item["run_id"], "retrieved": True, "local_complete": True, "download_available_until": item["local_download_until"]}


def cleanup_retrieve_work(state, item, directory_fd):
    work = item.get("retrieve_work")
    if not work:
        return
    name = work.get("temporary", "")
    if not re.fullmatch(r"\.remote-[0-9a-f]{32}\.partial", name) or work.get("scope") != (item.get("remote") or {}).get("scope"):
        raise BackupError("retrieve-intent-invalid")
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        info = None
    if info is not None:
        expected = work.get("identity")
        if (not artifacts.owned_file(info) or (expected is not None and (info.st_dev, info.st_ino) != (expected.get("device"), expected.get("inode")))
                or (expected is None and info.st_size != 0)):
            raise BackupError("retrieve-spool-changed")
        checked = artifacts.identity(info)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if artifacts.identity(current) != checked:
            raise BackupError("retrieve-spool-changed")
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    item.pop("retrieve_work", None)
    state.save(item)


def resume_retrieval(state, item, location, directory_fd):
    """Finish an already published, identity-bound archive after a worker crash."""
    intent = item.get("retrieve_intent")
    if not intent:
        return None
    try:
        descriptor = os.open(location[1].name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        expected = intent.get("archive") or {}
        current = artifacts.identity(info)
        # Hardlink publication changes ctime/nlink; all immutable source fields
        # and full bytes still must match the pre-publication receipt.
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink not in (1, 2) or any(current[key] != expected.get(key) for key in ("device", "inode", "size", "mtime_ns"))):
            raise BackupError("retrieve-local-copy-exists")
        manifest = intent["manifest"]
        artifacts.validate_manifest(state, item, manifest)
        artifact = artifacts.Artifact(item, manifest, descriptor, info)
        if artifact.digest() != manifest["archive"]["sha256"]:
            raise BackupError("retrieve-local-copy-changed")
        from .engine import check_archive
        check_archive(artifact.reader, manifest)
        if not artifact.unchanged():
            raise BackupError("retrieve-local-copy-changed")
        temporary = intent.get("temporary", "")
        if not re.fullmatch(r"\.remote-[0-9a-f]{32}\.partial", temporary):
            raise BackupError("retrieve-intent-invalid")
        try:
            linked = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                raise BackupError("retrieve-local-copy-changed")
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        pending_name = temporary + ".manifest"
        try:
            pending_fd = os.open(pending_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
        except FileNotFoundError:
            pending_fd = None
        if pending_fd is not None:
            try:
                pending_info = os.fstat(pending_fd)
                expected_data = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
                if (not stat.S_ISREG(pending_info.st_mode) or pending_info.st_uid != os.getuid()
                        or stat.S_IMODE(pending_info.st_mode) != 0o600 or pending_info.st_nlink not in (1, 2)
                        or pending_info.st_size != len(expected_data) or os.read(pending_fd, len(expected_data) + 1) != expected_data
                        or artifacts.identity(os.fstat(pending_fd)) != artifacts.identity(pending_info)):
                    raise BackupError("retrieve-local-copy-changed")
                current_pending = os.stat(pending_name, dir_fd=directory_fd, follow_symlinks=False)
                if artifacts.identity(current_pending) != artifacts.identity(pending_info):
                    raise BackupError("retrieve-local-copy-changed")
                os.unlink(pending_name, dir_fd=directory_fd)
            finally:
                os.close(pending_fd)
        try:
            if artifacts.read_manifest(state, item, location) != manifest:
                raise BackupError("retrieve-local-copy-changed")
        except BackupError as error:
            if error.code != "artifact-missing":
                raise
            name = ".retrieve-manifest-" + uuid.uuid4().hex
            try:
                manifest_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
                with os.fdopen(manifest_fd, "wb") as output:
                    output.write(json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.link(name, location[2].name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
            finally:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        os.fsync(directory_fd)
        return complete_retrieval(state, item, manifest)
    finally:
        os.close(descriptor)


def retrieve(state, envelope, run_id, envelope_loader=None):
    with state.lock():
        if envelope_loader is not None:
            envelope = envelope_loader()
            state.apply_policy(envelope['settings'])
        preview = retrieve_plan(state, envelope, run_id)
        if preview["refusal"]:
            raise BackupError(preview["refusal"])
        item = artifacts.journal(state, run_id)
        location = artifacts.locations(state, item)
        directory_fd = artifacts.open_directory(location[0])
        try:
            info = os.fstat(directory_fd)
            if (info.st_dev, info.st_ino) != location.key:
                raise BackupError("artifact-location-invalid")
            resumed = resume_retrieval(state, item, location, directory_fd)
            if resumed:
                return resumed
            cleanup_retrieve_work(state, item, directory_fd)
            for path in (location[1], location[2]):
                try:
                    os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise BackupError("retrieve-local-copy-exists")
            with checked_remote(state, envelope, item, record=True, directory=location[0], directory_fd=directory_fd) as (temporary, manifest):
                # Recheck the current name before publishing into the held safe
                # descriptor. A replaced parent is never traversed for writes.
                checked = artifacts.open_directory(location[0])
                try:
                    current = os.fstat(checked)
                    if (current.st_dev, current.st_ino) != location.key:
                        raise BackupError("artifact-location-invalid")
                finally:
                    os.close(checked)
                info = os.stat(temporary.name, dir_fd=directory_fd, follow_symlinks=False)
                if artifacts.identity(info) != item["retrieve_work"].get("verified_identity"):
                    raise BackupError("retrieve-spool-changed")
                item["retrieve_intent"] = {"at": time.time(), "manifest": manifest, "archive": artifacts.identity(info),
                                          "temporary": temporary.name, "scope": item["remote"]["scope"]}
                state.save(item)
                manifest_temporary = temporary.name + ".manifest"
                try:
                    data = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
                    descriptor = os.open(manifest_temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    try:
                        os.link(temporary.name, location[1].name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                        os.link(manifest_temporary, location[2].name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                    except FileExistsError:
                        raise BackupError("retrieve-local-copy-exists") from None
                except Exception:
                    try:
                        current = os.stat(location[1].name, dir_fd=directory_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                            os.unlink(location[1].name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                    raise
                finally:
                    try:
                        os.unlink(manifest_temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                os.unlink(temporary.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return complete_retrieval(state, item, manifest)


def hash_or_missing(s3, bucket, key, limit=None):
    try:
        return whole_hash(s3, bucket, key, limit=limit)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
