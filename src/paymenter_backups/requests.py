"""Durable requests and DST-aware backup schedule slots."""
import datetime
import hashlib
import json
import math
import pathlib
import time
import uuid
from zoneinfo import ZoneInfo

from .common import BackupError, atomic_json, private_file, read_json
from .process import detach, process_identity
from .config import schedule_settings

ACTIONS = ("run", "verify", "deliver", "retrieve", "retention", "destination-test", "delete")
ACTIVE = ("pending", "deferred", "launching", "running")
MODES = ("full-quiesced", "database-online")


def path(state, request_id):
    # Reuse the strict run-ID grammar without creating a run.
    state.run_path(request_id)
    target = state.root / "requests" / (request_id + ".json")
    if target.is_symlink():
        raise BackupError("private-state-permissions")
    return target


def create(state, action="run", arguments=None, slot=None, scheduled_metadata=None):
    if action not in ACTIONS:
        raise BackupError("request-action-invalid")
    arguments = arguments or {}
    if scheduled_metadata is not None and (not slot or set(scheduled_metadata) not in ({"slot_at", "schedule_fingerprint", "schedule_since"}, {"slot_at", "schedule_fingerprint", "schedule_since", "schedule_kind"})):
        raise BackupError("request-arguments-invalid")
    if set(arguments) - {"run_id", "execute", "mode", "actor", "location"}:
        raise BackupError("request-arguments-invalid")
    if "mode" in arguments and (action != "run" or arguments["mode"] not in MODES):
        raise BackupError("request-arguments-invalid")
    if "actor" in arguments and (type(arguments["actor"]) is not int or arguments["actor"] < 0):
        raise BackupError("request-arguments-invalid")
    if "location" in arguments and (action != "delete" or arguments["location"] not in ("local", "s3", "all")):
        raise BackupError("request-arguments-invalid")
    if action in ("verify", "deliver", "retrieve", "delete"):
        state.run_path(arguments.get("run_id"))
    if action == "delete" and arguments.get("execute") is not True:
        raise BackupError("delete-confirmation-required")
    request_id = hashlib.sha256((state.uuid + ":" + slot).encode()).hexdigest()[:32] if slot else uuid.uuid4().hex
    target = path(state, request_id)
    with state.lock("requests.lock"):
        if target.exists():
            return read_json(target)
        existing = list_requests(state)
        if not slot:
            # A repeated click returns the waiting request instead of queueing a copy.
            comparable = {key: value for key, value in arguments.items() if key != "actor"}
            for item in existing:
                if (item.get("status") in ("pending", "deferred") and not item.get("slot") and item.get("action") == action
                        and {key: value for key, value in item.get("arguments", {}).items() if key != "actor"} == comparable):
                    return item
        if action == "delete":
            from . import artifacts
            journal = artifacts.journal(state, arguments["run_id"])
            if journal.get("retained_deleted"):
                raise BackupError("artifact-deleted")
            if not journal.get("local_complete") and not journal.get("remote_delivered") and not journal.get("delete_intent"):
                raise BackupError("run-incomplete")
            if any(item.get("status") in ACTIVE and item.get("arguments", {}).get("run_id") == arguments["run_id"] for item in existing):
                raise BackupError("delete-conflict")
        item = {"schema_version": 1, "request_id": request_id, "installation_uuid": state.uuid,
                "created_at": time.time(), "action": action, "arguments": arguments,
                "slot": slot, "status": "pending", "attempts": 0}
        if scheduled_metadata is not None:
            item.update(scheduled_metadata)
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


def due_slots(settings, now=None, since=None, window_seconds=None, kind=None):
    if kind is not None:
        settings = schedule_settings(settings, kind)
    if settings["schedule_frequency"] == "off":
        return
    now = time.time() if now is None else now
    zone = ZoneInfo(settings["schedule_timezone"])
    # Enumerating real UTC minutes handles local gaps naturally; repeated wall
    # slots share a single durable identifier and therefore execute once.
    window = min(settings["request_max_age"], 172800) if window_seconds is None else min(window_seconds, 8 * 86400)
    lower = now - window
    if since is not None:
        lower = max(lower, since)
    start = int(lower // 60)
    end = int(now // 60)
    seen = set()
    for minute in range(start, end + 1):
        if minute * 60 < lower:
            continue
        if since is not None and minute * 60 <= since:
            continue
        local = datetime.datetime.fromtimestamp(minute * 60, zone)
        if settings.get("settings_version", 1) == 2 and local.fold:
            continue
        if not slot_matches(settings, local):
            continue
        if settings["schedule_frequency"] == "weekly" and local.weekday() not in settings["schedule_weekdays"]:
            continue
        slot = local.strftime("%Y-%m-%dT%H:%M") + "@" + settings["schedule_timezone"]
        if slot not in seen:
            seen.add(slot)
            yield slot, minute * 60


def next_slot(settings, now=None, horizon=8 * 86400, kind=None):
    """First future schedule minute, using the same rules as due_slots."""
    if kind is not None:
        settings = schedule_settings(settings, kind)
    if settings["schedule_frequency"] == "off":
        return None
    now = time.time() if now is None else now
    zone = ZoneInfo(settings["schedule_timezone"])
    start = int(now // 60) + 1
    seen = {slot for slot, _ in due_slots(settings, now, window_seconds=86400)} if settings.get("settings_version", 1) == 2 else set()
    for minute in range(start, start + horizon // 60):
        local = datetime.datetime.fromtimestamp(minute * 60, zone)
        if settings.get("settings_version", 1) == 2 and local.fold:
            continue
        if not slot_matches(settings, local):
            continue
        if settings["schedule_frequency"] == "weekly" and local.weekday() not in settings["schedule_weekdays"]:
            continue
        slot = local.strftime("%Y-%m-%dT%H:%M") + "@" + settings["schedule_timezone"]
        if slot not in seen:
            return minute * 60
    return None


def slot_matches(settings, local):
    if settings["schedule_frequency"] == "hourly":
        return local.hour % settings["database_every_hours"] == 0 and local.minute == settings["database_minute"]
    return local.strftime("%H:%M") == settings["schedule_time"]


def schedule_fingerprint(settings):
    fields = ("enabled", "schedule_frequency", "schedule_time", "schedule_timezone", "schedule_weekdays")
    values = {key: settings[key] for key in fields}
    values["schedule_weekdays"] = sorted(set(values["schedule_weekdays"]))
    if settings["schedule_frequency"] == "hourly":
        values.update(database_every_hours=settings["database_every_hours"], database_minute=settings["database_minute"])
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sync_schedule(state, settings, now):
    if settings.get("settings_version", 1) == 2:
        return _sync_schedules_v2(state, settings, now)
    target = state.root / "schedule.json"
    fingerprint = schedule_fingerprint(settings)
    previous = None
    if target.exists() or target.is_symlink():
        private_file(target)
        previous = read_json(target)
        if (not isinstance(previous, dict) or previous.get("schema_version") != 1
                or not isinstance(previous.get("fingerprint"), str)
                or type(previous.get("since")) not in (int, float)
                or not math.isfinite(previous["since"])):
            raise BackupError("private-state-permissions")
    if previous is None or previous["fingerprint"] != fingerprint or previous["since"] > now:
        previous = {"schema_version": 1, "fingerprint": fingerprint, "since": now}
        atomic_json(target, previous)
    return previous


def sync_schedule(state, settings, now=None, settings_loader=None):
    """Record schedule edits at save time without starting work or backfilling."""
    with state.lock("tick.lock", timeout=10):
        if settings_loader is not None:
            settings = settings_loader()
        return _sync_schedule(state, settings, time.time() if now is None else now)


def tick(state, envelope, command_prefix, envelope_loader=None):
    from .maintenance import recover
    # Independently usable recovery is called by the CLI before bridge reads too.
    if (state.app / "storage/framework/down").exists():
        return {"maintenance": True, "recovery": recover(state, auto=True)}
    settings = envelope["settings"]
    if not settings.get("extension_enabled", True):
        return {"enabled": False}
    with state.lock("tick.lock"):
        if envelope_loader is not None:
            envelope = envelope_loader()
            settings = envelope["settings"]
            if not settings.get("extension_enabled", True):
                return {"enabled": False}
        now = time.time()
        from .discovery import refresh_cache
        refresh_cache(state, envelope, now)
        epoch = _sync_schedule(state, settings, now)
        v2 = settings.get("settings_version", 1) == 2
        kinds = ("full", "database") if v2 else (None,)
        dues = {}
        for kind in kinds:
            view = schedule_settings(settings, kind) if v2 else settings
            current = epoch["kinds"][kind] if v2 else epoch
            due = []
            if view["enabled"]:
                lower = max(current["since"], now - 8 * 86400)
                due = list(due_slots(view, now, since=lower, window_seconds=8 * 86400))
                if due:
                    slot, instant = due[-1]
                    slot += ":" + current["fingerprint"][:16] + ":" + str(current["since"])
                    metadata = {"slot_at": instant, "schedule_fingerprint": current["fingerprint"], "schedule_since": current["since"]}
                    if v2:
                        slot = kind + ":" + slot
                        metadata["schedule_kind"] = kind
                    mode = ("full-quiesced" if kind == "full" else "database-online") if v2 else settings["mode"]
                    create(state, arguments={"mode": mode}, slot=slot, scheduled_metadata=metadata)
            dues[kind] = due
        reconcile_coverage(state)
        with state.lock("requests.lock"):
            items = list_requests(state)
            eligible = []
            for item in items:
                if item["status"] in ("launching", "running"):
                    if item.get("worker") and process_identity(item["worker"]["pid"]) == item["worker"]:
                        return {"busy": True}
                    item["status"] = "deferred"
                    item["error"] = "worker-interrupted"
                if item["status"] not in ("pending", "deferred"):
                    continue
                kind = item.get("schedule_kind") if v2 else None
                current = epoch["kinds"].get(kind) if v2 else epoch
                view = schedule_settings(settings, kind) if v2 and kind in kinds else settings
                due = dues.get(kind, [])
                if item.get("slot") and (current is None or not view["enabled"]
                        or item.get("schedule_fingerprint") != current["fingerprint"]
                        or item.get("schedule_since") != current["since"]
                        or (due and item.get("slot_at", 0) < due[-1][1])):
                    item.update(status="cancelled", error="schedule-superseded", updated_at=now)
                    atomic_json(path(state, item["request_id"]), item)
                    continue
                if now - item["created_at"] > settings["request_max_age"]:
                    item["status"] = "expired"
                    atomic_json(path(state, item["request_id"]), item)
                    continue
                # Freeze old mode-less pending requests before any later edit.
                if item["action"] == "run" and not item["arguments"].get("mode"):
                    item["arguments"]["mode"] = settings["mode"]
                atomic_json(path(state, item["request_id"]), item)
                if item.get("retry_at", 0) <= now:
                    eligible.append(item)
            # Least recently attempted first; full wins equal/new obligations.
            eligible.sort(key=lambda item: (max(item["created_at"], item.get("last_attempt_at", 0)),
                           item.get("attempts", 0) > 0,
                           0 if item.get("arguments", {}).get("mode") == "full-quiesced" else 1,
                           item["created_at"]))
            launched = []
            if eligible:
                item = eligible[0]
                item.update(status="launching", attempts=item["attempts"] + 1, last_attempt_at=now)
                atomic_json(path(state, item["request_id"]), item)
                pid = detach(command_prefix + ["request-worker", item["request_id"], "--execute"])
                item["worker"] = process_identity(pid)
                atomic_json(path(state, item["request_id"]), item)
                launched.append(item["request_id"])
            return {"launched": launched}


def _sync_schedules_v2(state, settings, now):
    target = state.root / "schedule.json"
    previous = None
    if target.exists() or target.is_symlink():
        private_file(target)
        previous = read_json(target)
        if not isinstance(previous, dict) or previous.get("schema_version") not in (1, 2):
            raise BackupError("private-state-permissions")
    old = previous.get("kinds", {}) if previous and previous["schema_version"] == 2 else {}
    result = {"schema_version": 2, "kinds": {}}
    for kind in ("full", "database"):
        fingerprint = schedule_fingerprint(schedule_settings(settings, kind))
        current = old.get(kind)
        if current is not None and (not isinstance(current, dict) or not isinstance(current.get("fingerprint"), str)
                or type(current.get("since")) not in (int, float) or not math.isfinite(current["since"])):
            raise BackupError("private-state-permissions")
        if current is None or current["fingerprint"] != fingerprint or current["since"] > now:
            current = {"fingerprint": fingerprint, "since": now}
        result["kinds"][kind] = current
    if result != previous:
        atomic_json(target, result)
    return result


def freeze_legacy_requests(state, mode):
    """Freeze pre-0.4 mode-less obligations before changing legacy defaults."""
    with state.lock('requests.lock'):
        for item in list_requests(state):
            if item.get('status') in ('pending', 'deferred') and item.get('action') == 'run' and not item.get('arguments', {}).get('mode'):
                item['arguments']['mode'] = mode
                atomic_json(path(state, item['request_id']), item)


def reconcile_coverage(state):
    """Reconcile from durable capture evidence, including after delivery failure.

    A journal's completion proof is written before any network/delivery attempt.
    Pending manual requests are never absorbed.
    """
    fulls = [item for item in state.journals() if item.get("mode") == "full-quiesced"
             and item.get("capture_complete") and item.get("service_recovered")
             and item.get("database_coverage") and type(item.get("database_snapshot_lower_bound")) in (int, float)
             and math.isfinite(item["database_snapshot_lower_bound"])]
    epoch = read_json(state.root / 'schedule.json') if (state.root / 'schedule.json').exists() else {}
    current = epoch.get('kinds', {}).get('database')
    with state.lock("requests.lock"):
        for item in list_requests(state):
            if (item.get("schedule_kind") != "database" or not item.get("slot")
                    or item.get("status") not in ("pending", "deferred")):
                continue
            if current is not None and (item.get('schedule_fingerprint') != current['fingerprint'] or item.get('schedule_since') != current['since']):
                continue
            covering = next((run for run in fulls if run["database_snapshot_lower_bound"] >= item["slot_at"]), None)
            if covering:
                item.update(status="satisfied", covered_by=covering["run_id"], finished_at=time.time())
                atomic_json(path(state, item["request_id"]), item)


def worker(state, envelope, request_id, envelope_loader=None):
    from .engine import capture, verify
    from .storage import delete_run, deliver, destination_test, retention, retrieve
    with state.lock("requests.lock"):
        item = read_json(path(state, request_id))
        if item["status"] != "launching":
            raise BackupError("request-not-launchable")
        item.update({"status": "running", "worker": process_identity(__import__("os").getpid())})
        atomic_json(path(state, request_id), item)
    try:
        action, arguments = item["action"], item["arguments"]
        if action == "run":
            if arguments.get("mode"):
                envelope["settings"]["mode"] = arguments["mode"]
            journal = capture(state, envelope, scheduled=bool(item.get("slot")), request_id=request_id)
            reconcile_coverage(state)
            result = {"run_id": journal["run_id"], "local_complete": journal["local_complete"]}
            if envelope["settings"]["destination"] == "s3":
                result.update(deliver(state, envelope, journal["run_id"], envelope_loader=envelope_loader))
        elif action == "verify":
            # Recording a verification needs the run lock; busy work defers.
            with state.lock():
                result = verify(state, arguments["run_id"], record=True, envelope=envelope)
        elif action == "retrieve":
            result = retrieve(state, envelope, arguments["run_id"])
        elif action == "delete":
            result = delete_run(state, envelope, arguments["run_id"], request_id, location=arguments.get("location", "all"))
        elif action == "deliver":
            result = deliver(state, envelope, arguments["run_id"], envelope_loader=envelope_loader)
        elif action == "retention":
            result = retention(state, envelope, bool(arguments.get("execute")))
        else:
            result = destination_test(state, envelope, envelope_loader=envelope_loader)
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
        if item["status"] == "deferred":
            item["retry_at"] = time.time() + 60
    item["finished_at"] = time.time()
    with state.lock("requests.lock"):
        atomic_json(path(state, request_id), item)
    return {"request_id": request_id, "status": item["status"], "error": item.get("error")}
