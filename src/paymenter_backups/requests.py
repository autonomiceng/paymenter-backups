"""Durable requests and DST-aware backup schedule slots."""
import datetime
import hashlib
import pathlib
import time
import uuid
from zoneinfo import ZoneInfo

from .common import BackupError, atomic_json, read_json
from .process import detach, process_identity

ACTIONS = ("run", "verify", "deliver", "retention", "destination-test")


def path(state, request_id):
    # Reuse the strict run-ID grammar without creating a run.
    state.run_path(request_id)
    target = state.root / "requests" / (request_id + ".json")
    if target.is_symlink():
        raise BackupError("private-state-permissions")
    return target


def create(state, action="run", arguments=None, slot=None):
    if action not in ACTIONS:
        raise BackupError("request-action-invalid")
    arguments = arguments or {}
    if set(arguments) - {"run_id", "execute"}:
        raise BackupError("request-arguments-invalid")
    if action in ("verify", "deliver"):
        state.run_path(arguments.get("run_id"))
    request_id = hashlib.sha256((state.uuid + ":" + slot).encode()).hexdigest()[:32] if slot else uuid.uuid4().hex
    target = path(state, request_id)
    with state.lock("requests.lock"):
        if target.exists():
            return read_json(target)
        item = {"schema_version": 1, "request_id": request_id, "installation_uuid": state.uuid,
                "created_at": time.time(), "action": action, "arguments": arguments,
                "slot": slot, "status": "pending", "attempts": 0}
        atomic_json(target, item)
    return item


def list_requests(state):
    return [read_json(item) for item in sorted((state.root / "requests").glob("*.json"))]


def cancel(state, request_id):
    with state.lock("requests.lock"):
        item = read_json(path(state, request_id))
        if item["status"] not in ("pending", "deferred"):
            raise BackupError("request-not-cancellable")
        item["status"] = "cancelled"
        item["updated_at"] = time.time()
        atomic_json(path(state, request_id), item)
    return {"request_id": request_id, "status": "cancelled"}


def due_slots(settings, now=None):
    now = time.time() if now is None else now
    zone = ZoneInfo(settings["schedule_timezone"])
    # Enumerating real UTC minutes handles local gaps naturally; repeated wall
    # slots share a single durable identifier and therefore execute once.
    start = int((now - min(settings["request_max_age"], 172800)) // 60)
    end = int(now // 60)
    seen = set()
    for minute in range(start, end + 1):
        local = datetime.datetime.fromtimestamp(minute * 60, zone)
        if local.strftime("%H:%M") != settings["schedule_time"]:
            continue
        if settings["schedule_frequency"] == "weekly" and local.weekday() not in settings["schedule_weekdays"]:
            continue
        slot = local.strftime("%Y-%m-%dT%H:%M") + "@" + settings["schedule_timezone"]
        if slot not in seen:
            seen.add(slot)
            yield slot, minute * 60


def tick(state, envelope, command_prefix):
    from .maintenance import recover
    # Independently usable recovery is called by the CLI before bridge reads too.
    if (state.app / "storage/framework/down").exists():
        return {"maintenance": True, "recovery": recover(state, auto=True)}
    settings = envelope["settings"]
    if not settings.get("extension_enabled", True):
        return {"enabled": False}
    with state.lock("tick.lock"):
        if settings["enabled"]:
            for slot, instant in due_slots(settings):
                item = create(state, slot=slot)
                if item.get("slot_at") is None:
                    item["slot_at"] = instant
                    atomic_json(path(state, item["request_id"]), item)
        with state.lock("requests.lock"):
            items = sorted(list_requests(state), key=lambda item: item["created_at"])
            launched = []
            for item in items:
                if item["status"] in ("launching", "running"):
                    if item.get("worker") and process_identity(item["worker"]["pid"]) == item["worker"]:
                        return {"busy": True}
                    item["status"] = "deferred"
                    item["error"] = "worker-interrupted"
                if item["status"] not in ("pending", "deferred"):
                    continue
                if time.time() - item.get("slot_at", item["created_at"]) > settings["request_max_age"]:
                    item["status"] = "expired"
                    atomic_json(path(state, item["request_id"]), item)
                    continue
                item["status"] = "launching"
                item["attempts"] += 1
                atomic_json(path(state, item["request_id"]), item)
                pid = detach(command_prefix + ["request-worker", item["request_id"], "--execute"])
                item["worker"] = process_identity(pid)
                atomic_json(path(state, item["request_id"]), item)
                launched.append(item["request_id"])
                break
            return {"launched": launched}


def worker(state, envelope, request_id):
    from .engine import capture, verify
    from .storage import deliver, destination_test, retention
    with state.lock("requests.lock"):
        item = read_json(path(state, request_id))
        if item["status"] != "launching":
            raise BackupError("request-not-launchable")
        item.update({"status": "running", "worker": process_identity(__import__("os").getpid())})
        atomic_json(path(state, request_id), item)
    try:
        action, arguments = item["action"], item["arguments"]
        if action == "run":
            journal = capture(state, envelope, scheduled=bool(item.get("slot")), request_id=request_id)
            result = {"run_id": journal["run_id"], "local_complete": journal["local_complete"]}
            if envelope["settings"]["destination"] == "s3":
                result.update(deliver(state, envelope, journal["run_id"]))
        elif action == "verify":
            result = verify(state, arguments["run_id"])
        elif action == "deliver":
            result = deliver(state, envelope, arguments["run_id"])
        elif action == "retention":
            result = retention(state, envelope, bool(arguments.get("execute")))
        else:
            result = destination_test(state, envelope)
        if action in ("run", "deliver"):
            from .cli import apply_automatic_retention
            apply_automatic_retention(state, envelope, result)
        item.update({"status": "complete", "result": result})
    except BackupError as error:
        deferred = {"busy", "billing-guard-stale", "billing-window-protected", "job-lock-busy"}
        item["status"] = "deferred" if error.code in deferred else "failed"
        if error.code == "initial-maintenance-scheduled-skip":
            item["status"] = "skipped"
        item["error"] = error.code
    item["finished_at"] = time.time()
    with state.lock("requests.lock"):
        atomic_json(path(state, request_id), item)
    return {"request_id": request_id, "status": item["status"], "error": item.get("error")}
