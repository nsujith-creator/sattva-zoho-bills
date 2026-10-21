"""
Windows-safe watchdog for watcher.py recovery.

This script only checks watcher health and restarts watcher.py when safe.
It does not call Zoho, parse PDFs, or process bills.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


PYTHON_EXE = r"C:\Users\sujit\AppData\Local\Programs\Python\Python312\python.exe"
WATCHER_PYTHON_EXE = r"C:\Users\sujit\AppData\Local\Programs\Python\Python312\pythonw.exe"
WATCHER_SCRIPT = r"C:\temp\zoho\watcher.py"
WATCHER_LOCK = r"C:\temp\zoho\watcher.lock"
WATCHER_LOG = r"C:\temp\zoho\watcher.log"
WATCHER_STARTUP_LOG = r"C:\temp\zoho\watcher_startup.log"
WATCHER_CWD = r"C:\temp\zoho"
SYSTEM_HEALTH_FILE = r"C:\temp\zoho\system_health.json"
WATCHER_HEARTBEAT_MAX_AGE_SECONDS = 300
WATCHDOG_LOG = r"C:\temp\zoho\watchdog.log"
WATCHER_START_VERIFY_SECONDS = 3


def safe_json_read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def log(message):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {message}"
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def process_exists(pid):
    try:
        pid = int(pid)
    except Exception:
        return False

    if pid <= 0:
        return False

    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return True

    if result.returncode != 0:
        return True

    needle = f'"{pid}"'
    return needle in result.stdout


def read_watcher_pid_from_lock():
    try:
        with open(WATCHER_LOCK, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        return None

    if pid <= 0:
        return None
    return pid


def heartbeat_age_seconds():
    health = safe_json_read(SYSTEM_HEALTH_FILE, {})
    if not isinstance(health, dict):
        return None

    watcher = health.get("watcher")
    if not isinstance(watcher, dict):
        return None

    try:
        heartbeat_epoch = float(watcher["heartbeat_epoch"])
    except Exception:
        return None

    return time.time() - heartbeat_epoch


def is_watcher_healthy():
    if not os.path.exists(SYSTEM_HEALTH_FILE):
        return False

    health = safe_json_read(SYSTEM_HEALTH_FILE, {})
    if not isinstance(health, dict):
        return False

    watcher = health.get("watcher")
    if not isinstance(watcher, dict):
        return False

    if watcher.get("status") != "running":
        return False

    age = heartbeat_age_seconds()
    if age is None or age > WATCHER_HEARTBEAT_MAX_AGE_SECONDS:
        return False

    try:
        pid = int(watcher["pid"])
    except Exception:
        return False

    return process_exists(pid)


def remove_stale_watcher_lock_if_safe():
    if not os.path.exists(WATCHER_LOCK):
        return False

    pid = read_watcher_pid_from_lock()
    if pid is not None and process_exists(pid):
        return False

    os.remove(WATCHER_LOCK)
    return True


def watcher_log_has_new_startup_line(previous_size):
    try:
        with open(WATCHER_LOG, encoding="utf-8", errors="replace") as f:
            f.seek(previous_size)
            new_text = f.read()
    except Exception:
        return False

    return "ZOHO BILLS WATCHER v2 STARTED" in new_text


def child_process_is_alive(proc):
    try:
        return proc.poll() is None
    except Exception:
        return False


def watcher_python_exe():
    if os.path.exists(WATCHER_PYTHON_EXE):
        return WATCHER_PYTHON_EXE

    log("WATCHDOG_PYTHONW_MISSING_FALLBACK")
    return PYTHON_EXE


def start_watcher():
    if is_watcher_healthy():
        log("WATCHDOG_OK")
        return True

    previous_watcher_log_size = 0
    try:
        previous_watcher_log_size = os.path.getsize(WATCHER_LOG)
    except OSError:
        pass

    creationflags = 0
    for flag_name in (
        "CREATE_NO_WINDOW",
        "DETACHED_PROCESS",
        "CREATE_NEW_PROCESS_GROUP",
    ):
        creationflags |= getattr(subprocess, flag_name, 0)

    try:
        with open(WATCHER_STARTUP_LOG, "ab") as startup_log:
            proc = subprocess.Popen(
                [watcher_python_exe(), WATCHER_SCRIPT],
                cwd=WATCHER_CWD,
                stdout=startup_log,
                stderr=startup_log,
                creationflags=creationflags,
            )
    except Exception as exc:
        log(f"WATCHDOG_START_VERIFY_FAILED launch_error={exc}")
        return False

    log(f"WATCHDOG_STARTED pid={proc.pid}")
    time.sleep(WATCHER_START_VERIFY_SECONDS)

    lock_exists = os.path.exists(WATCHER_LOCK)
    startup_line_seen = watcher_log_has_new_startup_line(previous_watcher_log_size)
    child_alive = child_process_is_alive(proc)

    if lock_exists or startup_line_seen or child_alive:
        log(
            "WATCHDOG_START_VERIFY_OK "
            f"lock_exists={lock_exists} "
            f"startup_line_seen={startup_line_seen} "
            f"child_alive={child_alive}"
        )
        return True

    returncode = None
    try:
        returncode = proc.poll()
    except Exception:
        pass

    log(
        "WATCHDOG_START_VERIFY_FAILED "
        f"lock_exists={lock_exists} "
        f"startup_line_seen={startup_line_seen} "
        f"child_alive={child_alive} "
        f"returncode={returncode} "
        f"startup_log={WATCHER_STARTUP_LOG}"
    )
    return False


def main():
    if is_watcher_healthy():
        log("WATCHDOG_OK")
        return 0

    if os.path.exists(WATCHER_LOCK):
        pid = read_watcher_pid_from_lock()
        if pid is not None and process_exists(pid):
            log("WATCHDOG_LOCK_LIVE_SKIP")
            return 0

        try:
            if remove_stale_watcher_lock_if_safe():
                log("WATCHDOG_STALE_LOCK_REMOVED")
        except Exception as exc:
            log(f"WATCHDOG_STALE_LOCK_REMOVE_FAILED {exc}")
            return 0

    start_watcher()
    return 0


class WatchdogSelfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.originals = {
            "WATCHER_PYTHON_EXE": WATCHER_PYTHON_EXE,
            "WATCHER_SCRIPT": WATCHER_SCRIPT,
            "WATCHER_LOCK": WATCHER_LOCK,
            "WATCHER_LOG": WATCHER_LOG,
            "WATCHER_STARTUP_LOG": WATCHER_STARTUP_LOG,
            "SYSTEM_HEALTH_FILE": SYSTEM_HEALTH_FILE,
            "WATCHDOG_LOG": WATCHDOG_LOG,
            "WATCHER_START_VERIFY_SECONDS": WATCHER_START_VERIFY_SECONDS,
        }
        self.health_path = os.path.join(self.tmp.name, "system_health.json")
        self.lock_path = os.path.join(self.tmp.name, "watcher.lock")
        self.log_path = os.path.join(self.tmp.name, "watchdog.log")
        self.watcher_log_path = os.path.join(self.tmp.name, "watcher.log")
        self.startup_log_path = os.path.join(self.tmp.name, "watcher_startup.log")
        self.script_path = os.path.join(self.tmp.name, "watcher.py")
        self.pythonw_path = os.path.join(self.tmp.name, "pythonw.exe")
        with open(self.pythonw_path, "w", encoding="utf-8") as f:
            f.write("")
        globals()["WATCHER_PYTHON_EXE"] = self.pythonw_path
        globals()["SYSTEM_HEALTH_FILE"] = self.health_path
        globals()["WATCHER_LOCK"] = self.lock_path
        globals()["WATCHER_LOG"] = self.watcher_log_path
        globals()["WATCHER_STARTUP_LOG"] = self.startup_log_path
        globals()["WATCHDOG_LOG"] = self.log_path
        globals()["WATCHER_SCRIPT"] = self.script_path
        globals()["WATCHER_START_VERIFY_SECONDS"] = 0
        self.started = []
        self.fake_returncode = None

        def fake_popen(args, cwd=None, stdout=None, stderr=None, creationflags=0):
            fake_returncode = self.fake_returncode
            self.started.append(
                {
                    "args": args,
                    "cwd": cwd,
                    "stdout": stdout,
                    "stderr": stderr,
                    "creationflags": creationflags,
                }
            )

            class FakeProcess:
                pid = 99999

                def poll(self):
                    return fake_returncode

            return FakeProcess()

        self.popen_patch = mock.patch("subprocess.Popen", side_effect=fake_popen)
        self.popen_patch.start()
        self.addCleanup(self.popen_patch.stop)
        self.addCleanup(self.restore_globals)

    def restore_globals(self):
        for key, value in self.originals.items():
            globals()[key] = value

    def write_health(self, watcher):
        with open(self.health_path, "w", encoding="utf-8") as f:
            json.dump({"watcher": watcher}, f)

    def log_text(self):
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def test_healthy_heartbeat_live_pid_no_start(self):
        self.write_health(
            {
                "status": "running",
                "pid": 1234,
                "heartbeat_epoch": time.time(),
            }
        )
        with mock.patch(__name__ + ".process_exists", return_value=True):
            self.assertEqual(main(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("WATCHDOG_OK", self.log_text())

    def test_stale_heartbeat_no_lock_would_start(self):
        self.write_health(
            {
                "status": "running",
                "pid": 1234,
                "heartbeat_epoch": time.time() - 999,
            }
        )
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertEqual(main(), 0)
        self.assertEqual(len(self.started), 1)
        self.assertIn("WATCHDOG_STARTED pid=99999", self.log_text())
        self.assertIn("WATCHDOG_START_VERIFY_OK", self.log_text())

    def test_missing_heartbeat_would_start(self):
        self.write_health({"status": "running", "pid": 1234})
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertEqual(main(), 0)
        self.assertEqual(len(self.started), 1)
        self.assertIn("WATCHDOG_STARTED pid=99999", self.log_text())
        self.assertIn("WATCHDOG_START_VERIFY_OK", self.log_text())

    def test_lock_with_live_pid_skip_start(self):
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write("4321")
        with mock.patch(__name__ + ".process_exists", return_value=True):
            self.assertEqual(main(), 0)
        self.assertEqual(self.started, [])
        self.assertTrue(os.path.exists(self.lock_path))
        self.assertIn("WATCHDOG_LOCK_LIVE_SKIP", self.log_text())

    def test_lock_with_dead_pid_remove_and_start(self):
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write("4321")
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertEqual(main(), 0)
        self.assertFalse(os.path.exists(self.lock_path))
        self.assertEqual(len(self.started), 1)
        self.assertIn("WATCHDOG_STALE_LOCK_REMOVED", self.log_text())
        self.assertIn("WATCHDOG_STARTED pid=99999", self.log_text())
        self.assertIn("WATCHDOG_START_VERIFY_OK", self.log_text())

    def test_corrupt_lock_remove_and_start(self):
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write("not-a-pid")
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertEqual(main(), 0)
        self.assertFalse(os.path.exists(self.lock_path))
        self.assertEqual(len(self.started), 1)
        self.assertIn("WATCHDOG_STALE_LOCK_REMOVED", self.log_text())
        self.assertIn("WATCHDOG_STARTED pid=99999", self.log_text())
        self.assertIn("WATCHDOG_START_VERIFY_OK", self.log_text())

    def test_tasklist_failure_assumes_process_exists(self):
        with mock.patch("subprocess.run", side_effect=OSError("tasklist failed")):
            self.assertTrue(process_exists(1234))

    def test_no_real_subprocess_spawned_during_tests(self):
        self.write_health(
            {
                "status": "running",
                "pid": 1234,
                "heartbeat_epoch": time.time() - 999,
            }
        )
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertEqual(main(), 0)
        self.assertEqual(self.started[0]["args"], [self.pythonw_path, self.script_path])
        self.assertEqual(self.started[0]["cwd"], r"C:\temp\zoho")

        real_paths = [
            r"C:\temp\zoho\system_health.json",
            r"C:\temp\zoho\watcher.lock",
            r"C:\temp\zoho\watchdog.log",
            r"C:\temp\zoho\watcher_startup.log",
        ]
        for real_path in real_paths:
            self.assertNotEqual(os.path.abspath(self.health_path), real_path)
            self.assertNotEqual(os.path.abspath(self.lock_path), real_path)
            self.assertNotEqual(os.path.abspath(self.log_path), real_path)
            self.assertNotEqual(os.path.abspath(self.startup_log_path), real_path)

    def test_start_watcher_uses_pythonw_when_available(self):
        self.fake_returncode = 1
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertFalse(start_watcher())

        self.assertEqual(self.started[0]["args"], [self.pythonw_path, self.script_path])
        self.assertNotIn("WATCHDOG_PYTHONW_MISSING_FALLBACK", self.log_text())

    def test_start_watcher_falls_back_when_pythonw_missing(self):
        globals()["WATCHER_PYTHON_EXE"] = os.path.join(self.tmp.name, "missing-pythonw.exe")
        self.fake_returncode = 1
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertFalse(start_watcher())

        self.assertEqual(self.started[0]["args"], [PYTHON_EXE, self.script_path])
        self.assertIn("WATCHDOG_PYTHONW_MISSING_FALLBACK", self.log_text())

    def test_start_watcher_redirects_stdout_stderr_to_startup_log(self):
        self.fake_returncode = 1
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertFalse(start_watcher())

        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0]["cwd"], r"C:\temp\zoho")
        self.assertEqual(
            os.path.abspath(self.started[0]["stdout"].name),
            os.path.abspath(self.startup_log_path),
        )
        self.assertIs(self.started[0]["stdout"], self.started[0]["stderr"])
        self.assertTrue(os.path.exists(self.startup_log_path))

    def test_start_verification_success_when_fake_lock_appears(self):
        self.fake_returncode = 1

        def fake_sleep(_seconds):
            with open(self.lock_path, "w", encoding="utf-8") as f:
                f.write("99999")

        with mock.patch(__name__ + ".process_exists", return_value=False), mock.patch(
            "time.sleep", side_effect=fake_sleep
        ):
            self.assertTrue(start_watcher())

        self.assertIn("WATCHDOG_START_VERIFY_OK", self.log_text())
        self.assertIn("lock_exists=True", self.log_text())

    def test_start_verification_failure_logs_failure(self):
        self.fake_returncode = 1
        with mock.patch(__name__ + ".process_exists", return_value=False):
            self.assertFalse(start_watcher())

        self.assertIn("WATCHDOG_STARTED pid=99999", self.log_text())
        self.assertIn("WATCHDOG_START_VERIFY_FAILED", self.log_text())
        self.assertIn("returncode=1", self.log_text())

    def test_start_watcher_does_not_duplicate_healthy_watcher(self):
        self.write_health(
            {
                "status": "running",
                "pid": 1234,
                "heartbeat_epoch": time.time(),
            }
        )
        with mock.patch(__name__ + ".process_exists", return_value=True):
            self.assertTrue(start_watcher())

        self.assertEqual(self.started, [])
        self.assertIn("WATCHDOG_OK", self.log_text())


def run_self_tests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(WatchdogSelfTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("watchdog.py self-test passed")
        return 0
    return 1


if __name__ == "__main__":
    if "--test" in sys.argv[1:]:
        sys.exit(run_self_tests())
    sys.exit(main())
