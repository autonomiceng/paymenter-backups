import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

from . import __version__
from .artifacts import journal as artifact_journal, stream
from .common import BackupError, State, atomic_json
from .config import DEFAULTS, bridge, load, normalize, redact, validate
from .engine import capture, export, plan, verify
from .maintenance import recover, schedule_admission
from .presentation import overview
from .requests import cancel, create, list_requests, tick, worker
from .process import safe_environment
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
    return result


def set_config(args, state, changes):
    credentials = changes.pop("credentials", None)
    probe_credentials = changes.pop("maintenance_probe_credentials", None)
    if "settings" in changes and set(changes) != {"settings"}:
        raise BackupError("config-invalid")
    settings = changes.pop("settings", changes)
    if not isinstance(settings, dict):
        raise BackupError("config-invalid")
    permitted = set(DEFAULTS) | {"private_root", "python", "php", "dump", "mysql", "tar", "gzip"}
    if set(settings) - permitted:
        raise BackupError("config-invalid")
    envelope = bridge(args)
    envelope["settings"] = {**DEFAULTS, **envelope.get("settings", {}), **settings}
    normalize(envelope["settings"])
    validate(envelope)
    if probe_credentials is not None and probe_credentials != {"clear": True}:
        from .maintenance import probe_authorization
        probe_authorization(probe_credentials, envelope["framework"].get("app_url", ""))
    state.apply_policy(envelope["settings"])
    schedule_admission(state, envelope)
    with state.lock("config.lock"), state.lock("tick.lock", timeout=10):
        # Re-read after acquiring the scheduler lock. A previously read envelope
        # must never reset a schedule epoch saved by another administrator.
        envelope = bridge(args)
        envelope["settings"] = {**DEFAULTS, **envelope.get("settings", {}), **settings}
        normalize(envelope["settings"])
        validate(envelope)
        state.apply_policy(envelope["settings"])
        schedule_admission(state, envelope)
        if probe_credentials is not None and probe_credentials != {"clear": True}:
            probe_authorization(probe_credentials, envelope["framework"].get("app_url", ""))
        if settings:
            values = {key: json.dumps(value) if isinstance(value, list) else value for key, value in settings.items()}
            try:
                result = subprocess.run([args.php, str(state.app / "artisan"), "backups:config-set", "--defer-init"],
                    input=json.dumps(values).encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, timeout=30, env=safe_environment())
                if result.returncode:
                    raise BackupError("config-write-failed")
            except (OSError, subprocess.TimeoutExpired):
                raise BackupError("config-write-failed") from None
        if credentials is not None:
            bridge(args, "credentials-write", credentials)
        if probe_credentials is not None:
            bridge(args, "maintenance-probe-credentials-write", probe_credentials)
        persisted = load(args)
        state.apply_policy(persisted["settings"], write=True)
        if settings:
            from .requests import _sync_schedule
            _sync_schedule(state, persisted["settings"], time.time())
    return {"updated": True, "credentials_updated": credentials is not None, "maintenance_probe_credentials_updated": probe_credentials is not None}


def dispatch(args):
    clean_environment()
    args.app = str(pathlib.Path(args.app).resolve())
    args.private_root = args.private_root or str(pathlib.Path(args.app).parent / "paymenter-backups-private")
    args.bridge = args.bridge or str(pathlib.Path(args.app) / "extensions/Others/Backups/bridge.php")
    command = args.command
    initialize = command == "init" or command == "config" and args.verb == "set" or command == "schedule" and args.verb != "show" or command == "run" and args.execute
    preliminary = bridge(args) if initialize else None
    if preliminary:
        preliminary["settings"] = {**DEFAULTS, **preliminary.get("settings", {})}
        normalize(preliminary["settings"])
    if command == "init":
        # Refuse invalid settings before any identity is created.
        validate(preliminary)
    state = State(args.app, args.private_root, initialize=initialize,
                  extra_docroots=preliminary["settings"]["extra_docroots"] if preliminary else None)
    if command == "init":
        state.apply_policy(preliminary["settings"], write=True)
        from .requests import sync_schedule
        sync_schedule(state, preliminary["settings"], settings_loader=lambda: load(args)["settings"])
        return {"initialized": True, "installation_uuid": state.uuid}
    if command == "overview":
        envelope, error = None, None
        if not args.offline:
            try:
                envelope = bridge(args)
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
        if args.verb == "show":
            settings = load(args)["settings"]
            return {key: value for key, value in settings.items() if key.startswith("schedule_") or key == "enabled"}
        changes = stdin_json() if args.verb == "set" else {"enabled": args.verb == "enable"}
        if set(changes) - {key for key in DEFAULTS if key.startswith("schedule_")} - {"enabled"}:
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
            result.update(deliver(state, envelope, journal["run_id"]))
        apply_automatic_retention(state, envelope, result)
        return result
    if command == "deliver":
        result = deliver(state, envelope, args.run_id)
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
            return destination_test(state, envelope)
    if command == "request-worker":
        if not args.execute:
            raise BackupError("execute-required")
        return worker(state, envelope, args.request_id)
    if command == "tick":
        # sys.argv[0] is a release wrapper, preserving its isolated runtime paths.
        prefix = [envelope["tools"].get("python", sys.executable), "-I", str(pathlib.Path(sys.argv[0]).resolve()),
                  "--app", args.app, "--private-root", args.private_root, "--bridge", args.bridge, "--php", args.php]
        return tick(state, envelope, prefix, envelope_loader=lambda: load(args))
    raise BackupError("command-invalid")


def check_config(args, state, changes):
    """Authoritative validation of proposed nonsecret settings; writes nothing."""
    settings = changes.get("settings") if set(changes) == {"settings"} else None
    if not isinstance(settings, dict):
        raise BackupError("config-invalid")
    permitted = set(DEFAULTS) | {"private_root", "python", "php", "dump", "mysql", "tar", "gzip"}
    if set(settings) - permitted:
        raise BackupError("config-invalid")
    envelope = bridge(args)
    envelope["settings"] = {**DEFAULTS, **envelope.get("settings", {}), **settings}
    normalize(envelope["settings"])
    for name in ("python", "php", "dump", "mysql", "tar", "gzip"):
        if name in settings:
            envelope.setdefault("tools", {})[name] = settings[name]
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
        print(json.dumps({"error": error.code}), file=sys.stderr)
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
