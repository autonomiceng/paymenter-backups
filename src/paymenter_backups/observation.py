"""Independent, read-only incident checks. Never launch or repair backup work."""
import time

from . import artifacts
from .common import BackupError, private_file, read_json
from .config import schedule_settings
from .requests import CAPTURE_ATTEMPTS, list_requests, next_slot
from .storage import scope_matches

MODES = {"full": "full-quiesced", "database": "database-online"}
TICK_GRACE = 5 * 60


def saved(path, default):
    try:
        private_file(path)
        return read_json(path)
    except FileNotFoundError:
        return default


def local_available(state, item):
    try:
        with artifacts.open_archive(state, item["run_id"], item) as archive:
            return artifacts.verification_current(archive)
    except (BackupError, OSError):
        return False


def check(state, envelope, now=None):
    """Return fixed incident codes, no paths, credentials or child errors.

    A new successful copy closes its location's incident. Repeated failed slots
    belong to that same incident until recovery, rather than producing mail
    every time a schedule or delivery retry fails.
    """
    now = time.time() if now is None else now
    settings = envelope["settings"]
    if not settings.get("extension_enabled", True):
        return {"installation_uuid": state.uuid, "checks": []}
    if not state.initialized:
        raise BackupError("installation-missing")
    journals = state.journals()
    requests = list_requests(state)
    by_id = {item["run_id"]: item for item in journals}
    epoch = saved(state.root / "schedule.json", {})
    initialized_at = state.binding.stat().st_mtime
    heartbeat = saved(state.root / "scheduler.json", {}).get("last_tick_at")
    checks = []
    active_since = []
    for kind, mode in MODES.items():
        if settings["settings_version"] == 1 and settings["mode"] != mode:
            continue
        view = schedule_settings(settings, kind)
        enabled = view["enabled"] and view["schedule_frequency"] != "off"
        current = epoch.get("kinds", {}).get(kind, {}) if settings["settings_version"] == 2 else epoch
        since = current.get("since", initialized_at)
        if enabled:
            active_since.append(since)
        covered = {item.get("covered_by") for item in requests if kind == "database"
                   and item.get("schedule_kind") == "database" and item.get("status") == "satisfied"
                   and item.get("schedule_since") == since}
        copies = [item for item in journals if (item.get("mode") == mode or item["run_id"] in covered)
                  and item.get("capture_complete") and item.get("service_recovered")
                  and not artifacts.deleted(item) and not artifacts.deleting(item)]
        locations = ["local"] if settings.get("keep_local", True) else []
        if settings["destination"] == "s3":
            locations.append("offsite")
        for location in locations:
            available = [item for item in copies if (
                local_available(state, item) if location == "local" else
                item.get("remote_delivered") and scope_matches(state, envelope, item)
                and (item.get("remote_observation") or {}).get("status") not in ("missing", "damaged", "unknown"))]
            latest = max((item.get("database_snapshot_lower_bound", item["created_at"])
                          if kind == "database" else item["created_at"] for item in available), default=0)
            failures = []
            for item in requests:
                if item.get("status") not in ("failed", "expired"):
                    continue  # The runtime still owns recovery and retries.
                action = item.get("action")
                run = by_id.get(item.get("arguments", {}).get("run_id"), {})
                request_mode = run.get("mode") if action == "deliver" else item.get("arguments", {}).get("mode")
                if request_mode != mode or action not in ("run", "deliver"):
                    continue
                if item.get("slot") and (not enabled or item.get("created_at", 0) < since):
                    continue
                if action == "deliver" and location != "offsite":
                    continue
                if item.get("created_at", 0) > latest:
                    failures.append(item)
            # Direct CLI captures have no request to retry. Report their terminal
            # journal only; intermediate journals from queued retries stay quiet.
            direct_failed = any(item.get("mode") == mode and not item.get("request_id")
                                and item.get("phase") == "failed" and item.get("created_at", 0) > latest
                                for item in journals)
            due = next_slot(view, now=max(since, latest)) if enabled else None
            # Allow all bounded capture attempts, compression, and retry backoff.
            # Offsite gets an additional transport window. These are our alarm
            # thresholds, derived from existing timeouts, with no new setting.
            grace = max(15 * 60, CAPTURE_ATTEMPTS * (3 * settings["capture_timeout"]
                        + max(60, settings["lock_timeout"])) + 3 * 60)
            if location == "offsite":
                grace += settings["s3_timeout"] * settings["s3_retries"] + 5 * 60
            reason = "backup-failed" if failures or direct_failed else (
                "backup-overdue" if due is not None and now > due + grace else None)
            checks.append({"key": kind + ":" + location, "reason": reason,
                           "last_good_at": latest or None, "due_at": due,
                           "deadline_at": due + grace if due is not None else None})
    if active_since:
        last_tick = heartbeat if heartbeat is not None else min(active_since)
        checks.append({"key": "scheduler", "reason": "scheduler-stopped" if now - last_tick > TICK_GRACE else None,
                       "last_good_at": heartbeat, "due_at": None, "deadline_at": last_tick + TICK_GRACE})
    return {"installation_uuid": state.uuid, "checks": checks}
