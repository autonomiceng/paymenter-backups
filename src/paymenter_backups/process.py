"""Linux subprocess containment; children inherit no coordination descriptors."""
import ctypes
import atexit
import os
import pathlib
import signal
import selectors
import resource
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


def safe_environment():
    # Do not inherit TAR_OPTIONS, MYSQL_PWD, GZIP, LD_PRELOAD, provider/CA/proxy
    # overrides, application variable overrides or TLS session key logging.
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}


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


def run(command, timeout, output=None, input_bytes=None, register=None, budget_check=None, file_limit=None):
    parent = os.getpid()
    stream = open(output, "wb") if output else subprocess.PIPE
    proc = None
    group = None
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                stdout=stream, stderr=subprocess.DEVNULL, close_fds=True,
                                start_new_session=True, preexec_fn=lambda: child_setup(parent, file_limit), env=safe_environment())
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
                terminate(group)
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


def hash_stream(command, timeout, register=None):
    """Stream the second dump through a hasher, with no second SQL file."""
    import hashlib
    import selectors
    parent = os.getpid()
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
                            preexec_fn=lambda: child_setup(parent), env=safe_environment())
    group = process_identity(proc.pid)
    if register and group:
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
        if proc.wait(timeout=max(0.1, deadline - time.monotonic())):
            raise BackupError("child-failed")
        return hasher.hexdigest()
    finally:
        selector.close()
        if proc.poll() is None and group:
            terminate(group)
            proc.wait(timeout=3)
        proc.stdout.close()


def app_workers(app):
    found = []
    for path in pathlib.Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            arguments = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            if "artisan" not in arguments or not any(word in arguments for word in ("schedule:run", "queue:work", "queue:listen", "horizon")):
                continue
            cwd = (path / "cwd").resolve()
            if cwd == app or str(app / "artisan") in arguments:
                found.append(int(path.name))
        except (OSError, RuntimeError):
            continue
    return found


def detach(command):
    # Popen actually forks/execs, closes caller flock descriptors and setsid.
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True, env=safe_environment())
    # Retain ownership while this short launcher lives; avoid Popen destructor
    # warnings or premature orphan bookkeeping. OS reparents after launcher exit.
    reap_detached()
    _DETACHED_CHILDREN.append(child)
    return child.pid
