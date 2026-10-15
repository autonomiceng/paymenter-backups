"""Capture checkpoint, restore service, then package immutable payloads."""
import contextlib
import hashlib
import gzip
import json
import os
import re
import pathlib
import shutil
import shlex
import signal
import tarfile
import time
import uuid

from .common import BackupError, atomic_json, digest, identity, read_json
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


def space(state, settings):
    used = usage(state.root)
    local = pathlib.Path(settings["local_path"]).resolve() if settings.get("local_path") else state.root
    if local != state.root and state.root not in local.parents and local.exists():
        used += usage(local)
    if used >= settings["local_byte_budget"]:
        raise BackupError("local-byte-budget-exceeded")
    if shutil.disk_usage(state.root).free < settings["minimum_free_bytes"]:
        raise BackupError("disk-reserve-exceeded")
    if local.exists() and shutil.disk_usage(local).free < settings["minimum_free_bytes"]:
        raise BackupError("disk-reserve-exceeded")


def output_limit(state, settings, output_root=None):
    used = usage(state.root)
    if output_root and output_root != state.root and state.root not in output_root.parents:
        used += usage(output_root)
    reserve = settings["minimum_free_bytes"]
    available = min(settings["local_byte_budget"] - used,
                    shutil.disk_usage(state.root).free - reserve)
    if output_root:
        available = min(available, shutil.disk_usage(output_root).free - reserve)
    if available <= 0:
        raise BackupError("local-byte-budget-exceeded")
    return int(available)


def destination(state, envelope):
    value = envelope["settings"].get("local_path")
    path = pathlib.Path(value).absolute() if value else state.root / "artifacts"
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


def plan(state, envelope):
    settings = envelope["settings"]
    errors = []
    for name in ("php", "dump", "mysql", "tar", "gzip"):
        binary = envelope.get("tools", {}).get(name)
        if not binary or not pathlib.Path(binary).is_file() or not os.access(binary, os.X_OK):
            errors.append("tool-unavailable")
            break
    if not state.initialized:
        errors.append("installation-not-initialized")
    if settings["mode"] == "full-quiesced":
        try:
            billing_admission(state, envelope)
            schedule_admission(state, envelope)
        except BackupError as error:
            errors.append(error.code)
        if not settings["job_lock_paths"]:
            errors.append("job-locks-unverified")
        for key in ("cooperative_writers_acknowledged", "external_state_acknowledged"):
            if not settings[key]:
                errors.append(key.replace("_", "-"))
    if not settings["no_concurrent_ddl_acknowledged"]:
        errors.append("no-concurrent-ddl-acknowledgement-required")
    if any(not item.get("service_recovered") and not item.get("recovery_acknowledged") for item in state.journals()):
        errors.append("recovery-required")
    return {"mode": settings["mode"], "destination": settings["destination"],
            "installation_uuid": state.uuid, "app": str(state.app), "private_root": str(state.root),
            "capture_timeout": settings["capture_timeout"], "admission_errors": errors,
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


def inventory(envelope, defaults, timeout, register):
    database = envelope["database"]
    if database.get("driver") not in ("mysql", "mariadb") or not database.get("name"):
        raise BackupError("unsupported-database")
    # Database name never interpolates into SQL: DATABASE() reflects CLI selection.
    query = """SELECT 'tables', COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND (TABLE_TYPE!='BASE TABLE' OR ENGINE!='InnoDB');
SELECT 'routines', COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=DATABASE();
SELECT 'events', COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA=DATABASE();
SELECT 'triggers', COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE();
SHOW GRANTS FOR CURRENT_USER;"""
    try:
        result = run([envelope["tools"]["mysql"], "--defaults-file=" + str(defaults), "--batch", "--skip-column-names", "--", database["name"]],
                     timeout, input_bytes=query.encode(), register=register).decode()
        rows = result.splitlines()
        counts = {}
        for line in rows[:4]:
            name, count = line.split("\t")
            counts[name] = int(count)
        if set(counts) != {"tables", "routines", "events", "triggers"} or not rows[4:]:
            raise ValueError()
    except (BackupError, KeyError, UnicodeError, ValueError):
        raise BackupError("database-inventory-failed") from None
    if any(counts.values()):
        raise BackupError("unsupported-db-objects")
    grants = [re.split(r"\s+IDENTIFIED\b", row, maxsplit=1, flags=re.IGNORECASE)[0] + " IDENTIFIED [redacted]"
              if re.search(r"\s+IDENTIFIED\b", row, re.IGNORECASE) else row for row in rows[4:]]
    return {"state": "visible-zero", "counts": counts, "current_user_grants": grants,
            "grants_redaction": "Authentication clauses are omitted; privilege clauses recorded.",
            "scope": "Zero visible under recorded current-user grants; hidden objects are unknown. Routines and events are not exported; native trigger default retained."}


def external_symlinks(app):
    for directory, dirs, files in os.walk(app, followlinks=False):
        for name in dirs + files:
            path = pathlib.Path(directory) / name
            if path.is_symlink():
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise BackupError("external-symlink-target") from None
                if target != app and app not in target.parents:
                    raise BackupError("external-symlink-target")


def interrupted(signum, _):
    raise BackupError("capture-interrupted")


def capture(state, envelope, scheduled=False, request_id=None):
    settings = envelope["settings"]
    with state.lock():
        proposed = plan(state, envelope)
        if proposed["admission_errors"]:
            raise BackupError(proposed["admission_errors"][0])
        state.apply_policy(settings, write=True)
        space(state, settings)
        output = destination(state, envelope)
        if envelope["framework"].get("maintenance_driver") != "file" and settings["mode"] == "full-quiesced":
            raise BackupError("maintenance-driver-unsupported")
        full = settings["mode"] == "full-quiesced"
        if full and any(envelope["framework"].get(key) not in allowed for key, allowed in (
            ("queue_driver", ("database", "sync", "null")),
            ("session_driver", ("file", "database")),
            ("cache_driver", ("file", "database", "array")),
            ("filesystem_driver", ("local", "public")),
        )):
            raise BackupError("external-durable-state-unsupported")
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
        try:
            defaults_file(defaults, envelope["database"])
            context = coordination(state, envelope) if full else contextlib.nullcontext()
            with context:
                if full:
                    journal["billing"] = billing_admission(state, envelope)
                    deadline = time.monotonic() + settings["capture_timeout"] + settings["drain_seconds"] + 15
                    journal["deadline"] = time.time() + settings["capture_timeout"] + settings["drain_seconds"] + 15
                    journal["phase"] = "entering-maintenance"
                    journal["hold_started_at"] = time.time()
                    state.save(journal)
                    if not initial:
                        # Journal the intent first. Crash before ownership recording stays closed.
                        run([envelope["tools"]["php"], str(state.app / "artisan"), "down", "--render=errors::503", "--status=503"],
                            min(15, remaining()), register=register)
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
                run(command, remaining(), output=sql, register=register, budget_check=lambda: space(state, settings), file_limit=output_limit(state, settings, output))
                if not sql.stat().st_size:
                    raise BackupError("dump-empty")
                sql_hash = digest(sql, remaining)
                if full:
                    # Data stability is required across the filesystem capture.
                    excludes = list(journal["owned_markers"])
                    tar_command = [envelope["tools"]["tar"], "--acls", "--xattrs", "--numeric-owner", "-cf", str(payload / "app.tar")]
                    tar_command += ["--exclude=./" + relative for relative in excludes]
                    tar_command += ["-C", str(state.app), "."]
                    run(tar_command, remaining(), register=register, budget_check=lambda: space(state, settings), file_limit=output_limit(state, settings, output))
                    if hash_stream(command, remaining(), register) != sql_hash:
                        raise BackupError("database-changed-during-capture")
                    journal["phase"] = "releasing"
                    state.save(journal)
                    cleanup(state, journal)
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
            run([envelope["tools"]["tar"], "--use-compress-program=" + shlex.quote(envelope["tools"].get("gzip", "/usr/bin/gzip")), "-cf", str(temporary), "-C", str(payload), "."],
                settings["capture_timeout"] * 2, register=register, budget_check=lambda: space(state, settings), file_limit=output_limit(state, settings, output))
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            check_archive(temporary, manifest)
            manifest["archive"] = {"sha256": digest(temporary), "size": temporary.stat().st_size}
            complete = output / (run_id + ".tar.gz")
            os.replace(temporary, complete)
            atomic_json(output / (run_id + ".manifest.json"), manifest)
            journal.update({"phase": "complete", "local_complete": True,
                            "archive_path": str(complete), "manifest_path": str(output / (run_id + ".manifest.json"))})
            state.save(journal)
            shutil.rmtree(payload)
            return journal
        except BaseException as error:
            defaults.unlink(missing_ok=True)
            journal["error"] = error.code if isinstance(error, BackupError) else "capture-failed"
            journal["phase"] = "failed"
            if full:
                try:
                    cleanup(state, journal)
                except (BackupError, OSError) as recovery_error:
                    journal["recovery_error"] = recovery_error.code if isinstance(recovery_error, BackupError) else "recovery-failed"
            state.save(journal)
            if isinstance(error, BackupError):
                raise
            raise BackupError("capture-failed") from None
        finally:
            for sig, previous in old_signals.items():
                signal.signal(sig, previous)


def check_archive(path, manifest):
    expected = manifest["payloads"]
    seen = set()
    try:
        # Tar EOF precedes gzip footer; consume all compressed bytes to validate
        # CRC/length and detect truncated or corrupted trailer data.
        with gzip.open(path, "rb") as stream:
            for _ in iter(lambda: stream.read(1024 * 1024), b""):
                pass
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                if member.isdir() and name in ("", "."):
                    continue
                if name == "manifest.json" and member.isfile():
                    embedded = json.load(archive.extractfile(member))
                    if embedded != {key: value for key, value in manifest.items() if key != "archive"}:
                        raise BackupError("archive-manifest-mismatch")
                    seen.add(name)
                    continue
                if name not in expected or not member.isfile() or name in seen:
                    raise BackupError("archive-layout-invalid")
                hasher = hashlib.sha256()
                size = 0
                with archive.extractfile(member) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                        size += len(chunk)
                if hasher.hexdigest() != expected[name]["sha256"] or size != expected[name]["size"]:
                    raise BackupError("archive-payload-mismatch")
                seen.add(name)
        if seen != set(expected) | {"manifest.json"}:
            raise BackupError("archive-layout-invalid")
    except (OSError, EOFError, ValueError, tarfile.TarError):
        raise BackupError("archive-unreadable") from None
    return True


def verify(state, run_id, show=False):
    journal = read_json(state.run_path(run_id) / "journal.json")
    if not journal.get("local_complete"):
        raise BackupError("run-incomplete")
    manifest = read_json(journal["manifest_path"])
    if manifest.get("installation_uuid") != state.uuid or manifest.get("run_id") != run_id:
        raise BackupError("artifact-identity-mismatch")
    archive = pathlib.Path(journal["archive_path"])
    if archive.stat().st_size != manifest["archive"]["size"] or digest(archive) != manifest["archive"]["sha256"]:
        raise BackupError("archive-checksum-mismatch")
    check_archive(archive, manifest)
    return manifest if show else {"run_id": run_id, "verified": True, "sha256": manifest["archive"]["sha256"]}


def export(state, run_id, target):
    manifest = verify(state, run_id, show=True)
    journal = read_json(state.run_path(run_id) / "journal.json")
    target = pathlib.Path(target).resolve()
    state.guard_location(target, "export-destination-unsafe")
    if target.exists() or state.app == target or state.app in target.parents:
        raise BackupError("export-destination-unsafe")
    target.mkdir(mode=0o700)
    shutil.copyfile(journal["archive_path"], target / "backup.tar.gz")
    os.chmod(target / "backup.tar.gz", 0o600)
    atomic_json(target / "manifest.json", manifest)
    (target / "RESTORE.txt").write_text("Verify SHA256 in manifest.json. Extract outer backup.tar.gz, then app.tar into a new private application path. Import database.sql with matching MariaDB client. Preserve APP_KEY and Passport keys. Consult project docs/RECOVERY.md before starting cron, queues, webhooks or providers. Re-enter S3 credentials; this kit excludes them. New host needs a new installation UUID.\n")
    return {"run_id": run_id, "export_path": str(target), "verified": True}
