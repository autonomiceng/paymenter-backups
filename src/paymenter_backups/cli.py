import argparse
import json
import os
import pathlib
import subprocess
import sys

from . import __version__
from .common import BackupError, State
from .config import DEFAULTS, bridge, load, normalize, redact, validate
from .engine import capture, export, plan, verify
from .maintenance import recover, schedule_admission
from .requests import cancel, create, list_requests, tick, worker
from .process import safe_environment
from .storage import clean_environment, deliver, destination_test, retention


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
    for name, verbs in (("config", ("show", "set", "validate")), ("schedule", ("show", "set", "enable", "disable"))):
        sub = commands.add_parser(name)
        sub.add_argument("verb", choices=verbs)
    commands.add_parser("plan")
    sub = commands.add_parser("run")
    sub.add_argument("--execute", action="store_true")
    sub.add_argument("--mode", choices=("full-quiesced", "database-online"))
    commands.add_parser("list")
    commands.add_parser("status")
    commands.add_parser("init", help="Explicitly initialize owner-private installation state")
    sub = commands.add_parser("verify")
    sub.add_argument("run_id")
    sub.add_argument("--show", action="store_true")
    sub = commands.add_parser("deliver")
    sub.add_argument("run_id")
    sub = commands.add_parser("retention")
    sub.add_argument("verb", choices=("plan", "apply"))
    commands.add_parser("destination-test")
    sub = commands.add_parser("requests")
    sub.add_argument("verb", choices=("list", "cancel", "create"))
    sub.add_argument("request_id", nargs="?")
    sub.add_argument("--action", choices=("run", "verify", "deliver", "retention", "destination-test"), default="run")
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
    with state.lock("config.lock"):
        if settings:
            values = {key: json.dumps(value) if isinstance(value, list) else value for key, value in settings.items()}
            try:
                result = subprocess.run([args.php, str(state.app / "artisan"), "backups:config-set"],
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
        state.apply_policy(envelope["settings"], write=True)
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
    state = State(args.app, args.private_root, initialize=initialize,
                  extra_docroots=preliminary["settings"]["extra_docroots"] if preliminary else None)
    if command == "init":
        validate(preliminary)
        state.apply_policy(preliminary["settings"], write=True)
        return {"initialized": True, "installation_uuid": state.uuid}
    if command in ("status", "list"):
        journals = state.journals()
        if command == "list":
            return journals
        return {"installation_uuid": state.uuid, "initialized": state.initialized, "runs": journals,
                "unresolved": [item["run_id"] for item in journals if not item.get("service_recovered") and not item.get("recovery_acknowledged")],
                "maintenance": (state.app / "storage/framework/down").exists(), "requests": list_requests(state)}
    if command == "verify":
        return verify(state, args.run_id, args.show)
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
        return tick(state, envelope, prefix)
    raise BackupError("command-invalid")


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
