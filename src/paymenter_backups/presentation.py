"""Whitelisted overview for the administrator page and terminal summaries.

Output holds fixed codes, run and request IDs, timestamps, sizes, booleans
and configured nonsecret settings. It never includes artifact paths,
manifests, database grants, process identities, request results, stderr or
credential material.
"""
import pathlib
import re
import time
import urllib.parse

from . import artifacts
from .common import BackupError, private_file, read_json
from .engine import plan, usage
from .process import process_identity
from .requests import ACTIVE, list_requests, next_slot

SAFE_CODE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
REQUEST_HISTORY = 30


def code(value):
    if value is None or value is False:
        return None
    return value if isinstance(value, str) and SAFE_CODE.fullmatch(value) else "unknown"


def identifier(value):
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else None


def number(value):
    return value if type(value) in (int, float) else None


def alive(item):
    recorded = item.get("process")
    return isinstance(recorded, dict) and type(recorded.get("pid")) is int and process_identity(recorded["pid"]) == recorded


def run_status(item, active_runs):
    if item.get("retained_deleted"):
        return "deleted"
    if item.get("delete_intent"):
        return "deleting" if ("delete", item["run_id"]) in active_runs else "delete-interrupted"
    if item.get("local_complete"):
        return "damaged" if item.get("verification_error") and not item.get("verification") else "ready"
    unresolved = not item.get("service_recovered") and not item.get("recovery_acknowledged")
    if item.get("phase") == "failed":
        return "attention" if unresolved else "failed"
    if alive(item):
        return "running"
    return "attention" if unresolved else "interrupted"


def run_entry(state, item, active_runs):
    status = run_status(item, active_runs)
    entry = {"run_id": item["run_id"], "created_at": number(item.get("created_at")), "mode": code(item.get("mode")),
             "status": status, "phase": code(item.get("phase")), "error": code(item.get("error")),
             "recovery_error": code(item.get("recovery_error")), "size": None, "download": None,
             "local": False, "remote": "none", "delivery_error": code(item.get("delivery_error")),
             "verified_at": None, "verification_source": None,
             "verification_error": code((item.get("verification_error") or {}).get("code")),
             "deleted_at": None, "deletion_reason": None, "request_id": identifier(item.get("request_id"))}
    verification = item.get("verification")
    if isinstance(verification, dict):
        entry["verified_at"] = number(verification.get("verified_at"))
        entry["verification_source"] = code(verification.get("source"))
    if status in ("ready", "damaged"):
        entry["local"] = True
        try:
            with artifacts.open_archive(state, item["run_id"], item) as artifact:
                entry["size"] = artifact.manifest["archive"]["size"]
                entry["download"] = "ready" if artifacts.verification_current(artifact) else "verify"
        except BackupError as error:
            entry["download"] = code(error.code)
    intent = item.get("delete_intent") or item.get("retention_delete_intent")
    if entry["size"] is None and isinstance(intent, dict):
        entry["size"] = number(((intent.get("manifest") or {}).get("archive") or {}).get("size"))
    if item.get("remote_delivered"):
        entry["remote"] = "delivered"
    elif item.get("delivery_error") and status in ("ready", "damaged"):
        entry["remote"] = "failed"
    deletion = item.get("deletion")
    if isinstance(deletion, dict):
        entry["deleted_at"] = number(deletion.get("at"))
        entry["deletion_reason"] = code(deletion.get("reason"))
    elif status == "deleted":
        entry["deletion_reason"] = "retention"
    return entry


def request_entry(item):
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    return {"request_id": identifier(item.get("request_id")), "action": code(item.get("action")),
            "status": code(item.get("status")), "created_at": number(item.get("created_at")),
            "finished_at": number(item.get("finished_at") or item.get("updated_at")),
            "attempts": number(item.get("attempts")), "error": code(item.get("error")),
            "run_id": identifier(arguments.get("run_id")), "mode": code(arguments.get("mode")),
            "scheduled": bool(item.get("slot")), "slot_at": number(item.get("slot_at")),
            "actor": arguments.get("actor") if type(arguments.get("actor")) is int else None,
            "result_run_id": identifier(result.get("run_id"))}


def origin(url):
    try:
        parsed = urllib.parse.urlsplit(url or "")
        if parsed.scheme in ("http", "https") and parsed.hostname:
            port = parsed.port
            default = 443 if parsed.scheme == "https" else 80
            return parsed.scheme + "://" + parsed.hostname.lower() + (":" + str(port) if port not in (None, default) else "")
    except ValueError:
        pass
    return None


def configuration(state, envelope, error=None):
    if envelope is None:
        return {"available": False, "valid": False, "error": code(error) or "unknown"}
    settings = envelope["settings"]
    local_path = settings.get("local_path") or str(state.root / "artifacts")
    result = {"available": True, "valid": error is None, "error": code(error),
              "extension_enabled": bool(settings.get("extension_enabled", True)),
              "mode": code(settings["mode"]),
              "destination": {"type": code(settings["destination"]), "local_path": local_path,
                              "s3_bucket": settings["s3_bucket"], "s3_region": settings["s3_region"],
                              "s3_endpoint": origin(settings["s3_endpoint"]) or "", "s3_prefix": settings["s3_prefix"],
                              "s3_path_style": bool(settings["s3_path_style"])},
              "schedule": {"enabled": bool(settings["enabled"]), "frequency": code(settings["schedule_frequency"]),
                           "time": settings["schedule_time"], "weekdays": list(settings["schedule_weekdays"]),
                           "timezone": settings["schedule_timezone"], "next_at": None},
              "retention": {"keep": settings["retention_keep"], "automatic": bool(settings["automatic_retention"])},
              "space": {"budget": settings["local_byte_budget"], "minimum_free": settings["minimum_free_bytes"], "used": None},
              # Booleans only. The key avoids names that public output redaction replaces.
              "saved_keys": {"s3": bool((envelope.get("credentials") or {}).get("access_key") and (envelope.get("credentials") or {}).get("secret_key")),
                             "site": bool(envelope.get("maintenance_probe_credentials")),
                             "site_origin": (envelope.get("maintenance_probe_credentials") or {}).get("origin")},
              "app_origin": origin((envelope.get("framework") or {}).get("app_url")),
              "billing": {"time": (envelope.get("billing") or {}).get("daily_time"), "timezone": (envelope.get("billing") or {}).get("timezone")},
              "admission": {}}
    if error is None:
        if settings["enabled"]:
            try:
                result["schedule"]["next_at"] = next_slot(settings)
            except (KeyError, ValueError):
                pass
        for mode in ("full-quiesced", "database-online"):
            proposed = dict(envelope, settings=dict(settings, mode=mode))
            try:
                result["admission"][mode] = [code(item) for item in plan(state, proposed)["admission_errors"]]
            except BackupError as failure:
                result["admission"][mode] = [code(failure.code)]
    if state.root.exists():
        used = usage(state.root)
        if settings.get("local_path"):
            local = pathlib.Path(settings["local_path"])
            if local.exists() and local.resolve() != state.root and state.root not in local.resolve().parents:
                used += usage(local)
        result["space"]["used"] = used
    return result


def scheduler(state):
    path = state.root / "scheduler.json"
    try:
        private_file(path)
        return {"last_tick_at": number(read_json(path).get("last_tick_at"))}
    except (OSError, BackupError):
        return {"last_tick_at": None}


def overview(state, envelope=None, error=None):
    journals = state.journals() if state.initialized else []
    requests = list_requests(state) if state.initialized else []
    active_runs = {(item.get("action"), (item.get("arguments") or {}).get("run_id")) for item in requests if item.get("status") in ACTIVE}
    runs = sorted((run_entry(state, item, active_runs) for item in journals), key=lambda item: item["created_at"] or 0, reverse=True)
    ordered = sorted((request_entry(item) for item in requests), key=lambda item: item["created_at"] or 0, reverse=True)
    active = [item for item in ordered if item["status"] in ACTIVE]
    recent = [item for item in ordered if item["status"] not in ACTIVE][:REQUEST_HISTORY]
    return {"schema_version": 1, "generated_at": time.time(), "initialized": state.initialized,
            "installation_uuid": state.uuid, "maintenance": (state.app / "storage/framework/down").exists(),
            "scheduler": scheduler(state) if state.initialized else {"last_tick_at": None},
            "configuration": configuration(state, envelope, error), "runs": runs,
            "requests": active + recent}
