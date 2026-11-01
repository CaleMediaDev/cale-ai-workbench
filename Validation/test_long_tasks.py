"""Behavior checks for the companion (no model or native board writes)."""

import importlib.util
import io
import json
import os
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
    def test_idle_launch_applies_hermes_source_fix_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "app/.git").mkdir(parents=True)
            (root / "hermes-read-consistency.patch").write_text("patch")
            (root / "hermes-host-read-consistency.patch").write_text("patch")
            replies = [subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                       subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "projects", return_value=[]),
                  patch.object(task_module.shutil, "which", return_value="git"),
                  patch.object(task_module.subprocess, "run", side_effect=replies) as run):
                task_module.sync_hermes_source_patch()
            self.assertEqual(run.call_count, 4)
            self.assertIn("--reverse", run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[-1].args[0][-1], str(root / "hermes-host-read-consistency.patch"))

    def test_busy_tasks_defer_hermes_source_fix(self):
        with (patch.object(task_module, "projects", return_value=[{"status": "working"}]),
              patch.object(task_module.subprocess, "run") as run):
            task_module.sync_hermes_source_patch()
        run.assert_not_called()

    def test_stale_gateway_restarts_without_interrupting_live_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            (root / "data/config.yaml").write_text("model: {}")
            with (patch.object(task_module, "ROOT", root),
                  patch.object(task_module, "hermes", side_effect=[
                      "Gateway running (PID: 123)", "Restarted"
                  ]) as cli,
                  patch("psutil.Process") as process):
                process.return_value.create_time.return_value = 1
                task_module.ensure_gateway_current()
            self.assertEqual(cli.call_args.args, ("gateway", "restart", "--detach"))

    def test_supervisor_can_dispatch_ready_work_when_gateway_misses_it(self):
        with (patch.object(task_module, "projects", return_value=[{"status": "working"}]),
              patch.object(task_module, "board", return_value={"spawned": []}) as board):
            task_module.dispatch_ready_fallback()
        self.assertEqual(board.call_args.args, ("dispatch", "--max", "1", "--json"))

    def test_legacy_main_alias_is_repaired_to_64k_without_repulling_weights(self):
        requests = []

        def response(request, timeout=0):
            url = request.full_url if hasattr(request, "full_url") else request
            requests.append(url)
            if url.endswith("/api/show"):
                return io.BytesIO(json.dumps({"parameters": "num_ctx 32768\n"}).encode())
            return io.BytesIO(b'{"status":"success"}')

        with patch("urllib.request.urlopen", side_effect=response):
            task_module.ensure_ollama("qwen3-coder-local")
        self.assertEqual(requests[-1], "http://127.0.0.1:11434/api/create")

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

    def test_new_project_has_recovery_notes_without_replacing_existing_notes(self):
        notes = self.base / "project" / ".local-ai"
        self.assertIn("not written milestones", (notes / "PLAN.md").read_text())
        self.assertIn("No work has been completed", (notes / "PROGRESS.md").read_text())
        existing = self.base / "other" / ".local-ai"
        existing.mkdir(parents=True)
        (existing / "PLAN.md").write_text("Keep this plan", encoding="utf-8")
        (existing / "PROGRESS.md").write_text("Keep this progress", encoding="utf-8")
        task_module.add_project("reuse", str(existing.parent), "Continue")
        self.assertEqual((existing / "PLAN.md").read_text(), "Keep this plan")
        self.assertEqual((existing / "PROGRESS.md").read_text(), "Keep this progress")

    def verify_script(self, text):
        (Path(self.item()["folder"]) / ".local-ai/verify.ps1").write_text(
            text, encoding="utf-8"
        )

    def test_persistence_and_overlap_guard(self):
        self.assertEqual(task_module.projects()[0]["id"], self.pid)
        self.assertTrue((self.base / "project/.local-ai/memory.sqlite3").is_file())
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

    def test_chat_recalls_older_user_detail_without_trusting_old_answers(self):
        task_module.save_chat("user", "My dog's name is Rufus.")
        task_module.save_chat("assistant", "Your dog's name is Wrong.")
        for number in range(12):
            task_module.save_chat("user", f"Unrelated message {number}")
            task_module.save_chat("assistant", f"Reply {number}")
        context = task_module.chat_context("What is my dog's name?", recent=4)
        self.assertEqual(len(context), 5)
        self.assertIn("Rufus", context[0]["content"])
        self.assertIn("may be outdated", context[0]["content"])
        self.assertNotIn("Wrong", str(context))
        self.assertEqual(context[-1]["content"], "Reply 11")
        import chat_assistant
        with patch("cloud_ai.post", return_value={"choices": [{"message": {"content": "Rufus"}}]}) as model:
            self.assertEqual(chat_assistant.local_answer("What is my dog's name?", context), "Rufus")
        self.assertTrue(any("Rufus" in item["content"] for item in model.call_args.args[1]["messages"]))

        # The insert trigger also indexes messages added after migration.
        task_module.save_chat("user", "My favorite color is teal.")
        for number in range(4):
            task_module.save_chat("assistant", f"Other reply {number}")
        later = task_module.chat_context("What is my favorite color?", recent=2)
        self.assertIn("teal", later[0]["content"])

    def test_task_saved_during_setup_starts_after_setup_finishes(self):
        task_module.update(self.pid, status="paused")
        progress = task_module.STATE / "install-progress.json"
        progress.write_text('{"status":"working"}', encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "second"), "Build a website", waiting_for_setup=True
        )
        with patch.object(task_module, "create_card") as create:
            task_module.tick()
            create.assert_not_called()
            self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "waiting_for_setup")
            progress.write_text('{"status":"complete"}', encoding="utf-8")
            task_module.tick()
            self.assertEqual(create.call_count, 1)
            self.assertEqual(create.call_args.args[0]["id"], pid)
            self.assertEqual(next(p for p in task_module.projects() if p["id"] == pid)["status"], "queued")

    def test_followup_to_setup_task_stays_waiting(self):
        progress = task_module.STATE / "install-progress.json"
        progress.write_text('{"status":"working"}', encoding="utf-8")
        pid = task_module.add_project(
            "", str(self.base / "second"), "Build a website", waiting_for_setup=True
        )
        task_module.request(pid, "note", "Add large buttons")
        task_module.process_requests()
        updated = next(p for p in task_module.projects() if p["id"] == pid)
        self.assertEqual(updated["status"], "waiting_for_setup")
        self.assertEqual(updated["brief"], "Build a website")
        self.assertIn("Add large buttons", (Path(updated["folder"]) / ".local-ai/USER-REQUESTS.md").read_text())

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

    def test_new_worker_receives_relevant_durable_memory(self):
        import project_memory

        project_memory.save(
            self.item()["folder"], "decision", "Fixture database choice",
            "Use SQLite for the working fixture database.",
        )
        with patch.object(task_module, "board", return_value={"id": "card-1"}) as board:
            task_module.create_card(self.item())
        args = board.call_args.args
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("Fixture database choice", body)
        self.assertIn("project_memory_search", body)

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
        task_module.update(self.pid, card="build-card")
        with (
            patch.object(task_module, "board") as board,
            patch.object(task_module, "create_card") as create,
        ):
            task_module.handle_handoff(self.item(), {"id": "test", "status": "review"})
        self.assertEqual(self.item()["phase"], "review")
        self.assertNotEqual(self.item()["status"], "ready_for_review")
        self.assertEqual(create.call_count, 1)
        self.assertEqual(board.call_args.args, ("archive", "build-card"))

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
        with (
            patch.object(task_module, "board") as board,
            patch.object(task_module, "create_card") as create,
        ):
            for _ in range(3):
                task_module.handle_handoff(
                    self.item(), {"id": "test", "status": "review"}
                )
        self.assertEqual(self.item()["failures"], 3)
        self.assertEqual(self.item()["status"], "needs_input")
        self.assertEqual(create.call_count, 2)
        board.assert_not_called()

    def test_verified_task_archives_ready_card_from_older_generation_once(self):
        folder = self.base / "project"
        task_module.update(
            self.pid, card="current-review-card", phase="review",
            status="ready_for_review", fingerprint=task_module.fingerprint(folder),
        )
        listing = [
            {"id": "ready-card", "status": "ready", "workspace_path": str(folder)},
            {"id": "current-review-card", "status": "review", "workspace_path": str(folder)},
            {"id": "other-card", "status": "ready", "workspace_path": str(self.base / "other")},
        ]
        with (
            patch.object(task_module, "REVIEW_PROJECTS_CHECKED", {}),
            patch.object(task_module, "board", side_effect=[listing, "Archived"]) as board,
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(self.item()["status"], "ready_for_review")
        self.assertEqual([call.args[0] for call in board.call_args_list], ["list", "archive"])
        self.assertEqual(board.call_args.args[1], "ready-card")

    def test_verified_task_blocks_running_worker_before_archiving(self):
        folder = self.base / "project"
        task_module.update(
            self.pid, card="running-card", phase="review",
            status="ready_for_review", fingerprint=task_module.fingerprint(folder),
        )
        states = iter(("running", "triage"))

        def board_call(command, *args, **kwargs):
            if command == "list":
                return [{"id": "running-card", "status": next(states),
                         "workspace_path": str(folder)}]
            return "ok"

        with (
            patch.object(task_module, "REVIEW_PROJECTS_CHECKED", {}),
            patch.object(task_module, "board", side_effect=board_call) as board,
        ):
            task_module.tick()
            task_module.tick()
        self.assertEqual(
            [call.args[0] for call in board.call_args_list],
            ["list", "block", "list", "archive"],
        )

    def test_new_review_generation_rechecks_old_cards(self):
        folder = self.base / "project"
        old = {
            **self.item(), "card": "review-0", "generation": 0,
        }
        newer = {**old, "card": "review-1", "generation": 1}
        listings = [
            [{"id": "review-0", "status": "review", "workspace_path": str(folder)}],
            [{"id": "review-0", "status": "ready", "workspace_path": str(folder)}],
        ]

        def board_call(command, *args, **kwargs):
            return listings.pop(0) if command == "list" else "Archived"

        with (
            patch.object(task_module, "REVIEW_PROJECTS_CHECKED", {}),
            patch.object(task_module, "board", side_effect=board_call) as board,
        ):
            task_module.retire_review_cards(old)
            task_module.retire_review_cards(old)
            task_module.retire_review_cards(newer)
        self.assertEqual(
            [call.args[0] for call in board.call_args_list],
            ["list", "list", "archive"],
        )

    def test_review_feedback_starts_a_new_saved_generation(self):
        task_module.update(self.pid, card="review-card", phase="review", status="ready_for_review")
        task_module.request(self.pid, "note", "Add a larger search box")
        with patch.object(
            task_module, "board", return_value={"task": {"status": "review"}}
        ) as board:
            task_module.process_requests()
        updated = self.item()
        self.assertEqual(updated["card"], "")
        self.assertEqual(updated["generation"], 1)
        self.assertEqual(updated["status"], "working")
        self.assertEqual(updated["brief"], "Build and test a working fixture")
        self.assertIn("Add a larger search box", (Path(updated["folder"]) / ".local-ai/USER-REQUESTS.md").read_text())
        self.assertFalse(any(call.args[0] == "reopen-review" for call in board.call_args_list))

    def test_failed_review_repair_card_carries_failure_evidence(self):
        self.verify_script('Write-Output "missing behavior"; exit 3')
        task_module.update(self.pid, card="old-review", phase="review")
        with patch.object(task_module, "board", return_value={"id": "repair-card"}) as board:
            task_module.handle_handoff(
                self.item(), {"id": "old-review", "status": "review"}
            )
        self.assertEqual(self.item()["card"], "repair-card")
        self.assertEqual(self.item()["generation"], 1)
        args = board.call_args.args
        body = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        self.assertIn("Build and test a working fixture", body)
        self.assertIn("missing behavior", body)
        self.assertIn("REVIEW PASS", body)

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
        self.assertEqual(self.item()["brief"], "Build and test a working fixture")
        self.assertIn("Use SQLite", (self.base / "project/.local-ai/USER-REQUESTS.md").read_text())
        self.assertTrue(any(c.args[0] == "unblock" for c in board.call_args_list))

    def test_many_followups_remain_durable_without_filling_worker_context(self):
        for number in range(12):
            task_module.request(self.pid, "note", f"Requirement {number}: " + "detail " * 400)
        task_module.process_requests()
        self.assertEqual(self.item()["brief"], "Build and test a working fixture")
        history = (self.base / "project/.local-ai/USER-REQUESTS.md").read_text()
        for number in range(12):
            self.assertIn(f"Requirement {number}:", history)
        with patch.object(task_module, "board", return_value={"id": "card-1"}) as board:
            task_module.create_card(self.item())
        args = board.call_args.args
        prompt = Path(args[args.index("--body-file") + 1]).read_text()
        self.assertIn("USER-REQUESTS.md", prompt)
        self.assertIn("Requirement 11:", prompt)
        self.assertNotIn("Requirement 0:", prompt)
        self.assertLess(len(prompt), 15_000)

    def test_legacy_followups_compact_only_when_queue_matches(self):
        legacy = "Build and test a working fixture\n\nUSER FOLLOW-UP:\nAdd search"
        task_module.update(self.pid, brief=legacy)
        task_module.request(self.pid, "note", "Add search")
        with task_module.database() as conn:
            conn.execute("UPDATE requests SET status='done'")
        item = self.item()
        task_module.sync_user_requests(item)
        self.assertEqual(self.item()["brief"], "Build and test a working fixture")
        self.assertIn("Add search", (self.base / "project/.local-ai/USER-REQUESTS.md").read_text())
        task_module.update(self.pid, brief=legacy + " with extra detail")
        task_module.sync_user_requests(self.item())
        self.assertEqual(self.item()["brief"], legacy + " with extra detail")

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

    def test_one_task_failure_does_not_starve_later_tasks(self):
        second = task_module.add_project(
            "second", str(self.base / "second"), "Build another fixture"
        )

        def create(project):
            if project["id"] == self.pid:
                raise RuntimeError("temporary board outage")
            task_module.update(project["id"], card="second-card", status="working")

        with patch.object(task_module, "create_card", side_effect=create) as cards:
            task_module.tick()
        self.assertEqual(cards.call_count, 2)
        self.assertIn("Supervisor will retry", self.item()["detail"])
        self.assertEqual(next(p for p in task_module.projects() if p["id"] == second)["card"], "second-card")

    def test_service_startup_failure_still_processes_pause_request(self):
        task_module.request(self.pid, "pause")

        def unavailable():
            raise RuntimeError("model service offline")

        error = task_module.supervisor_step(unavailable)
        self.assertEqual(self.item()["status"], "paused")
        self.assertIn("model service offline", error)
        self.assertIn("Model service retrying", (task_module.STATE / "service-status.txt").read_text())

    def test_accept_rejects_stale_files(self):
        task_module.update(self.pid, status="ready_for_review", fingerprint="old")
        task_module.request(self.pid, "accept")
        task_module.process_requests()
        self.assertNotEqual(self.item()["status"], "accepted")
        with task_module.database() as db:
            self.assertEqual(
                db.execute("SELECT status FROM requests").fetchone()[0], "error"
            )

    def test_same_size_edit_with_restored_timestamp_invalidates_review(self):
        source = self.base / "project/source.txt"
        source.write_text("first", encoding="utf-8")
        original = source.stat()
        checked = task_module.fingerprint(self.base / "project")
        source.write_text("other", encoding="utf-8")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertNotEqual(task_module.fingerprint(self.base / "project"), checked)
        task_module.update(self.pid, status="ready_for_review", fingerprint=checked)
        task_module.request(self.pid, "accept")
        task_module.process_requests()
        self.assertNotEqual(self.item()["status"], "accepted")

    def test_check_that_edits_project_cannot_pass_as_final(self):
        source = self.base / "project/source.txt"
        source.write_text("before", encoding="utf-8")
        self.verify_script(
            f'Set-Content -LiteralPath "{source}" -Value "after"\nexit 0\n'
        )
        passed, evidence = task_module.verify_project(self.item())
        self.assertFalse(passed)
        self.assertIn("changed during verification", evidence)

    def test_old_review_checksum_gets_fresh_check_before_migration(self):
        self.verify_script('Write-Output "real check passed"; exit 0')
        old = task_module.legacy_fingerprint(self.base / "project")
        task_module.update(self.pid, status="ready_for_review", fingerprint=old)
        with patch.object(task_module, "board") as board:
            task_module.tick()
        board.assert_not_called()
        self.assertEqual(self.item()["status"], "ready_for_review")
        self.assertTrue(self.item()["fingerprint"].startswith("content-v2:"))

    def test_resume_waits_for_active_paused_worker_to_retire(self):
        task_module.update(self.pid, card="active-card", status="working")
        running = {"task": {"status": "running"}, "runs": [
            {"status": "running", "started_at": 100, "ended_at": None},
        ]}
        with patch.object(task_module, "board", return_value=running):
            task_module.request(self.pid, "pause")
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "paused")

        blocked_but_alive = {"task": {"status": "blocked"}, "runs": [
            {"status": "running", "started_at": 100, "ended_at": None},
        ]}
        with patch.object(task_module, "board", return_value=blocked_but_alive) as board:
            task_module.request(self.pid, "resume")
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "paused")
        self.assertFalse(any(call.args[0] == "unblock" for call in board.call_args_list))
        with task_module.database() as conn:
            result = conn.execute(
                "SELECT status,result FROM requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(result["status"], "error")
        self.assertIn("still retiring", result["result"])

        retired = {"task": {"status": "blocked"}, "runs": [
            {"status": "blocked", "started_at": 100, "ended_at": 101},
        ]}
        with patch.object(task_module, "board", return_value=retired) as board:
            task_module.request(self.pid, "resume")
            task_module.process_requests()
        self.assertEqual(self.item()["status"], "working")
        self.assertTrue(any(call.args[0] == "unblock" for call in board.call_args_list))

    def test_stop_during_review_migration_is_not_overwritten(self):
        self.verify_script('Write-Output "real check passed"; exit 0')
        old = task_module.legacy_fingerprint(self.base / "project")
        task_module.update(self.pid, status="ready_for_review", fingerprint=old)

        def interrupted(_project):
            task_module.request(self.pid, "stop")
            return False, "Stopped by user"

        with patch.object(task_module, "verify_project", side_effect=interrupted):
            task_module.tick()
        self.assertEqual(self.item()["status"], "stopped")

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
