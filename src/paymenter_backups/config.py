import json
import math
import pathlib
import os
import re
import shutil
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import BackupError

DEFAULTS = {
    "settings_version": 1,
    "full_schedule": "off", "full_time": "03:00", "full_weekdays": [6],
    "database_schedule": "off", "database_every_hours": 1, "database_minute": 0,
    "database_time": "02:00", "database_weekdays": [0, 1, 2, 3, 4, 5, 6],
    "keep_local": True, "upload_s3": False,
    "retention_keep_full": 7, "retention_keep_database": 24,
    "s3_retention_policy_full": "local-count", "s3_retention_policy_database": "local-count",
    "s3_retention_keep_full": 30, "s3_retention_keep_database": 168,
    "s3_retention_days_full": 90, "s3_retention_days_database": 90,
    "full_host_assumptions_acknowledged": False, "s3_encryption": True,
    "python": "", "php": "", "dump": "", "mysql": "", "tar": "", "gzip": "", "gpg": "",
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
    "s3_retention_policy": "local-count", "s3_retention_keep": 30, "s3_retention_days": 90,
}


LEGACY_KEYS = {'enabled', 'destination', 'schedule_frequency', 'schedule_time', 'schedule_weekdays',
               'retention_keep', 's3_retention_policy', 's3_retention_keep', 's3_retention_days',
               'cooperative_writers_acknowledged', 'external_state_acknowledged'}

BRIDGE_ERROR_CODES = frozenset({
    "private-pipe-required", "application-unavailable", "database-connection-invalid",
    "database-pdo-driver-unavailable", "database-connection-failed", "database-driver-unsupported",
    "database-tls-unsupported", "database-prefix-unsupported", "unsupported-database",
    "extension-uninstalled", "native-secret-row-refused", "private-root-invalid",
    "document-roots-invalid", "private-root-unavailable", "billing-settings-unreadable",
    "installation-mismatch", "installation-missing", "credentials-unreadable", "credentials-invalid",
    "bridge-command-invalid", "private-file-invalid", "private-write-failed",
    "maintenance-probe-credentials-invalid", "maintenance-probe-origin-invalid",
    "maintenance-probe-origin-mismatch", "archive-key-invalid", "archive-key-mismatch",
    "archive-key-required", "archive-key-change-busy", "archive-key-pending-exists",
    "archive-key-already-exists", "archive-key-limit", "archive-key-audit-failed",
    "bridge-unavailable", "config-invalid", "archive-key-save-required", "archive-key-unavailable",
    "archive-encryption-tool-missing", "archive-encryption-tool-unusable", "archive-encryption-failed",
    "archive-decryption-failed", "archive-integrity-failed", "private-root-inside-docroot",
    "maintenance-probe-http-saved",
})


def billing_fingerprint(billing):
    import hashlib
    value = {"daily_time": billing.get("daily_time"), "timezone": billing.get("timezone")}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def redact(value):
    sensitive = {"password", "app_key", "app_previous_keys", "access_key", "secret_key", "session_token", "credentials", "maintenance_probe_credentials", "archive_encryption", "archive_encryption_credentials", "secret", "passphrase", "recovery_secret", "recovery_key"}
    if isinstance(value, dict):
        return {key: "[redacted]" if key in sensitive else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def validate_database_context(context):
    """Check the short-lived Laravel connection snapshot before forwarding it."""
    fields = {"driver", "host", "port", "unix_socket", "database", "username", "password", "prefix",
              "options", "charset", "collation", "strict", "engine", "prefix_indexes"}
    if (not isinstance(context, dict) or set(context) != {"schema_version", "default", "connection"}
            or type(context["schema_version"]) is not int or context["schema_version"] != 1
            or not isinstance(context["default"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", context["default"])
            or not isinstance(context["connection"], dict) or set(context["connection"]) != fields):
        raise BackupError("database-connection-invalid")
    connection = context["connection"]
    if connection.get("options") == []:
        connection["options"] = {}
    limits = {"driver": 64, "host": 1024, "unix_socket": 4096, "database": 256,
              "username": 1024, "password": 8192, "prefix": 128, "charset": 64, "collation": 128}
    for field, limit in limits.items():
        value = connection[field]
        if not isinstance(value, str) or len(value.encode()) > limit or "\0" in value:
            raise BackupError("database-connection-invalid")
    port = connection["port"]
    if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", connection["driver"])
            or ";" in connection["host"] or ";" in connection["unix_socket"] or ";" in connection["database"]
            or isinstance(port, bool) or not (isinstance(port, int) or isinstance(port, str) and port.isdigit())
            or not 1 <= int(port) <= 65535 or not isinstance(connection["options"], dict)
            or len(connection["options"]) > 128 or not isinstance(connection["strict"], bool)
            or not isinstance(connection["prefix_indexes"], bool)
            or not (connection["engine"] is None or isinstance(connection["engine"], str)
                    and len(connection["engine"].encode()) <= 128 and "\0" not in connection["engine"])):
        raise BackupError("database-connection-invalid")
    if connection["driver"] not in {"mysql", "mariadb"}:
        raise BackupError("database-driver-unsupported")
    for key, value in connection["options"].items():
        if (not (isinstance(key, (int, str)) and not isinstance(key, bool))
                or isinstance(key, str) and len(key.encode()) > 128
                or not (isinstance(value, (bool, int, str, float)) and not isinstance(value, float) or isinstance(value, float) and math.isfinite(value))
                or isinstance(value, str) and (len(value.encode()) > 4096 or "\0" in value)):
            raise BackupError("database-connection-invalid")
    try:
        if len(json.dumps(context, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()) > 32768:
            raise BackupError("database-connection-invalid")
    except (TypeError, ValueError, UnicodeError):
        raise BackupError("database-connection-invalid") from None
    return context


def bridge(args, verb="read", payload=None):
    from .process import safe_environment
    if not args.bridge:
        raise BackupError("bridge-required")
    command = [args.php, args.bridge, verb, "--app", str(args.app), "--private-root", str(args.private_root)]
    database_context = getattr(args, "database_connection", None)
    if database_context is not None:
        database_context = validate_database_context(database_context)
        command.append("--database-connection-stdin")
        payload = {"database_connection": database_context, "payload": payload}
    input_bytes = json.dumps(payload).encode() if payload is not None else None
    if input_bytes is not None and len(input_bytes) > 65536:
        raise BackupError("input-too-large")
    try:
        result = subprocess.run(command, input=input_bytes,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, close_fds=True, env=safe_environment())
        for output in (result.stderr, result.stdout):
            try:
                value = json.loads(output)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and "error" in value:
                error = value["error"]
                raise BackupError(error if isinstance(error, str) and error in BRIDGE_ERROR_CODES else "bridge-failed")
        if result.returncode:
            raise BackupError("bridge-failed")
        value = json.loads(result.stdout)
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BackupError("bridge-failed") from None


def load(args, credential_free=False, coordinated=True):
    # The cold bridge reads native rows and the encrypted store separately.
    # Read both beneath the Save lock, including worker/storage reloads. Callers
    # already owning config.lock explicitly suppress this nested acquisition.
    if coordinated:
        from .common import State
        state = State(args.app, args.private_root, initialize=False)
        if state.initialized:
            with state.lock("config.lock", timeout=10):
                return load(args, credential_free, coordinated=False)
    if not credential_free and (pathlib.Path(args.private_root) / "config-save.json").exists():
        raise BackupError("config-save-outcome-unknown")
    envelope = bridge(args, "read-settings" if credential_free else "read")
    if envelope.get("schema_version") != 1 or pathlib.Path(envelope.get("app", "")).resolve() != pathlib.Path(args.app).resolve():
        raise BackupError("bridge-identity-mismatch")
    import os
    if envelope.get("owner_uid") != os.getuid() or pathlib.Path(envelope.get("private_root", "")).resolve() != pathlib.Path(args.private_root).resolve():
        raise BackupError("bridge-identity-mismatch")
    envelope["settings"] = dict(DEFAULTS, **envelope.get("settings", {}))
    normalize(envelope["settings"])
    validate(envelope)
    return envelope


def load_policy(args, bridge_reader=None, installation_uuid=None):
    """Read current privacy boundaries without credentials or capture admission."""
    envelope = (bridge_reader or bridge)(args, "document-roots")
    try:
        if (not isinstance(envelope, dict) or type(envelope.get("schema_version")) is not int or envelope["schema_version"] != 1
                or not isinstance(envelope.get("app"), str)
                or not envelope["app"].startswith("/")
                or pathlib.Path(envelope["app"]).resolve(strict=True) != pathlib.Path(args.app).resolve(strict=True)
                or type(envelope.get("owner_uid")) is not int or envelope["owner_uid"] != os.getuid()
                or not isinstance(envelope.get("private_root"), str)
                or not envelope["private_root"].startswith("/")
                or pathlib.Path(envelope["private_root"]).resolve(strict=True) != pathlib.Path(args.private_root).resolve(strict=True)):
            raise BackupError("bridge-identity-mismatch")
        if installation_uuid is not None and envelope.get("installation_uuid") != installation_uuid:
            raise BackupError("bridge-identity-mismatch")
        settings = envelope.get("settings")
        roots = settings.get("extra_docroots") if isinstance(settings, dict) else None
        if not isinstance(roots, list):
            raise BackupError("document-roots-invalid")
        canonical = []
        for value in roots:
            if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
                raise BackupError("document-roots-invalid")
            root = pathlib.Path(value).resolve(strict=True)
            if not root.is_dir():
                raise BackupError("document-roots-invalid")
            canonical.append(str(root))
        return {"extra_docroots": canonical}
    except (OSError, RuntimeError, ValueError):
        raise BackupError("document-roots-invalid") from None


def normalize(settings, adapt=True):
    for key, value in DEFAULTS.items():
        settings.setdefault(key, value.copy() if isinstance(value, list) else value)
    for key, default in DEFAULTS.items():
        if settings.get("settings_version") in (2, "2") and key in LEGACY_KEYS:
            settings[key] = default.copy() if isinstance(default, list) else default
            continue
        value = settings[key]
        try:
            if isinstance(default, bool):
                if value not in (True, False, "1", "0", "true", "false", "", 1, 0):
                    raise ValueError()
                settings[key] = value in (True, "1", "true", 1)
            elif isinstance(default, int):
                # Native rows are digit strings; JSON input is an integer. No floats.
                if type(value) is not int and not (isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,20}", value)):
                    raise ValueError()
                settings[key] = int(value)
            elif isinstance(default, str):
                # The native editor stores an emptied text field as NULL.
                if value is None:
                    settings[key] = ""
                elif not isinstance(value, str):
                    raise ValueError()
            elif isinstance(default, list):
                if isinstance(value, str):
                    value = json.loads(value)
                if not isinstance(value, list):
                    raise ValueError()
                settings[key] = value
        except (TypeError, ValueError):
            raise BackupError("config-invalid") from None
    if settings["settings_version"] == 1 and adapt:
        settings.update(from_legacy(settings))
    elif settings["settings_version"] == 2:
        settings["destination"] = "s3" if settings["upload_s3"] else "local"
        settings["enabled"] = settings["full_schedule"] != "off" or settings["database_schedule"] != "off"


def from_legacy(values):
    """Proposed v2 fields only; interpretation stays v1 until explicit adoption.

    Shared vectors define the PHP Settings::fromLegacy parity contract.
    """
    legacy = dict(DEFAULTS, **values)
    # Normalize native row scalars without recursively invoking this adapter.
    legacy["settings_version"] = 1
    normalize(legacy, adapt=False)
    result = {key: DEFAULTS[key] for key in (
        "full_schedule", "full_time", "full_weekdays", "database_schedule",
        "database_every_hours", "database_minute", "database_time", "database_weekdays")}
    kind = "full" if legacy["mode"] == "full-quiesced" else "database"
    enabled = legacy["enabled"]
    destination = legacy["destination"]
    result[kind + "_schedule"] = legacy["schedule_frequency"] if enabled else "off"
    result[kind + "_time"] = legacy["schedule_time"]
    result[kind + "_weekdays"] = list(legacy["schedule_weekdays"])
    result.update(keep_local=True, upload_s3=destination == "s3",
                  full_host_assumptions_acknowledged=legacy["cooperative_writers_acknowledged"] and legacy["external_state_acknowledged"],
                  s3_encryption=not (destination == "s3" and legacy["plaintext_offsite_acknowledged"]))
    for kind in ("full", "database"):
        result["retention_keep_" + kind] = legacy["retention_keep"]
        for suffix in ("policy", "keep", "days"):
            result["s3_retention_" + suffix + "_" + kind] = legacy["s3_retention_" + suffix]
    for key in ("job_lock_paths", "python", "php", "dump", "mysql", "tar", "gzip"):
        result[key] = legacy[key]
    return result


def schedule_settings(settings, kind):
    """A singleton slot view; legacy callers retain their exact old semantics."""
    if settings.get("settings_version", 1) == 1:
        return dict(settings)
    return dict(settings, enabled=settings[kind + "_schedule"] != "off",
                schedule_frequency=settings[kind + "_schedule"],
                schedule_time=settings[kind + "_time"], schedule_weekdays=settings[kind + "_weekdays"])


def validate(envelope):
    settings = envelope["settings"]
    if settings["settings_version"] not in (1, 2):
        raise BackupError("config-invalid")
    if settings["settings_version"] == 2:
        if not settings["keep_local"] and not settings["upload_s3"]:
            raise BackupError("storage-required", "keep_local")
        for kind in ("full", "database"):
            if settings[kind + "_schedule"] not in (("off", "daily", "weekly") if kind == "full" else ("off", "hourly", "daily", "weekly")):
                raise BackupError("config-invalid", kind + "_schedule")
            if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", settings[kind + "_time"]):
                raise BackupError("config-invalid", kind + "_time")
            days = settings[kind + "_weekdays"]
            if any(type(day) is not int or day not in range(7) for day in days) or (settings[kind + "_schedule"] == "weekly" and not days):
                raise BackupError("config-invalid", kind + "_weekdays")
            if settings["s3_retention_policy_" + kind] not in ("local-count", "count", "age", "keep-all"):
                raise BackupError("config-invalid")
            for key in ("retention_keep_" + kind, "s3_retention_keep_" + kind, "s3_retention_days_" + kind):
                if settings[key] <= 0:
                    raise BackupError("config-invalid", key)
        if settings["database_schedule"] == "hourly" and (settings["database_every_hours"] not in (1, 2, 3, 4, 6, 8, 12) or not 0 <= settings["database_minute"] <= 59):
            raise BackupError("config-invalid", "database_every_hours")
    if settings["mode"] not in ("full-quiesced", "database-online") or settings["destination"] not in ("local", "s3"):
        raise BackupError("config-invalid")
    if settings["schedule_frequency"] not in ("daily", "weekly"):
        raise BackupError("config-invalid")
    if settings["s3_retention_policy"] not in ("local-count", "count", "age", "keep-all"):
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
        for key in ("local_byte_budget", "capture_timeout", "billing_guard_max_age", "request_max_age", "s3_timeout", "s3_retries", "retention_keep", "s3_retention_keep", "s3_retention_days"):
            if isinstance(settings[key], bool) or int(settings[key]) <= 0:
                raise ValueError()
        for key in ("minimum_free_bytes", "drain_seconds", "lock_timeout", "billing_margin_seconds"):
            if isinstance(settings[key], bool) or int(settings[key]) < 0:
                raise ValueError()
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise BackupError("config-invalid") from None
    if not isinstance(settings["job_lock_paths"], list) or any(not isinstance(item, str) or not item.startswith("/") or "\0" in item for item in settings["job_lock_paths"]):
        raise BackupError("config-invalid")
    if not isinstance(settings["extra_docroots"], list) or any(not isinstance(item, str) or not item.startswith("/") or "\0" in item for item in settings["extra_docroots"]):
        raise BackupError("config-invalid")
    if not isinstance(settings["local_path"], str) or (settings["local_path"] and not settings["local_path"].startswith("/")):
        raise BackupError("config-invalid")
    # Same rule as storage.prefix, the native editor and the admin form.
    prefix = settings["s3_prefix"].strip("/")
    if "\\" in prefix or any(part in ("", ".", "..") for part in prefix.split("/")):
        raise BackupError("s3-prefix-invalid")
    if settings["destination"] == "s3":
        if (settings["settings_version"] == 1 or not settings["s3_encryption"]) and not settings["plaintext_offsite_acknowledged"]:
            raise BackupError("plaintext-offsite-acknowledgement-required")
        if not settings["s3_bucket"]:
            raise BackupError("remote-configuration-required", "s3_bucket")
        if settings["s3_endpoint"] and not settings["s3_endpoint"].startswith("https://"):
            raise BackupError("s3-tls-required")
        if settings["s3_endpoint"]:
            from urllib.parse import urlsplit
            endpoint = urlsplit(settings["s3_endpoint"])
            if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise BackupError("s3-endpoint-invalid")
    from .tools import checks
    tools = envelope.setdefault("tools", {})
    reports = checks(envelope, capabilities=settings["settings_version"] == 2)
    envelope["tool_checks"] = reports
    for name, report in reports.items():
        if name == "gpg":  # The encryption module checks GnuPG capability and delivery readiness.
            if settings["gpg"] and (not pathlib.Path(settings["gpg"]).is_absolute() or "\0" in settings["gpg"]):
                raise BackupError("tool-path-invalid", "gpg", {"tool": "gpg", "path": settings["gpg"]})
            continue
        if not report["ok"]:
            raise BackupError(report["code"], name, {"tool": name, "path": report["resolved"] or report["configured"]})
        tools[name] = report["resolved"]
    return envelope
