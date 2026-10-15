import json
import pathlib
import os
import shutil
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import BackupError

DEFAULTS = {
    "enabled": False, "mode": "full-quiesced", "destination": "local",
    "schedule_frequency": "daily", "schedule_time": "02:00", "schedule_weekdays": [0],
    "schedule_timezone": "UTC", "local_byte_budget": 10 * 1024**3,
    "minimum_free_bytes": 1024**3, "capture_timeout": 300, "drain_seconds": 30,
    "lock_timeout": 15, "billing_margin_seconds": 120, "billing_guard_max_age": 180,
    "request_max_age": 86400, "job_lock_paths": [], "retention_keep": 7,
    "plaintext_offsite_acknowledged": False, "s3_region": "us-east-1", "s3_bucket": "",
    "s3_endpoint": "", "s3_prefix": "paymenter", "s3_path_style": False,
    "s3_timeout": 30, "s3_retries": 3, "cooperative_writers_acknowledged": False,
    "external_state_acknowledged": False, "no_concurrent_ddl_acknowledged": False,
    "local_path": "", "extra_docroots": [], "automatic_retention": True,
}


def billing_fingerprint(billing):
    import hashlib
    value = {"daily_time": billing.get("daily_time"), "timezone": billing.get("timezone")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def redact(value):
    sensitive = {"password", "app_key", "app_previous_keys", "access_key", "secret_key", "session_token", "credentials", "maintenance_probe_credentials"}
    if isinstance(value, dict):
        return {key: "[redacted]" if key in sensitive else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def bridge(args, verb="read", payload=None):
    from .process import safe_environment
    if not args.bridge:
        raise BackupError("bridge-required")
    command = [args.php, args.bridge, verb, "--app", str(args.app), "--private-root", str(args.private_root)]
    try:
        result = subprocess.run(command, input=json.dumps(payload).encode() if payload is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, close_fds=True, env=safe_environment())
        if result.returncode:
            try:
                error = json.loads(result.stderr).get("error")
            except (ValueError, AttributeError):
                error = None
            if error in {"credentials-unreadable", "database-tls-unsupported", "database-driver-unsupported",
                         "database-prefix-unsupported", "native-secret-row-refused", "billing-settings-unreadable",
                         "installation-mismatch", "private-root-invalid", "private-file-invalid",
                         "private-root-inside-docroot", "private-pipe-required", "application-unavailable",
                         "extension-uninstalled", "credentials-invalid", "private-write-failed", "maintenance-probe-credentials-invalid", "maintenance-probe-origin-mismatch"}:
                raise BackupError(error)
            raise BackupError("bridge-failed")
        value = json.loads(result.stdout)
        if isinstance(value, dict) and value.get("error"):
            raise BackupError(value["error"] if value["error"] in ("credentials-unreadable", "config-invalid") else "bridge-failed")
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BackupError("bridge-failed") from None


def load(args):
    envelope = bridge(args)
    if envelope.get("schema_version") != 1 or pathlib.Path(envelope.get("app", "")).resolve() != pathlib.Path(args.app).resolve():
        raise BackupError("bridge-identity-mismatch")
    import os
    if envelope.get("owner_uid") != os.getuid() or pathlib.Path(envelope.get("private_root", "")).resolve() != pathlib.Path(args.private_root).resolve():
        raise BackupError("bridge-identity-mismatch")
    envelope["settings"] = dict(DEFAULTS, **envelope.get("settings", {}))
    normalize(envelope["settings"])
    validate(envelope)
    return envelope


def normalize(settings):
    for key, default in DEFAULTS.items():
        value = settings[key]
        try:
            if isinstance(default, bool):
                if value not in (True, False, "1", "0", "true", "false", "", 1, 0):
                    raise ValueError()
                settings[key] = value in (True, "1", "true", 1)
            elif isinstance(default, int):
                if isinstance(value, bool):
                    raise ValueError()
                settings[key] = int(value)
            elif isinstance(default, list) and isinstance(value, str):
                settings[key] = json.loads(value)
        except (TypeError, ValueError):
            raise BackupError("config-invalid") from None


def validate(envelope):
    settings = envelope["settings"]
    if settings["mode"] not in ("full-quiesced", "database-online") or settings["destination"] not in ("local", "s3"):
        raise BackupError("config-invalid")
    if settings["schedule_frequency"] not in ("daily", "weekly"):
        raise BackupError("config-invalid")
    try:
        hour, minute = settings["schedule_time"].split(":")
        if len(hour) != 2 or len(minute) != 2 or not 0 <= int(hour) < 24 or not 0 <= int(minute) < 60:
            raise ValueError()
        ZoneInfo(settings["schedule_timezone"])
        if any(type(day) is not int or day not in range(7) for day in settings["schedule_weekdays"]):
            raise ValueError()
        if settings["enabled"] and settings["schedule_frequency"] == "weekly" and not settings["schedule_weekdays"]:
            raise ValueError()
        for key in ("local_byte_budget", "capture_timeout", "billing_guard_max_age", "request_max_age", "s3_timeout", "s3_retries", "retention_keep"):
            if isinstance(settings[key], bool) or int(settings[key]) <= 0:
                raise ValueError()
        for key in ("minimum_free_bytes", "drain_seconds", "lock_timeout", "billing_margin_seconds"):
            if isinstance(settings[key], bool) or int(settings[key]) < 0:
                raise ValueError()
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise BackupError("config-invalid") from None
    if not isinstance(settings["job_lock_paths"], list) or any(not pathlib.Path(item).is_absolute() for item in settings["job_lock_paths"]):
        raise BackupError("config-invalid")
    if not isinstance(settings["extra_docroots"], list) or any(not pathlib.Path(item).is_absolute() for item in settings["extra_docroots"]):
        raise BackupError("config-invalid")
    if settings["destination"] == "s3":
        if not settings["plaintext_offsite_acknowledged"] or not settings["s3_bucket"]:
            raise BackupError("plaintext-offsite-acknowledgement-required")
        if settings["s3_endpoint"] and not settings["s3_endpoint"].startswith("https://"):
            raise BackupError("s3-tls-required")
        if settings["s3_endpoint"]:
            from urllib.parse import urlsplit
            endpoint = urlsplit(settings["s3_endpoint"])
            if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise BackupError("s3-endpoint-invalid")
    tools = envelope.setdefault("tools", {})
    for name, default in (("python", "python3"), ("php", "php"), ("dump", "mariadb-dump"), ("mysql", "mariadb"), ("tar", "tar"), ("gzip", "gzip")):
        value = tools.get(name) or shutil.which(default)
        if value:
            if not pathlib.Path(value).is_absolute():
                raise BackupError("tool-path-invalid")
            tools[name] = value
        if not value or not pathlib.Path(value).is_file() or not os.access(value, os.X_OK):
            raise BackupError("tool-unavailable")
    return envelope
