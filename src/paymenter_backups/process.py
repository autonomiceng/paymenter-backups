"""Linux subprocess containment; children inherit no coordination descriptors."""
import ctypes
import atexit
import os
import pathlib
import signal
import selectors
import resource
import shlex
import re
import subprocess
import time
import warnings

from .common import BackupError

_DETACHED_CHILDREN = []


def reap_detached():
    _DETACHED_CHILDREN[:] = [item for item in _DETACHED_CHILDREN if item.poll() is None]


def forget_detached_on_exit():
    # A detached worker intentionally outlives this launcher. Popen's generic
    # destructor warning is inapplicable; kernel parenting/reaping owns it now.
    reap_detached()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        _DETACHED_CHILDREN.clear()


atexit.register(forget_detached_on_exit)


def safe_environment(database_context=None):
    # Do not inherit TAR_OPTIONS, MYSQL_PWD, GZIP, LD_PRELOAD, provider/CA/proxy
    # overrides, application variable overrides or TLS session key logging.
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
    from .scheduler import SETTING, guarded_scheduler_path
    if guarded_scheduler_path():
        environment[SETTING] = guarded_scheduler_path()
    if database_context is not None:
        from .config import database_connection_environment
        environment.update(database_connection_environment(database_context))
    return environment


def boot_id():
    return pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid):
    try:
        text = pathlib.Path(f"/proc/{pid}/stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "starttime": fields[19], "pgid": int(fields[2]), "boot_id": boot_id()}
    except (OSError, ValueError, IndexError):
        return None


def group_members(group):
    if group.get("boot_id") != boot_id():
        return []
    members = []
    for path in pathlib.Path("/proc").iterdir():
        if path.name.isdigit():
            item = process_identity(int(path.name))
            if item and item["pgid"] == group["pgid"]:
                members.append(item)
    return members


def child_setup(parent, file_limit=None):
    # Linux PR_SET_PDEATHSIG; recheck closes the parent-death race before prctl.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(127)
    if file_limit is not None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))


def terminate(group, grace=2):
    leader = process_identity(group["pid"])
    if leader != group:
        # A vanished leader plus live members is uncertain: never signal a reused PGID.
        if group_members(group):
            raise BackupError("process-state-uncertain")
        return
    try:
        os.killpg(group["pgid"], signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and group_members(group):
            time.sleep(0.05)
        if group_members(group):
            if process_identity(group["pid"]) != group:
                raise BackupError("process-state-uncertain")
            os.killpg(group["pgid"], signal.SIGKILL)
    except ProcessLookupError:
        pass


def terminate_child(proc, group):
    if group['starttime'] != 'unknown':
        return terminate(group)
    if proc.returncode is not None or group['pid'] != proc.pid or group['pgid'] != proc.pid:
        raise BackupError('process-state-uncertain')
    # This setsid child has not been reaped, so its PID/PGID cannot be reused.
    # Do not poll/wait before signaling: even a zombie must retain that pin.
    # Without /proc identity, kill the owned group before reaping its leader.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run(command, timeout, output=None, input_bytes=None, register=None, budget_check=None, file_limit=None,
        database_context=None):
    parent = os.getpid()
    stream = open(output, "wb") if output else subprocess.PIPE
    proc = None
    group = None
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                stdout=stream, stderr=subprocess.DEVNULL, close_fds=True,
                                start_new_session=True, preexec_fn=lambda: child_setup(parent, file_limit),
                                env=safe_environment(database_context))
        group = process_identity(proc.pid)
        if group is None:
            # An immediately exited process is handled by return code below.
            group = {"pid": proc.pid, "pgid": proc.pid, "starttime": "unknown", "boot_id": boot_id()}
        if register:
            register(group)
        if output:
            if input_bytes is not None:
                proc.stdin.write(input_bytes)
                proc.stdin.close()
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    raise BackupError("child-timeout")
                if budget_check:
                    budget_check()
                time.sleep(0.05)
            result = b""
            stream.flush()
            os.fsync(stream.fileno())
        else:
            if input_bytes is not None:
                proc.stdin.write(input_bytes)
                proc.stdin.close()
            chunks = []
            size = 0
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if budget_check:
                        budget_check()
                    if time.monotonic() >= deadline:
                        raise BackupError("child-timeout")
                    for key, _ in selector.select(0.05):
                        chunk = os.read(key.fd, 65536)
                        if chunk:
                            size += len(chunk)
                            if size > 16 * 1024 * 1024:
                                raise BackupError("child-output-limit")
                            chunks.append(chunk)
                        else:
                            selector.unregister(key.fileobj)
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            result = b"".join(chunks)
        if budget_check:
            budget_check()
        if proc.returncode:
            if file_limit is not None and output and pathlib.Path(output).stat().st_size >= file_limit:
                raise BackupError("child-output-limit")
            raise BackupError("child-failed")
        if group_members(group):
            raise BackupError("process-state-uncertain")
        return result
    finally:
        if proc is not None and proc.poll() is None:
            try:
                terminate_child(proc, group)
            finally:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        if output:
            stream.close()
        elif proc is not None and proc.stdout:
            proc.stdout.close()
        if proc is not None and proc.stdin and not proc.stdin.closed:
            proc.stdin.close()


def probe(command, timeout=5, stdout_limit=262144, stderr_limit=8192):
    """Bounded diagnostic stdout/stderr with supervised child-group cleanup."""
    parent = os.getpid()
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                            preexec_fn=lambda: child_setup(parent), env=safe_environment())
    group = process_identity(proc.pid)
    output = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in output:
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise BackupError('child-timeout')
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.fileobj].extend(chunk)
                    limit = stdout_limit if key.fileobj is proc.stdout else stderr_limit
                    if len(output[key.fileobj]) > limit:
                        raise BackupError('child-output-limit')
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise BackupError("child-timeout") from None
        if group and group_members(group):
            raise BackupError('process-state-uncertain')
        return proc.returncode, bytes(output[proc.stdout]), bytes(output[proc.stderr])
    finally:
        if proc.poll() is None:
            try:
                if group:
                    terminate(group)
                else:
                    proc.kill()
            finally:
                proc.wait(timeout=3)
        proc.stdout.close()
        proc.stderr.close()


def hash_stream(command, timeout, register=None):
    """Stream the second dump through a hasher, with no second SQL file."""
    import hashlib
    import selectors
    parent = os.getpid()
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                            preexec_fn=lambda: child_setup(parent), env=safe_environment())
    group = process_identity(proc.pid)
    if group is None:
        group = {"pid": proc.pid, "pgid": proc.pid, "starttime": "unknown", "boot_id": boot_id()}
    if register:
        register(group)
    hasher = hashlib.sha256()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise BackupError("child-timeout")
            for key, _ in selector.select(0.1):
                chunk = os.read(key.fd, 1024 * 1024)
                if chunk:
                    hasher.update(chunk)
                else:
                    selector.unregister(key.fileobj)
        try:
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise BackupError("child-timeout") from None
        if code:
            raise BackupError("child-failed")
        if group_members(group):
            raise BackupError("process-state-uncertain")
        return hasher.hexdigest()
    finally:
        selector.close()
        try:
            if proc.poll() is None:
                try:
                    terminate_child(proc, group)
                finally:
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            proc.stdout.close()


def waiting_launchers(held_locks):
    """Kernel-proven flock waiters cannot spawn a writer while we own these inodes."""
    held = set()
    for descriptor in held_locks:
        info = os.fstat(descriptor)
        held.add((os.major(info.st_dev), os.minor(info.st_dev), info.st_ino))
    if not held:
        return set()
    waiting = set()
    try:
        lines = pathlib.Path("/proc/locks").read_text().splitlines()
    except OSError:
        return waiting  # Missing proof keeps the process in the writer scan.
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[1:4] != ["->", "FLOCK", "ADVISORY"]:
            continue
        try:
            major, minor, inode = fields[6].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) in held:
                pid = int(fields[5])
                if os.path.samefile(f"/proc/{pid}/exe", "/usr/bin/flock"):
                    waiting.add(pid)
        except (OSError, ValueError):
            continue
    return waiting


def _continuous_worker(words):
    command = next((words[index + 1] for index, word in enumerate(words[:-1])
                    if pathlib.Path(word).name == 'artisan'), None)
    if command in ('queue:listen', 'horizon', 'schedule:work'):
        return True
    if command == 'queue:work':
        bounded = any(word in words for word in ('--stop-when-empty', '--once')) or any(re.fullmatch(r'--(?:max-time|max-jobs)=[1-9][0-9]*', word) for word in words)
        return not bounded or '--force' in words
    return False


def app_workers(app, *, held_locks=(), allow_continuous=False):
    from .scheduler import guarded_relevant
    found = []
    waiters = waiting_launchers(held_locks)
    for path in pathlib.Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid() or int(path.name) in waiters:
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            raw = (path / "cmdline").read_bytes()
            words = [os.fsdecode(word) for word in raw.rstrip(b'\0').split(b'\0')]
            # A retired direct script can still be writing after its cron entry
            # and selected path change. Relevance does not authorize its launch.
            guarded = bool(words[:1] == ['/usr/local/bin/php'] and guarded_relevant(shlex.join(words), app))
            arguments = raw.replace(b"\0", b" ").decode(errors="replace")
            if not guarded and ("artisan" not in arguments or not any(word in arguments for word in ("schedule:run", "schedule:work", "queue:work", "queue:listen", "horizon"))):
                continue
            cwd = (path / "cwd").resolve()
            if guarded or cwd == app or str(app / "artisan") in arguments:
                if allow_continuous and not guarded and _continuous_worker(words):
                    continue
                found.append(int(path.name))
        except (OSError, RuntimeError):
            continue
    return found


def continuous_workers(app):
    """Inspect live PHP workers without returning command lines or identities."""
    for pid in app_workers(app):
        try:
            words = pathlib.Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
            # flock waiting to spawn a child is not itself an application writer.
            if pathlib.Path(words[0]).name in ('flock', 'env'):
                continue
            if _continuous_worker(words):
                return True
        except (OSError, UnicodeError):
            continue
    return False


def detach(command, input_bytes=None):
    # Popen actually forks/execs, closes caller flock descriptors and setsid.
    child = subprocess.Popen(command, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, env=safe_environment())
    if input_bytes is not None:
        try:
            child.stdin.write(input_bytes)
            child.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            try:
                child.stdin.close()
            except BrokenPipeError:
                pass
    # Retain ownership while this short launcher lives; avoid Popen destructor
    # warnings or premature orphan bookkeeping. OS reparents after launcher exit.
    reap_detached()
    _DETACHED_CHILDREN.append(child)
    return child.pid
