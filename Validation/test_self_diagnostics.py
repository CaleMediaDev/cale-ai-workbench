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


def diagnostic_install(tmp_path):
    root = tmp_path / "install"
    state = root / "LongTasks"
    state.mkdir(parents=True)
    (root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.498")
    python = root / "Toolbox/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    validation = root / "Validation/test_long_tasks.py"
    validation.parent.mkdir()
    validation.touch()
    return root, state


def test_deferred_diagnostics_distinguish_database_error_without_raw_details(tmp_path):
    root, state = diagnostic_install(tmp_path)
    (state / "projects.db").touch()
    with patch.object(diagnostics.sqlite3, "connect", side_effect=sqlite3.OperationalError(
            "PRIVATE-TASK-CONTENT AND PRIVATE-PATH")), patch.object(diagnostics.subprocess, "Popen") as launch:
        report = diagnostics.run(root, state)
    launch.assert_not_called()
    assert report["status"] == "deferred"
    assert report.get("deferred_reason") == "project_db_unavailable"
    assert report.get("activity_error_type") == "OperationalError"
    assert "PRIVATE" not in json.dumps(report)


def test_deferred_diagnostics_identify_actual_project_activity(tmp_path):
    root, state = diagnostic_install(tmp_path)
    with closing(sqlite3.connect(state / "projects.db")) as conn, conn:
        conn.execute("CREATE TABLE projects(status TEXT)")
        conn.execute("INSERT INTO projects VALUES('working')")
    with patch.object(diagnostics.subprocess, "Popen") as launch:
        report = diagnostics.run(root, state)
    launch.assert_not_called()
    assert report["status"] == "deferred" and report.get("deferred_reason") == "active_projects"
    assert "activity_error_type" not in report


def test_scheduler_preserves_bounded_sanitized_previous_attempt_evidence(tmp_path):
    root, state = diagnostic_install(tmp_path)
    private = "PRIVATE-PATH PRIVATE-TASK-CONTENT"
    progress = "." * 30 + " [ 30%]"
    footer = "2 passed, 1 skipped in 0.10s"
    with patch.object(diagnostics, "active_projects", return_value=False), patch.object(
            diagnostics.subprocess, "Popen", return_value=SimpleNamespace(pid=123)), patch("psutil.Process") as proc:
        proc.return_value.create_time.return_value = 100
        for checked in range(1, 6):
            (state / "self-check.json").write_text(json.dumps({
                "version": diagnostics.version(root), "status": "deferred", "checked_at": checked,
                "summary": private, "deferred_reason": "project_db_unavailable",
                "activity_error_type": "OperationalError", "duration_seconds": 12.5}))
            (state / "self-check.log").write_text(private * 1000 + "\n" + progress + "\n" + footer + "\n")
            assert diagnostics.schedule(root, state, now=100 + checked)
    history = state / "self-check-history.json"
    assert history.is_file()
    raw = history.read_text()
    attempts = json.loads(raw)["attempts"]
    assert len(attempts) == 3 and [r["checked_at"] for r in attempts] == [3, 4, 5]
    assert all(r["log_tail"] == [progress, footer] for r in attempts)
    assert private not in raw and len(raw) < 8192


def test_optional_history_write_failure_does_not_prevent_launch(tmp_path):
    root, state = diagnostic_install(tmp_path)
    (state / "self-check.json").write_text(json.dumps({
        "version": diagnostics.version(root), "status": "deferred", "checked_at": 1}))
    actual_write = diagnostics._write
    def write(path, data):
        if path.name == "self-check-history.json":
            raise PermissionError("History unavailable")
        actual_write(path, data)
    with patch.object(diagnostics, "active_projects", return_value=False), patch.object(
            diagnostics, "_write", side_effect=write), patch.object(diagnostics.subprocess, "Popen",
            return_value=SimpleNamespace(pid=123)) as launch, patch("psutil.Process") as proc:
        proc.return_value.create_time.return_value = 100
        assert diagnostics.schedule(root, state, now=100)
    launch.assert_called_once()
    assert json.loads((state / "self-check.json").read_text())["status"] == "running"


def test_running_check_records_database_deferral_and_preserves_cancellation(tmp_path):
    root, state = diagnostic_install(tmp_path)
    with closing(sqlite3.connect(state / "projects.db")) as conn, conn:
        conn.execute("CREATE TABLE projects(status TEXT)")
    actual_connect = sqlite3.connect
    calls = []
    def connect(*args, **kwargs):
        calls.append(True)
        if len(calls) > 1:
            raise sqlite3.OperationalError("PRIVATE DATABASE DETAILS")
        return actual_connect(*args, **kwargs)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    with patch.object(diagnostics.sqlite3, "connect", side_effect=connect), patch.object(
            diagnostics.subprocess, "Popen", return_value=process), patch.object(
            diagnostics, "_stop_check") as stop, patch.object(diagnostics.time, "sleep") as sleep:
        report = diagnostics.run(root, state)
    stop.assert_called_once_with(process)
    sleep.assert_not_called()
    assert report["status"] == "deferred" and report["failures"] == 0
    assert report["deferred_reason"] == "project_db_unavailable"
    assert report["activity_error_type"] == "OperationalError" and "PRIVATE" not in json.dumps(report)


def test_malformed_optional_history_never_prevents_validation_launch(tmp_path):
    for index, content in enumerate((
            "[" * 5000 + "0" + "]" * 5000,
            json.dumps({"attempts": [{"status": {}, "version": "PRIVATE"}, {
                "status": "deferred", "version": "PRIVATE", "checked_at": 10 ** 1000,
                "deferred_reason": {}, "activity_error_type": {}, "log_tail": ["PRIVATE"]}]}))):
        root, state = diagnostic_install(tmp_path / str(index))
        (state / "self-check.json").write_text(json.dumps({
            "version": diagnostics.version(root), "status": "deferred", "checked_at": 1}))
        (state / "self-check-history.json").write_text(content)
        with patch.object(diagnostics, "active_projects", return_value=False), patch.object(
                diagnostics.subprocess, "Popen", return_value=SimpleNamespace(pid=123)) as launch, patch("psutil.Process") as proc:
            proc.return_value.create_time.return_value = 100
            assert diagnostics.schedule(root, state, now=100)
        launch.assert_called_once()
        assert json.loads((state / "self-check.json").read_text())["status"] == "running"
        retained = (state / "self-check-history.json").read_text()
        assert "PRIVATE" not in retained
        assert json.loads(retained)["attempts"][-1]["checked_at"] == 1


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
