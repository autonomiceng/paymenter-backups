import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

from . import __version__
from .artifacts import journal as artifact_journal, stream
from .common import BackupError, State, atomic_json, read_json, private_file
from .config import DEFAULTS, bridge, load, load_policy, normalize, redact, validate
from .engine import capture, export, plan, verify
from .maintenance import recover, schedule_admission
from .presentation import overview
from .requests import cancel, create, list_requests, tick, worker
from .native_writer import run as run_native_writer
from .storage import clean_environment, delete_plan, delete_run, deliver, destination_test, retention, retrieve, retrieve_plan


def stdin_json():
    if sys.stdin.isatty():
        raise BackupError("json-stdin-required")
    data = sys.stdin.buffer.read(65537)
    if len(data) > 65536:
        raise BackupError("input-too-large")
    try:
        value = json.loads(data or b"{}")
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        raise BackupError("json-stdin-invalid") from None


def parser():
    result = argparse.ArgumentParser(description="Independent Paymenter backup CLI. Mutations are explicit; JSON output has no runtime secrets.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--app", default=os.getcwd())
    result.add_argument("--private-root")
    result.add_argument("--bridge")
    result.add_argument("--php", default="/usr/bin/php")
    commands = result.add_subparsers(dest="command", required=True)
    for name, verbs in (("config", ("show", "set", "validate", "check")), ("schedule", ("show", "set", "enable", "disable"))):
        sub = commands.add_parser(name)
        sub.add_argument("verb", choices=verbs)
        if name == "schedule":
            sub.add_argument("--kind", choices=("full", "database"))
    commands.add_parser("plan")
    sub = commands.add_parser("run")
    sub.add_argument("--execute", action="store_true")
    sub.add_argument("--mode", choices=("full-quiesced", "database-online"))
    commands.add_parser("list")
    commands.add_parser("status")
    sub = commands.add_parser("overview", help="Whitelisted backup, task and readiness summary without paths or manifests")
    sub.add_argument("--offline", action="store_true", help="Skip configuration and readiness checks")
    commands.add_parser("init", help="Explicitly initialize owner-private installation state")
    sub = commands.add_parser("verify")
    sub.add_argument("run_id")
    sub.add_argument("--show", action="store_true")
    sub.add_argument("--record", action="store_true", help="Record the verified archive identity, enabling admin download")
    sub = commands.add_parser("delete", help="Preview, or with --execute delete, one backup's owned copies")
    sub.add_argument("run_id")
    sub.add_argument("--execute", action="store_true")
    sub.add_argument("--location", choices=("local", "s3", "all"), default="all")
    sub = commands.add_parser("retrieve", help="Prepare an S3 backup for local download; --execute publishes the verified copy")
    sub.add_argument("run_id")
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("deliver")
    sub.add_argument("run_id")
    sub = commands.add_parser("retention")
    sub.add_argument("verb", choices=("plan", "apply"))
    commands.add_parser("destination-test")
    sub = commands.add_parser("requests")
    sub.add_argument("verb", choices=("list", "cancel", "create"))
    sub.add_argument("request_id", nargs="?")
    sub.add_argument("--action", choices=("run", "verify", "deliver", "retrieve", "retention", "destination-test", "delete"), default="run")
    sub = commands.add_parser("recover")
    sub.add_argument("verb", nargs="?", choices=("acknowledge",))
    sub.add_argument("--auto", action="store_true")
    sub.add_argument("--execute", action="store_true")
    sub.add_argument("--run", dest="run_id")
    sub = commands.add_parser("export")
    sub.add_argument("run_id")
    sub.add_argument("--output", required=True)
    sub = commands.add_parser("tick", help=argparse.SUPPRESS)
    sub.add_argument("--execute", action="store_true")
    sub = commands.add_parser("artifact-stream", help=argparse.SUPPRESS)
    sub.add_argument("run_id")
    sub.add_argument("--actor", type=int)
    sub = commands.add_parser("request-worker", help=argparse.SUPPRESS)
    sub.add_argument("request_id")
    sub.add_argument("--execute", action="store_true")
    from .key_cli import add_parsers
    add_parsers(commands)
    return result


def native_settings(args, state, values, descriptors, actor=None, restore=False, finalize=False):
    """Audited native rows; inherited flocks prove the coordinator owns publication."""
    command = [args.php, str(state.app / "artisan"), "backups:config-set", "--defer-init",
               "--config-lock-fd=" + str(descriptors[0]), "--tick-lock-fd=" + str(descriptors[1]),
               "--coordination-root=" + str(state.root)]
    if restore:
        command.append("--restore")
    if finalize:
        command.append("--finalize")
    if actor is not None:
        command.append("--actor=" + str(actor))
    try:
        result = run_native_writer(command, json.dumps(values).encode(), descriptors)
        if result.returncode:
            try:
                refusal = json.loads(result.stderr)
                code = refusal.get("error") if isinstance(refusal, dict) else None
            except (ValueError, UnicodeDecodeError):
                code = None
            safe_refusals = {"config-changed", "credentials-unreadable", "credentials-invalid", "maintenance-probe-credentials-invalid",
                "maintenance-probe-origin-mismatch", "maintenance-probe-origin-invalid", "installation-missing", "installation-mismatch",
                "private-file-invalid", "private-root-invalid", "billing-settings-unreadable", "extension-uninstalled"}
            if finalize and code in safe_refusals:
                raise BackupError(code)
            raise BackupError("config-write-failed" if code == "config-coordination-required" else "config-write-outcome-unknown")
        output = json.loads(result.stdout)
        if not isinstance(output, dict) or output.get("ok") is not True:
            raise ValueError()
        return output
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BackupError("config-write-outcome-unknown") from None


def finish_config_save(state):
    path = state.root / "config-save.json"
    intent = configuration_file_snapshot(path)
    path.unlink(missing_ok=True)
    try:
        descriptor = os.open(state.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        if intent is not None:
            restore_configuration_file(path, intent)
        raise


def configuration_file_snapshot(path):
    if path.exists() or path.is_symlink():
        private_file(path)
        return path.read_bytes()
    return None


def restore_configuration_file(path, snapshot):
    """Restore exact private bytes/existence, with durable publication under locks."""
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(snapshot)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def set_config(args, state, changes):
    credentials = changes.pop("credentials", None)
    probe_credentials = changes.pop("maintenance_probe_credentials", None)
    actor = changes.pop("_actor", None)
    if actor is not None and (type(actor) is not int or actor <= 0):
        raise BackupError("config-invalid")
    if "settings" in changes and set(changes) != {"settings"}:
        raise BackupError("config-invalid")
    settings = changes.pop("settings", changes)
    if not isinstance(settings, dict):
        raise BackupError("config-invalid")
    permitted = set(DEFAULTS) | {"private_root"}
    if set(settings) - permitted:
        raise BackupError("config-invalid")
    # One deadline for both coordination locks. Neither native rows nor secrets
    # are read for publication before the authoritative locked baseline.
    deadline = time.monotonic() + 10
    with state.lock("config.lock", timeout=10) as config_fd, state.lock("tick.lock", timeout=max(0, deadline - time.monotonic())) as tick_fd:
        descriptors = (config_fd, tick_fd)
        original = bridge(args, "read-settings")
        if "private_root" in settings and pathlib.Path(settings["private_root"] or state.app.parent / "paymenter-backups-private").resolve() != state.root:
            raise BackupError("private-root-move-refused")
        envelope = {**original, "settings": {**DEFAULTS, **original.get("settings", {}), **settings}}
        require_fresh_plaintext_ack(original.get("settings", {}), settings)
        normalize(envelope["settings"])
        override_tools(envelope, settings, getattr(args, "php", envelope.get("tools", {}).get("php", "/usr/bin/php")))
        validate(envelope)
        state.apply_policy(envelope["settings"])
        if envelope["settings"]["local_path"]:
            local = pathlib.Path(envelope["settings"]["local_path"]).resolve()
            if local == state.app or state.app in local.parents:
                raise BackupError("destination-unsafe")
            state.guard_location(local, "destination-inside-docroot")
        schedule_admission(state, envelope)
        secrets = credentials is not None or probe_credentials is not None
        merge = {"s3": credentials, "site": probe_credentials}
        if secrets:
            if probe_credentials is not None and probe_credentials != {"clear": True}:
                from .maintenance import probe_authorization
                probe_authorization(probe_credentials, envelope["framework"].get("app_url", ""))
            bridge(args, "credentials-check", merge)
        pending_path = state.root / "config-save.json"
        previous_intent = configuration_file_snapshot(pending_path)
        if previous_intent is not None:
            pending = read_json(pending_path)
            if pending.get("s3_requested") and credentials is None or pending.get("site_requested") and probe_credentials is None:
                raise BackupError("config-save-outcome-unknown")
        derived_before = {state.root / name: configuration_file_snapshot(state.root / name)
                          for name in ("schedule.json", "policy.json", "configuration-advisory.json")}
        atomic_json(pending_path, {"schema_version": 1, "s3_requested": credentials is not None,
                                  "site_requested": probe_credentials is not None})
        current_intent = pending_path.read_bytes()
        receipt = {}
        committed_credentials = False
        credentials_attempted = False
        from .requests import _sync_schedule
        try:
            if settings:
                from .requests import freeze_legacy_requests
                freeze_legacy_requests(state, original.get("settings", {}).get("mode", "full-quiesced"))
            values = {key: json.dumps(value) if isinstance(value, list) else value for key, value in settings.items()}
            native = native_settings(args, state, values, descriptors, actor)
            receipt, snapshot = native.get("receipt"), native.get("snapshot")
            if not isinstance(receipt, dict) or not isinstance(snapshot, dict):
                raise BackupError("config-write-outcome-unknown")
            # Initialization/validation uses no credential read and holds the same
            # coordination locks, so publication cannot expose an intermediate pair.
            persisted = load(args, credential_free=True, coordinated=False)
            if persisted["settings"] != envelope["settings"]:
                raise BackupError("config-changed")
            State(state.app, state.root, initialize=True, extra_docroots=persisted["settings"]["extra_docroots"])
            state.apply_policy(persisted["settings"], write=True)
            if secrets:
                credentials_attempted = True
                native_settings(args, state, {"snapshot": snapshot, "credentials": merge}, descriptors, actor, finalize=True)
                committed_credentials = True
            _sync_schedule(state, persisted["settings"], time.time())
            atomic_json(state.root / "configuration-advisory.json", {"schema_version": 1, "checked_at": time.time(), "valid": True})
            finish_config_save(state)
        except (BackupError, OSError) as error:
            failure = error if isinstance(error, BackupError) else BackupError("config-save-failed")
            definite_credential_refusals = {"config-changed", "credentials-unreadable", "credentials-invalid", "maintenance-probe-credentials-invalid",
                "maintenance-probe-origin-mismatch", "maintenance-probe-origin-invalid", "installation-missing", "installation-mismatch",
                "private-file-invalid", "private-root-invalid", "billing-settings-unreadable", "extension-uninstalled"}
            if failure.code == "config-write-outcome-unknown" or credentials_attempted and not committed_credentials and failure.code not in definite_credential_refusals:
                # A child may have committed before timing out or losing stdout.
                # Keep the guard durable: workers refuse either possibly mixed
                # pair until a later complete Save supplies the uncertain groups.
                return {"updated": False, "credentials_updated": False, "maintenance_probe_credentials_updated": False,
                        "partial": True, "error": "config-save-outcome-unknown", "settings_restored": False, "outcome_unknown": True}
            if committed_credentials:
                # Credentials are durably committed. Restoring destination rows
                # would publish an old destination/new-key pair instead.
                try:
                    finish_config_save(state)
                except OSError:
                    return {"updated": True, "credentials_updated": credentials is not None,
                            "maintenance_probe_credentials_updated": probe_credentials is not None,
                            "partial": True, "error": "config-save-outcome-unknown", "outcome_unknown": True}
                return {"updated": True, "credentials_updated": credentials is not None,
                        "maintenance_probe_credentials_updated": probe_credentials is not None,
                        "partial": True, "error": failure.code, "settings_restored": False}
            try:
                # Cleanup may unlink the intent before directory fsync fails.
                # Reestablish it before rollback, which can itself fail.
                if not pending_path.exists():
                    restore_configuration_file(pending_path, previous_intent or current_intent)
                skipped = native_settings(args, state, receipt, descriptors, actor, restore=True).get("skipped", []) if receipt else []
                restored = load(args, credential_free=True, coordinated=False)
                baseline = {**DEFAULTS, **original.get("settings", {})}
                try:
                    normalize(baseline)
                except BackupError:
                    baseline = None
                concurrent_changes = restored["settings"] != baseline
                for path, snapshot in derived_before.items():
                    restore_configuration_file(path, snapshot)
                state.apply_policy(restored["settings"], write=concurrent_changes)
                if concurrent_changes:
                    _sync_schedule(state, restored["settings"], time.time())
                if previous_intent is not None:
                    # A definite refusal in this attempt cannot establish what
                    # an earlier writer committed. Only confirmed replacement
                    # of every uncertain namespace resolves the earlier guard.
                    restore_configuration_file(pending_path, previous_intent)
                else:
                    finish_config_save(state)
            except (BackupError, OSError):
                try:
                    if not pending_path.exists():
                        restore_configuration_file(pending_path, previous_intent or current_intent)
                except OSError:
                    return {"updated": False, "credentials_updated": False, "maintenance_probe_credentials_updated": False,
                            "partial": True, "error": "config-save-outcome-unknown", "settings_restored": False, "outcome_unknown": True}
                return {"updated": False, "credentials_updated": False, "maintenance_probe_credentials_updated": False,
                        "partial": True, "error": failure.code, "settings_restored": False}
            if skipped:
                return {"updated": False, "credentials_updated": False, "maintenance_probe_credentials_updated": False,
                        "partial": True, "error": failure.code, "settings_restored": False, "concurrent_settings_preserved": True}
            raise failure
    return {"updated": True, "credentials_updated": credentials is not None,
            "maintenance_probe_credentials_updated": probe_credentials is not None}


def dispatch(args):
    clean_environment()
    args.app = str(pathlib.Path(args.app).resolve())
    args.private_root = args.private_root or str(pathlib.Path(args.app).parent / "paymenter-backups-private")
    args.bridge = args.bridge or str(pathlib.Path(args.app) / "extensions/Others/Backups/bridge.php")
    command = args.command
    initialize = command == "init" or command == "config" and args.verb == "set" or command == "schedule" and args.verb != "show" or command == "run" and args.execute
    preliminary = bridge(args, "read-settings") if initialize else None
    if preliminary:
        preliminary["settings"] = {**DEFAULTS, **preliminary.get("settings", {})}
        # config set may repair malformed native scalars/optional lists. Its
        # locked authoritative baseline is normalized only after applying the
        # submitted repairs. The cold bridge still validates document roots
        # before State can use those privacy boundaries.
        if not (command == "config" and args.verb == "set"):
            normalize(preliminary["settings"])
    if command == "init":
        # Refuse invalid settings before any identity is created.
        validate(preliminary)
    state = State(args.app, args.private_root, initialize=initialize,
                  extra_docroots=preliminary["settings"]["extra_docroots"] if preliminary else None)
    from .key_cli import COMMANDS as KEY_COMMANDS, dispatch as key_dispatch
    if command in KEY_COMMANDS:
        return key_dispatch(args, state, bridge, stdin_json)
    if command == "init":
        deadline = time.monotonic() + 10
        with state.lock("config.lock", timeout=10), state.lock("tick.lock", timeout=max(0, deadline - time.monotonic())):
            if (state.root / "config-save.json").exists():
                raise BackupError("config-save-outcome-unknown")
            current = load(args, credential_free=True, coordinated=False)
            state.apply_policy(current["settings"], write=True)
            from .requests import _sync_schedule
            _sync_schedule(state, current["settings"], time.time())
        return {"initialized": True, "installation_uuid": state.uuid}
    if command == "overview":
        envelope, error = None, None
        if not args.offline:
            try:
                with state.lock("config.lock", timeout=10, create=state.initialized):
                    envelope = bridge(args)
                    pending_save = (state.root / "config-save.json").exists()
                envelope["settings"] = {**DEFAULTS, **envelope.get("settings", {})}
                normalize(envelope["settings"])
            except BackupError as failure:
                envelope, error = None, failure.code
            if envelope is not None:
                try:
                    validate(envelope)
                    state.apply_policy(envelope["settings"])
                except BackupError as failure:
                    error = failure.code
                    envelope["validation_error"] = {"field": failure.field, "detail": failure.detail}
                if pending_save:
                    error = "config-save-outcome-unknown"
                    envelope["validation_error"] = {"field": None, "detail": {}}
        return overview(state, envelope, error)
    if command == "artifact-stream":
        return stream_artifact(state, args)
    if command in ("status", "list"):
        journals = state.journals()
        if command == "list":
            return journals
        return {"installation_uuid": state.uuid, "initialized": state.initialized, "runs": journals,
                "unresolved": [item["run_id"] for item in journals if not item.get("service_recovered") and not item.get("recovery_acknowledged")],
                "maintenance": (state.app / "storage/framework/down").exists(), "requests": list_requests(state)}
    if command == "verify":
        item = artifact_journal(state, args.run_id)
        envelope = load(args) if not item.get("local_complete") and item.get("remote_delivered") else None
        if args.record:
            if not state.initialized:
                raise BackupError("installation-not-initialized")
            with state.lock():
                return verify(state, args.run_id, args.show, record=True, envelope=envelope)
        return verify(state, args.run_id, args.show, envelope=envelope)
    if command == "export":
        state.apply_policy(load_policy(args, bridge, state.uuid))
        return export(state, args.run_id, args.output)
    if command == "recover":
        if args.verb == "acknowledge" and not args.run_id:
            raise BackupError("run-id-required")
        return recover(state, args.execute, args.auto, args.run_id, args.verb == "acknowledge")
    if command == "requests":
        if args.verb == "list":
            return list_requests(state)
        if args.verb == "cancel":
            if not args.request_id:
                raise BackupError("request-id-required")
            return cancel(state, args.request_id)
        if not state.initialized:
            raise BackupError("installation-not-initialized")
        return create(state, args.action, stdin_json())
    if command == "config":
        if args.verb == "show":
            return redact(bridge(args))
        if args.verb == "set":
            return set_config(args, state, stdin_json())
        if args.verb == "check":
            return check_config(args, state, stdin_json())
        envelope = load(args)
        state.apply_policy(envelope["settings"])
        schedule_admission(state, envelope)
        return {"valid": True}
    if command == "schedule":
        settings = load(args, credential_free=True)["settings"]
        if settings["settings_version"] == 1:
            selected = "full" if settings["mode"] == "full-quiesced" else "database"
            if args.kind and args.kind != selected:
                raise BackupError("schedule-kind-required")
            if args.verb == "show":
                return {key: value for key, value in settings.items() if key.startswith("schedule_") or key == "enabled"}
            changes = stdin_json() if args.verb == "set" else {"enabled": args.verb == "enable"}
            if set(changes) - {key for key in DEFAULTS if key.startswith("schedule_")} - {"enabled"}:
                raise BackupError("schedule-field-invalid")
        else:
            from .requests import next_slot
            if args.verb == "show":
                return {"settings_version": 2, "timezone": settings["schedule_timezone"],
                        "schedules": {kind: {key.removeprefix(kind + "_"): value for key, value in settings.items() if key.startswith(kind + "_") and key in (kind + "_schedule", kind + "_time", kind + "_weekdays", "database_every_hours", "database_minute")}
                                      | {"next_at": next_slot(settings, kind=kind)} for kind in ((args.kind,) if args.kind else ("full", "database"))}}
            kind = args.kind
            if kind is None and args.verb != "disable":
                enabled = [kind for kind in ("full", "database") if settings[kind + "_schedule"] != "off"]
                if len(enabled) != 1:
                    raise BackupError("schedule-kind-required")
                kind = enabled[0]
            if args.verb == "disable":
                changes = {key + "_schedule": "off" for key in ((kind,) if kind else ("full", "database"))}
            elif args.verb == "enable":
                changes = {kind + "_schedule": settings[kind + "_schedule"] if settings[kind + "_schedule"] != "off" else ("daily" if kind == "full" else "hourly")}
            else:
                values = stdin_json()
                mapping = {"frequency": kind + "_schedule", "time": kind + "_time", "weekdays": kind + "_weekdays",
                           "timezone": "schedule_timezone"}
                if kind == "database":
                    mapping.update(every_hours="database_every_hours", minute="database_minute")
                allowed = set(mapping.values())
                changes = {mapping.get(key, key): value for key, value in values.items()}
                if set(changes) - allowed:
                    raise BackupError("schedule-field-invalid")
        return set_config(args, state, {"settings": changes})
    if command == "tick":
        if not args.execute:
            return {"execute_required": True}
        if not state.initialized:
            return {"initialized": False, "recovery": []}
        atomic_json(state.root / "scheduler.json", {"schema_version": 1, "last_tick_at": time.time()})
        recovery = recover(state, auto=True)
        if (state.app / "storage/framework/down").exists():
            return {"maintenance": True, "recovery": recovery}
    envelope = load(args)
    state.apply_policy(envelope["settings"])
    if command == "plan":
        return plan(state, envelope)
    if command == "run":
        if args.mode:
            envelope["settings"]["mode"] = args.mode
        if not args.execute:
            return plan(state, envelope)
        journal = capture(state, envelope)
        result = {"run_id": journal["run_id"], "local_complete": True, "archive_path": journal["archive_path"]}
        if envelope["settings"]["destination"] == "s3":
            result.update(deliver(state, envelope, journal["run_id"], envelope_loader=lambda: load(args)))
        apply_automatic_retention(state, envelope, result)
        return result
    if command == "deliver":
        result = deliver(state, envelope, args.run_id, envelope_loader=lambda: load(args))
        apply_automatic_retention(state, envelope, result)
        return result
    if command == "retention":
        return retention(state, envelope, args.verb == "apply")
    if command == "delete":
        if not args.execute:
            return delete_plan(state, envelope, args.run_id, location=args.location)
        return delete_run(state, envelope, args.run_id, location=args.location)
    if command == "retrieve":
        return retrieve(state, envelope, args.run_id) if args.execute else retrieve_plan(state, envelope, args.run_id)
    if command == "destination-test":
        with state.lock():
            return destination_test(state, envelope, envelope_loader=lambda: load(args), locked=True)
    if command == "request-worker":
        if not args.execute:
            raise BackupError("execute-required")
        return worker(state, envelope, args.request_id, envelope_loader=lambda: load(args))
    if command == "tick":
        # sys.argv[0] is a release wrapper, preserving its isolated runtime paths.
        prefix = [envelope["tools"].get("python", sys.executable), "-I", str(pathlib.Path(sys.argv[0]).resolve()),
                  "--app", args.app, "--private-root", args.private_root, "--bridge", args.bridge, "--php", args.php]
        return tick(state, envelope, prefix, envelope_loader=lambda: load(args, coordinated=False))
    raise BackupError("command-invalid")


def override_tools(envelope, changes, php):
    for name in ('python', 'php', 'dump', 'mysql', 'tar', 'gzip', 'gpg'):
        if name in changes:
            envelope.setdefault('tools', {})[name] = changes[name] or (php if name == 'php' else '')


def require_fresh_plaintext_ack(previous, changes):
    # Check only encryption/storage scalars, so repairing an unrelated invalid
    # legacy JSON row does not require successfully normalizing the old row.
    off = changes.get('s3_encryption') in (False, 0, '0', 'false', '')
    if not off:
        return
    if previous.get('settings_version', 1) in (1, '1'):
        encrypted = not (previous.get('destination') == 's3' and previous.get('plaintext_offsite_acknowledged') in (True, 1, '1', 'true'))
    else:
        encrypted = previous.get('s3_encryption', True) not in (False, 0, '0', 'false', '')
    if encrypted and changes.get('plaintext_offsite_acknowledged') not in (True, 1, '1', 'true'):
        raise BackupError('plaintext-offsite-acknowledgement-required', 'plaintext_offsite_acknowledged')


def check_config(args, state, changes):
    """Authoritative validation of proposed nonsecret settings; writes nothing."""
    settings = changes.get("settings") if set(changes) == {"settings"} else None
    if not isinstance(settings, dict):
        raise BackupError("config-invalid")
    permitted = set(DEFAULTS) | {"private_root", "python", "php", "dump", "mysql", "tar", "gzip"}
    if set(settings) - permitted:
        raise BackupError("config-invalid")
    envelope = bridge(args, "read-settings")
    require_fresh_plaintext_ack(envelope.get("settings", {}), settings)
    envelope["settings"] = {**DEFAULTS, **envelope.get("settings", {}), **settings}
    normalize(envelope["settings"])
    override_tools(envelope, settings, getattr(args, "php", envelope.get("tools", {}).get("php", "/usr/bin/php")))
    validate(envelope)
    state.apply_policy(envelope["settings"])
    if envelope["settings"]["local_path"]:
        # The same location rule capture applies, checked before anything is saved.
        local = pathlib.Path(envelope["settings"]["local_path"]).resolve()
        if local == state.app or state.app in local.parents:
            raise BackupError("destination-unsafe")
        state.guard_location(local, "destination-inside-docroot")
    schedule_admission(state, envelope)
    return {"valid": True}


def stream_artifact(state, args):
    if os.isatty(1):
        raise BackupError("artifact-stream-pipe-required")
    if not state.initialized:
        raise BackupError("installation-not-initialized")
    outcome, size = "failed", 0
    try:
        result = stream(state, args.run_id, 1)
        outcome, size = "complete", result["size"]
        return None
    except BrokenPipeError:
        outcome = "interrupted"
        return None
    except BackupError as error:
        outcome = error.code
        raise
    finally:
        audit = {"at": time.time(), "event": "download", "run_id": args.run_id if isinstance(args.run_id, str) and len(args.run_id) <= 100 else None,
                 "actor": args.actor, "outcome": outcome, "bytes": size}
        descriptor = os.open(state.root / "audit.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(descriptor, (json.dumps(audit, sort_keys=True) + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def apply_automatic_retention(state, envelope, result):
    if envelope["settings"]["automatic_retention"]:
        try:
            result["retention"] = retention(state, envelope, execute=True)
        except BackupError as error:
            result["retention_error"] = error.code


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        if args.command == "artifact-stream":
            return 0
        print(json.dumps(redact(result), sort_keys=True, indent=2))
        return 0
    except BackupError as error:
        result = {"error": error.code}
        if error.field is not None:
            result["field"] = error.field
        if error.detail:
            result["detail"] = error.detail
        print(json.dumps(result), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": "operation-interrupted"}), file=sys.stderr)
        return 1
    except Exception:
        # Runtime errors must not disclose driver messages, SQL or credentials.
        print(json.dumps({"error": "operation-failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
