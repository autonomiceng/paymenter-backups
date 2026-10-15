import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from paymenter_backups.common import BackupError, State
from paymenter_backups.process import detach, group_members, process_identity, reap_detached, run, terminate


class ProcessTests(unittest.TestCase):
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
