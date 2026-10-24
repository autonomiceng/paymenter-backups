"""Short audited writers and their credential children share owned containment.

The group leader retains the coordinator's lock descriptors. Parent death or a
writer timeout kills the whole group before those descriptors can be released.
"""
import ctypes
import os
import pathlib
import signal
import subprocess
import sys
import time


def _wait_for_group_exit(pgid, timeout=3):
    from .process import boot_id, group_members

    group = {'pgid': pgid, 'boot_id': boot_id()}
    deadline = time.monotonic() + timeout
    while group_members(group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(['wait for writer process group', str(pgid)], timeout)
        time.sleep(min(0.02, remaining))


def run(command, input_bytes, descriptors, timeout=35, database_context=None):
    supervisor = [sys.executable, '-I', str(pathlib.Path(__file__).resolve()),
                  str(os.getpid()), ','.join(str(fd) for fd in descriptors), str(timeout - min(3, timeout / 3)), *command]
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TZ': 'UTC'}
    if database_context is not None:
        from .config import database_connection_environment
        environment.update(database_connection_environment(database_context))
    process = subprocess.Popen(supervisor, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, close_fds=True, pass_fds=tuple(descriptors),
                               start_new_session=True,
                               env=environment)
    try:
        output, error = process.communicate(input_bytes, timeout=timeout)
        _wait_for_group_exit(process.pid)
        return subprocess.CompletedProcess(command, process.returncode, output, error)
    except BaseException:
        # The unreaped session leader still owns this group ID. Kill every
        # writer (including a Symfony bridge grandchild), then drain/reap.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        _wait_for_group_exit(process.pid)
        raise
    finally:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe and not pipe.closed:
                pipe.close()


def supervise():
    parent = int(sys.argv[1])
    descriptors = tuple(int(fd) for fd in sys.argv[2].split(',') if fd)
    timeout = float(sys.argv[3])
    command = sys.argv[4:]
    if os.getpgrp() != os.getpid():
        raise SystemExit(125)

    def stop(_signal=None, _frame=None):
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
        stop()
    child = subprocess.Popen(command, close_fds=True, pass_fds=descriptors)
    try:
        result = child.wait(timeout=timeout)
        # Even a failed/exited native writer may have a live credential child.
        # Drain its descendants by killing the whole unreaped supervisor group.
        # Success is emitted by the writer; the engine treats a missing receipt
        # or any failure as uncertain and keeps its durable save intent.
        if result:
            stop()
        for path in pathlib.Path('/proc').iterdir():
            if not path.name.isdigit() or int(path.name) == os.getpid():
                continue
            try:
                status = (path / 'stat').read_text()
                fields = status[status.rfind(')') + 2:].split()
                if fields[0] != 'Z' and int(fields[2]) == os.getpgrp():
                    stop()
            except (OSError, ValueError, IndexError):
                continue
        return result
    except BaseException:
        stop()


if __name__ == '__main__':
    raise SystemExit(supervise())
