"""Explicit S3 configuration and complete streamed object verification."""
import hashlib
import json
import os
import pathlib
import time
import uuid

from .common import BackupError, atomic_json, digest, read_json
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


def whole_hash(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    result = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            result.update(chunk)
            size += len(chunk)
    finally:
        body.close()
    return result.hexdigest(), size


def upload(s3, bucket, key, path, journal=None, state=None):
    size = pathlib.Path(path).stat().st_size
    with open(path, "rb") as stream:
        if size <= 8 * 1024**2:
            s3.put_object(Bucket=bucket, Key=key, Body=stream, ContentLength=size)
            return
        # Explicit multipart journal ensures failures remain discoverable.
        upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
        if journal is not None:
            journal["multipart"] = {"bucket": bucket, "key": key, "upload_id": upload_id}
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


def deliver(state, envelope, run_id):
    with state.lock():
        manifest = verify(state, run_id, show=True)
        journal = read_json(state.run_path(run_id) / "journal.json")
        if envelope["settings"]["destination"] != "s3":
            return {"run_id": run_id, "local_complete": True, "remote_delivered": False}
        s3 = client(envelope)
        bucket = envelope["settings"]["s3_bucket"]
        base = prefix(state, envelope)
        archive_key, manifest_key = base + run_id + ".tar.gz", base + run_id + ".manifest.json"
        encoded = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
        try:
            # Retried delivery first disposes of explicitly recorded unfinished work.
            old = journal.get("multipart")
            if old:
                if old["bucket"] != bucket or old["key"] != archive_key:
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
                    verified = whole_hash(s3, bucket, archive_key) == (manifest["archive"]["sha256"], manifest["archive"]["size"])
                    verified = verified and whole_hash(s3, bucket, manifest_key) == (hashlib.sha256(encoded).hexdigest(), len(encoded))
                    if verified:
                        return {"run_id": run_id, "remote_delivered": True, "already_delivered": True}
                except Exception:
                    pass
            upload(s3, bucket, archive_key, journal["archive_path"], journal, state)
            if whole_hash(s3, bucket, archive_key) != (manifest["archive"]["sha256"], manifest["archive"]["size"]):
                raise BackupError("s3-readback-mismatch")
            s3.put_object(Bucket=bucket, Key=manifest_key, Body=encoded, ContentLength=len(encoded))
            if whole_hash(s3, bucket, manifest_key) != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
                raise BackupError("s3-manifest-readback-mismatch")
            newer = [item for item in state.journals() if item.get("remote_delivered") and not item.get("retained_deleted")
                     and item["created_at"] > journal["created_at"] and item.get("remote", {}).get("bucket") == bucket
                     and item["remote"].get("manifest_key") == base + item["run_id"] + ".manifest.json"]
            if not newer:
                last = json.dumps({"installation_uuid": state.uuid, "run_id": run_id, "manifest_key": manifest_key}).encode()
                s3.put_object(Bucket=bucket, Key=base + "lastmanifest.json", Body=last, ContentLength=len(last))
            journal.update({"remote_delivered": True, "delivery_error": None,
                            "remote": {"bucket": bucket, "archive_key": archive_key, "manifest_key": manifest_key,
                                       "delivered_at": time.time()}})
            state.save(journal)
            return {"run_id": run_id, "remote_delivered": True}
        except Exception as error:
            journal["delivery_error"] = error.code if isinstance(error, BackupError) else "s3-delivery-failed"
            state.save(journal)
            raise BackupError(journal["delivery_error"]) from None


def destination_test(state, envelope):
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
        return {"destination": "local", "verified": True}
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
    return {"destination": "s3", "verified": True}


def retention(state, envelope, execute=False):
    with state.lock(create=execute):
        keep = envelope["settings"]["retention_keep"]
        complete = sorted((item for item in state.journals() if item.get("local_complete") and not item.get("retained_deleted")), key=lambda item: item["created_at"], reverse=True)
        current_prefix = prefix(state, envelope) if envelope["settings"]["destination"] == "s3" else None
        latest_remote = next((item["run_id"] for item in complete if item.get("remote_delivered")
                              and item.get("remote", {}).get("bucket") == envelope["settings"]["s3_bucket"]
                              and item["remote"].get("manifest_key") == current_prefix + item["run_id"] + ".manifest.json"), None) if current_prefix else None
        candidates = [item for item in complete if (item.get("retention_delete_intent") or item in complete[keep:])
                      and not (envelope["settings"]["destination"] == "s3" and (not item.get("remote_delivered") or item["run_id"] == latest_remote))]
        for item in complete:
            if not item.get("retention_delete_intent"):
                verify(state, item["run_id"])
        result = {"keep": [item["run_id"] for item in complete[:keep]], "delete": [item["run_id"] for item in candidates], "execute_required": not execute}
        if not execute:
            return result
        s3 = client(envelope) if envelope["settings"]["destination"] == "s3" else None
        base = prefix(state, envelope) if s3 else None
        bucket = envelope["settings"]["s3_bucket"]
        for item in candidates:
            intent = item.get("retention_delete_intent")
            manifest = intent["manifest"] if intent else verify(state, item["run_id"], show=True)
            if manifest.get("installation_uuid") != state.uuid or manifest.get("run_id") != item["run_id"]:
                raise BackupError("retention-ownership-mismatch")
            for key in ("manifest_path", "archive_path"):
                path = pathlib.Path(item[key])
                if path.is_symlink() or path.resolve() != path:
                    raise BackupError("retention-ownership-mismatch")
                if path.exists():
                    if key == "manifest_path" and read_json(path) != manifest:
                        raise BackupError("retention-local-verification-failed")
                    if key == "archive_path" and (path.stat().st_size != manifest["archive"]["size"] or digest(path) != manifest["archive"]["sha256"]):
                        raise BackupError("retention-local-verification-failed")
                elif not intent:
                    raise BackupError("retention-local-verification-failed")
            if not intent:
                item["retention_delete_intent"] = {"at": time.time(), "manifest": manifest}
                state.save(item)
            if s3 and item.get("remote_delivered"):
                remote = item["remote"]
                if item["run_id"] == latest_remote:
                    continue
                expected = {"bucket": bucket, "archive_key": base + item["run_id"] + ".tar.gz", "manifest_key": base + item["run_id"] + ".manifest.json"}
                if any(remote.get(key) != value for key, value in expected.items()):
                    raise BackupError("retention-ownership-mismatch")
                try:
                    archive_hash = hash_or_missing(s3, bucket, remote["archive_key"])
                    if archive_hash is not None and archive_hash != (manifest["archive"]["sha256"], manifest["archive"]["size"]):
                        raise BackupError("retention-remote-verification-failed")
                    encoded = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
                    manifest_hash = hash_or_missing(s3, bucket, remote["manifest_key"])
                    if manifest_hash is not None and manifest_hash != (hashlib.sha256(encoded).hexdigest(), len(encoded)):
                        raise BackupError("retention-remote-verification-failed")
                    if not (intent and intent.get("remote_verified")) and (archive_hash is None or manifest_hash is None):
                        raise BackupError("retention-remote-verification-failed")
                    item["retention_delete_intent"]["remote_verified"] = True
                    state.save(item)
                    # Remove manifest first so incomplete removal never advertises recovery.
                    s3.delete_object(Bucket=bucket, Key=remote["manifest_key"])
                    s3.delete_object(Bucket=bucket, Key=remote["archive_key"])
                except Exception:
                    raise BackupError("retention-remote-delete-failed") from None
            for key in ("manifest_path", "archive_path"):
                path = pathlib.Path(item[key])
                if path.is_symlink() or path.resolve() != path:
                    raise BackupError("retention-ownership-mismatch")
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    raise BackupError("retention-local-delete-failed") from None
            item.update({"retained_deleted": True, "local_complete": False, "remote_delivered": False})
            state.save(item)
        return result


def hash_or_missing(s3, bucket, key):
    try:
        return whole_hash(s3, bucket, key)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
