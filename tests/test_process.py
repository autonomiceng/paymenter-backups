import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from paymenter_backups.common import BackupError, State
from paymenter_backups import process
from paymenter_backups.process import detach, group_members, hash_stream, process_identity, reap_detached, run, terminate


class ProcessTests(unittest.TestCase):
    def test_hash_stream_registers_and_terminates_fallback_group(self):
        self.check_fallback_cleanup(hash_stream)

    def test_run_terminates_fallback_group(self):
        self.check_fallback_cleanup(run)

    def check_fallback_cleanup(self, runner):
        for inspection in ('initial', 'unavailable'):
            with self.subTest(inspection=inspection):
                children, groups = [], []
                popen = subprocess.Popen
                def spawn(*args, **kwargs):
                    child = popen(*args, **kwargs)
                    children.append(child)
                    return child
                identity = process_identity
                first = True
                def inspect(pid):
                    nonlocal first
                    if first or inspection == 'unavailable':
                        first = False
                        return None
                    return identity(pid)
                try:
                    with patch.object(process.subprocess, 'Popen', side_effect=spawn), \
                            patch.object(process, 'process_identity', side_effect=inspect):
                        with self.assertRaisesRegex(BackupError, 'child-timeout'):
                            runner([sys.executable, '-c', 'import time;time.sleep(30)'], .1,
                                   register=groups.append)
                    self.assertEqual(len(groups), 1)
                    self.assertEqual(groups[0]['starttime'], 'unknown')
                    self.assertEqual(groups[0]['pid'], groups[0]['pgid'])
                    self.assertEqual(children[0].returncode, -signal.SIGKILL)
                    self.assertFalse(group_members(groups[0]))
                    self.assertTrue(children[0].stdout.closed)
                finally:
                    for child in children:
                        if child.poll() is None:
                            os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                        child.stdout.close()

    def test_recorded_fallback_group_cannot_authorize_signaling(self):
        group = {'pid': 123, 'pgid': 123, 'starttime': 'unknown', 'boot_id': process.boot_id()}
        with patch.object(process, 'process_identity', return_value=None), \
                patch.object(process, 'group_members', return_value=[group]), \
                patch.object(process.os, 'killpg') as kill:
            with self.assertRaisesRegex(BackupError, 'process-state-uncertain'):
                terminate(group)
        kill.assert_not_called()

    def test_reaped_child_cannot_authorize_fallback_signaling(self):
        with subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True) as child:
            child.wait()
            group = {'pid': child.pid, 'pgid': child.pid, 'starttime': 'unknown',
                     'boot_id': process.boot_id()}
            with patch.object(process.os, 'killpg') as kill:
                with self.assertRaisesRegex(BackupError, 'process-state-uncertain'):
                    process.terminate_child(child, group)
            kill.assert_not_called()

    def test_hash_stream_cleanup_wait_preserves_timeout_and_closes_pipe(self):
        children = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        try:
            with patch.object(process.subprocess, 'Popen', side_effect=spawn), \
                    patch.object(process, 'terminate'), \
                    patch.object(popen, 'wait', side_effect=subprocess.TimeoutExpired('fixture', 3)):
                with self.assertRaisesRegex(BackupError, 'child-timeout'):
                    hash_stream([sys.executable, '-c', 'import time;time.sleep(30)'], .1)
            self.assertTrue(children[0].stdout.closed)
        finally:
            for child in children:
                child.kill()
                child.wait()

    def test_hash_stream_successful_leader_with_live_descendant_refuses(self):
        groups = []
        try:
            with self.assertRaisesRegex(BackupError, 'process-state-uncertain'):
                hash_stream([sys.executable, '-c',
                             'import os,time;p=os.fork();os.close(1) if p==0 else None;time.sleep(30) if p==0 else None'],
                            2, groups.append)
            self.assertTrue(group_members(groups[0]))
        finally:
            if groups and group_members(groups[0]):
                os.killpg(groups[0]['pgid'], signal.SIGKILL)

    def test_subprocess_cannot_inherit_flock(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = pathlib.Path(temporary) / "app"
            app.mkdir()
            state = State(app, pathlib.Path(temporary) / "private")
            with state.lock() as descriptor:
                output = run([sys.executable, "-c", "import os;print(os.listdir('/proc/self/fd'))"], 2)
                # Check actual fd target, not descriptor number (numbers can be reused).
                script = "import os; print([os.readlink('/proc/self/fd/'+f) for f in os.listdir('/proc/self/fd') if os.path.exists('/proc/self/fd/'+f)])"
                self.assertNotIn(str(state.root / "run.lock").encode(), run([sys.executable, "-c", script], 2))

    def test_timeout_closes_live_group(self):
        groups = []
        with self.assertRaisesRegex(BackupError, "child-timeout"):
            run([sys.executable, "-c", "import time;time.sleep(30)"], .1, register=groups.append)
        self.assertFalse(group_members(groups[0]))

    def test_successful_leader_with_live_descendant_refuses(self):
        groups = []
        with tempfile.TemporaryDirectory() as temporary:
            try:
                with self.assertRaisesRegex(BackupError, "process-state-uncertain"):
                    run([sys.executable, "-c", "import os,time; p=os.fork();time.sleep(30) if p==0 else None"], 2,
                        output=pathlib.Path(temporary) / "output", register=groups.append)
                self.assertTrue(group_members(groups[0]))
            finally:
                if groups and group_members(groups[0]):
                    os.killpg(groups[0]["pgid"], signal.SIGKILL)

    def test_parent_death_kills_actual_child(self):
        module = pathlib.Path(__file__).resolve().parents[1] / "src"
        script = "import sys,subprocess,os,time;sys.path.insert(0,sys.argv[1]);from paymenter_backups.process import child_setup;parent=os.getpid();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True,preexec_fn=lambda:child_setup(parent));print(p.pid,flush=True);time.sleep(60)"
        parent = subprocess.Popen([sys.executable, "-c", script, str(module)], stdout=subprocess.PIPE)
        child_pid = int(parent.stdout.readline())
        parent.kill()
        parent.wait()
        parent.stdout.close()
        deadline = time.time() + 3
        while process_identity(child_pid) and time.time() < deadline:
            time.sleep(.02)
        self.assertIsNone(process_identity(child_pid))

    def test_pipe_output_bound_refuses_without_consuming_unbounded_output(self):
        with self.assertRaisesRegex(BackupError, "child-output-limit"):
            run([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*(20*1024*1024))"], 3)

    def test_detached_launcher_sets_session_and_closes_actual_caller_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = pathlib.Path(temporary) / "app"
            app.mkdir()
            state = State(app, pathlib.Path(temporary) / "private")
            receipt = pathlib.Path(temporary) / "receipt.json"
            script = "import os,json,sys,time;targets=[os.readlink('/proc/self/fd/'+f) for f in os.listdir('/proc/self/fd') if os.path.exists('/proc/self/fd/'+f)];open(sys.argv[1],'w').write(json.dumps({'sid':os.getsid(0),'pid':os.getpid(),'targets':targets}));time.sleep(30)"
            with state.lock():
                pid = detach([sys.executable, "-I", "-c", script, str(receipt)])
                try:
                    deadline = time.time() + 3
                    while not receipt.exists() and time.time() < deadline:
                        time.sleep(.02)
                    data = json.loads(receipt.read_text())
                    self.assertEqual(data["sid"], pid)
                    self.assertNotIn(str(state.root / "run.lock"), data["targets"])
                finally:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    reap_detached()


if __name__ == "__main__":
    unittest.main()
