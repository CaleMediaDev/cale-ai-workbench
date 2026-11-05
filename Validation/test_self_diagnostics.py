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
            assert diagnostics.schedule(root, state, now=100 + checked + diagnostics.FAILURE_RETRY)
    history = state / "self-check-history.json"
    assert history.is_file()
    raw = history.read_text()
    attempts = json.loads(raw)["attempts"]
    assert len(attempts) == 3 and [r["checked_at"] for r in attempts] == [3, 4, 5]
    assert all(r["log_tail"] == [progress, footer] for r in attempts)
    assert private not in raw and len(raw) < 8192


def test_deeply_nested_report_does_not_prevent_validation_launch(tmp_path):
    root, state = diagnostic_install(tmp_path)
    report_path = state / "self-check.json"
    report_path.write_text("[" * 10000 + "0" + "]" * 10000, encoding="utf-8")
    assert diagnostics._read(report_path) == {}
    with patch.object(diagnostics, "active_projects", return_value=False), patch.object(
            diagnostics.subprocess, "Popen", return_value=SimpleNamespace(pid=123)) as launch, patch("psutil.Process") as proc:
        proc.return_value.create_time.return_value = 100
        assert diagnostics.schedule(root, state, now=100)
    launch.assert_called_once()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "running"
    assert report["version"] == diagnostics.version(root)


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
        assert diagnostics.schedule(root, state, now=100 + diagnostics.FAILURE_RETRY)
    launch.assert_called_once()
    assert json.loads((state / "self-check.json").read_text())["status"] == "running"


def test_running_check_records_database_deferral_and_preserves_cancellation(tmp_path):
    root, state = diagnostic_install(tmp_path)
    with closing(sqlite3.connect(state / "projects.db")) as conn, conn:
        conn.execute("CREATE TABLE projects(status TEXT)")
    actual_connect = sqlite3.connect
    calls = []
    def connect(*args, **kwargs):
        if "projects.db" in str(args[0]):
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
            assert diagnostics.schedule(root, state, now=100 + diagnostics.FAILURE_RETRY)
        launch.assert_called_once()
        assert json.loads((state / "self-check.json").read_text())["status"] == "running"
        retained = (state / "self-check-history.json").read_text()
        assert "PRIVATE" not in retained
        assert json.loads(retained)["attempts"][-1]["checked_at"] == 1


def test_history_growth_is_bounded_at_read_and_oversized_bytes_are_not_decoded(tmp_path):
    root, state = diagnostic_install(tmp_path)
    history = state / "self-check-history.json"
    history.write_text("{}")
    (state / "self-check.json").write_text(json.dumps({
        "version": diagnostics.version(root), "status": "deferred", "checked_at": 1}))
    marker = "OVERSIZED_HISTORY_MARKER"
    grown = (json.dumps({"unused": marker, "attempts": []}) + " " * 64000).encode()
    real_open, real_loads = Path.open, json.loads
    read_sizes, decoded_oversized = [], []

    class ObservedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size=-1):
            read_sizes.append(size)
            return self.stream.read(size)

    def growing_open(path, mode="r", *args, **kwargs):
        if path == history and mode in {"r", "rb"}:
            # Replace a small, already-inspected file immediately before read.
            with real_open(path, "wb") as stream:
                stream.write(grown)
            return ObservedReader(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    def tracked_loads(value, *args, **kwargs):
        if marker in (value.decode() if isinstance(value, bytes) else value):
            decoded_oversized.append(True)
        return real_loads(value, *args, **kwargs)

    with patch.object(Path, "open", growing_open), patch.object(diagnostics.json, "loads", tracked_loads), patch.object(
            diagnostics, "active_projects", return_value=False), patch.object(diagnostics.subprocess,
            "Popen", return_value=SimpleNamespace(pid=123)) as launch, patch("psutil.Process") as proc:
        proc.return_value.create_time.return_value = 100
        assert diagnostics.schedule(root, state, now=100 + diagnostics.FAILURE_RETRY)
    launch.assert_called_once()
    assert not decoded_oversized
    assert read_sizes == [32001]
    assert history.stat().st_size < 32000 and marker not in history.read_text()


def test_overflowing_running_metadata_does_not_prevent_replacement_launch(tmp_path):
    root, state = diagnostic_install(tmp_path)
    report_path = state / "self-check.json"
    for metadata in (
            {"pid": float("inf"), "process_started": 100},
            {"pid": 123, "process_started": 10 ** 1000}):
        report_path.write_text(json.dumps({
            "version": diagnostics.version(root), "status": "running",
            **metadata}), encoding="utf-8")
        with patch.object(diagnostics, "active_projects", return_value=False), patch.object(
                diagnostics.subprocess, "Popen", return_value=SimpleNamespace(pid=456)) as launch, patch(
                "psutil.Process") as process:
            process.return_value.create_time.return_value = 100
            assert diagnostics.schedule(root, state, now=100)
        launch.assert_called_once()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["status"] == "running"
        assert report["pid"] == 456
        assert report["process_started"] == 100


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
            return SimpleNamespace(pid=123, poll=lambda: 1)

        with patch.object(diagnostics.subprocess, "Popen", side_effect=launch) as run, patch.object(
                diagnostics, "_read_live_progress", side_effect=lambda context: {"complete": True, "finished": True,
                    "session_exitstatus": 1, "child_pid": 123, "failed": 1, "binding_unchanged": True,
                    "unconfigured": True, "owner": context["owner"]}):
            report = diagnostics.run(self.root, self.state)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"], 1)
        self.assertIn("1 failed", report["summary"])
        self.assertIn("details", (self.state / "self-check.log").read_text())
        self.assertEqual(run.call_args.args[0][1:], ["-B", "-m", "pytest", "-q", "-p", "self_diagnostics", "Validation"])
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         diagnostics.HIDE | diagnostics.LOW_PRIORITY)

    def test_monitoring_error_preserves_captured_test_output(self):
        process = Mock()
        process.poll.side_effect = OSError("monitoring failed")

        def launch(*_args, **kwargs):
            kwargs["stdout"].write(b"failure details before monitoring error\n")
            kwargs["stdout"].flush()
            return process

        with patch.object(diagnostics.subprocess, "Popen", side_effect=launch), patch.object(diagnostics, "_stop_check") as stopped:
            report = diagnostics.run(self.root, self.state)

        stopped.assert_called_once_with(process)
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["failures"], 0)
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
            return SimpleNamespace(pid=123, poll=lambda: 0)

        with patch.object(diagnostics.subprocess, "Popen", side_effect=launch), patch.object(
                diagnostics, "_read_live_progress", side_effect=lambda context: {"complete": True, "finished": True,
                    "session_exitstatus": 0, "child_pid": 123, "failed": 0, "owner": context["owner"]}):
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
            self.assertFalse(diagnostics.schedule(self.root, self.state, now=report["checked_at"] + 1))
            self.assertTrue(diagnostics.schedule(self.root, self.state, now=report["checked_at"] + diagnostics.FAILURE_RETRY + 1))

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


def test_optional_history_limits_release_string_before_regex_and_write(tmp_path):
    state=tmp_path/'LongTasks';state.mkdir()
    report={'status':'deferred','version':'Cale AI Workbench '+'1'*100000+'.2.3','checked_at':1}
    diagnostics._retain_previous_attempt(state,report)
    target=state/'self-check-history.json';raw=target.read_bytes();history=json.loads(raw)
    assert len(raw)<diagnostics.HISTORY_READ_BYTES
    assert history['attempts'][-1]['version']=='unknown'
    diagnostics._retain_previous_attempt(state,{'status':'passed','version':'Cale AI Workbench 0.5.500','checked_at':2})
    history=json.loads(target.read_bytes())
    assert history['attempts'][-1]['version']=='Cale AI Workbench 0.5.500'
    assert len(target.read_bytes())<diagnostics.HISTORY_READ_BYTES


# These real subprocess cases use ONLY owned TEMP installs and tiny CPU tests.
def live_install(tmp_path, source):
    import shutil
    root = tmp_path / "live-install"
    state = root / "LongTasks"
    state.mkdir(parents=True)
    (root / "Validation").mkdir()
    shutil.copyfile(diagnostics.__file__, root / "self_diagnostics.py")
    (root / ".cale-ai-install").write_text("Cale AI Workbench 0.5.521", encoding="utf-8")
    (root / "Validation/test_case.py").write_text(source, encoding="utf-8")
    with closing(sqlite3.connect(state / "projects.db")) as database, database:
        database.execute("CREATE TABLE projects(status TEXT)")
    return root, state


def run_live(root, state, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    with patch.object(diagnostics, "_check_python", return_value=Path(sys.executable)):
        return diagnostics.run(root, state)


_LIVE_CASE = """from pathlib import Path
import os,time,pytest
root=Path.cwd()
@pytest.fixture(scope='session')
def state():
    shared={'dirty':False}
    yield shared
    FINALIZER

def test_a(state):
    target=root/'counter.txt'
    target.write_text(str(int(target.read_text())+1) if target.exists() else '1')
    state['dirty']=True
    (root/'a-pid.txt').write_text(str(os.getpid()))
    (root/'ready.txt').write_text('ready')
    until=time.monotonic()+10
    while not (root/'locked.txt').exists():
        assert time.monotonic()<until
        time.sleep(.02)

def test_b():
    assert (root/'released.txt').exists()
    (root/'b-pid.txt').write_text(str(os.getpid()))
"""


def transient_run(tmp_path, monkeypatch, finalizer="pass", change_source=False):
    root, state = live_install(tmp_path, _LIVE_CASE.replace("FINALIZER", finalizer))
    actual = diagnostics.active_projects
    held = []
    timers = []
    observed = {"busy": 0, "at_boundary": False}
    def release(database):
        import time
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                with closing(sqlite3.connect((state / "self-check-progress.sqlite3").resolve().as_uri()+"?mode=ro", uri=True)) as progress_db:
                    row = progress_db.execute("SELECT value FROM live_session WHERE slot=1").fetchone()
                if row and json.loads(row[0]).get("waiting_at_boundary") and observed["busy"] >= 1:
                    observed["at_boundary"] = True
                    break
            except sqlite3.Error:
                pass
            time.sleep(.02)
        if change_source:
            with (root / "Validation/test_case.py").open("a", encoding="utf-8") as stream:
                stream.write("\n# Changed while the same child was paused\n")
        (root / "released.txt").write_text("released", encoding="utf-8")
        database.rollback()
        database.close()
    def activity(directory, *, details=None):
        if (root / "ready.txt").exists() and not held:
            database = sqlite3.connect(state / "projects.db", check_same_thread=False)
            database.execute("BEGIN EXCLUSIVE")
            held.append(database)
            timer = threading.Timer(.05, release, args=(database,))
            timer.start()
            timers.append(timer)
            (root / "locked.txt").write_text("locked", encoding="utf-8")
        result = actual(directory, details=details)
        if result:
            observed["busy"] += 1
        progress = state / "self-check-progress.sqlite3"
        if progress.is_file():
            try:
                with closing(sqlite3.connect(progress.resolve().as_uri()+"?mode=ro", uri=True)) as database:
                    row = database.execute("SELECT value FROM live_session WHERE slot=1").fetchone()
                observed["at_boundary"] |= bool(row and json.loads(row[0]).get("waiting_at_boundary"))
            except sqlite3.Error:
                pass
        return result
    try:
        with patch.object(diagnostics, "active_projects", side_effect=activity), patch.object(diagnostics, "_stop_check", wraps=diagnostics._stop_check) as stopped:
            report = run_live(root, state, monkeypatch)
        assert stopped.call_count == 0
    finally:
        for timer in timers:
            timer.join(8)
    return root, state, report, observed


def test_real_transient_sqlite_busy_resumes_same_child_without_replaying_a(tmp_path, monkeypatch):
    root, state, report, observed = transient_run(tmp_path, monkeypatch)
    assert observed["busy"] >= 1 and observed["at_boundary"]
    assert report["status"] == "passed" and report["progress"]["complete"] is True
    assert report["progress"]["collected"] == report["progress"]["completed"] == 2
    assert report["progress"]["boundary_waits"] >= 1
    assert (root / "counter.txt").read_text() == "1"
    assert (root / "a-pid.txt").read_text() == (root / "b-pid.txt").read_text()


import pytest

@pytest.mark.parametrize("finalizer", ["raise RuntimeError('late session fixture failure')", "if shared['dirty']: raise RuntimeError('body state retained')"])
def test_same_child_pause_preserves_late_session_finalizer_failures(tmp_path, monkeypatch, finalizer):
    root, state, report, observed = transient_run(tmp_path, monkeypatch, finalizer)
    assert observed["busy"] >= 1 and observed["at_boundary"]
    assert report["status"] == "failed" and report["progress"]["failed"] >= 1
    assert (root / "counter.txt").read_text() == "1"
    assert (root / "a-pid.txt").read_text() == (root / "b-pid.txt").read_text()


def test_source_change_during_pause_cannot_publish_old_session_pass(tmp_path, monkeypatch):
    root, state, report, observed = transient_run(tmp_path, monkeypatch, change_source=True)
    assert report["status"] == "interrupted"
    assert report["progress"]["complete"] is False
    assert (root / "counter.txt").read_text() == "1"


@pytest.mark.parametrize("body", ["__import__('os')._exit(0)", "pytest.exit('early zero', returncode=0)"])
def test_real_early_zero_exit_is_incomplete_not_pass_or_test_failure(tmp_path, monkeypatch, body):
    root, state = live_install(tmp_path, "import pytest\ndef test_a():\n    " + body + "\n\ndef test_b():\n    assert True\n")
    report = run_live(root, state, monkeypatch)
    assert report["status"] == "interrupted" and report["failures"] == 0
    assert report["progress"]["complete"] is False
    assert "no full-suite result" in report["summary"]


def test_real_runtime_environment_change_cannot_borrow_complete_pass(tmp_path, monkeypatch):
    root, state = live_install(tmp_path, "def test_a():\n    __import__('os').environ['OLLAMA_HOST']='changed-test-only'\n")
    report = run_live(root, state, monkeypatch)
    assert report["status"] == "interrupted" and report["progress"]["complete"] is False


def test_normal_pytest_plugin_flag_without_owned_context_writes_no_progress(tmp_path, monkeypatch):
    import subprocess
    root, state = live_install(tmp_path, "def test_a():\n    assert True\n")
    monkeypatch.delenv(diagnostics._LIVE_ENV, raising=False)
    environment = {**diagnostics.os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "self_diagnostics", "Validation"],
        cwd=root, env=environment, capture_output=True, timeout=20)
    assert result.returncode == 0
    assert not (state / "self-check-progress.sqlite3").exists()


def test_diagnostics_preserve_only_safe_sqlite_code_and_name(tmp_path):
    root, state = diagnostic_install(tmp_path)
    with closing(sqlite3.connect(state / "projects.db")) as database, database:
        database.execute("CREATE TABLE unrelated(value TEXT)")
    report = diagnostics.run(root, state)
    assert report["status"] == "deferred"
    assert report["sqlite_errorcode"] == sqlite3.SQLITE_ERROR
    assert report["sqlite_errorname"] == "SQLITE_ERROR"
    assert "no such table" not in json.dumps(report)
    diagnostics._retain_previous_attempt(state, report)
    history = json.loads((state / "self-check-history.json").read_text())
    assert history["attempts"][-1]["sqlite_errorcode"] == sqlite3.SQLITE_ERROR
    assert history["attempts"][-1]["sqlite_errorname"] == "SQLITE_ERROR"


def test_real_windows_reader_retry_only_repeats_metadata_write(tmp_path):
    if diagnostics.os.name != "nt":
        pytest.skip("Windows sharing semantics")
    path = tmp_path / "control.json"
    path.write_text("{}", encoding="utf-8")
    acquired = threading.Event()
    released = threading.Event()
    def hold():
        with path.open("rb"):
            acquired.set()
            released.wait(2)
    reader = threading.Thread(target=hold)
    reader.start()
    assert acquired.wait(2)
    timer = threading.Timer(.08, released.set)
    timer.start()
    try:
        diagnostics._write(path, {"phase": "paused"})
    finally:
        released.set()
        reader.join(2)
        timer.join(2)
    assert json.loads(path.read_text()) == {"phase": "paused"}
    assert not list(tmp_path.glob("*.tmp"))



def test_real_known_user_activity_stops_only_owned_pytest_child(tmp_path, monkeypatch):
    source = """from pathlib import Path
import os,time
root=Path.cwd()
def test_a():
    (root/'child-pid.txt').write_text(str(os.getpid()))
    (root/'ready.txt').write_text('ready')
    time.sleep(20)
"""
    root, state = live_install(tmp_path, source)
    actual = diagnostics.active_projects
    inserted = []
    def activity(directory, *, details=None):
        if (root / "ready.txt").exists() and not inserted:
            with closing(sqlite3.connect(state / "projects.db")) as database, database:
                database.execute("INSERT INTO projects VALUES ('working')")
            inserted.append(True)
        return actual(directory, details=details)
    with patch.object(diagnostics, "active_projects", side_effect=activity), patch.object(
            diagnostics, "_stop_check", wraps=diagnostics._stop_check) as stopped:
        report = run_live(root, state, monkeypatch)
    assert report["status"] == "deferred" and report["deferred_reason"] == "active_projects"
    assert stopped.call_count == 1
    import psutil
    assert not psutil.pid_exists(int((root / "child-pid.txt").read_text()))
    assert psutil.Process().is_running()


def test_real_persistent_sqlite_busy_exhausts_bounded_grace_and_keeps_partial_counts(tmp_path, monkeypatch):
    source = """from pathlib import Path
import time
root=Path.cwd()
def test_a():
    (root/'ready.txt').write_text('ready')
    until=time.monotonic()+10
    while not (root/'locked.txt').exists():
        assert time.monotonic()<until
        time.sleep(.02)
def test_b():
    (root/'must-not-start.txt').write_text('bad')
"""
    root, state = live_install(tmp_path, source)
    actual = diagnostics.active_projects
    held = []
    def activity(directory, *, details=None):
        if (root / "ready.txt").exists() and not held:
            database = sqlite3.connect(state / "projects.db")
            database.execute("BEGIN EXCLUSIVE")
            held.append(database)
            (root / "locked.txt").write_text("locked", encoding="utf-8")
        return actual(directory, details=details)
    try:
        with patch.object(diagnostics, "UNKNOWN_GRACE", .3), patch.object(
                diagnostics, "active_projects", side_effect=activity), patch.object(
                diagnostics, "_stop_check", wraps=diagnostics._stop_check) as stopped:
            report = run_live(root, state, monkeypatch)
    finally:
        for database in held:
            database.rollback()
            database.close()
    assert stopped.call_count == 1
    assert report["status"] == "deferred" and report["sqlite_errorcode"] & 255 == sqlite3.SQLITE_BUSY
    assert report["progress"]["completed"] == 1 and report["progress"]["complete"] is False
    assert not (root / "must-not-start.txt").exists()


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_ERROR, sqlite3.SQLITE_CANTOPEN])
def test_untyped_or_nontransient_activity_errors_keep_immediate_defer(tmp_path, code):
    root, state = diagnostic_install(tmp_path)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    calls = []
    def activity(directory, *, details=None):
        calls.append(True)
        if len(calls) == 1:
            return False
        details.update(reason="project_db_unavailable", error_type="OperationalError")
        if code is not None:
            details["sqlite_errorcode"] = code
        return True
    with patch.object(diagnostics, "active_projects", side_effect=activity), patch.object(
            diagnostics.subprocess, "Popen", return_value=process), patch.object(
            diagnostics, "_stop_check") as stopped, patch.object(diagnostics.time, "sleep") as sleep:
        report = diagnostics.run(root, state)
    stopped.assert_called_once_with(process)
    sleep.assert_not_called()
    assert report["status"] == "deferred"


def test_separate_new_process_never_reuses_passes_or_skips(tmp_path, monkeypatch):
    source = """from pathlib import Path
import pytest
root=Path.cwd()
def test_a():
    path=root/'counter.txt'
    path.write_text(str(int(path.read_text())+1) if path.exists() else '1')
def test_b():
    pytest.skip('fixture skip')
"""
    root, state = live_install(tmp_path, source)
    first = run_live(root, state, monkeypatch)
    second = run_live(root, state, monkeypatch)
    assert first["status"] == second["status"] == "passed"
    assert first["progress"]["nonce"] != second["progress"]["nonce"]
    assert first["progress"]["skipped"] == second["progress"]["skipped"] == 1
    assert (root / "counter.txt").read_text() == "2"


def test_safe_progress_history_keeps_observed_failures_without_claiming_complete(tmp_path):
    report = {"version": "Cale AI Workbench 0.5.521", "status": "deferred", "checked_at": 1,
        "progress": {"completed": 4, "failed": 1, "passed": 3, "skipped": 0,
                     "complete": True, "nodeids": ["PRIVATE-NODE"]}}
    diagnostics._retain_previous_attempt(tmp_path, report)
    raw = (tmp_path / "self-check-history.json").read_text()
    item = json.loads(raw)["attempts"][-1]
    assert item["progress"]["failed"] == 1
    assert "complete" not in item["progress"] and "PRIVATE" not in raw


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY | (2 << 8), sqlite3.SQLITE_LOCKED | (1 << 8)])
def test_only_typed_busy_and_locked_base_codes_qualify_for_grace(code):
    assert diagnostics._transient_database_busy({"reason": "project_db_unavailable", "sqlite_errorcode": code})
    for bad in (True, "5", None, -1, 2**100, sqlite3.SQLITE_CANTOPEN):
        assert not diagnostics._transient_database_busy({"reason": "project_db_unavailable", "sqlite_errorcode": bad})


@pytest.mark.parametrize("invalid", ["[]", "{", '{"complete":true}' + " "*100000])
def test_corrupt_or_oversized_progress_is_not_pass_evidence(tmp_path, invalid):
    state = tmp_path / "LongTasks"
    state.mkdir()
    context = {"state": str(state), "nonce": "owned", "basis": "unchanged"}
    with diagnostics._live_progress_db(state) as database:
        database.execute("INSERT INTO live_session VALUES (1,?)", (invalid,))
    assert diagnostics._read_live_progress(context) == {"complete": False}


@pytest.mark.parametrize("fault", ["missing-node", "replaced-node", "unconfigure-missing"])
def test_partial_or_substituted_completion_receipt_never_becomes_green(tmp_path, fault):
    import hashlib
    state = tmp_path / "LongTasks"
    state.mkdir()
    context = {"state": str(state), "nonce": "owned", "basis": "source-runtime"}
    node = "Validation/test_case.py::test_a"
    data = {"nonce": "owned", "basis": "source-runtime", "collected": 1, "completed": 1,
        "passed": 1, "failed": 0, "skipped": 0, "complete": True, "finished": True,
        "all_nodes_finalized": True, "binding_unchanged": True, "unconfigured": True,
        "node_set_sha256": hashlib.sha256(json.dumps([node]).encode()).hexdigest()}
    with diagnostics._live_progress_db(state) as database:
        if fault == "unconfigure-missing":
            data.pop("unconfigured")
        database.execute("INSERT INTO live_session VALUES (1,?)", (json.dumps(data),))
        if fault != "missing-node":
            database.execute("INSERT INTO live_results VALUES (?,?)", ("different-node" if fault == "replaced-node" else node, "passed"))
    assert diagnostics._read_live_progress(context)["complete"] is False



def test_control_metadata_failure_stops_owned_check_once_and_keeps_incomplete_status(tmp_path):
    root, state = diagnostic_install(tmp_path)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    writes = []
    def control(*args, **kwargs):
        writes.append(True)
        if len(writes) > 1:
            raise PermissionError("Control publication failed")
    with patch.object(diagnostics, "active_projects", return_value=False), patch.object(
            diagnostics, "_write_control", side_effect=control), patch.object(
            diagnostics.subprocess, "Popen", return_value=process), patch.object(
            diagnostics, "_stop_check") as stopped:
        report = diagnostics.run(root, state)
    stopped.assert_called_once_with(process)
    assert report["status"] == "interrupted" and report["failures"] == 0


@pytest.mark.parametrize("source", ["def invalid syntax(\n", "import definitely_missing_selfcheck_fixture_module\n"])
def test_real_collection_failure_is_failed_and_not_silent_interruption(tmp_path, monkeypatch, source):
    root, state = live_install(tmp_path, source)
    report = run_live(root, state, monkeypatch)
    assert report["status"] == "failed" and report["failures"] == 1
    assert report["progress"]["collection_failed"] >= 1
    assert report["progress"]["complete"] is False
    assert report["progress"]["unconfigured"] is True


def test_declared_nested_runtime_code_change_invalidates_live_result(tmp_path, monkeypatch):
    root, state = live_install(tmp_path, "from pathlib import Path\ndef test_a():\n    Path('nested/runtime.py').write_text('changed')\n")
    nested = root / "nested/runtime.py"
    nested.parent.mkdir()
    nested.write_text("original", encoding="utf-8")
    (root / "install-files.json").write_text(json.dumps(["self_diagnostics.py", "Validation/test_case.py", "nested/runtime.py"]), encoding="utf-8")
    report = run_live(root, state, monkeypatch)
    assert report["status"] == "interrupted" and report["progress"]["complete"] is False
    assert report["progress"]["binding_unchanged"] is False


def test_missing_declared_runtime_file_cannot_launch_or_borrow_pass(tmp_path, monkeypatch):
    root, state = live_install(tmp_path, "def test_a():\n    assert True\n")
    (root / "install-files.json").write_text(json.dumps(["missing/nested.py"]), encoding="utf-8")
    with patch.object(diagnostics.subprocess, "Popen") as launch:
        report = run_live(root, state, monkeypatch)
    launch.assert_not_called()
    assert report["status"] == "interrupted"


def test_progress_read_has_one_snapshot_across_interleaved_writer_commit(tmp_path, monkeypatch):
    state = tmp_path / "LongTasks"
    state.mkdir()
    context = {"state": str(state), "nonce": "owned", "basis": "source"}
    initial = {"nonce": "owned", "basis": "source", "collected": 1, "completed": 0,
               "passed": 0, "failed": 0, "skipped": 0, "complete": False, "plugin_active": True}
    diagnostics._save_live_progress(context, initial, initialize=True)
    actual_connect = sqlite3.connect
    raced = []
    class HeaderCursor:
        def __init__(self, cursor): self.cursor = cursor
        def fetchone(self):
            row = self.cursor.fetchone()
            if not raced:
                raced.append(True)
                updated = {**initial, "completed": 1, "passed": 1}
                with closing(actual_connect(state / "self-check-progress.sqlite3")) as writer, writer:
                    writer.execute("UPDATE live_session SET value=? WHERE slot=1", (json.dumps(updated),))
                    writer.execute("INSERT INTO live_results VALUES ('test_a','passed')")
            return row
    class Reader:
        def __init__(self, connection): self.connection = connection
        def execute(self, sql, *args):
            cursor = self.connection.execute(sql, *args)
            return HeaderCursor(cursor) if sql.startswith("SELECT substr(value") else cursor
        def close(self): self.connection.close()
    def connect(*args, **kwargs):
        return Reader(actual_connect(*args, **kwargs))
    with monkeypatch.context() as patched:
        patched.setattr(diagnostics.sqlite3, "connect", connect)
        snapshot = diagnostics._read_live_progress(context)
    assert raced and snapshot["plugin_active"] is True and snapshot["completed"] == 0
    assert diagnostics._read_live_progress(context)["completed"] == 1


def test_parent_source_replacement_after_child_failure_is_incomplete_not_current_failure(tmp_path, monkeypatch):
    root, state = live_install(tmp_path, "def test_a():\n    assert False, 'real observed failure'\n")
    runtime = root / "runtime.py"
    runtime.write_text("original = True\n", encoding="utf-8")
    original_basis = diagnostics._live_basis
    observations = []
    def current_basis(folder, python):
        observations.append(True)
        if len(observations) == 2:
            # The real child has exited and sealed its old-source failure. The
            # parent must not attribute that failure to this replacement source.
            runtime.write_text("replacement = True\n", encoding="utf-8")
        return original_basis(folder, python)
    monkeypatch.setattr(diagnostics, "_live_basis", current_basis)
    report = run_live(root, state, monkeypatch)
    assert len(observations) == 2
    assert report["status"] == "interrupted" and report["failures"] == 0
    assert report["progress"]["failed"] == 1
    assert report["progress"]["binding_unchanged"] is False
    assert report["progress"]["invalidated"] == "source_runtime_changed"


def test_real_wall_timeout_stops_owned_child_once_without_accepting_partial_failures(tmp_path, monkeypatch):
    source = """from pathlib import Path
import os,time
root=Path.cwd()

def test_a():
    assert False, 'observed failure before timeout'

def test_b():
    (root/'child-pid.txt').write_text(str(os.getpid()))
    time.sleep(20)

def test_c():
    (root/'must-not-start.txt').write_text('bad')
"""
    root, state = live_install(tmp_path, source)
    (state / "self-check.json").write_text(json.dumps({
        "version": diagnostics.version(root), "status": "failed", "failures": 2}), encoding="utf-8")
    with patch.object(diagnostics, "MAX_SECONDS", 1), patch.object(
            diagnostics, "_stop_check", wraps=diagnostics._stop_check) as stopped:
        report = run_live(root, state, monkeypatch)
    stopped.assert_called_once()
    assert report["status"] == "interrupted" and report["failures"] == 2
    assert report["progress"]["completed"] == 1 and report["progress"]["failed"] == 1
    assert report["progress"]["complete"] is False
    assert "exceeded its time limit" in (state / "self-check.log").read_text(encoding="utf-8")
    assert not (root / "must-not-start.txt").exists()
    import psutil
    assert not psutil.pid_exists(int((root / "child-pid.txt").read_text()))
    assert psutil.Process().is_running()
