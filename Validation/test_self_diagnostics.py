"""The installed regression runner stays local, bounded, and repeatable."""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import self_diagnostics as diagnostics


class SelfDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "LongTasks"
        self.state.mkdir()
        (self.root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.206")
        python = self.root / "Toolbox/python/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        validation = self.root / "Validation/test_long_tasks.py"
        validation.parent.mkdir()
        validation.touch()

    def test_self_check_waits_for_music_and_other_active_projects(self):
        with closing(sqlite3.connect(self.state / "projects.db")) as connection:
            connection.execute("CREATE TABLE projects (status TEXT)")
            connection.execute("INSERT INTO projects VALUES ('working')")
            connection.commit()
        with patch.object(diagnostics.subprocess, "Popen") as launch:
            self.assertFalse(diagnostics.schedule(self.root, self.state, now=100))
            launch.assert_not_called()
        with closing(sqlite3.connect(self.state / "projects.db")) as connection:
            connection.execute("UPDATE projects SET status='ready_for_review'")
            connection.commit()
        proc = SimpleNamespace(pid=123)
        with (patch.object(diagnostics.subprocess, "Popen", return_value=proc) as launch,
              patch("psutil.Process") as process):
            process.return_value.create_time.return_value = 101
            self.assertTrue(diagnostics.schedule(self.root, self.state, now=101))
            launch.assert_called_once()

    def test_active_projects_handles_uri_reserved_characters_in_path(self):
        state = self.state / "projects#archive"
        state.mkdir()
        with closing(sqlite3.connect(state / "projects.db")) as connection:
            connection.execute("CREATE TABLE projects (status TEXT)")
            connection.execute("INSERT INTO projects VALUES ('ready_for_review')")
            connection.commit()
            self.assertFalse(diagnostics.active_projects(state))
            connection.execute("UPDATE projects SET status='working'")
            connection.commit()
            self.assertTrue(diagnostics.active_projects(state))

    def test_failed_check_retries_then_backs_off_for_a_week(self):
        proc = SimpleNamespace(pid=123)
        with (patch.object(diagnostics.subprocess, "Popen", return_value=proc) as launch,
              patch("psutil.Process") as process):
            process.return_value.create_time.return_value = 100
            self.assertTrue(diagnostics.schedule(self.root, self.state, now=100))
            report = json.loads((self.state / "self-check.json").read_text())
            self.assertEqual(report["status"], "running")
            self.assertEqual(launch.call_args.args[0][0],
                             str(self.root / "Toolbox/python/Scripts/python.exe"))
        with patch.object(diagnostics, "_running", return_value=True):
            self.assertFalse(diagnostics.schedule(self.root, self.state, now=101))
        report.update(status="failed", checked_at=100)
        (self.state / "self-check.json").write_text(json.dumps(report))
        self.assertFalse(diagnostics.schedule(self.root, self.state, now=101))
        with (patch.object(diagnostics.subprocess, "Popen", return_value=proc),
              patch("psutil.Process") as process):
            process.return_value.create_time.return_value = 200
            self.assertTrue(diagnostics.schedule(
                self.root, self.state, now=101 + diagnostics.FAILURE_RETRY))
        report.update(status="failed", checked_at=200, failures=diagnostics.MAX_FAILURES)
        (self.state / "self-check.json").write_text(json.dumps(report))
        self.assertFalse(diagnostics.schedule(
            self.root, self.state, now=200 + diagnostics.FAILURE_RETRY))

    @unittest.skipIf(diagnostics.os.name == "nt", "POSIX file locking")
    def test_posix_report_lock_rejects_contention_and_releases(self):
        with diagnostics._report_lock(self.state, blocking=False) as acquired:
            self.assertTrue(acquired)
            with diagnostics._report_lock(self.state, blocking=False) as contender:
                self.assertFalse(contender)
            # A failed contender must not release the original lock.
            with diagnostics._report_lock(self.state, blocking=False) as contender:
                self.assertFalse(contender)
        with diagnostics._report_lock(self.state, blocking=False) as acquired:
            self.assertTrue(acquired)

    def test_two_simultaneous_schedulers_launch_one_check(self):
        entered = threading.Event()
        release = threading.Event()
        results = []
        launches = []

        def launch(*args, **kwargs):
            launches.append(args)
            entered.set()
            self.assertTrue(release.wait(5))
            return SimpleNamespace(pid=123)

        with (patch.object(diagnostics.subprocess, "Popen", side_effect=launch),
              patch("psutil.Process") as process):
            process.return_value.create_time.return_value = 100
            thread = threading.Thread(target=lambda: results.append(
                diagnostics.schedule(self.root, self.state, now=100)))
            thread.start()
            self.assertTrue(entered.wait(5))
            self.assertFalse(diagnostics.schedule(self.root, self.state, now=100))
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True])
        self.assertEqual(len(launches), 1)

    def test_real_test_exit_is_saved_with_log(self):
        def launch(*_args, **kwargs):
            kwargs["stdout"].write(b"1 failed\ndetails\n")
            kwargs["stdout"].flush()
            return SimpleNamespace(poll=lambda: 1)

        with patch.object(diagnostics.subprocess, "Popen", side_effect=launch) as run:
            report = diagnostics.run(self.root, self.state)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"], 1)
        self.assertIn("1 failed", report["summary"])
        self.assertIn("details", (self.state / "self-check.log").read_text())
        self.assertEqual(run.call_args.args[0][1:], ["-m", "pytest", "-q", "Validation"])
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         diagnostics.HIDE | diagnostics.LOW_PRIORITY)

    def test_failure_count_resets_after_passing_check(self):
        (self.state / "self-check.json").write_text(json.dumps({
            "version": diagnostics.version(self.root), "status": "running", "failures": 2,
        }))
        def launch(*_args, **kwargs):
            kwargs["stdout"].write(b"527 passed\n")
            kwargs["stdout"].flush()
            return SimpleNamespace(poll=lambda: 0)

        with patch.object(diagnostics.subprocess, "Popen", side_effect=launch):
            report = diagnostics.run(self.root, self.state)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["failures"], 0)

    def test_old_check_cannot_overwrite_a_newer_release_report(self):
        new_report = {"version": "Cale AI Workbench 2", "status": "passed"}

        def finish_after_update(*args, **kwargs):
            (self.root / ".cale-ai-install").write_text(new_report["version"])
            (self.state / "self-check.json").write_text(json.dumps(new_report))
            kwargs["stdout"].write(b"old failure")
            kwargs["stdout"].flush()
            return SimpleNamespace(poll=lambda: 1)

        with patch.object(diagnostics.subprocess, "Popen", side_effect=finish_after_update):
            result = diagnostics.run(self.root, self.state)
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(json.loads((self.state / "self-check.json").read_text()), new_report)

    def test_running_check_yields_when_a_user_task_starts(self):
        process = SimpleNamespace(pid=123, poll=lambda: None)
        with (patch.object(diagnostics, "active_projects", side_effect=[False, True]),
              patch.object(diagnostics.subprocess, "Popen", return_value=process) as launch,
              patch.object(diagnostics, "_stop_check") as stop,
              patch.object(diagnostics.time, "sleep") as sleep):
            report = diagnostics.run(self.root, self.state)
        self.assertEqual(report["status"], "deferred")
        self.assertEqual(report["failures"], 0)
        launch.assert_called_once()
        stop.assert_called_once_with(process)
        sleep.assert_not_called()
        with (patch.object(diagnostics, "active_projects", return_value=False),
              patch.object(diagnostics.subprocess, "Popen", return_value=process),
              patch("psutil.Process") as running):
            running.return_value.create_time.return_value = 100
            self.assertTrue(diagnostics.schedule(self.root, self.state, now=100))

    def test_only_repeated_failure_on_current_version_notifies(self):
        current = diagnostics.version(self.root)
        self.assertFalse(diagnostics.persistent_failure(
            {"version": current, "status": "failed", "failures": 1}, current))
        self.assertTrue(diagnostics.persistent_failure(
            {"version": current, "status": "failed", "failures": 2}, current))
        self.assertFalse(diagnostics.persistent_failure(
            {"version": "older", "status": "failed", "failures": 3}, current))


if __name__ == "__main__":
    unittest.main()
