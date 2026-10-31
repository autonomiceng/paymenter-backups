"""Capture checkpoint, restore service, then package immutable payloads."""
import fcntl
import hashlib
import gzip
import json
import os
import re
import pathlib
import shutil
import shlex
import signal
import stat
import tarfile
import time
import uuid

from . import artifacts
from .common import BackupError, TRANSIENT_ERRORS, atomic_json, digest, identity, read_json
from .maintenance import MARKERS, billing_admission, cleanup, coordination, probe, strict_payload, schedule_admission
from .process import boot_id, hash_stream, process_identity, run


def usage(path):
    total = 0
    for directory, _, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (pathlib.Path(directory) / name).lstat().st_size
            except FileNotFoundError:
                pass
    return total


def storage_usage(state, settings, output_root=None):
    """Count all journaled private artifact directories across setting changes."""
    directories = [state.root]
    if settings.get("local_path"):
        directories.append(pathlib.Path(settings["local_path"]).resolve())
    if output_root:
        directories.append(pathlib.Path(output_root).resolve())
    for item in state.journals():
        if not item.get("archive_path"):
            continue
        try:
            directories.append(artifacts.locations(state, item)[0])
        except BackupError:
            continue
    roots = []
    for directory in sorted(set(directories), key=lambda value: len(value.parts)):
        if not any(directory == root or root in directory.parents for root in roots):
            roots.append(directory)
    return sum(usage(directory) for directory in roots if directory.exists())


def space(state, settings):
    used = storage_usage(state, settings)
    local = pathlib.Path(settings["local_path"]).resolve() if settings.get("local_path") else state.root
    if used >= settings["local_byte_budget"]:
        raise BackupError("local-byte-budget-exceeded")
    if shutil.disk_usage(state.root).free < settings["minimum_free_bytes"]:
        raise BackupError("disk-reserve-exceeded")
    if local.exists() and shutil.disk_usage(local).free < settings["minimum_free_bytes"]:
        raise BackupError("disk-reserve-exceeded")


def output_limit(state, settings, output_root=None):
    used = storage_usage(state, settings, output_root)
    reserve = settings["minimum_free_bytes"]
    available = min(settings["local_byte_budget"] - used,
                    shutil.disk_usage(state.root).free - reserve)
    if output_root:
        available = min(available, shutil.disk_usage(output_root).free - reserve)
    if available <= 0:
        raise BackupError("local-byte-budget-exceeded")
    return int(available)


class DiskReserve:
    """Cheap reserve checks; full history accounting belongs at boundaries."""
    def __init__(self, state, settings, directory):
        self.paths = set((state.root, pathlib.Path(directory)))
        if settings.get('local_path'):
            local = pathlib.Path(settings['local_path']).resolve()
            if local.exists():
                self.paths.add(local)
        self.reserve = settings['minimum_free_bytes']
        self.last_at = None
        self.last_bytes = 0

    def check(self, total, force=False):
        now = time.monotonic()
        if not force and self.last_at is not None and now - self.last_at < 1 and total - self.last_bytes < 64 * 1024**2:
            return
        for path in self.paths:
            if shutil.disk_usage(path).free < self.reserve:
                raise BackupError('disk-reserve-exceeded')
        self.last_at, self.last_bytes = now, total


def run_output_tool(state, settings, name, command, timeout, target, *, stdout=False, register=None, deadline_check=None, output_root=None):
    """Bound one child output without scanning private history on every poll."""
    available = output_limit(state, settings, output_root or target.parent)
    reserve = DiskReserve(state, settings, target.parent)
    def check(force=False):
        if deadline_check:
            deadline_check()
        try:
            total = target.stat().st_size
        except FileNotFoundError:
            total = 0
        if total > available:
            raise BackupError('child-output-limit', name, {'tool': name})
        reserve.check(total, force=force)
    # Resolve the remaining duration after admission accounting, never before it.
    seconds = timeout() if callable(timeout) else timeout
    result = run_tool(name, command, seconds, output=target if stdout else None,
                      register=register, budget_check=check, file_limit=available)
    check(force=True)
    space(state, settings)
    if deadline_check:
        deadline_check()
    return result


def destination(state, envelope):
    value = envelope["settings"].get("local_path")
    path = pathlib.Path(value).absolute() if value else (state.root / "backups" if envelope["settings"].get("settings_version", 1) == 2 else state.default_destination())
    if path.is_symlink():
        raise BackupError("destination-unsafe")
    path = path.resolve()
    if path == state.app or state.app in path.parents:
        raise BackupError("destination-unsafe")
    state.guard_location(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = path.resolve()
    if path == state.app or state.app in path.parents or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise BackupError("destination-unsafe")
    return path


def plan(state, envelope, discovery_inventory=None, probe_locks=True, encryption_key_status=None):
    settings = envelope["settings"]
    errors = []
    items = []
    def add(code, field=None, detail=None):
        errors.append(code)
        items.append({"code": code, "field": field, "detail": detail or {}})
    for name in ("php", "dump", "mysql", "tar", "gzip"):
        report = (envelope.get("tool_checks") or {}).get(name)
        binary = report.get("resolved") if isinstance(report, dict) and report.get("ok") else envelope.get("tools", {}).get(name)
        if isinstance(report, dict) and not report.get("ok"):
            add(report.get("code") or "tool-unavailable", name,
                {"tool": name, "path": report.get("resolved") or report.get("configured")})
            continue
        if not binary or not pathlib.Path(binary).is_file() or not os.access(binary, os.X_OK):
            add("tool-unavailable", name, {"tool": name, "path": binary})
    if not state.initialized:
        add("installation-not-initialized")
    if settings["mode"] == "full-quiesced":
        try:
            from .maintenance import probe_authorization, web_origin
            web_origin(envelope.get("framework", {}).get("app_url", ""), allow_http=True)
            if envelope.get("maintenance_probe_credentials"):
                probe_authorization(envelope["maintenance_probe_credentials"], envelope.get("framework", {}).get("app_url", ""))
        except BackupError as error:
            add(error.code)
        try:
            billing_admission(state, envelope)
            schedule_admission(state, envelope)
        except BackupError as error:
            add(error.code)
        try:
            external_symlinks(state.app)
        except BackupError as error:
            add(error.code)
        from .readiness import stores
        detected = stores(envelope, legacy_policy=settings.get("settings_version", 1) == 1)
        if detected[0]['supported'] is not True:
            add('maintenance-driver-unsupported', detail={'driver': detected[0]['driver']})
        unsupported = [item for item in detected[1:] if item['supported'] is not True]
        if unsupported:
            add('external-durable-state-unsupported', detail={'stores': unsupported})
        if settings.get('settings_version', 1) == 2:
            from .discovery import admission, cached
            inventory = discovery_inventory if discovery_inventory is not None else cached(state)
            refusals, _ = admission(envelope, inventory)
            for item in refusals:
                add(item['code'], item['field'], item['detail'])
            from .process import continuous_workers
            if continuous_workers(state.app):
                add('continuous-worker-present')
            if probe_locks:
                for job in inventory['items']:
                    if job['coordination'] != 'coordinated':
                        continue
                    descriptor = None
                    try:
                        descriptor = os.open(job['lock'], os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        add('job-lock-busy', detail={'job': job['id']})
                    except OSError:
                        pass  # The lock-safety admission item already identifies it.
                    finally:
                        if descriptor is not None:
                            os.close(descriptor)
            if not settings['full_host_assumptions_acknowledged']:
                add('full-host-assumptions-acknowledgement-required', 'full_host_assumptions_acknowledged')
        else:
            if not settings['job_lock_paths']:
                add('job-locks-unverified')
            for key in ('cooperative_writers_acknowledged', 'external_state_acknowledged'):
                if not settings[key]:
                    add(key.replace('_', '-'), key)
    if probe_locks and state.initialized:
        try:
            space(state, settings)
        except BackupError as error:
            add(error.code)
        try:
            with state.lock(create=False):
                pass
        except BackupError as error:
            add(error.code)
    if not settings["no_concurrent_ddl_acknowledged"]:
        add("no-concurrent-ddl-acknowledgement-required", "no_concurrent_ddl_acknowledged")
    if any(not item.get("service_recovered") and not item.get("recovery_acknowledged") for item in state.journals()):
        add("recovery-required")
    # Delivery setup belongs in the page readiness report, while local capture
    # remains admissible and preserves its source if delivery cannot proceed.
    if settings.get("upload_s3", settings["destination"] == "s3"):
        from . import encryption
        delivery_code = encryption.readiness(envelope, key_status=encryption_key_status)
        if delivery_code:
            field = "gpg" if delivery_code.startswith("archive-encryption-tool-") else "s3_encryption"
            items.append({"code": delivery_code, "field": field, "detail": {}})
    return {"mode": settings["mode"], "destination": settings["destination"],
            "installation_uuid": state.uuid, "app": str(state.app), "private_root": str(state.root),
            "capture_timeout": settings["capture_timeout"], "admission_errors": errors, "readiness_items": items,
            "guarantee": "database-and-full-app-checkpoint" if settings["mode"] == "full-quiesced" else "transactional-database-only",
            "external_provider_state": "excluded", "execute_required": True}


def defaults_file(path, database):
    def quoted(value):
        # MySQL option files support these escapes; newlines cannot add options.
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'
    lines = ["[client]"]
    for key, source in (("user", "user"), ("password", "password"), ("host", "host"), ("port", "port"), ("socket", "socket")):
        if database.get(source) is not None and database.get(source) != "":
            lines.append(key + "=" + quoted(database[source]))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_tool(name, command, timeout, **kwargs):
    """Identify a failed capture program without returning command arguments."""
    try:
        return run(command, timeout, **kwargs)
    except BackupError as error:
        if error.code.startswith('child-'):
            raise BackupError(error.code, name, {'tool': name}) from None
        raise


def inventory(envelope, defaults, timeout, register):
    database = envelope["database"]
    if database.get("driver") not in ("mysql", "mariadb"):
        raise BackupError("database-driver-unsupported")
    if not database.get("name"):
        raise BackupError("unsupported-database")
    # Database name never interpolates into SQL: DATABASE() reflects CLI selection.
    query = """SELECT 'tables', COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND (TABLE_TYPE!='BASE TABLE' OR ENGINE!='InnoDB');
SELECT 'routines', COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE();
SELECT 'events', COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE();
SELECT 'triggers', COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE();
SHOW GRANTS FOR CURRENT_USER;"""
    try:
        result = run_tool("mysql", [envelope["tools"]["mysql"], "--defaults-file=" + str(defaults), "--batch", "--skip-column-names", "--", database["name"]],
                     timeout, input_bytes=query.encode(), register=register).decode()
        rows = result.splitlines()
        counts = {}
        for line in rows[:4]:
            name, count = line.split("\t")
            counts[name] = int(count)
        if set(counts) != {"tables", "routines", "events", "triggers"} or not rows[4:]:
            raise ValueError()
    except (BackupError, KeyError, UnicodeError, ValueError):
        raise BackupError("database-inventory-failed", "mysql", {"tool": "mysql"}) from None
    if any(counts.values()):
        raise BackupError("unsupported-db-objects", detail={"kinds": [key for key, count in counts.items() if count]})
    grants = [re.split(r"\s+IDENTIFIED\b", row, maxsplit=1, flags=re.IGNORECASE)[0] + " IDENTIFIED [redacted]"
              if re.search(r"\s+IDENTIFIED\b", row, re.IGNORECASE) else row for row in rows[4:]]
    return {"state": "visible-zero", "counts": counts, "current_user_grants": grants,
            "grants_redaction": "Authentication clauses are omitted; privilege clauses recorded.",
            "scope": "Zero visible under recorded current-user grants; hidden objects are unknown. Routines and events are not exported; native trigger default retained."}


def external_symlinks(app):
    # DirEntry uses the filesystem's directory metadata for ordinary files;
    # constructing a Path and lstat-ing every vendor file made page readiness
    # proportional to thousands of otherwise unnecessary system calls.
    pending = [str(app)]
    while pending:
        directory = pending.pop()
        # Recheck queued directories as os.walk does: a directory replaced by
        # a symlink after enumeration must not lead the scan outside the app.
        if directory != str(app) and os.path.islink(directory):
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        try:
                            target = pathlib.Path(entry.path).resolve(strict=True)
                        except (OSError, RuntimeError):
                            raise BackupError("external-symlink-target") from None
                        if target != app and app not in target.parents:
                            raise BackupError("external-symlink-target")
                    else:
                        try:
                            is_directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            is_directory = False
                        if is_directory:
                            pending.append(entry.path)
        except OSError:
            continue  # Match os.walk's handling of unreadable directories.


def interrupted(signum, _):
    raise BackupError("capture-interrupted")


def cleanup_failed_work(state):
    """Reclaim recorded capture work after service recovery and worker death."""
    for item in state.journals():
        if (item.get("local_complete") or item.get("capture_complete") or item.get("remote_delivered") or item.get("retained_deleted")
                or item.get("phase") == "complete" or (item.get("failed_work_removed_at") and not item.get("cleanup_error"))
                or not (item.get("service_recovered") or item.get("recovery_acknowledged"))):
            continue
        recorded = item.get("process")
        if recorded and process_identity(recorded.get("pid")) == recorded:
            continue
        directory = state.run_path(item["run_id"])
        payload = directory / "payload"
        try:
            if payload.exists() or payload.is_symlink():
                info = payload.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise BackupError("failed-work-cleanup-failed")
                shutil.rmtree(payload)
            value = item.get("output_directory")
            if value:
                output = pathlib.Path(value)
                if not output.is_absolute() or output.resolve() != output or output == state.app or state.app in output.parents:
                    raise BackupError("failed-work-cleanup-failed")
                state.guard_location(output, "failed-work-cleanup-failed")
                info = output.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise BackupError("failed-work-cleanup-failed")
                name = "." + item["run_id"] + ".tar.gz.partial"
                partial = output / name
                if partial.exists() or partial.is_symlink():
                    checked = partial.lstat()
                    if not artifacts.owned_file(checked):
                        raise BackupError("failed-work-cleanup-failed")
                    location = artifacts.Location(output, partial, partial, (info.st_dev, info.st_ino))
                    artifacts.remove_owned(location, [name], {name: artifacts.identity(checked)})
            item.pop("cleanup_error", None)
            item["failed_work_removed_at"] = time.time()
            state.save(item)
        except (OSError, BackupError):
            item["cleanup_error"] = "failed-work-cleanup-failed"
            state.save(item)


def discard_unstarted_capture(state, directory, payload, defaults):
    """Remove only this attempt's private setup files before capture starts."""
    try:
        directory_info = directory.lstat()
        if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid()
                or directory_info.st_mode & 0o077 or directory.parent != state.root / "runs"):
            return False
        children = {item.name for item in directory.iterdir()}
        if children - {"journal.json", "payload", defaults.name}:
            return False
        journal = directory / "journal.json"
        for path in (journal, defaults):
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    return False
        if payload.exists() or payload.is_symlink():
            info = payload.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or next(payload.iterdir(), None) is not None):
                return False
        shutil.rmtree(directory)
        return True
    except OSError:
        return False


def capture(state, envelope, scheduled=False, request_id=None, envelope_loader=None, database_context=None):
    with state.lock():
        if envelope_loader is not None:
            requested_mode = envelope['settings']['mode']
            envelope = envelope_loader()
            envelope['settings']['mode'] = requested_mode
        settings = envelope["settings"]
        fresh_discovery = None
        if settings.get("settings_version", 1) == 2 and settings["mode"] == "full-quiesced":
            from .discovery import discover
            fresh_discovery = discover(state, envelope)
        proposed = plan(state, envelope, fresh_discovery, probe_locks=False)
        if proposed["admission_errors"]:
            raise BackupError(proposed["admission_errors"][0])
        state.apply_policy(settings, write=True)
        cleanup_failed_work(state)
        try:
            space(state, settings)
        except BackupError as error:
            if error.code != "local-byte-budget-exceeded" or not settings.get("automatic_retention"):
                raise
            from .storage import retention
            retention(state, envelope, execute=True, locked=True, local_only=True)
            space(state, settings)
        output = destination(state, envelope)
        full = settings["mode"] == "full-quiesced"
        initial = {relative: identity(state.app / relative) for relative in MARKERS if (state.app / relative).exists()}
        if full and initial:
            if scheduled:
                raise BackupError("initial-maintenance-scheduled-skip")
            strict_payload(state.app)
        if full:
            external_symlinks(state.app)
        run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
        directory = state.run_path(run_id)
        directory.mkdir(mode=0o700)
        payload = directory / "payload"
        payload.mkdir(mode=0o700)
        journal = {"schema_version": 1, "run_id": run_id, "installation_uuid": state.uuid,
                   "created_at": time.time(), "phase": "admitted", "mode": settings["mode"],
                   "initial_markers": initial, "owned_markers": {}, "groups": [],
                   "process": process_identity(os.getpid()), "boot_id": boot_id(),
                   "service_recovered": not full, "local_complete": False,
                   "remote_delivered": False, "request_id": request_id}
        journal["output_directory"] = str(output)
        state.save(journal)
        def register(group):
            journal["groups"].append(group)
            state.save(journal)
        deadline = None
        def remaining():
            duration = settings["capture_timeout"] if deadline is None else deadline - time.monotonic()
            if duration <= 0:
                raise BackupError("capture-timeout")
            return duration
        old_signals = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
        defaults = directory / "client.cnf"
        temporary = None
        try:
            defaults_file(defaults, envelope["database"])
            with coordination(state, envelope, required=full) as coordinated:
                if coordinated:
                    journal["billing"] = billing_admission(state, envelope)
                    if not full:
                        deadline = time.monotonic() + settings["capture_timeout"]
                        journal["deadline"] = time.time() + settings["capture_timeout"]
                        journal["hold_started_at"] = time.time()
                if full:
                    deadline = time.monotonic() + settings["capture_timeout"] + settings["drain_seconds"] + 15
                    journal["deadline"] = time.time() + settings["capture_timeout"] + settings["drain_seconds"] + 15
                    journal["phase"] = "entering-maintenance"
                    journal["hold_started_at"] = time.time()
                    state.save(journal)
                    if not initial:
                        # Journal the intent first. Crash before ownership recording stays closed.
                        run_tool("php", [envelope["tools"]["php"], str(state.app / "artisan"), "down", "--render=errors::503", "--status=503"],
                            min(15, remaining()), register=register, database_context=database_context)
                        journal["owned_markers"] = {relative: identity(state.app / relative) for relative in MARKERS}
                        state.save(journal)
                    strict_payload(state.app)
                    probe(envelope)
                    journal["phase"] = "draining"
                    state.save(journal)
                    time.sleep(settings["drain_seconds"])
                    remaining()
                    # The full hold has one shared capture deadline, not one per child.
                    deadline = min(deadline, time.monotonic() + settings["capture_timeout"])
                journal["phase"] = "capturing"
                state.save(journal)
                database_inventory = inventory(envelope, defaults, remaining(), register)
                command = [envelope["tools"]["dump"], "--defaults-file=" + str(defaults),
                           "--single-transaction", "--quick", "--hex-blob", "--skip-comments",
                           "--skip-dump-date", "--", envelope["database"]["name"]]
                sql = payload / "database.sql"
                journal["database_snapshot_lower_bound"] = time.time()
                state.save(journal)
                run_output_tool(state, settings, "dump", command, remaining, sql,
                                stdout=True, register=register, deadline_check=remaining if coordinated else None, output_root=output)
                if not sql.stat().st_size:
                    raise BackupError("dump-empty")
                if full:
                    sql_hash = digest(sql, remaining)
                    # Data stability is required across the filesystem capture.
                    excludes = list(journal["owned_markers"])
                    tar_command = [envelope["tools"]["tar"], "--acls", "--xattrs", "--numeric-owner", "-cf", str(payload / "app.tar")]
                    tar_command += ["--exclude=./" + relative for relative in excludes]
                    tar_command += ["-C", str(state.app), "."]
                    run_output_tool(state, settings, "tar", tar_command, remaining, payload / "app.tar",
                                    register=register, deadline_check=remaining, output_root=output)
                    try:
                        stability_hash = hash_stream(command, remaining(), register)
                    except BackupError as error:
                        if error.code.startswith("child-"):
                            raise BackupError(error.code, "dump", {"tool": "dump"}) from None
                        raise
                    if stability_hash != sql_hash:
                        raise BackupError("database-changed-during-capture")
                    journal["phase"] = "releasing"
                    state.save(journal)
                    cleanup(state, journal)
                if coordinated and not full:
                    journal["hold_ended_at"] = time.time()
                journal["phase"] = "captured"
                state.save(journal)
            defaults.unlink(missing_ok=True)
            manifest = {"schema_version": 1, "run_id": run_id, "installation_uuid": state.uuid,
                        "mode": settings["mode"], "created_at": journal["created_at"],
                        "guarantee": proposed["guarantee"], "database_inventory": database_inventory,
                        "excluded_paths": list(journal["owned_markers"]),
                        "external_state": "Host configuration, cron, Redis, external storage and provider actions excluded",
                        "payloads": {path.name: {"sha256": digest(path), "size": path.stat().st_size} for path in payload.iterdir()}}
            atomic_json(payload / "manifest.json", manifest)
            journal["phase"] = "compressing"
            state.save(journal)
            temporary = output / ("." + run_id + ".tar.gz.partial")
            run_output_tool(state, settings, "tar", [envelope["tools"]["tar"], "--use-compress-program=" + shlex.quote(envelope["tools"].get("gzip", "/usr/bin/gzip")), "-cf", str(temporary), "-C", str(payload), "."],
                settings["capture_timeout"] * 2, temporary, register=register)
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            check_archive(temporary, manifest)
            manifest["archive"] = {"sha256": digest(temporary), "size": temporary.stat().st_size}
            complete = output / (run_id + ".tar.gz")
            os.replace(temporary, complete)
            atomic_json(output / (run_id + ".manifest.json"), manifest)
            journal.update({"phase": "complete", "local_complete": True,
                            "capture_complete": True, "manifest": manifest,
                            "archive_path": str(complete), "manifest_path": str(output / (run_id + ".manifest.json"))})
            # The bytes were fully checked above while this run lock was held.
            # Record the published descriptor identity so download can rely on it.
            with artifacts.open_archive(state, run_id, journal) as artifact:
                journal["verification"] = {"verified_at": time.time(), "source": "capture",
                                           "sha256": manifest["archive"]["sha256"],
                                           "identity": artifacts.identity(artifact.info)}
            if full and journal.get("service_recovered"):
                journal["database_coverage"] = {"verified_at": journal["verification"]["verified_at"], "snapshot_lower_bound": journal["database_snapshot_lower_bound"]}
            state.save(journal)
            from .requests import reconcile_coverage
            reconcile_coverage(state, envelope, locked=True)
            shutil.rmtree(payload)
            return journal
        except BaseException as error:
            defaults.unlink(missing_ok=True)
            if (isinstance(error, BackupError) and error.code in TRANSIENT_ERRORS
                    and journal.get("phase") == "admitted" and not journal.get("owned_markers")
                    and not journal.get("groups") and discard_unstarted_capture(state, directory, payload, defaults)):
                raise
            journal["error"] = error.code if isinstance(error, BackupError) else "capture-failed"
            journal["phase"] = "failed"
            if full:
                try:
                    cleanup(state, journal)
                except (BackupError, OSError) as recovery_error:
                    journal["recovery_error"] = recovery_error.code if isinstance(recovery_error, BackupError) else "recovery-failed"
            try:
                if payload.exists() and not payload.is_symlink():
                    shutil.rmtree(payload)
                if temporary is not None and temporary.exists():
                    info = temporary.lstat()
                    if artifacts.owned_file(info):
                        location = artifacts.Location(output, temporary, temporary, (output.stat().st_dev, output.stat().st_ino))
                        artifacts.remove_owned(location, [temporary.name], {temporary.name: artifacts.identity(info)})
            except (OSError, BackupError):
                journal["cleanup_error"] = "failed-work-cleanup-failed"
            state.save(journal)
            if isinstance(error, BackupError):
                raise
            raise BackupError("capture-failed") from None
        finally:
            for sig, previous in old_signals.items():
                signal.signal(sig, previous)


def check_archive(path, manifest):
    """One sequential gzip/tar pass checks payloads and drains the gzip trailer."""
    opener = path if callable(path) else (lambda: open(path, "rb"))
    expected = manifest["payloads"]
    seen = set()
    limit = sum(value["size"] for value in expected.values()) + 2 * 1024**2

    class BoundedPayloadReader:
        def __init__(self, source):
            self.source = source
            self.size = 0

        def read(self, size=-1):
            value = self.source.read(size)
            self.size += len(value)
            if self.size > limit:
                raise BackupError("archive-layout-invalid")
            return value

    try:
        with opener() as raw, gzip.GzipFile(fileobj=raw, mode="rb") as compressed:
            payload = BoundedPayloadReader(compressed)
            with tarfile.open(fileobj=payload, mode="r|") as archive:
                for member in archive:
                    name = member.name.removeprefix("./")
                    if member.isdir() and name in ("", "."):
                        continue
                    if name == "manifest.json" and member.isfile() and name not in seen:
                        if member.size > artifacts.MANIFEST_LIMIT:
                            raise BackupError("archive-manifest-mismatch")
                        embedded = json.load(archive.extractfile(member))
                        if embedded != {key: value for key, value in manifest.items() if key != "archive"}:
                            raise BackupError("archive-manifest-mismatch")
                        seen.add(name)
                        continue
                    if name not in expected or not member.isfile() or name in seen:
                        raise BackupError("archive-layout-invalid")
                    if member.size != expected[name]["size"]:
                        raise BackupError("archive-payload-mismatch")
                    hasher = hashlib.sha256()
                    size = 0
                    with archive.extractfile(member) as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            hasher.update(chunk)
                            size += len(chunk)
                    if hasher.hexdigest() != expected[name]["sha256"] or size != expected[name]["size"]:
                        raise BackupError("archive-payload-mismatch")
                    seen.add(name)
            # Tar's EOF may precede the gzip footer. Drain through the same
            # bounded decompressor, preserving CRC/truncation proof in one GET.
            for _ in iter(lambda: payload.read(1024 * 1024), b""):
                pass
        if seen != set(expected) | {"manifest.json"}:
            raise BackupError("archive-layout-invalid")
    except (OSError, EOFError, ValueError, tarfile.TarError):
        raise BackupError("archive-unreadable") from None
    return True


def verify(state, run_id, show=False, record=False, envelope=None):
    """Fully check one owned archive. record=True requires the caller's run lock."""
    journal = artifacts.journal(state, run_id)
    if not journal.get("local_complete") and journal.get("remote_delivered") and not artifacts.deleted(journal):
        if envelope is None:
            raise BackupError("remote-configuration-required")
        from .storage import checked_remote
        with checked_remote(state, envelope, journal, record=record) as (_path, manifest):
            return manifest if show else {"run_id": run_id, "verified": True, "location": "s3", "sha256": manifest["archive"]["sha256"]}
    try:
        with artifacts.open_archive(state, run_id) as artifact:
            manifest = artifact.manifest
            if artifact.digest() != manifest["archive"]["sha256"]:
                raise BackupError("archive-checksum-mismatch")
            check_archive(artifact.reader, manifest)
            if not artifact.unchanged():
                raise BackupError("archive-changed-during-verification")
            checked = artifacts.identity(artifact.info)
    except BackupError as error:
        if record and error.code not in ("run-not-found", "run-incomplete", "artifact-deleted", "invalid-run-id"):
            journal = artifacts.journal(state, run_id)
            journal.pop("verification", None)
            journal["verification_error"] = {"code": error.code, "at": time.time()}
            state.save(journal)
        raise
    if record:
        journal = artifacts.journal(state, run_id)
        journal.pop("verification_error", None)
        journal["verification"] = {"verified_at": time.time(), "source": "verify",
                                   "sha256": manifest["archive"]["sha256"], "identity": checked}
        state.save(journal)
    return manifest if show else {"run_id": run_id, "verified": True, "sha256": manifest["archive"]["sha256"]}


def export(state, run_id, target):
    manifest = verify(state, run_id, show=True)
    journal = artifacts.journal(state, run_id)
    target = pathlib.Path(target).resolve()
    state.guard_location(target, "export-destination-unsafe")
    if target.exists() or state.app == target or state.app in target.parents:
        raise BackupError("export-destination-unsafe")
    target.mkdir(mode=0o700)
    with artifacts.open_archive(state, run_id, journal) as artifact:
        with artifact.reader() as source, open(target / "backup.tar.gz", "xb") as copy:
            shutil.copyfileobj(source, copy, 1024 * 1024)
            copy.flush()
            os.fsync(copy.fileno())
        if not artifact.unchanged():
            raise BackupError("archive-changed-during-export")
    os.chmod(target / "backup.tar.gz", 0o600)
    atomic_json(target / "manifest.json", manifest)
    (target / "RESTORE.txt").write_text("Verify SHA256 in manifest.json. Extract outer backup.tar.gz, then app.tar into a new private application path. Import database.sql with matching MariaDB client. Preserve APP_KEY and Passport keys. Consult project docs/RECOVERY.md before starting cron, queues, webhooks or providers. Re-enter S3 credentials; this kit excludes them. New host needs a new installation UUID.\n")
    return {"run_id": run_id, "export_path": str(target), "verified": True}
