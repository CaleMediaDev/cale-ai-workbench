"""Behavior checks for the companion (no model or native board writes)."""

import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("long_tasks", ROOT / "long_tasks.py")
task_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_module)


class LongTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = patch.object(task_module, "STATE", self.base / "state")
        self.state.start()
        self.pid = task_module.add_project(
            "fixture", str(self.base / "project"), "Build and test a working fixture"
        )

    def tearDown(self):
        self.state.stop()
        self.temp.cleanup()

    def item(self):
        return task_module.projects()[0]

    def verify_script(self, text):
        (Path(self.item()["folder"]) / ".local-ai/verify.ps1").write_text(
            text, encoding="utf-8"
        )

    def test_persistence_and_overlap_guard(self):
        self.assertEqual(task_module.projects()[0]["id"], self.pid)
        with self.assertRaises(ValueError):
            task_module.add_project("bad", str(self.base / "project/child"), "overlap")
        with task_module.database() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_plain_language_task_can_name_itself(self):
        pid = task_module.add_project(
            "",
            str(self.base / "second"),
            "Make a simple recipe app\nwith large buttons",
        )
        item = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(item["title"], "Make a simple recipe app")
        self.assertTrue((Path(item["folder"]) / ".local-ai").is_dir())

    def test_new_worker_receives_saved_plan_and_latest_progress(self):
        notes = Path(self.item()["folder"]) / ".local-ai"
        (notes / "PLAN.md").write_text("Milestone: build search", encoding="utf-8")
        (notes / "PROGRESS.md").write_text(
            "x" * 6500 + "\nNext concrete step: test search", encoding="utf-8"
        )
        with patch.object(task_module, "board", return_value={"id": "card-1"}) as board:
            task_module.create_card(self.item())
        args = board.call_args.args
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("Milestone: build search", body)
        self.assertIn("Next concrete step: test search", body)
        self.assertNotIn("x" * 6500, body)

    def test_real_failing_check_cannot_pass(self):
        self.verify_script('Write-Output "actual check failed"\nexit 7\n')
        ok, text = task_module.verify_project(self.item())
        self.assertFalse(ok)
        self.assertIn("exit code: 7", text)

    def test_missing_check_cannot_pass(self):
        self.assertFalse(task_module.verify_project(self.item())[0])

    def test_success_starts_separate_review(self):
        self.verify_script(
            'if ((2 + 2) -ne 4) { exit 1 }; Write-Output "assertion passed"; exit 0'
        )
        with (
            patch.object(task_module, "board") as board,
            patch.object(task_module, "create_card") as create,
        ):
            task_module.handle_handoff(self.item(), {"id": "test", "status": "review"})
        self.assertEqual(self.item()["phase"], "review")
        self.assertNotEqual(self.item()["status"], "ready_for_review")
        self.assertEqual(create.call_count, 1)
        board.assert_not_called()

    def test_review_requires_fresh_passing_checks(self):
        self.verify_script('Write-Output "assertions passed"; exit 0')
        task_module.update(self.pid, phase="review", card="test")
        task_module.handle_handoff(self.item(), {"id": "test", "status": "review"})
        self.assertEqual(self.item()["status"], "ready_for_review")
        (self.base / "project/code.py").write_text("changed", encoding="utf-8")
        with patch.object(task_module, "board"):
            task_module.tick()
        self.assertEqual(self.item()["status"], "needs_input")

    def test_three_failures_stop_automatic_repairs(self):
        self.verify_script("exit 3")
        with patch.object(task_module, "board") as board:
            for _ in range(3):
                task_module.handle_handoff(
                    self.item(), {"id": "test", "status": "review"}
                )
        self.assertEqual(self.item()["failures"], 3)
        self.assertEqual(self.item()["status"], "needs_input")
        self.assertEqual(
            sum(c.args[0] == "reopen-review" for c in board.call_args_list), 2
        )

    def test_pause_queued_work_survives_ticks(self):
        task_module.request(self.pid, "pause")
        with patch.object(task_module, "board") as board:
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "paused")
        board.assert_not_called()

    def test_stop_keeps_files_and_prevents_restart(self):
        task_module.request(self.pid, "stop")
        with patch.object(task_module, "board") as board:
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "stopped")
        self.assertTrue((self.base / "project/AGENTS.md").exists())
        board.assert_not_called()

    def test_delete_removes_task_but_preserves_project_files(self):
        task_module.request(self.pid, "delete")
        with patch.object(task_module, "board") as board:
            task_module.tick()
            task_module.tick()
        self.assertEqual(task_module.projects(), [])
        self.assertTrue((self.base / "project/AGENTS.md").exists())
        board.assert_not_called()

    def test_progress_uses_saved_checklist_and_current_step(self):
        folder = self.base / "project/.local-ai"
        (folder / "PLAN.md").write_text(
            "- [x] Plan\n- [ ] Build\n- [ ] Test\n", encoding="utf-8"
        )
        (folder / "PROGRESS.md").write_text(
            "Next step: Build the homepage\n", encoding="utf-8"
        )
        percent, step = task_module.task_progress(self.item())
        self.assertEqual(percent, 33)
        self.assertEqual(step, "Build the homepage")
        task_module.update(self.pid, status="accepted")
        self.assertEqual(task_module.task_progress(self.item())[0], 100)

    def test_blocker_requires_explicit_resume(self):
        task_module.update(self.pid, card="test", status="working")
        with patch.object(
            task_module,
            "board",
            return_value={
                "task": {"status": "blocked"},
                "comments": [{"body": "need input"}],
            },
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "needs_input")
        task_module.request(self.pid, "resume", "Use SQLite")
        with patch.object(
            task_module, "board", return_value={"task": {"status": "blocked"}}
        ) as board:
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "working")
        self.assertIn("Use SQLite", self.item()["brief"])
        self.assertTrue(any(c.args[0] == "unblock" for c in board.call_args_list))

    def test_scheduled_card_waits_for_automatic_dispatch(self):
        task_module.update(self.pid, card="scheduled-card", status="working")
        with patch.object(
            task_module,
            "board",
            return_value={"task": {"status": "scheduled"}},
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "working")
        self.assertIn("scheduled", self.item()["detail"])

    def test_accept_rejects_stale_files(self):
        task_module.update(self.pid, status="ready_for_review", fingerprint="old")
        task_module.request(self.pid, "accept")
        task_module.process_requests()
        self.assertNotEqual(self.item()["status"], "accepted")
        with task_module.database() as db:
            self.assertEqual(
                db.execute("SELECT status FROM requests").fetchone()[0], "error"
            )

    def test_timeout_fails_instead_of_hanging(self):
        self.verify_script("Start-Sleep -Seconds 60; exit 0")
        passed, evidence = task_module.verify_project(self.item(), timeout=0.3)
        self.assertFalse(passed)
        self.assertIn("exit code: 124", evidence)

    def test_pause_cancels_verification_without_failure_retry(self):
        self.verify_script("Start-Sleep -Seconds 60; exit 0")
        task_module.update(self.pid, card="test")
        task_module.request(self.pid, "pause")
        with patch.object(
            task_module, "board", return_value={"task": {"status": "review"}}
        ):
            task_module.handle_handoff(self.item(), {"id": "test", "status": "review"})
        self.assertEqual(self.item()["status"], "paused")
        self.assertEqual(self.item()["failures"], 0)

    def test_supervisor_crash_retires_owned_check(self):
        import win32api
        import win32event

        marker = self.base / "check-pid.txt"
        self.verify_script(
            f'$PID | Set-Content -LiteralPath "{marker}"\nStart-Sleep -Seconds 60\n'
        )
        helper = self.base / "helper.py"
        helper.write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{str(ROOT)!r})\nimport long_tasks as task_module\n"
            f"task_module.STATE=Path({str(self.base / 'state')!r})\ntask_module.verify_project(task_module.projects()[0])\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(helper)],
            creationflags=task_module.HIDE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        handle = None
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "Child verification never started")
            child_pid = int(marker.read_text(encoding="ascii").strip())
            handle = win32api.OpenProcess(0x00100000, False, child_pid)  # SYNCHRONIZE
            process.terminate()
            process.wait(timeout=5)
            self.assertEqual(
                win32event.WaitForSingleObject(handle, 5000), win32event.WAIT_OBJECT_0
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            if handle:
                handle.Close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
