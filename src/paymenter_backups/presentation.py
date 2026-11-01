"""Whitelisted overview for the administrator page and terminal summaries.

Output holds fixed codes, run and request IDs, timestamps, sizes, booleans
and configured nonsecret settings. It never includes artifact paths,
manifests, database grants, process identities, request results, stderr or
credential material.
"""
import datetime
import hashlib
import hmac
import json
import pathlib
import re
import time
import urllib.parse

from . import artifacts
from .common import BackupError, private_file, read_json, safe_writer_cause
from .config import DEFAULTS
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
    remote = item.get("remote") or item.get("delivery_intent") or {}
    from . import remote_artifacts
    try:
        entry["remote_format"] = remote_artifacts.format_of(remote) if remote else None
    except BackupError:
        entry["remote_format"] = None
    fingerprint = remote.get("key_fingerprint")
    entry["key_short"] = fingerprint[:16] if isinstance(fingerprint, str) and re.fullmatch("[0-9a-f]{64}", fingerprint) else None
    entry["ciphertext_size"] = number((remote.get("archive_ciphertext") or {}).get("size"))
    entry["remote_verification_level"] = code((item.get("remote_verification") or {}).get("level"))
    entry['staging_removal_error'] = code(item.get('staging_removal_error'))
    entry['staging_removal_pending'] = bool(item.get('staging_removal_intent'))
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


def request_entry(item, state=None, envelope=None, destination_binding=None):
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    entry = {"request_id": identifier(item.get("request_id")), "action": code(item.get("action")),
            "status": code(item.get("status")), "created_at": number(item.get("created_at")),
            "finished_at": number(item.get("finished_at") or item.get("updated_at")),
            "attempts": number(item.get("attempts")), "error": code(item.get("error")),
            "run_id": identifier(arguments.get("run_id")), "mode": code(arguments.get("mode")),
            "scheduled": bool(item.get("slot")), "slot_at": number(item.get("slot_at")),
            "actor": arguments.get("actor") if type(arguments.get("actor")) is int else None,
            "location": code(arguments.get("location")),
            "result_destination_id": identifier(result.get("destination_id")),
            "skipped_count": len(result["skipped"]) if isinstance(result.get("skipped"), list) else None,
            "result_run_id": identifier(result.get("run_id")),
            "schedule_kind": item.get("schedule_kind") if item.get("schedule_kind") in ("full", "database") else None,
            "covered_by": identifier(item.get("covered_by"))}
    if item.get("action") == "destination-test":
        entry["result_verified"] = destination_test_current(state, envelope, item, result, destination_binding)
    return entry


def destination_credentials_marker(state, envelope):
    """Bind proof to current S3 credentials without persisting a secret or its hash."""
    key = envelope.get('framework', {}).get('app_key')
    credentials = envelope.get('credentials') or {}
    values = [credentials.get(name, '') for name in ('access_key', 'secret_key', 'session_token')]
    if not isinstance(key, str) or not key or not all(isinstance(value, str) for value in values) or not all(values[:2]):
        return None
    payload = json.dumps([state.uuid, *values], separators=(',', ':')).encode()
    return hmac.new(key.encode(), b'backups-destination-credentials-v1\0' + payload, hashlib.sha256).hexdigest()


def destination_test_binding(state, envelope):
    """Build one safe current binding; reuse expensive key validation in overview."""
    if state is None or envelope is None:
        return {}
    from . import encryption, remote_artifacts
    from .storage import destination_id
    try:
        binding = {"destination": envelope["settings"]["destination"],
                   "destination_id": destination_id(state, envelope), "key_status": None}
        if binding["destination"] == "s3":
            binding["credentials_marker"] = destination_credentials_marker(state, envelope)
            binding["format"] = encryption.FORMAT if encryption.enabled(envelope) else remote_artifacts.PLAINTEXT
            if binding["format"] == encryption.FORMAT:
                try:
                    record = encryption.key(envelope, active=True)
                    binding["key_fingerprint"] = record["fingerprint"]["value"]
                    binding["key_status"] = {"code": None}
                except BackupError as error:
                    binding["key_status"] = {"code": error.code}
        return binding
    except (BackupError, KeyError, TypeError, ValueError):
        return {}


def destination_test_current(state, envelope, item, result, destination_binding=None):
    """A test badge proves its current provider, credentials, format and acknowledged key."""
    if item.get("status") != "complete" or result.get("verified") is not True:
        return False
    from . import encryption, remote_artifacts
    binding = destination_binding if destination_binding is not None else destination_test_binding(state, envelope)
    if (not binding or result.get("destination") != binding["destination"]
            or result.get("destination_id") != binding["destination_id"]):
        return False
    if binding["destination"] == "local":
        return True
    if not binding.get('credentials_marker') or result.get('credentials_marker') != binding['credentials_marker']:
        return False
    if result.get("format", remote_artifacts.PLAINTEXT) != binding["format"]:
        return False
    if binding["format"] == encryption.FORMAT:
        return bool(binding.get("key_fingerprint") and result.get("key_fingerprint") == binding["key_fingerprint"])
    return "key_fingerprint" not in result


def origin(url):
    try:
        from .maintenance import web_origin
        return web_origin(url or "", allow_http=True)
    except BackupError:
        pass
    return None


def configuration(state, envelope, error=None, encryption_key_status=None, writer_cause=None, destination_binding=None):
    writer_cause = safe_writer_cause(writer_cause, fields=DEFAULTS)
    if envelope is None:
        result = {"available": False, "valid": False, "error": code(error) or "unknown"}
        if writer_cause is not None:
            result["writer_cause"] = writer_cause
        return result
    settings = envelope["settings"]
    v2 = settings.get("settings_version", 1) == 2
    from .tools import checks
    tool_reports = envelope.get("tool_checks")
    # Validation can stop before emitting tool reports. Resolve capabilities for
    # the overview in that case so configuration errors do not become false
    # "tool not found" diagnostics.
    if not isinstance(tool_reports, dict):
        tool_reports = checks(envelope, capabilities=error is not None)
    local_path = settings.get("local_path") or str(state.root / "backups" if v2 else state.default_destination())
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
    if writer_cause is not None:
        result["writer_cause"] = writer_cause
    validation = envelope.get("validation_error")
    field = validation.get("field") if isinstance(validation, dict) else None
    if isinstance(field, str) and field in settings:
        result["error_field"] = field
    from . import encryption
    namespace = envelope.get("archive_encryption_credentials") or {}
    records = namespace.get("keys") or {}
    active = records.get(namespace.get("active_fingerprint")) or {}
    pending = records.get(namespace.get("pending_fingerprint")) or {}
    selected = active or pending
    active_fp = (active.get("fingerprint") or {}).get("value")
    pending_fp = (pending.get("fingerprint") or {}).get("value")
    def short(value):
        return value[:16] if isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) else None
    try:
        binary, _ = encryption.tool(envelope)
        tool_status = {"ok": True, "path": binary, "code": None}
    except BackupError as failure:
        tool_status = {"ok": False, "path": None, "code": failure.code}
    result["encryption"] = {"enabled": encryption.enabled(envelope), "tool": tool_status,
        "key": {"state": "active" if active else "pending" if pending else "none", "short": short(active_fp or pending_fp),
                "source": selected.get("source") if selected.get("source") in ("generated", "passphrase") else None,
                "acknowledged": bool(active_fp and (active.get("acknowledgement") or {}).get("fingerprint") == active_fp),
                "created_at": number(selected.get("created_at")), "pending_short": short(pending_fp),
                # Nonsecret PBKDF2 identity (also in remote descriptors); the page acknowledges it.
                "pending_fingerprint": pending_fp if isinstance(pending_fp, str) and re.fullmatch("[0-9a-f]{64}", pending_fp) else None},
        "readiness_code": encryption.readiness(envelope, key_status=encryption_key_status)}
    app_url = (envelope.get("framework") or {}).get("app_url", "")
    current_origin = origin(app_url)
    probe_credentials = envelope.get("maintenance_probe_credentials") or {}
    saved_origin = origin(probe_credentials.get("origin"))
    state_code = "unavailable" if current_origin is None else "http" if current_origin.startswith("http://") else "not-saved" if not probe_credentials else "saved" if saved_origin == current_origin else "origin-changed"
    result["site_password"] = {"state": state_code, "origin": current_origin, "saved_origin": saved_origin,
                               "probe_base": current_origin + urllib.parse.urlsplit(app_url).path if current_origin else None}
    from .discovery import cached, suggestions
    from .readiness import state_for, stores
    result['settings_version'] = settings.get('settings_version', 1)
    result['schedules'] = {'timezone': settings['schedule_timezone']}
    for kind in ('full', 'database'):
        result['schedules'][kind] = {'frequency': settings[kind + '_schedule'], 'time': settings[kind + '_time'],
                                    'weekdays': list(settings[kind + '_weekdays']), 'next_at': None}
        if kind == 'database':
            result['schedules'][kind].update(every_hours=settings['database_every_hours'], minute=settings['database_minute'])
        try:
            # Adapted v1 fields are for display; singleton next-slot semantics stay.
            selected = 'full' if settings['mode'] == 'full-quiesced' else 'database'
            if v2:
                result['schedules'][kind]['next_at'] = next_slot(settings, kind=kind)
            elif kind == selected and settings['enabled']:
                result['schedules'][kind]['next_at'] = next_slot(settings)
        except (BackupError, KeyError, ValueError):
            pass
    result['storage'] = {'keep_local': bool(settings['keep_local']), 'upload_s3': bool(settings['upload_s3']),
                         'local_path': local_path, 'default_local_path': str(state.root / 'backups'),
                         'legacy_default_path': str(state.default_destination()) if state.default_destination() != state.root / 'backups' else None,
                         **{key: result['destination'][key] for key in ('s3_bucket', 's3_region', 's3_endpoint', 's3_prefix', 's3_path_style')}}
    if v2:
        result['retention'] = {'automatic': bool(settings['automatic_retention'])}
    for kind in ('full', 'database'):
        result['retention'][kind] = {'local_keep': settings['retention_keep_' + kind],
                                    **{'s3_' + suffix: settings['s3_retention_' + suffix + '_' + kind] for suffix in ('policy', 'keep', 'days')}}
    jobs = cached(state)
    try:
        import os, pwd
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = str(os.getuid())
    result['checks'] = {'jobs': {key: jobs.get(key) for key in ('checked_at', 'state', 'error', 'items')} |
                       {'suggestions': suggestions(state, envelope, jobs), 'crontab_user': user},
                       'stores': stores(envelope), 'tools': tool_reports,
                       'billing': billing_window(settings, envelope.get('billing', {}))}
    # Use the encryption module's delivery probe for the shared tool diagnostics.
    result['checks']['tools']['gpg'] = {
        'configured': settings.get('gpg', ''),
        'resolved': tool_status['path'], 'ok': tool_status['ok'], 'code': tool_status['code']}
    result['readiness'] = {}
    if settings['enabled'] and error is None and not v2:
        result['schedule']['next_at'] = next_slot(settings)
    for mode in ('full-quiesced', 'database-online'):
        proposed = dict(envelope, settings=dict(settings, mode=mode), tool_checks=tool_reports)
        try:
            proposed_plan = plan(state, proposed, jobs, encryption_key_status={'code': result['encryption']['readiness_code']})
            items = proposed_plan['readiness_items']
            admission = proposed_plan['admission_errors']
        except BackupError as failure:
            items = [{'code': code(failure.code), 'field': failure.field, 'detail': failure.detail}]
            admission = [code(failure.code)]
        if error:
            validation = envelope.get('validation_error', {})
            items.insert(0, {'code': code(error), 'field': validation.get('field'), 'detail': validation.get('detail', {})})
            admission = [code(error), *admission]
        result['admission'][mode] = list(dict.fromkeys(admission))
        result['readiness'][mode] = {'state': state_for(items), 'items': items}
    from .commissioning import check as installation_check
    result['installation'] = installation_check(state, envelope, inventory=jobs, mode_readiness=result['readiness'],
        destination_binding=destination_binding, encryption_key_status={'code': result['encryption']['readiness_code']})
    if state.root.exists():
        used = storage_usage(state, settings)
        result["space"]["used"] = used
        result["space"]["state"] = "full" if used >= settings["local_byte_budget"] else "ok"
    result["retention"]["pending"] = {kind: {"local": None, "s3": None} for kind in ("full", "database")}
    if not v2:
        result["retention"]["pending"].update(local=None, s3=None)
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
            if v2:
                result['retention']['pending'] = {kind: {'local': len(part['local_delete']), 's3': len(part['remote_delete'])} for kind, part in preview['by_kind'].items()}
                journal_kinds = {item['run_id']: 'full' if item.get('mode') == 'full-quiesced' else 'database' for item in state.journals()}
                for run_id in preview.get('staging_removal', []):
                    kind = journal_kinds[run_id]
                    if run_id not in preview['by_kind'][kind]['local_delete']:
                        result['retention']['pending'][kind]['local'] += 1
            else:
                modes = {item['run_id']: item.get('mode') for item in state.journals()}
                result['retention']['pending'] = {kind: {location: sum(modes.get(run_id) == mode for run_id in preview[key]) for location, key in (('local', 'local_delete'), ('s3', 'remote_delete'))} for kind, mode in (('full', 'full-quiesced'), ('database', 'database-online'))}
                result['retention']['pending'].update(local=len(preview['local_delete']), s3=len(preview['remote_delete']))
        except BackupError:
            pass
    return result


def billing_window(settings, billing):
    value = {'time': billing.get('daily_time'), 'timezone': billing.get('timezone'),
             'window_before': None, 'window_after': None}
    try:
        daily = datetime.datetime.strptime(value['time'], '%H:%M')
        from .maintenance import job_lock_wait
        wait = job_lock_wait(settings)
        if settings.get('settings_version', 1) == 2:
            wait = max(settings['lock_timeout'],
                       job_lock_wait(dict(settings, mode='database-online')))
        before = wait + settings['capture_timeout'] + 30 + settings['billing_margin_seconds']
        after = 60 + settings['billing_margin_seconds']
        value['window_before'] = (daily - datetime.timedelta(seconds=before)).strftime('%H:%M')
        value['window_after'] = (daily + datetime.timedelta(seconds=after)).strftime('%H:%M')
    except (TypeError, ValueError, OverflowError):
        pass
    return value


def scheduler(state):
    path = state.root / "scheduler.json"
    try:
        private_file(path)
        return {"last_tick_at": number(read_json(path).get("last_tick_at"))}
    except (OSError, BackupError):
        return {"last_tick_at": None}


def job_wait_overdue(requests, journals, now):
    # Keep the first contention time across superseded hourly slots. A successful
    # full checkpoint also resolves the missing database snapshot.
    latest = max((item.get("created_at", 0) for item in journals
                  if item.get("capture_complete") and item.get("service_recovered")), default=0)
    return any(item.get("action") == "run" and item.get("arguments", {}).get("mode") == "database-online"
               and (item.get("status") != "cancelled" or item.get("error") == "schedule-superseded")
               and latest < item.get("job_wait_since", now) <= now - 86400
               for item in requests)


def database_job_coordination(state):
    try:
        value = read_json(state.root / "job-coordination.json")
        if value.get("state") in ("absent", "verified", "unverified"):
            return {"state": value["state"], "checked_at": number(value.get("checked_at")), "error": code(value.get("error"))}
    except (OSError, BackupError):
        pass
    return {"state": "unknown", "checked_at": None, "error": None}


def overview(state, envelope=None, error=None, writer_cause=None):
    journals = state.journals() if state.initialized else []
    requests = list_requests(state) if state.initialized else []
    active_runs = {(item.get("action"), (item.get("arguments") or {}).get("run_id")) for item in requests if item.get("status") in ACTIVE}
    runs = sorted((run_entry(state, item, active_runs, envelope) for item in journals), key=lambda item: item["created_at"] or 0, reverse=True)
    destination_binding = destination_test_binding(state, envelope)
    ordered = sorted((request_entry(item, state, envelope, destination_binding) for item in requests), key=lambda item: item["created_at"] or 0, reverse=True)
    active = [item for item in ordered if item["status"] in ACTIVE]
    recent = [item for item in ordered if item["status"] not in ACTIVE][:REQUEST_HISTORY]
    pending_s3 = [item for item in journals if item.get("local_complete") and not item.get("remote_delivered") and not item.get("remote_pruned") and not item.get("retained_deleted")]
    verified_remote = {item["run_id"] for item in journals if item.get("remote_verification")}
    last_good = {"local": next((item["run_id"] for item in runs if item["local"] and item["download"] == "ready"), None),
                 "s3": next((item["run_id"] for item in runs if item["remote"] in ("delivered", "present") and item["run_id"] in verified_remote), None)}
    for kind, mode in (('full', 'full-quiesced'), ('database', 'database-online')):
        last_good[kind] = next((item['run_id'] for item in runs if item['mode'] == mode and
                              ((item['local'] and item['download'] == 'ready') or (item['remote'] in ('delivered', 'present') and item['run_id'] in verified_remote))), None)
        last_good['s3_' + kind] = next((item['run_id'] for item in runs if item['mode'] == mode and item['remote'] in ('delivered', 'present') and item['run_id'] in verified_remote), None)
    return {"schema_version": 1, "generated_at": time.time(), "initialized": state.initialized,
            "installation_uuid": state.uuid, "maintenance": (state.app / "storage/framework/down").exists(),
            "scheduler": scheduler(state) if state.initialized else {"last_tick_at": None},
            "configuration": configuration(state, envelope, error, encryption_key_status=destination_binding.get("key_status"),
                                            writer_cause=writer_cause, destination_binding=destination_binding), "runs": runs,
            "last_good": last_good,
            "job_wait_overdue": job_wait_overdue(requests, journals, time.time()),
            "database_job_coordination": database_job_coordination(state),
            "s3_pending": {"count": len(pending_s3) if envelope and envelope["settings"]["destination"] == "s3" else 0,
                           "oldest_at": min((number(item.get("created_at")) or 0 for item in pending_s3), default=None) if envelope and envelope["settings"]["destination"] == "s3" else None},
            "requests": active + recent}
