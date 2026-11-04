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
from unittest.mock import Mock, patch

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

    def test_schedule_preserves_report_when_supervisor_launch_is_denied(self):
        report_path = self.state / "self-check.json"
        previous = json.dumps({
            "version": "older", "status": "passed", "checked_at": 1,
        })
        report_path.write_text(previous)
        with (patch.object(diagnostics.subprocess, "Popen",
                           side_effect=PermissionError("execution denied")) as launch,
              patch("psutil.Process") as process):
            self.assertFalse(diagnostics.schedule(self.root, self.state, now=100))
            launch.assert_called_once()
            process.assert_not_called()
        self.assertEqual(report_path.read_text(), previous)

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

    def test_monitoring_error_stops_the_launched_check(self):
        process = Mock()
        process.poll.side_effect = OSError("monitoring failed")

        def launch(*_args, **kwargs):
            kwargs["stdout"].write(b"output before monitoring failed\n")
            kwargs["stdout"].flush()
            return process

        with (patch.object(diagnostics.subprocess, "Popen", side_effect=launch),
              patch.object(diagnostics, "_stop_check") as stop):
            report = diagnostics.run(self.root, self.state)

        stop.assert_called_once_with(process)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"], 1)
        output = (self.state / "self-check.log").read_text()
        self.assertIn("output before monitoring failed", output)
        self.assertIn("Self-check could not finish: OSError: monitoring failed", output)
        self.assertEqual(
            json.loads((self.state / "self-check.json").read_text()), report)

    def test_monitoring_error_preserves_captured_test_output(self):
        process = Mock()
        process.poll.side_effect = OSError("monitoring failed")

        def launch(*_args, **kwargs):
            kwargs["stdout"].write(b"failure details before monitoring error\n")
            kwargs["stdout"].flush()
            return process

        with patch.object(diagnostics.subprocess, "Popen", side_effect=launch):
            report = diagnostics.run(self.root, self.state)

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"], 1)
        output = (self.state / "self-check.log").read_text()
        self.assertIn("failure details before monitoring error", output)
        self.assertIn("Self-check could not finish: OSError: monitoring failed", output)
        self.assertIn("failure details before monitoring error", report["summary"])

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

    def test_stop_check_continues_after_a_child_exits_concurrently(self):
        import psutil

        process = Mock(pid=123)
        process.poll.return_value = None
        vanished = Mock()
        vanished.terminate.side_effect = psutil.NoSuchProcess(124)
        sibling = Mock()
        children = [vanished, sibling]
        with (patch("psutil.Process") as parent,
              patch("psutil.wait_procs", return_value=(children, [])) as wait):
            parent.return_value.children.return_value = children
            diagnostics._stop_check(process)

        vanished.terminate.assert_called_once_with()
        sibling.terminate.assert_called_once_with()
        process.terminate.assert_called_once_with()
        wait.assert_called_once_with(children, timeout=5)
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_stop_check_cleans_children_when_parent_exits_before_terminate(self):
        process = Mock(pid=123)
        process.poll.side_effect = [None, 0]
        process.terminate.side_effect = ProcessLookupError("parent exited")
        child = Mock()
        with (patch("psutil.Process") as parent,
              patch("psutil.wait_procs", return_value=([], [child])) as wait):
            parent.return_value.children.return_value = [child]
            diagnostics._stop_check(process)

        child.terminate.assert_called_once_with()
        process.terminate.assert_called_once_with()
        wait.assert_called_once_with([child], timeout=5)
        child.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_stop_check_cleans_children_when_parent_termination_is_denied(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        process.terminate.side_effect = PermissionError("termination denied")
        process.wait.side_effect = [
            diagnostics.subprocess.TimeoutExpired("pytest", 5), None,
        ]
        child = Mock()
        with (patch("psutil.Process") as parent,
              patch("psutil.wait_procs", return_value=([], [child])) as wait):
            parent.return_value.children.return_value = [child]
            diagnostics._stop_check(process)

        child.terminate.assert_called_once_with()
        process.terminate.assert_called_once_with()
        wait.assert_called_once_with([child], timeout=5)
        child.kill.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_stop_check_kills_siblings_when_a_child_exits_before_kill(self):
        import psutil

        process = Mock(pid=123)
        process.poll.return_value = None
        vanished = Mock()
        vanished.kill.side_effect = psutil.NoSuchProcess(124)
        sibling = Mock()
        children = [vanished, sibling]
        with (patch("psutil.Process") as parent,
              patch("psutil.wait_procs", return_value=([], children)) as wait):
            parent.return_value.children.return_value = children
            diagnostics._stop_check(process)

        vanished.terminate.assert_called_once_with()
        sibling.terminate.assert_called_once_with()
        process.terminate.assert_called_once_with()
        wait.assert_called_once_with(children, timeout=5)
        vanished.kill.assert_called_once_with()
        sibling.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_stop_check_continues_when_a_child_denies_access(self):
        import psutil

        process = Mock(pid=123)
        process.poll.return_value = None
        inaccessible = Mock()
        inaccessible.terminate.side_effect = psutil.AccessDenied(124)
        inaccessible.kill.side_effect = psutil.AccessDenied(124)
        sibling = Mock()
        children = [inaccessible, sibling]
        with (patch("psutil.Process") as parent,
              patch("psutil.wait_procs", return_value=([], children)) as wait):
            parent.return_value.children.return_value = children
            diagnostics._stop_check(process)

        inaccessible.terminate.assert_called_once_with()
        sibling.terminate.assert_called_once_with()
        process.terminate.assert_called_once_with()
        wait.assert_called_once_with(children, timeout=5)
        inaccessible.kill.assert_called_once_with()
        sibling.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_deferred_check_only_preserves_current_release_failures(self):
        current = diagnostics.version(self.root)
        for previous_version, expected_failures in (("older", 0), (current, 2)):
            with self.subTest(previous_version=previous_version):
                (self.state / "self-check.json").write_text(json.dumps({
                    "version": previous_version, "status": "failed", "failures": 2,
                }))
                with (patch.object(diagnostics, "active_projects", return_value=True),
                      patch.object(diagnostics.subprocess, "Popen") as launch):
                    report = diagnostics.run(self.root, self.state)
                launch.assert_not_called()
                self.assertEqual(report["status"], "deferred")
                self.assertEqual(report["version"], current)
                self.assertEqual(report["failures"], expected_failures)
                self.assertEqual(
                    json.loads((self.state / "self-check.json").read_text()), report)

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
