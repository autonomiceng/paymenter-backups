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
from .engine import plan, storage_usage
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
    if artifacts.deleting(item):
        return "deleting" if ("delete", item["run_id"]) in active_runs else "delete-interrupted"
    if item.get("local_complete") or item.get("remote_delivered"):
        if not item.get("local_complete") and (item.get("remote_observation") or {}).get("status") in ("missing", "damaged"):
            return "damaged"
        return "damaged" if item.get("verification_error") and not item.get("verification") else "ready"
    unresolved = not item.get("service_recovered") and not item.get("recovery_acknowledged")
    if item.get("phase") == "failed":
        return "attention" if unresolved else "failed"
    if alive(item):
        return "running"
    return "attention" if unresolved else "interrupted"


def run_entry(state, item, active_runs, envelope=None):
    status = run_status(item, active_runs)
    entry = {"run_id": item["run_id"], "created_at": number(item.get("created_at")), "mode": code(item.get("mode")),
             "status": status, "phase": code(item.get("phase")), "error": code(item.get("error")),
             "recovery_error": code(item.get("recovery_error")), "size": None, "download": None,
             "local": False, "remote": "none", "delivery_error": code(item.get("delivery_error")),
             "verified_at": None, "verification_source": None,
             "verification_error": code((item.get("verification_error") or {}).get("code")),
             "deleted_at": None, "deletion_reason": None, "request_id": identifier(item.get("request_id"))}
    entry["local_download_until"] = number(item.get("local_download_until"))
    entry["retention_error"] = code((item.get("retention_error") or {}).get("code"))
    entry["remote_checked_at"] = number((item.get("remote_observation") or {}).get("checked_at"))
    verification = item.get("verification")
    if not item.get("local_complete"):
        verification = item.get("remote_verification")
    if isinstance(verification, dict):
        entry["verified_at"] = number(verification.get("verified_at"))
        entry["verification_source"] = code(verification.get("source"))
    if item.get("local_complete") and not artifacts.deleted(item):
        entry["local"] = True
        try:
            with artifacts.open_archive(state, item["run_id"], item) as artifact:
                entry["size"] = artifact.manifest["archive"]["size"]
                entry["download"] = "ready" if artifacts.verification_current(artifact) else "verify"
        except BackupError as error:
            entry["download"] = code(error.code)
            entry["status"] = "damaged"
            if error.code in ("artifact-missing", "artifact-location-invalid", "run-incomplete"):
                entry["local"] = False
    intent = item.get("delete_intent") or item.get("retention_delete_intent")
    if entry["size"] is None and isinstance(intent, dict):
        entry["size"] = number(((intent.get("manifest") or {}).get("archive") or {}).get("size"))
    if item.get("remote_delivered"):
        from .storage import remote_readiness
        entry["remote"] = code(remote_readiness(state, envelope, item))
        if not item.get("local_complete"):
            entry["size"] = number(((item.get("manifest") or {}).get("archive") or {}).get("size"))
            entry["download"] = "retrieve" if envelope is not None and entry["remote"] in ("delivered", "present") else None
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
            "location": code(arguments.get("location")),
            "result_destination_id": identifier(result.get("destination_id")),
            "skipped_count": len(result["skipped"]) if isinstance(result.get("skipped"), list) else None,
            "result_run_id": identifier(result.get("run_id"))}


def origin(url):
    try:
        from .maintenance import web_origin
        return web_origin(url or "", allow_http=True)
    except BackupError:
        pass
    return None


def configuration(state, envelope, error=None):
    if envelope is None:
        return {"available": False, "valid": False, "error": code(error) or "unknown"}
    settings = envelope["settings"]
    local_path = settings.get("local_path") or str(state.default_destination())
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
              "retention": {"keep": settings["retention_keep"], "local_keep": settings["retention_keep"],
                            "s3_policy": settings.get("s3_retention_policy", "local-count"),
                            "s3_keep": settings.get("s3_retention_keep", 30), "s3_days": settings.get("s3_retention_days", 90),
                            "automatic": bool(settings["automatic_retention"])},
              "space": {"budget": settings["local_byte_budget"], "minimum_free": settings["minimum_free_bytes"], "used": None},
              # Booleans only. The key avoids names that public output redaction replaces.
              "saved_keys": {"s3": bool((envelope.get("credentials") or {}).get("access_key") and (envelope.get("credentials") or {}).get("secret_key")),
                             "site": bool(envelope.get("maintenance_probe_credentials")),
                             "site_origin": origin((envelope.get("maintenance_probe_credentials") or {}).get("origin"))},
              "app_origin": origin((envelope.get("framework") or {}).get("app_url")),
              "billing": {"time": (envelope.get("billing") or {}).get("daily_time"), "timezone": (envelope.get("billing") or {}).get("timezone")},
              "admission": {}}
    app_url = (envelope.get("framework") or {}).get("app_url", "")
    current_origin = origin(app_url)
    probe_credentials = envelope.get("maintenance_probe_credentials") or {}
    saved_origin = origin(probe_credentials.get("origin"))
    state_code = "unavailable" if current_origin is None else "http" if current_origin.startswith("http://") else "not-saved" if not probe_credentials else "saved" if saved_origin == current_origin else "origin-changed"
    result["site_password"] = {"state": state_code, "origin": current_origin, "saved_origin": saved_origin,
                               "probe_base": current_origin + urllib.parse.urlsplit(app_url).path if current_origin else None}
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
        used = storage_usage(state, settings)
        result["space"]["used"] = used
        result["space"]["state"] = "full" if used >= settings["local_byte_budget"] else "ok"
    result["retention"]["pending"] = {"local": None, "s3": None}
    result["retention"]["last_apply"] = None
    try:
        private_file(state.root / "retention.json")
        summary = read_json(state.root / "retention.json")
        result["retention"]["last_apply"] = {key: number(summary.get(key)) for key in ("at", "skipped_count", "local_removed", "s3_removed")}
    except (OSError, BackupError):
        pass
    if state.initialized and error is None:
        from .storage import retention, destination_id
        result["destination"]["id"] = destination_id(state, envelope)
        try:
            preview = retention(state, envelope)
            result["retention"]["pending"] = {"local": len(preview["local_delete"]), "s3": len(preview["remote_delete"])}
        except BackupError:
            pass
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
    runs = sorted((run_entry(state, item, active_runs, envelope) for item in journals), key=lambda item: item["created_at"] or 0, reverse=True)
    ordered = sorted((request_entry(item) for item in requests), key=lambda item: item["created_at"] or 0, reverse=True)
    active = [item for item in ordered if item["status"] in ACTIVE]
    recent = [item for item in ordered if item["status"] not in ACTIVE][:REQUEST_HISTORY]
    pending_s3 = [item for item in journals if item.get("local_complete") and not item.get("remote_delivered") and not item.get("remote_pruned") and not item.get("retained_deleted")]
    verified_remote = {item["run_id"] for item in journals if item.get("remote_verification")}
    return {"schema_version": 1, "generated_at": time.time(), "initialized": state.initialized,
            "installation_uuid": state.uuid, "maintenance": (state.app / "storage/framework/down").exists(),
            "scheduler": scheduler(state) if state.initialized else {"last_tick_at": None},
            "configuration": configuration(state, envelope, error), "runs": runs,
            "last_good": {"local": next((item["run_id"] for item in runs if item["local"] and item["download"] == "ready"), None),
                          "s3": next((item["run_id"] for item in runs if item["remote"] in ("delivered", "present")
                                      and item["run_id"] in verified_remote), None)},
            "s3_pending": {"count": len(pending_s3) if envelope and envelope["settings"]["destination"] == "s3" else 0,
                           "oldest_at": min((number(item.get("created_at")) or 0 for item in pending_s3), default=None) if envelope and envelope["settings"]["destination"] == "s3" else None},
            "requests": active + recent}
