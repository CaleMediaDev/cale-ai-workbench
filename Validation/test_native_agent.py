"""Behavior checks for Cale's native, restartable project worker."""

import copy
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import long_tasks
import native_agent
import cloud_ai
import task_context


def project(folder):
    folder.mkdir()
    (folder / ".local-ai").mkdir()
    return {"id": "sample", "folder": str(folder), "brief": "Make a working page"}


def test_native_worker_writes_files_and_checkpoints_tool_results():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        (Path(item["folder"]) / ".local-ai/verify.ps1").write_text("exit 0")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "write-1", "type": "function", "function": {
                    "name": "write_file", "arguments": '{"path":"index.html","content":"<h1>Ready</h1>"}'
                }}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Page is ready."}}]},
        ])
        state = native_agent.run_slice(item, complete=lambda _: next(responses))
        assert state["status"] == "done"
        assert (Path(item["folder"]) / "index.html").read_text() == "<h1>Ready</h1>"
        assert any(message.get("tool_call_id") == "write-1" for message in state["messages"])
        assert native_agent.read_state(item)["status"] == "done"


def test_read_file_streams_large_files_and_bounds_one_extremely_long_line():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        source = folder / "large.txt"
        with source.open("w", encoding="utf-8") as stream:
            stream.write("ordinary line\n" * 20_000)
            stream.write("x" * 250_000 + "\n")
            stream.write("last line\n")
        with patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
            result = native_agent.execute_tool(folder, "read_file", {
                "path": "large.txt", "start_line": 20_001, "max_lines": 2})
            last = native_agent.execute_tool(folder, "read_file", {
                "path": "large.txt", "start_line": 20_002, "max_lines": 1})
        assert result.startswith("20001: " + "x" * 100)
        assert "[line truncated]" in result
        assert "20002: last line" in result
        assert len(result) < 5000
        assert last == "20002: last line"


def test_native_worker_reuses_router_across_model_turns():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        (Path(item["folder"]) / ".local-ai/verify.ps1").write_text("exit 0")
        created = []

        class Router:
            def __init__(self):
                created.append(self)
                self.calls = 0

            def complete(self, _payload, _task_id):
                self.calls += 1
                if self.calls == 1:
                    return {"choices": [{"message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "read-1", "type": "function", "function": {
                            "name": "list_files", "arguments": "{}"}}]}}]}
                return {"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}

        with patch.object(cloud_ai, "Router", Router):
            state = native_agent.run_slice(item)
        assert state["status"] == "done"
        assert len(created) == 1
        assert created[0].calls == 2


def test_worker_start_does_not_overwrite_a_child_checkpoint():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        saved_messages = [{"role": "assistant", "content": "First action saved"}]

        def child_starts(*_args, **_kwargs):
            # Reproduce the child saving progress before Popen returns to the
            # supervisor. The parent must not write its older state over it.
            native_agent.save_state(item, {"status": "running", "turns": 1,
                                           "messages": saved_messages})
            return SimpleNamespace(pid=43210)

        with (patch.object(long_tasks, "environment", return_value={}),
              patch.object(native_agent.subprocess, "Popen", side_effect=child_starts),
              patch("psutil.Process", return_value=SimpleNamespace(create_time=lambda: 123.0))):
            state = native_agent.start_worker(item)
        assert state["messages"] == saved_messages
        assert state["turns"] == 1
        assert native_agent.read_state(item)["pid"] == 43210
        assert json.loads(native_agent.state_file(item).read_text())["messages"] == saved_messages
        assert json.loads(native_agent.process_file(item).read_text())["pid"] == 43210
        assert "native-worker-process.json" not in native_agent.execute_tool(
            Path(item["folder"]), "list_files", {"path": ".local-ai"})
        try:
            native_agent.project_path(Path(item["folder"]), ".local-ai/native-worker-process.json")
        except ValueError:
            pass
        else:
            raise AssertionError("Worker process metadata must not be editable by file tools")


def test_native_worker_continues_when_model_claims_done_without_verifier():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "content": "Finished."}}]},
            {"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "verify-1", "type": "function", "function": {
                    "name": "write_file", "arguments":
                    '{"path":".local-ai/verify.ps1","content":"if (-not (Test-Path index.html)) { exit 1 }"}'
                }}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Finished."}}]},
        ])
        seen = []
        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return next(responses)
        state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "done"
        assert len(seen) == 3
        assert "not ready for handoff" in seen[1]["messages"][-1]["content"]
        assert (Path(item["folder"]) / ".local-ai/verify.ps1").is_file()


def test_completion_gate_catches_requested_file_in_duplicate_project_folder():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "project")
        item["brief"] = "Create hello.txt containing exactly Hello from Cale."
        folder = Path(item["folder"])
        (folder / "project").mkdir()
        (folder / "project/hello.txt").write_text("Hello from Cale")
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        issue = native_agent.completion_issue(folder, item)
        assert "hello.txt" in issue and "project root" in issue
        (folder / "hello.txt").write_text("Hello from Cale")
        assert native_agent.completion_issue(folder, item) == ""
        assert native_agent.requested_root_files("Create index.html in src/ folder") == []


def test_completion_gate_requires_checked_plan_milestones():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        plan = folder / ".local-ai/PLAN.md"
        plan.write_text("- [ ] Check the page")
        assert "unchecked milestones" in native_agent.completion_issue(folder, item)
        plan.write_text("- [x] Check the page")
        assert native_agent.completion_issue(folder, item) == ""


def test_empty_final_reply_gets_one_summary_retry():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        (Path(item["folder"]) / ".local-ai/verify.ps1").write_text("exit 0")
        seen = []
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "content": ""}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Page created and checked."}}]},
        ])

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return next(responses)

        state = native_agent.run_slice(item, complete=complete)
        assert state["answer"] == "Page created and checked."
        assert len(seen) == 2
        assert "final reply was empty" in str(seen[1]["messages"])


def test_passing_verifier_and_complete_plan_hands_off_without_extra_model_calls():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        (notes / "PLAN.md").write_text("- [x] Create and test the page\n")
        (notes / "PROGRESS.md").write_text("Page created; the saved verifier passed its checks.\n")
        calls = []

        def complete(_payload):
            calls.append(1)
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "verify-1", "function": {"name": "run_command",
                    "arguments": '{"command":"powershell -File .local-ai/verify.ps1"}'}}]}}]}

        with patch.object(native_agent, "execute_tool", return_value="Exit code: 0\nPASS"):
            state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "done"
        assert state["answer"]
        assert len(calls) == 1


def test_passing_verifier_prompts_plan_closeout_and_then_hands_off():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        (notes / "PLAN.md").write_text("- [ ] Create and test the page\n")
        (notes / "PROGRESS.md").write_text("No work has been completed yet.\n")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "verify-1", "function": {"name": "run_command",
                    "arguments": '{"command":"powershell -File .local-ai/verify.ps1"}'}}]}}]},
            {"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "plan-1", "function": {"name": "write_file", "arguments":
                    '{"path":".local-ai/PLAN.md","content":"- [x] Create and test the page"}'}},
                {"id": "progress-1", "function": {"name": "write_file", "arguments":
                    '{"path":".local-ai/PROGRESS.md","content":"Page created and saved verifier passed its checks."}'}}
            ]}}]},
        ])
        seen = []
        original = native_agent.execute_tool

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return next(responses)

        def tool(root, name, args):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args)

        with patch.object(native_agent, "execute_tool", side_effect=tool):
            state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "done"
        assert len(seen) == 2
        assert "rerun unchanged checks" in str(seen[1]["messages"])


def test_source_write_after_verification_invalidates_automatic_handoff():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        (notes / "PLAN.md").write_text("- [ ] Create and test the page\n")
        (notes / "PROGRESS.md").write_text("Working on the page and its saved checks.\n")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "verify-1", "function": {"name": "run_command",
                    "arguments": '{"command":"powershell -File .local-ai/verify.ps1"}'}}]}}]},
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "write-1", "function": {"name": "write_file",
                    "arguments": '{"path":"index.html","content":"new work"}'}}]}}]},
        ])
        original = native_agent.execute_tool

        def tool(root, name, args):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args)

        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), \
                patch.object(native_agent, "execute_tool", side_effect=tool):
            state = native_agent.run_slice(item, complete=lambda _: next(responses))
        assert state["status"] == "continue"
        assert state["verify_passed"] is False


def test_valid_model_turn_resets_consecutive_worker_failures():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        (Path(item["folder"]) / ".local-ai/verify.ps1").write_text("exit 0")
        native_agent.save_state(item, {"status": "continue", "messages": [], "failures": 2})
        state = native_agent.run_slice(item, complete=lambda _: {
            "choices": [{"message": {"role": "assistant", "content": "Finished."}}],
        })
        assert state["status"] == "done"
        assert state["failures"] == 0


def test_repeated_premature_finish_does_not_burn_an_entire_slice():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        calls = []
        def complete(_payload):
            calls.append(1)
            return {"choices": [{"message": {"role": "assistant", "content": "Finished."}}]}
        state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "continue"
        assert len(calls) == 2
        assert state["premature_finishes"] == 2


def test_native_file_tools_cannot_escape_project_folder():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        result = native_agent.execute_tool(folder, "write_file", {
            "path": "../outside.txt", "content": "secret",
        })
        assert result.startswith("Tool error:")
        assert not (Path(temporary) / "outside.txt").exists()


def test_powershell_file_tool_uses_utf8_bom_and_reads_without_marker():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        script = "Write-Output '✓ check passed'\n"
        result = native_agent.execute_tool(folder, "write_file", {
            "path": ".local-ai/verify.ps1", "content": script})
        assert result.startswith("Saved ")
        assert (folder / ".local-ai/verify.ps1").read_bytes().startswith(b"\xef\xbb\xbf")
        read = native_agent.execute_tool(folder, "read_file", {"path": ".local-ai/verify.ps1"})
        assert read.startswith("1: Write-Output")
        assert "✓ check passed" in read


def test_native_worker_indexes_existing_code_and_recalls_typed_memory():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / "component.py").write_text("def turquoise_widget(): return 42")
        (folder / ".local-ai/verify.ps1").write_text("if (-not (Test-Path component.py)) { exit 1 }")
        native_agent.run_slice(item, complete=lambda _: {
            "choices": [{"message": {"role": "assistant", "content": "Finished."}}],
        })
        code = json.loads(native_agent.execute_tool(folder, "search_project_memory",
                                                  {"query": "turquoise_widget"}))
        assert code[0]["source_path"] == "component.py"
        saved = json.loads(native_agent.execute_tool(folder, "save_project_memory", {
            "kind": "decision", "title": "Widget color", "body": "Use turquoise for the main widget.",
        }))
        found = json.loads(native_agent.execute_tool(folder, "search_project_memory",
                                                   {"query": "turquoise", "kind": "decision"}))
        assert found[0]["id"] == saved["id"]
        full = json.loads(native_agent.execute_tool(folder, "read_project_memory",
                                                  {"id": saved["id"]}))
        assert full["body"] == "Use turquoise for the main widget."
        native_agent.execute_tool(folder, "write_file", {
            "path": "new.py", "content": "def lavender_widget(): return 1",
        })
        new = json.loads(native_agent.execute_tool(folder, "search_project_memory",
                                                 {"query": "lavender_widget", "kind": "file"}))
        assert new[0]["source_path"] == "new.py"


def test_native_context_rebuilds_from_durable_plan():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        notes = Path(item["folder"]) / ".local-ai"
        (notes / "PLAN.md").write_text("- [x] Build page\n- [ ] Check page")
        messages = [{"role": "system", "content": "Original request"}]
        messages.extend({"role": "user", "content": str(number) + " note " * 100}
                        for number in range(40))
        compacted = native_agent.compact_messages(messages, Path(item["folder"]), context_limit=6000)
        assert len(compacted) < len(messages)
        assert "Check page" in compacted[1]["content"]
        assert compacted[-1]["content"].startswith("39")


def test_fresh_context_uses_database_checkpoint_and_keeps_latest_tool_batch():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "PLAN.md").write_text("- [x] Draft\n- [ ] Render and verify")
        (notes / "PROGRESS.md").write_text("Draft saved. Next: run browser check.")
        messages = [{"role": "system", "content": "Original request"}]
        messages.extend({"role": "assistant", "content": f"old step {number} " * 1000} for number in range(30))
        messages.extend([
            {"role": "user", "content": "Also make the title blue."},
            {"role": "assistant", "tool_calls": [{"id": "read-1", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "read-1", "content": "Latest file result"},
        ])
        task_context.record_action(folder, "read_file", "private contents must not be stored")
        fresh = native_agent.fresh_messages(messages, folder)
        assert len(fresh) <= 7
        assert "Render and verify" in fresh[1]["content"]
        assert "read_file: Completed" in fresh[1]["content"]
        assert "private contents" not in fresh[1]["content"]
        assert [entry["role"] for entry in fresh[-3:]] == ["user", "assistant", "tool"]
        assert fresh[-1]["content"] == "Latest file result"
        assert "old step 0" not in str(fresh)


def test_task_checkpoint_refreshes_after_restart_and_does_not_lose_new_user_message():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        plan = folder / ".local-ai/PLAN.md"
        plan.write_text("- [ ] First milestone")
        assert "First milestone" in task_context.snapshot(folder)
        plan.write_text("- [x] First milestone\n- [ ] Second milestone")
        messages = [{"role": "system", "content": "Original request"},
                    {"role": "user", "content": "Use a larger font"}]
        messages.extend({"role": "assistant", "content": f"step {number}"} for number in range(12))
        fresh = native_agent.fresh_messages(messages, folder)
        assert "Second milestone" in fresh[1]["content"]
        assert any(entry.get("content") == "Use a larger font" for entry in fresh)


def test_unchanged_task_checkpoint_reads_cached_sqlite_document():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        plan = folder / ".local-ai/PLAN.md"
        plan.write_text("- [ ] Build the page")
        assert "Build the page" in task_context.snapshot(folder)
        with patch.object(Path, "read_text", side_effect=AssertionError("file reread")):
            assert "Build the page" in task_context.snapshot(folder)


def test_next_step_survives_when_large_plan_excerpt_omits_it():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        plan = folder / ".local-ai/PLAN.md"
        plan.write_text("- [x] Past step\n" * 300 + "- [ ] Important middle step\n" +
                        "- [x] Later step\n" * 300)
        summary = task_context.snapshot(folder)
        assert "Next step: Important middle step" in summary
        assert "Important middle step" not in summary.split("PLAN.md:\n", 1)[1]
        assert "Next step: Important middle step" in task_context.snapshot(folder)


def test_checkpoint_uses_latest_progress_action_until_plan_is_updated():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        plan = folder / ".local-ai/PLAN.md"
        progress = folder / ".local-ai/PROGRESS.md"
        plan.write_text("- [ ] Old plan action", encoding="utf-8")
        progress.write_text("Next: Check the first draft.\n"
                            "Draft checked. Next step: Fix the navigation.\n",
                            encoding="utf-8")
        assert "Next step: Fix the navigation." in task_context.snapshot(folder)
        progress.write_text(progress.read_text(encoding="utf-8") +
                            "Navigation fixed. Up next: Run the browser verifier.\n",
                            encoding="utf-8")
        assert "Next step: Run the browser verifier." in task_context.snapshot(folder)
        plan.write_text("- [x] Old plan action\n- [ ] Publish the checked page",
                        encoding="utf-8")
        assert "Next step: Publish the checked page" in task_context.snapshot(folder)


def test_task_checkpoint_periodically_detects_same_stamp_edit():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        plan = folder / ".local-ai/PLAN.md"
        plan.write_text("- [ ] Step one")
        assert "Next step: Step one" in task_context.snapshot(folder)
        before = plan.stat()
        plan.write_text("- [ ] Step two")
        os.utime(plan, ns=(before.st_atime_ns, before.st_mtime_ns))
        import project_memory

        with project_memory.connection(folder) as conn:
            conn.execute("UPDATE task_documents SET updated=0 WHERE name='PLAN.md'")
        assert "Next step: Step two" in task_context.snapshot(folder)


def test_inflight_user_instruction_prevents_early_completion():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        notes = Path(item["folder"]) / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        seen = []

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            if len(seen) == 1:
                (notes / "USER-REQUESTS.md").write_text("Make the title blue")
                return {"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}
            assert "Make the title blue" in str(payload["messages"])
            return {"choices": [{"message": {"role": "assistant", "content": "Blue title finished"}}]}

        state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "done"
        assert state["answer"] == "Blue title finished"
        assert len(seen) == 2


def test_task_action_ledger_names_files_without_storing_content():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        task_context.record_action(folder, "read_file", "secret document text",
                                   {"path": "src/page.py"})
        summary = task_context.snapshot(folder)
        assert "read_file src/page.py: Completed" in summary
        assert "secret document text" not in summary


def test_task_action_ledger_marks_structured_failures_without_error_details():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        task_context.record_action(folder, "search_web",
                                   '{"results":[],"error":"private query failed"}')
        task_context.record_action(folder, "call_local_tool",
                                   '{"isError":true,"content":[{"text":"private backend detail"}]}')
        task_context.record_action(folder, "run_command", "Exit code: 7\nprivate command output")
        summary = task_context.snapshot(folder)
        assert "search_web: Failed" in summary
        assert "call_local_tool: Failed" in summary
        assert "run_command: Failed (exit 7)" in summary
        assert "private" not in summary
        assert task_context.action_outcome("call_local_tool", '{"status":[]}') == "Completed"


def test_repeated_identical_tool_failure_prompts_a_different_approach():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": call_id, "function": {"name": "read_file",
                    "arguments": '{"path":"missing.txt"}'}}]}}]}
            for call_id in ("read-1", "read-2")
        ] + [{"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}])
        seen = []

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return next(responses)

        state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "done"
        assert len(seen) == 3
        assert "Do not retry it unchanged" in str(seen[2]["messages"])
        assert "read_file missing.txt: Failed" in task_context.snapshot(folder)


def test_instruction_during_tool_call_follows_complete_tool_batch():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "list-1", "function": {"name": "list_files", "arguments": "{}"}}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Finished"}}]},
        ])
        seen = []
        original = native_agent.execute_tool

        def tool_with_note(root, name, arguments):
            (notes / "USER-REQUESTS.md").write_text("Use blue")
            return original(root, name, arguments)

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return next(responses)

        with patch.object(native_agent, "execute_tool", side_effect=tool_with_note):
            assert native_agent.run_slice(item, complete=complete)["status"] == "done"
        assert [entry["role"] for entry in seen[1]["messages"][-3:]] == ["assistant", "tool", "user"]
        assert "Use blue" in str(seen[1]["messages"])


def test_existing_task_database_migrates_action_subject_column():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        import project_memory

        project_memory.initialize(folder)
        conn = sqlite3.connect(project_memory.database_path(folder))
        try:
            conn.execute("""CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL,
                outcome TEXT NOT NULL, created REAL NOT NULL)""")
            conn.commit()
        finally:
            conn.close()
        task_context.record_action(folder, "write_file", "Saved src/app.py", {"path": "src/app.py"})
        assert "write_file src/app.py: Completed" in task_context.snapshot(folder)


def test_existing_task_database_migrates_cached_document_columns():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        import project_memory

        project_memory.initialize(folder)
        conn = sqlite3.connect(project_memory.database_path(folder))
        try:
            conn.execute("""CREATE TABLE task_documents (
                name TEXT PRIMARY KEY, digest TEXT NOT NULL,
                body TEXT NOT NULL, updated REAL NOT NULL)""")
            conn.execute("INSERT INTO task_documents VALUES('PLAN.md','old','stale plan',0)")
            conn.commit()
        finally:
            conn.close()
        (folder / ".local-ai/PLAN.md").write_text("- [ ] Fresh plan")
        assert "Next step: Fresh plan" in task_context.snapshot(folder)
        assert "stale plan" not in task_context.snapshot(folder)


def test_context_compaction_uses_configured_window_and_keeps_tool_batches():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / "data/config.yaml"
        config.parent.mkdir()
        config.write_text("model:\n  context_length: 262144\nfallback_providers: []\n")
        assert native_agent.configured_context(root) == 262144
        folder = root / "task"
        folder.mkdir()
        messages = [{"role": "system", "content": "Original task"}]
        messages.extend({"role": "user", "content": str(i)} for i in range(40))
        assert native_agent.compact_messages(messages, folder, 262144) is messages
        messages.insert(1, {"role": "user", "content": "old context " * 5000})
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {
                "name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "recent result"},
            {"role": "user", "content": "Use the recent result."},
        ])
        compacted = native_agent.compact_messages(messages, folder, 6000)
        assert all("old context" not in str(item.get("content", "")) for item in compacted)
        assert [item["role"] for item in compacted[-3:]] == ["assistant", "tool", "user"]


def test_oversized_latest_tool_exchange_is_trimmed_not_dropped():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        messages = [
            {"role": "system", "content": "Original request"},
            {"role": "system", "content": "Current task checkpoint: next step is to verify." + " note" * 2000},
            {"role": "assistant", "tool_calls": [{"id": "write-1", "function": {
                "name": "write_file", "arguments": json.dumps({
                    "path": "src/page.html", "content": "content" * 8000})}}]},
            {"role": "tool", "tool_call_id": "write-1", "content": "long result " * 4000 + "END-RESULT"},
            {"role": "user", "content": "Keep the blue title."},
        ]
        compacted = native_agent.compact_messages(messages, folder, 6000)
        assert [entry["role"] for entry in compacted] == ["system", "system", "assistant", "tool", "user"]
        assert compacted[2]["tool_calls"][0]["id"] == "write-1"
        assert "src/page.html" in compacted[2]["tool_calls"][0]["function"]["arguments"]
        assert "END-RESULT" in compacted[3]["content"]
        assert "Keep the blue title" in compacted[4]["content"]
        assert cloud_ai.estimated_input_tokens({"messages": compacted, "tools": native_agent.TOOLS}) <= 4500


def test_native_prompt_keeps_large_original_request_in_file():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        item["brief"] = "Start: " + "many requirements " * 7000 + "Finish: verify everything."
        text = native_agent.prompt(item)
        assert len(text) < 10000
        assert "Start:" in text and "Finish: verify everything." in text
        assert ".local-ai/ORIGINAL-REQUEST-sample.md" in text


def test_interrupted_tool_reply_precedes_later_user_steering():
    messages = [
        {"role": "system", "content": "Work on the task."},
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "write_file"}},
            {"id": "b", "function": {"name": "run_command"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "Saved file"},
        {"role": "user", "content": "Also make it blue."},
    ]
    repaired = native_agent.repair_interrupted_tools(messages)
    assert [item["role"] for item in repaired] == ["system", "assistant", "tool", "tool", "user"]
    assert repaired[2]["tool_call_id"] == "a"
    assert repaired[3]["tool_call_id"] == "b"
    assert "Inspect the project state" in repaired[3]["content"]
    assert repaired[4]["content"] == "Also make it blue."


def test_new_task_uses_native_worker_and_completes_independent_check():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        with patch.object(long_tasks, "STATE", base / "state"):
            task_id = long_tasks.add_project("Page", str(base / "page"), "Make a page")
            item = next(p for p in long_tasks.projects() if p["id"] == task_id)
            assert item["engine"] == "native"
            native_agent.save_state(item, {"status": "done", "messages": [], "turns": 2})
            with patch.object(long_tasks, "board", side_effect=AssertionError("Hermes used")):
                long_tasks.advance_project(item)
                item = next(p for p in long_tasks.projects() if p["id"] == task_id)
                assert item["status"] == "checking"
                with patch.object(long_tasks, "verify_project", return_value=(True, "Checks passed")):
                    long_tasks.advance_project(item)
            item = next(p for p in long_tasks.projects() if p["id"] == task_id)
            assert item["status"] == "ready_for_review"
            assert item["fingerprint"]


def test_passing_review_resets_old_verification_failures():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        with patch.object(long_tasks, "STATE", base / "state"):
            task_id = long_tasks.add_project("Page", str(base / "page"), "Make a page")
            item = next(p for p in long_tasks.projects() if p["id"] == task_id)
            native_agent.save_state(item, {"status": "done", "messages": [],
                                          "verification_failures": 2})
            long_tasks.advance_project(item)
            item = next(p for p in long_tasks.projects() if p["id"] == task_id)
            with patch.object(long_tasks, "verify_project", return_value=(True, "Checks passed")):
                long_tasks.advance_project(item)
            assert native_agent.read_state(item)["verification_failures"] == 0


def test_native_pause_and_resume_never_touch_hermes_board():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        with patch.object(long_tasks, "STATE", base / "state"):
            task_id = long_tasks.add_project("Page", str(base / "page"), "Make a page")
            with patch.object(long_tasks, "board", side_effect=AssertionError("Hermes used")):
                long_tasks.request(task_id, "pause")
                long_tasks.process_requests()
                item = next(p for p in long_tasks.projects() if p["id"] == task_id)
                assert item["status"] == "paused"
                long_tasks.request(task_id, "resume", "Use larger text")
                long_tasks.process_requests()
            item = next(p for p in long_tasks.projects() if p["id"] == task_id)
            assert item["status"] == "working"
            assert "Use larger text" in (base / "page/.local-ai/USER-REQUESTS.md").read_text()
