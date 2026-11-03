"""Behavior checks for Cale's native, restartable project worker."""

import copy
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cloud_ai
import long_tasks
import native_agent
import project_memory
import task_context


def project(folder):
    folder.mkdir()
    (folder / ".local-ai").mkdir()
    return {"id": "sample", "folder": str(folder), "brief": "Make a working page"}


def queue_step(title, command, phase="Core"):
    return {"phase": phase, "title": title, "check_command": command}


def test_step_queue_checks_real_work_and_resumes_in_another_process():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "game")["folder"])
        (folder / "check.py").write_text(
            "import pathlib,sys\nsource=pathlib.Path('engine.py').read_text()\n"
            "assert ('def score' if sys.argv[1]=='score' else 'def next_wave') in source\n")
        check = f'"{sys.executable}" check.py '
        queued = task_context.plan_steps(folder, [
            queue_step("Add scoring", check + "score"),
            queue_step("Add wave progression", check + "wave", "Waves")])
        first_id = queued["active"]["id"]
        failed = json.loads(native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": first_id}))
        assert not failed["passed"] and failed["done"] == 0
        assert failed["active"]["attempts"] == 1
        (folder / "engine.py").write_text("def score(kills): return kills * 10\n")
        passed = json.loads(native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": first_id}))
        assert passed["passed"] and passed["done"] == 1
        assert passed["active"]["phase"] == "Waves"
        # A fresh interpreter must resume the database queue, not chat history.
        script = ("import sys,json;sys.path.insert(0,sys.argv[1]);import task_context;"
                  "print(json.dumps(task_context.step_status(sys.argv[2])))")
        resumed = json.loads(subprocess.check_output(
            [sys.executable, "-c", script, str(native_agent.ROOT), str(folder)], text=True))
        assert resumed["active"]["id"] == passed["active"]["id"]
        assert resumed["done"] == 1
        (folder / "engine.py").write_text(
            "def score(kills): return kills * 10\ndef next_wave(wave): return wave + 1\n")
        final = json.loads(native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": resumed["active"]["id"]}))
        assert final["passed"] and final["remaining"] == 0 and final["done"] == 2
        assert "nonempty" in native_agent.completion_issue(folder)


def test_step_queue_cannot_skip_work_or_overwrite_an_unfinished_phase():
    import pytest

    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        (folder / "check.py").write_text("from pathlib import Path;Path('ran').touch()")
        queue = task_context.plan_steps(folder, [queue_step("First", f'"{sys.executable}" check.py'),
                                                queue_step("Second", "exit 0")])
        wrong = native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": queue["active"]["id"] + 1})
        assert wrong.startswith("Tool error:") and not (folder / "ran").exists()
        with pytest.raises(ValueError, match="Finish the active batch"):
            task_context.plan_steps(folder, [queue_step("Overwrite", "exit 0")])
        assert "unfinished work" in native_agent.completion_issue(folder, state={"verify_passed": True})


def test_step_check_cannot_create_the_output_it_claims_to_verify():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        (folder / "check.py").write_text("from pathlib import Path;Path('output.txt').write_text('fake')")
        queue = task_context.plan_steps(folder, [queue_step("Build output", f'"{sys.executable}" check.py')])
        result = json.loads(native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": queue["active"]["id"]}))
        assert result["passed"] is False and result["done"] == 0
        assert "changed during check" in result["active"]["evidence"]


def test_split_keeps_completed_work_and_runs_children_before_later_work():
    import pytest

    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        queue = task_context.plan_steps(folder, [queue_step("Done", "exit 0"),
            queue_step("Too broad", "exit 0"), queue_step("Integration", "exit 0")])
        request_hash = task_context.request_identity(folder)
        queue = task_context.record_step_check(folder, queue["active"]["id"], request_hash,
                                               "Exit code: 0\n", unchanged=True)
        original = queue["active"]["id"]
        split = task_context.plan_steps(folder, [queue_step("Part A", "exit 0"),
            queue_step("Part B", "exit 0")], split_id=original)
        assert split["done"] == 1 and split["remaining"] == 3 and split["total"] == 4
        assert split["active"]["title"] == "Part A"
        with pytest.raises(ValueError, match="Only the current"):
            task_context.plan_steps(folder, [queue_step("No", "exit 0"),
                queue_step("No2", "exit 0")], split_id=original)
        for title in ("Part A", "Part B", "Integration"):
            assert split["active"]["title"] == title
            split = task_context.record_step_check(folder, split["active"]["id"], request_hash,
                                                   "Exit code: 0\n", unchanged=True)
        assert split["done"] == 4 and split["remaining"] == 0


def test_new_instructions_isolate_old_queue_and_reject_late_check():
    import pytest

    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        queue = task_context.plan_steps(folder, [queue_step("Old", "exit 0")])
        old_hash = task_context.request_identity(folder)
        (folder / ".local-ai/USER-REQUESTS.md").write_text("Make the new requested version")
        assert task_context.step_status(folder)["total"] == 0
        with pytest.raises(ValueError, match="instructions changed"):
            task_context.record_step_check(folder, queue["active"]["id"], old_hash,
                                           "Exit code: 0\n", unchanged=True)
        fresh = task_context.plan_steps(folder, [queue_step("New", "exit 0")])
        assert fresh["active"]["title"] == "New" and fresh["done"] == 0


def test_queue_snapshot_uses_current_unit_and_short_recent_conversation():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        task_context.plan_steps(folder, [queue_step("Implement collisions", "exit 0", "Combat")])
        (folder / ".local-ai/PLAN.md").write_text("Roadmap " * 2000)
        messages = [{"role": "system", "content": "Original request and rules"},
                    {"role": "user", "content": "Keep the original requirements"}]
        for number in range(8):
            messages += [{"role": "assistant", "content": f"Old turn {number}"}]
        fresh = native_agent.fresh_messages(messages, folder)
        assert fresh[0]["content"] == "Original request and rules"
        assert "Implement collisions" in fresh[1]["content"]
        assert "Small-step execution queue" in fresh[1]["content"]
        assert len(fresh[1]["content"]) < 3000
        assert len(fresh) == 5 and "Old turn 5" not in str(fresh)


def test_step_check_history_excludes_private_stdout_and_failures_are_not_success():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        queue = task_context.plan_steps(folder, [queue_step("Check", "exit 0")])
        result = task_context.record_step_check(folder, queue["active"]["id"],
            task_context.request_identity(folder), "Exit code: 9\nprivate document words", unchanged=True)
        assert not result["passed"]
        assert "private document words" not in task_context.snapshot(folder)
        assert task_context.action_outcome("task_steps", json.dumps(result)) == "Failed"
        assert task_context.action_outcome("task_steps", json.dumps(queue)) == "Completed"


def test_planning_boundary_blocks_later_stale_actions_in_same_model_response():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        response = {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "plan", "function": {"name": "task_steps", "arguments": json.dumps({
                "action": "plan", "steps": [queue_step("First tiny change", "exit 0")]})}},
            {"id": "stale-write", "function": {"name": "write_file", "arguments": json.dumps({
                "path": "stale.txt", "content": "Should not run before reading the fresh unit"})}},
        ]}}]}
        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=lambda _: response)
        assert not (folder / "stale.txt").exists()
        assert task_context.step_status(folder)["remaining"] == 1
        replies = [message for message in state["messages"] if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in replies] == ["plan", "stale-write"]
        assert "queue advanced" in replies[-1]["content"]


def test_step_tool_focus_restores_specialists_periodically_and_after_phase():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        task_context.plan_steps(item["folder"], [queue_step("Tiny component", "exit 0")])
        state = {}
        for _ in range(4):
            names = {tool["function"]["name"] for tool in native_agent.step_tools(item, state, native_agent.TOOLS)}
            assert {"task_steps", "run_command", "write_file"} <= names
            assert "discover_local_tools" not in names
        assert native_agent.step_tools(item, state, native_agent.TOOLS) is native_agent.TOOLS
        queue = task_context.step_status(item["folder"])
        task_context.record_step_check(item["folder"], queue["active"]["id"],
            task_context.request_identity(item["folder"]), "Exit code: 0\n", unchanged=True)
        assert native_agent.step_tools(item, state, native_agent.TOOLS) is native_agent.TOOLS


def test_revise_repairs_an_active_check_without_erasing_failed_attempts():
    import pytest

    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        queue = task_context.plan_steps(folder, [queue_step("Behavior check", "exit 3")])
        step_id = queue["active"]["id"]
        failed = json.loads(native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": step_id}))
        assert not failed["passed"] and failed["active"]["attempts"] == 1
        fixed = json.loads(native_agent.execute_tool(folder, "task_steps", {
            "action": "revise", "step_id": step_id,
            "steps": [queue_step("Behavior check", "exit 0")]}))
        assert fixed["done"] == 0 and fixed["active"]["attempts"] == 1
        assert "not yet passed" in fixed["active"]["evidence"]
        passed = json.loads(native_agent.execute_tool(folder, "task_steps", {
            "action": "complete", "step_id": step_id}))
        assert passed["passed"] and passed["done"] == 1
        with pytest.raises(ValueError, match="Only the current"):
            task_context.revise_step(folder, step_id, [queue_step("Cannot undo success", "exit 9")])


def test_running_saved_check_auto_advances_and_stops_stale_batch_actions():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / "check.py").write_text("from pathlib import Path;assert Path('output.txt').read_text()=='ready'")
        (folder / "output.txt").write_text("ready")
        command = f'"{sys.executable}" check.py'
        task_context.plan_steps(folder, [queue_step("Check output", command), queue_step("Next", "exit 0")])
        response = {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "check", "function": {"name": "run_command", "arguments": json.dumps({"command": command})}},
            {"id": "stale", "function": {"name": "write_file", "arguments": json.dumps({
                "path": "should-not-exist", "content": "stale second action"})}},
        ]}}]}
        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=lambda _: response)
        queue = task_context.step_status(folder)
        assert queue["done"] == 1 and queue["active"]["title"] == "Next"
        assert not (folder / "should-not-exist").exists()
        assert "SQLite step progress" in str(state["messages"])
        assert native_agent.step_check_context(folder, "run_command", {"command": "echo unrelated"}) is None


def test_saved_verifier_check_recognizes_direct_supervisor_equivalent():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        task_context.plan_steps(folder, [queue_step("Boundary cases",
            "powershell -NoProfile -File .local-ai/verify.ps1")])
        assert native_agent.step_check_context(folder, "run_verifier", {})
        assert native_agent.step_check_context(folder, "run_command", {
            "command": "powershell -NonInteractive -File .local-ai/verify.ps1"})
        assert native_agent.step_check_context(folder, "run_command", {
            "command": "powershell -File other.ps1"}) is None


def run_typed_python_check(item, arguments):
    folder = Path(item["folder"])
    state = folder / ".local-ai/test-supervisor"
    state.mkdir(exist_ok=True)
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        return json.loads(native_agent.execute_tool(folder, "check_python_function", arguments))


def run_typed_javascript_check(item, arguments):
    folder = Path(item["folder"])
    state = folder / ".local-ai/test-supervisor"
    state.mkdir(exist_ok=True)
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        return json.loads(native_agent.execute_tool(folder, "check_javascript_function", arguments))


def javascript_report(folder, source, cases, name="calculate", suffix="js"):
    (folder / ("logic." + suffix)).write_text(source, encoding="utf-8")
    raw = json.dumps({"request_hash": task_context.request_identity(folder), "checks": [{
        "path": "logic." + suffix, "function": name, "cases": cases}]}).encode()
    target = folder / ".local-ai/test-javascript.json"
    target.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return native_agent.run_javascript_checks(folder, target, digest), target, digest


def test_saved_function_input_revision_preserves_other_cases_and_steps(tmp_path):
    for javascript in (False, True):
        item = project(tmp_path / ("revise-js" if javascript else "revise-py"))
        folder = Path(item["folder"])
        path = "logic.js" if javascript else "logic.py"
        source = ("function subtract(x,y){return x-y}\nfunction double(x){return x*2}\n" if javascript else
                  "def subtract(x,y): return x-y\ndef double(x): return x*2\n")
        (folder / path).write_text(source)
        run = run_typed_javascript_check if javascript else run_typed_python_check
        tool = "check_javascript_function" if javascript else "check_python_function"
        run(item, {"path": path, "function": "double", "cases": [{"args": [2], "expected": 4}]})
        spec = {"path": path, "function": "subtract", "cases": [
            {"args": [2, 8], "expected": 6}, {"args": [5, 2], "expected": 3}]}
        report = run(item, spec)
        assert report["passed"] is False and "suite_sha256" in report
        step = {"phase": "Logic", "title": "Subtraction", "check_tool": tool, "check_arguments": spec}
        active = task_context.plan_steps(folder, [step])["active"]
        task_context.record_action(folder, tool, json.dumps(report), spec)
        snapshot = task_context.snapshot(folder)
        assert report["suite_sha256"] in snapshot and "saved_case" in snapshot
        revision = {"path": path, "function": "subtract", "saved_case": 1,
            "suite_sha256": report["suite_sha256"], "cases": [{"args": [8, 2], "expected": 6}]}
        assert native_agent.step_check_context(folder, tool, revision) is None
        fixed = run(item, revision)
        assert fixed["passed"] and fixed["case_count"] == 3 and fixed["verifier_saved"]
        assert (folder / path).read_text() == source
        assert task_context.step_status(folder)["done"] == 0
        corrected = {**spec, "cases": [revision["cases"][0], spec["cases"][1]]}
        task_context.revise_step(folder, active["id"], [{**step, "check_arguments": corrected}])
        with patch.object(long_tasks, "projects", return_value=[item]):
            result = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
        assert result["passed"] and result["done"] == 1
        # A different function's previously saved behavior still matters.
        (folder / path).write_text(source.replace("x*2", "x*3"))
        assert run(item, corrected)["passed"] is False


def test_saved_function_revision_rejects_stale_or_invalid_references_before_write(tmp_path):
    import pytest

    item = project(tmp_path / "revision-guards")
    folder = Path(item["folder"])
    (folder / "logic.js").write_text("function calculate(x){return x}\nfunction other(x){return x}")
    spec = {"path": "logic.js", "function": "calculate", "cases": [{"args": [2], "expected": 3}]}
    report = run_typed_javascript_check(item, spec)
    target = folder / ".local-ai/cale-javascript-checks.json"
    raw = target.read_bytes()
    revision = {**spec, "saved_case": 1, "suite_sha256": report["suite_sha256"]}
    for change in ({"saved_case": True}, {"saved_case": 2}, {"saved_case": 0},
                   {"suite_sha256": "0" * 64}, {"suite_sha256": None},
                   {"cases": spec["cases"] * 2}, {"function": "other"}):
        with pytest.raises(ValueError):
            native_agent.check_javascript_function(folder, {**revision, **change})
        assert target.read_bytes() == raw
    with pytest.raises(ValueError, match="one-time checker action"):
        task_context.plan_steps(folder, [{"phase": "Logic", "title": "Bad revision", "check_tool":
            "check_javascript_function", "check_arguments": revision}])
    (folder / ".local-ai/USER-REQUESTS.md").write_text("A new user request")
    with pytest.raises(ValueError, match="instructions changed"):
        native_agent.check_javascript_function(folder, revision)
    assert target.read_bytes() == raw


def test_partial_function_verifier_does_not_nudge_plan_handoff_with_steps_remaining(tmp_path):
    item = project(tmp_path / "partial-js-verifier")
    folder = Path(item["folder"])
    (folder / "logic.js").write_text("function first(x){return x+1}\nfunction second(x){return x*2}")
    first = {"path": "logic.js", "function": "first", "cases": [{"args": [1], "expected": 2}]}
    second = {"path": "logic.js", "function": "second", "cases": [{"args": [2], "expected": 4}]}
    task_context.plan_steps(folder, [{"phase": "Logic", "title": name, "check_tool": "check_javascript_function",
        "check_arguments": spec} for name, spec in (("First", first), ("Second", second))])
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "check-first", "function": {
        "name": "check_javascript_function", "arguments": json.dumps(first)}}]}}]}
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: response)
    assert state["verify_passed"] and task_context.step_status(folder)["done"] == 1
    assert task_context.step_status(folder)["remaining"] == 1
    assert not state.get("verifier_nudged")
    assert not any("The saved verifier passed. Update" in str(m.get("content", "")) for m in state["messages"])


def test_worker_preserves_owned_function_suite_and_directs_case_revision(tmp_path):
    item = project(tmp_path / "owned-case-suite")
    folder = Path(item["folder"])
    target = folder / ".local-ai/cale-javascript-checks.json"
    raw = b'{"request_hash":"fixture","checks":[]}'
    target.write_bytes(raw)
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"id": "overwrite", "function": {"name": "write_file", "arguments": json.dumps({
            "path": "./.local-ai/cale-javascript-checks.json", "content": "{}"})}},
        {"id": "edit", "function": {"name": "edit_file", "arguments": json.dumps({
            "path": ".local-ai\\cale-javascript-checks.json", "old_text": "fixture", "new_text": "different"})}}]}}]}
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: response)
    assert target.read_bytes() == raw
    replies = [m["content"] for m in state["messages"] if m.get("role") == "tool"]
    assert len(replies) == 2 and all("saved_case" in reply and "suite_sha256" in reply for reply in replies)
    assert native_agent.python_verifier_write_issue(folder, item, "write_file", {"path": "custom-tests.json"}) == ""
    assert "check_python_function" in native_agent.python_verifier_write_issue(folder, item, "edit_file", {
        "path": ".local-ai/cale-python-checks.json"})


def test_automatic_acceptance_advances_real_steps_without_inference_or_handoff(tmp_path):
    item = project(tmp_path / "automatic-steps")
    folder = Path(item["folder"])
    (folder / "logic.js").write_text("function first(x){return x+1}\nfunction second(x){return x*2}")
    specs = [{"path": "logic.js", "function": name, "cases": [{"args": [2], "expected": expected}]}
             for name, expected in (("first", 3), ("second", 4))]
    task_context.plan_steps(folder, [{"phase": "Logic", "title": spec["function"],
        "check_tool": "check_javascript_function", "check_arguments": spec} for spec in specs])
    state = {"status": "running", "turns": 1}
    native_agent.save_state(item, state)
    messages = []
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.advance_checked_steps(item, state, messages)
    assert task_context.step_status(folder)["done"] == 2
    assert state["turns"] == 1 and state["automatic_step_checks"] == 2
    assert state["verify_passed"] and state["status"] == "running" and len(messages) == 2
    # The accumulated verifier must still test the earlier function.
    (folder / "logic.js").write_text("function first(x){return 0}\nfunction second(x){return x*2}")
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")


def test_automatic_acceptance_caches_failure_across_restart_and_retries_source_or_check_changes(tmp_path):
    item = project(tmp_path / "automatic-retry")
    folder = Path(item["folder"])
    source = folder / "logic.js"
    source.write_text("function calculate(x){return x}")
    spec = {"path": "logic.js", "function": "calculate", "cases": [{"args": [2], "expected": 4}]}
    step = {"phase": "Logic", "title": "Calculate", "check_tool": "check_javascript_function", "check_arguments": spec}
    active = task_context.plan_steps(folder, [step])["active"]
    state = {"status": "running", "turns": 1}
    native_agent.remember_local_tool_feedback(state, "fixture-local", "", task_context.request_identity(folder), failed=True, recovered=False)
    native_agent.remember_local_tool_feedback(state, "fixture-local", "", task_context.request_identity(folder), failed=True, recovered=False)
    native_agent.save_state(item, state)
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 1 and task_context.step_status(folder)["done"] == 0
        assert state["local_repair_feedback"]
        state = native_agent.read_state(item)
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 1
        # A revised acceptance identity retries even with unchanged source.
        corrected = {**spec, "cases": [{"args": [2], "expected": 3}]}
        task_context.revise_step(folder, active["id"], [{**step, "check_arguments": corrected}])
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 2 and task_context.step_status(folder)["done"] == 0
        source.write_text("function calculate(x){return x+1}")
        assert native_agent.advance_checked_steps(item, state, [])
    assert state["automatic_step_checks"] == 3 and task_context.step_status(folder)["done"] == 1
    assert "local_repair_feedback" not in state


def test_automatic_acceptance_does_not_commit_control_request_source_or_acceptance_races(tmp_path):
    for race in ("pause", "delete", "request", "source", "acceptance"):
        item = project(tmp_path / ("automatic-race-" + race))
        folder = Path(item["folder"])
        source = folder / "logic.js"
        source.write_text("function calculate(x){return x}")
        spec = {"path": "logic.js", "function": "calculate", "cases": [{"args": [2], "expected": 2}]}
        step = {"phase": "Logic", "title": "Calculate", "check_tool": "check_javascript_function", "check_arguments": spec}
        active = task_context.plan_steps(folder, [step])["active"]
        state = {"status": "running", "turns": 1}
        native_agent.save_state(item, state)
        execute = native_agent.execute_tool
        registered = [item]
        def intervene(root, name, arguments, **kwargs):
            if name == "task_steps":
                allowed = kwargs["before_step_commit"]
                def commit():
                    if race == "pause":
                        native_agent.save_state(item, {**state, "status": "paused"})
                    elif race == "delete":
                        registered.clear()
                    elif race == "request":
                        (folder / ".local-ai/USER-REQUESTS.md").write_text("New instruction")
                    elif race == "source":
                        source.write_text("function calculate(x){return 999}")
                    else:
                        task_context.revise_step(folder, active["id"], [{**step, "check_arguments": {
                            **spec, "cases": [{"args": [2], "expected": 3}]}}])
                    return allowed()
                kwargs["before_step_commit"] = commit
            return execute(root, name, arguments, **kwargs)
        with patch.object(long_tasks, "projects", return_value=registered), patch.object(native_agent, "execute_tool", side_effect=intervene):
            result = native_agent.advance_checked_steps(item, state, [])
        assert task_context.step_status(folder)["done"] == 0
        if race in {"pause", "delete", "request"}:
            assert result is False
        if race == "pause":
            assert native_agent.read_state(item)["status"] == "paused"


def test_automatic_acceptance_waits_for_source_and_leaves_shell_checks_explicit(tmp_path):
    for name, step in (("missing", {"phase": "Logic", "title": "Not created", "check_tool": "check_javascript_function",
        "check_arguments": {"path": "missing.js", "function": "calculate", "cases": [{"args": [2], "expected": 2}]}}),
        ("shell", {"phase": "Integration", "title": "Explicit command", "check_command": "echo check"})):
        item = project(tmp_path / name)
        folder = Path(item["folder"])
        task_context.plan_steps(folder, [step])
        state = {"status": "running", "turns": 1}
        native_agent.save_state(item, state)
        with patch.object(native_agent, "execute_tool") as execute:
            assert native_agent.advance_checked_steps(item, state, [])
        execute.assert_not_called()
        assert task_context.step_status(folder)["done"] == 0


def test_javascript_formats_execute_current_source_and_generate_repeatable_verifiers(tmp_path):
    formats = [("js", "function calculate(x){return x*2}", "calculate"),
        ("js", "const calculate=x=>x*2", "calculate"),
        ("js", "export function calculate(x){return x*2}", "calculate"),
        ("mjs", "export default x=>x*2", "default"),
        ("cjs", "exports.calculate=x=>x*2", "calculate"),
        ("cjs", "module.exports=x=>x*2", "default")]
    for number, (suffix, source, function) in enumerate(formats):
        item = project(tmp_path / ("O'Brien JS " + str(number)))
        folder = Path(item["folder"])
        path = "logic." + suffix
        (folder / path).write_text(source)
        spec = {"path": path, "function": function, "cases": [{"args": [2], "expected": 4}, {"args": [-1], "expected": -2}]}
        result = run_typed_javascript_check(item, spec)
        assert result["passed"] and result["verifier_saved"] and result["verifier_check_passed"]
        (folder / path).write_text(source.replace("x*2", "x*3"))  # Same size, no module cache allowed.
        with patch.object(long_tasks, "projects", return_value=[item]):
            assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")


def test_javascript_cases_compare_nested_types_unicode_and_exception_names(tmp_path):
    folder = Path(project(tmp_path / "typed-js")["folder"])
    source = """function calculate(x){if(x<0)throw new RangeError('negative');
      console.log('discard this');return {ok:true,total:x,parts:[null,'£é']}}"""
    cases = [{"args": [3.25], "expected": {"ok": True, "total": 3.25, "parts": [None, '£é']}},
             {"args": [-1], "raises": "RangeError"}]
    assert javascript_report(folder, source, cases)[0]["passed"]
    cases[0]["expected"]["ok"] = 1
    assert not javascript_report(folder, source, cases)[0]["passed"]
    cases[0]["expected"]["ok"] = True
    cases[1]["raises"] = "TypeError"
    assert not javascript_report(folder, source, cases)[0]["passed"]
    custom = "class NeedsPositive extends Error{};function calculate(){throw new NeedsPositive('negative')}"
    assert javascript_report(folder, custom, [{"raises": "NeedsPositive"}])[0]["passed"]


def test_function_numbers_do_not_round_large_integer_mismatches_into_success(tmp_path):
    assert native_agent.python_values_match(10**200, 10**200, 1e-8)
    assert not native_agent.python_values_match(2**53, 2**53 + 1, 1e-8)
    assert not native_agent.python_values_match(2**53 + 1, float(2**53), 1e-8)
    folder = Path(project(tmp_path / "large-js")["folder"])
    report = javascript_report(folder, "function calculate(){return 9007199254740992}", [
        {"expected": 9007199254740993}])[0]
    assert not report["passed"]


def test_javascript_harness_failures_never_satisfy_expected_error_cases(tmp_path):
    folder = Path(project(tmp_path / "unsupported-js")["folder"])
    for expression in ("undefined", "NaN", "Infinity", "Array(1)", "Promise.resolve(2)", "new Date()", "1n"):
        source = f"function calculate(){{return {expression}}}"
        result = javascript_report(folder, source, [{"raises": "Error"}])[0]
        assert not result["passed"] and "checker" in str(result).lower()
    for source in ("function calculate(){while(true){}}", "while(true){};function calculate(){}"):
        started = time.monotonic()
        assert not javascript_report(folder, source, [{"raises": "Error"}])[0]["passed"]
        assert time.monotonic() - started < 12
    assert not javascript_report(folder, '// calculate is absent', [{"args": [2], "expected": {}}], name="Object")[0]["passed"]
    assert not javascript_report(folder, "import fs from 'node:fs';export function calculate(){}",
                                 [{"raises": "Error"}], suffix="mjs")[0]["passed"]


def test_javascript_cases_reset_module_state_and_clone_mutable_arguments(tmp_path):
    folder = Path(project(tmp_path / "fresh-js")["folder"])
    assert javascript_report(folder, "let n=0;function calculate(){return ++n}", [
        {"expected": 1}, {"expected": 1}])[0]["passed"]
    cases = [{"args": [[1, 2]], "expected": [2, 1]}, {"args": [[3, 4]], "expected": [4, 3]}]
    assert javascript_report(folder, "function calculate(x){x.reverse();return x}", cases)[0]["passed"]
    assert cases[0]["args"] == [[1, 2]]


def test_javascript_cases_accumulate_across_functions_and_preserve_custom_verifier(tmp_path):
    item = project(tmp_path / "retained-js")
    folder = Path(item["folder"])
    source = "function double(n){return n*2} function increment(n){return n+1}"
    (folder / "logic.js").write_text(source)
    def check(function, value, expected):
        return run_typed_javascript_check(item, {"path": "logic.js", "function": function,
            "cases": [{"args": [value], "expected": expected}]})
    assert check("double", 2, 4)["passed"]
    assert check("increment", 2, 3)["passed"]
    (folder / "logic.js").write_text(source.replace("n*2", "n*3"))
    failed = check("increment", 3, 4)
    assert not failed["passed"] and failed["case_count"] == 3
    assert failed["cases"][0]["function"] == "double"
    state = {}
    native_agent.remember_checker_verdict(state, "check_javascript_function", {"path": "logic.js"}, json.dumps(failed))
    assert state["failed_checks"]
    (folder / "logic.js").write_text(source)
    passed = check("increment", 3, 4)
    native_agent.remember_checker_verdict(state, "check_javascript_function", {"path": "logic.js"}, json.dumps(passed))
    assert passed["passed"] and not state["failed_checks"]
    custom = '# User integration suite\nexit 0\n'
    (folder / ".local-ai/verify.ps1").write_text(custom)
    assert not check("double", 3, 6)["verifier_saved"]
    assert (folder / ".local-ai/verify.ps1").read_text() == custom


def test_javascript_sqlite_units_use_saved_cases_and_auto_advance_on_real_direct_check(tmp_path):
    item = project(tmp_path / "queued-js")
    folder = Path(item["folder"])
    spec = {"path": "logic.js", "function": "double", "cases": [{"args": [2], "expected": 4}]}
    queue = task_context.plan_steps(folder, [{"phase": "Logic", "title": "Fix double",
        "check_tool": "check_javascript_function", "check_arguments": spec}])
    (folder / "logic.js").write_text("function double(n){return n*3}")
    with patch.object(long_tasks, "projects", return_value=[item]):
        failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": queue["active"]["id"]}))
    assert not failed["passed"] and "double" in task_context.snapshot(folder)
    (folder / "logic.js").write_text("function double(n){return n*2}")
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "check", "function": {
        "name": "check_javascript_function", "arguments": json.dumps(spec)}}]}}]}
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: response)
    assert task_context.step_status(folder)["done"] == 1
    assert state["verify_passed"] and not state["failed_checks"]


def test_javascript_acceptance_pins_cases_request_and_source_snapshot(tmp_path):
    import pytest

    folder = Path(project(tmp_path / "bound-js")["folder"])
    result, target, digest = javascript_report(folder, "function calculate(x){return x}", [{"args": [2], "expected": 2}])
    assert result["passed"]
    raw = target.read_bytes()
    target.write_bytes(raw + b' ')
    with pytest.raises(ValueError, match="cases changed"):
        native_agent.run_javascript_checks(folder, target, digest)
    target.write_bytes(raw)
    (folder / ".local-ai/USER-REQUESTS.md").write_text("New instructions")
    with pytest.raises(ValueError, match="instructions changed"):
        native_agent.run_javascript_checks(folder, target, digest)
    (folder / ".local-ai/USER-REQUESTS.md").write_text("")
    run = native_agent.subprocess.run
    def edit_source(*args, **kwargs):
        result = run(*args, **kwargs)
        (folder / "logic.js").write_text("function calculate(x){return x+1}")
        return result
    with patch.object(native_agent.subprocess, "run", side_effect=edit_source):
        report = native_agent.run_javascript_checks(folder, target, digest)
    assert not report["passed"] and "changed during verification" in str(report["errors"])


def test_javascript_rejects_expression_names_keyword_args_and_unbounded_case_data(tmp_path):
    import pytest

    folder = Path(project(tmp_path / "js-arguments")["folder"])
    (folder / "logic.js").write_text("function calculate(x){return x}")
    base = {"path": "logic.js", "function": "calculate", "cases": [{"args": [1], "expected": 1}]}
    for change in ({"function": "process.exit()"}, {"path": "../outside.js"},
                   {"cases": [{"kwargs": {"x": 1}, "expected": 1}]}, {"cases": []},
                   {"cases": [{"args": [1], "expected": float('nan')}]}, {"tolerance": True},
                   {"cases": [{"expected": 1, "raises": "Error"}]}, {"cases": [{"args": ['x' * 9000], "expected": 1}]}):
        with pytest.raises(ValueError):
            native_agent.normalize_javascript_check(folder, {**base, **change})
    assert not (folder / ".local-ai/cale-javascript-checks.json").exists()


def test_javascript_function_count_and_missing_node_fail_before_subject_execution(tmp_path):
    import pytest

    folder = Path(project(tmp_path / "node-readiness")["folder"])
    _, target, _ = javascript_report(folder, "function calculate(x){return x}", [{"args": [1], "expected": 1}])
    data = json.loads(target.read_text())
    data["checks"] *= 9
    raw = json.dumps(data).encode()
    target.write_bytes(raw)
    with patch.object(native_agent.subprocess, "run") as child, pytest.raises(ValueError, match="1 to 8"):
        native_agent.run_javascript_checks(folder, target, hashlib.sha256(raw).hexdigest())
    child.assert_not_called()
    data["checks"] = data["checks"][:1]
    raw = json.dumps(data).encode()
    target.write_bytes(raw)
    with (patch.object(native_agent, "ROOT", folder), patch.object(native_agent.sys, "executable", r'C:\missing-python.exe'),
          patch.object(native_agent.shutil, "which", return_value=None), pytest.raises(ValueError, match="Node runtime is not ready")):
        native_agent.run_javascript_checks(folder, target, hashlib.sha256(raw).hexdigest())


def test_javascript_module_sources_are_searchable_and_explicit_outputs_are_required(tmp_path):
    folder = Path(project(tmp_path / "module-memory")["folder"])
    (folder / "movement.mjs").write_text("export function steerShip(x){return x+1}")
    (folder / "waves.cjs").write_text("exports.finishWave=n=>n+1")
    project_memory.index(folder, include_code=True)
    assert project_memory.search(folder, "steerShip")
    assert project_memory.search(folder, "finishWave")
    assert native_agent.requested_root_files("Create logic.mjs and save waves.cjs") == ["logic.mjs", "waves.cjs"]


def test_javascript_structured_argument_errors_identify_array_path_without_copying_bad_value(tmp_path):
    import pytest

    folder = Path(project(tmp_path / "js-plan-diagnostic")["folder"])
    with pytest.raises(ValueError) as failure:
        task_context.plan_steps(folder, [{"phase": "Movement", "title": "Move player", "check_tool": "check_javascript_function",
            "check_arguments": {"path": "logic.js", "function": "movePlayer", "cases": [
                {"args": {"private_named_argument": 10}, "expected": 10}]}}])
    assert "cases.0.args" in str(failure.value) and 'expected "array"' in str(failure.value)
    assert "positional JSON args" in str(failure.value)
    assert "private_named_argument" not in str(failure.value)


def test_typed_python_checker_passes_real_cases_and_rejects_regression_in_saved_verifier():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "O'Brien's folder")
        folder = Path(item["folder"])
        source = folder / "temperature.py"
        source.write_text("def to_celsius(f):\n    return (f-32)*5/9\n")
        result = run_typed_python_check(item, {"path": "temperature.py", "function": "to_celsius",
            "cases": [{"args": [32], "expected": 0}, {"args": [212], "expected": 100},
                      {"args": [-40], "expected": -40}, {"args": [98.6], "expected": 37}]})
        assert result["passed"] and result["case_count"] == 4
        assert result["verifier_saved"] and result["verifier_check_passed"], result
        source.write_text("def to_celsius(f):\n    return f*5/9\n")
        checked = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
            str(folder / ".local-ai/verify.ps1")], cwd=folder, capture_output=True, text=True, timeout=20)
        assert checked.returncode != 0 and '"passed": false' in checked.stdout


def test_typed_python_cases_check_nested_values_kwargs_exceptions_and_bool_types():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        (folder / "calc.py").write_text(
            "def calculate(n, scale=2):\n    if n<0: raise ValueError('negative')\n"
            "    return {'value':n*scale,'flags':[True,False]}\n")
        spec = {"path": "calc.py", "function": "calculate", "cases": [
            {"args": [2], "kwargs": {"scale": 3}, "expected": {"value": 6, "flags": [True, False]}},
            {"args": [-1], "raises": "ValueError"}]}
        result = run_typed_python_check(item, spec)
        assert result["passed"] and result["case_count"] == 2
        spec["cases"][0]["expected"]["flags"] = [1, 0]
        assert not run_typed_python_check(item, spec)["passed"]


def test_typed_python_suite_keeps_other_functions_and_earlier_boundary_cases():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        source = folder / "helpers.py"
        correct = "def double(n): return n*2\ndef increment(n): return n+1\n"
        source.write_text(correct)
        assert run_typed_python_check(item, {"path": "helpers.py", "function": "double",
            "cases": [{"args": [2], "expected": 4}]})["passed"]
        assert run_typed_python_check(item, {"path": "helpers.py", "function": "increment",
            "cases": [{"args": [2], "expected": 3}]})["passed"]
        source.write_text(correct.replace("n*2", "n*3"))
        request = {"path": "helpers.py", "function": "increment", "cases": [{"args": [3], "expected": 4}]}
        failed = run_typed_python_check(item, request)
        assert not failed["passed"] and failed["case_count"] == 3
        assert failed["cases"][0]["function"] == "double"
        state = {}
        native_agent.remember_checker_verdict(state, "check_python_function", request, json.dumps(failed))
        assert state["failed_checks"]
        source.write_text(correct)
        passed = run_typed_python_check(item, request)
        native_agent.remember_checker_verdict(state, "check_python_function", request, json.dumps(passed))
        assert passed["passed"] and passed["case_count"] == 3 and not state["failed_checks"]


def test_typed_python_input_rejects_expressions_vacuous_cases_and_nonfinite_numbers():
    import pytest

    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "code")["folder"])
        (folder / "code.py").write_text("def calculate(n): return n\n")
        base = {"path": "code.py", "function": "calculate", "cases": [{"args": [1], "expected": 1}]}
        for change in ({"function": "__import__('os').system('bad')"}, {"cases": []},
                       {"cases": [{"args": [1]}]}, {"cases": [{"expected": 1, "raises": "ValueError"}]},
                       {"cases": [{"expected": float("nan")}]}, {"tolerance": 100}):
            with pytest.raises(ValueError):
                native_agent.normalize_python_check(folder, {**base, **change})
        assert not (folder / ".local-ai/cale-python-checks.json").exists()


def test_typed_python_checker_rejects_output_creation_and_preserves_custom_verifier():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        source = folder / "code.py"
        source.write_text("def calculate(n):\n    open('fake-output.txt','w').write('made by test')\n    return n*2\n")
        arguments = {"path": "code.py", "function": "calculate", "cases": [{"args": [2], "expected": 4}]}
        failed = run_typed_python_check(item, arguments)
        assert not failed["passed"] and not failed["verifier_saved"]
        assert "read-only" in str(failed["errors"])
        source.write_text("def calculate(n): return n*2\n")
        verifier = folder / ".local-ai/verify.ps1"
        original = "# Custom project verifier\nexit 1\n"
        verifier.write_text(original)
        passed = run_typed_python_check(item, arguments)
        assert passed["passed"] and not passed["verifier_saved"]
        assert verifier.read_text() == original


def test_python_verifier_rejects_changed_cases_and_changed_user_instructions():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        (folder / "code.py").write_text("def calculate(n): return n*2\n")
        assert run_typed_python_check(item, {"path": "code.py", "function": "calculate",
            "cases": [{"args": [2], "expected": 4}]})["passed"]
        record = folder / ".local-ai/cale-python-checks.json"
        original = record.read_bytes()
        record.write_bytes(original + b" ")
        command = ["powershell.exe", "-NoProfile", "-File", str(folder / ".local-ai/verify.ps1")]
        changed = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=20)
        assert changed.returncode != 0 and "cases changed" in changed.stdout
        record.write_bytes(original)
        (folder / ".local-ai/USER-REQUESTS.md").write_text("Change the required multiplier to three")
        stale = subprocess.run(command, cwd=folder, capture_output=True, text=True, timeout=20)
        assert stale.returncode != 0 and "instructions changed" in stale.stdout


def test_python_checker_ignores_stale_same_timestamp_dependency_bytecode():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        (folder / "code.py").write_text("import helper\ndef calculate(n): return helper.add(n)\n")
        helper = folder / "helper.py"
        helper.write_text("def add(n): return n+1\n")
        subprocess.run([sys.executable, "-m", "py_compile", str(helper)], check=True)
        stamp = helper.stat()
        helper.write_text("def add(n): return n+9\n")
        os.utime(helper, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        result = run_typed_python_check(item, {"path": "code.py", "function": "calculate",
            "cases": [{"args": [2], "expected": 3}]})
        assert not result["passed"] and "got 11" in str(result)


def test_python_checker_guard_preserves_owned_verifier_and_respects_user_framework_choice():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        item["brief"] = "Fix the arithmetic function in arithmetic.py"
        folder = Path(item["folder"])
        (folder / "arithmetic.py").write_text("def double(n): return n*2\n")
        args = {"path": ".local-ai/verify.ps1", "content": "wrong generated script"}
        assert native_agent.python_function_candidate(item) == ("arithmetic.py", ["double"])
        assert "check_python_function" in native_agent.python_verifier_write_issue(folder, item, "write_file", args)
        verifier = folder / ".local-ai/verify.ps1"
        verifier.write_text("# Generated by Cale check_python_function\nexit 0")
        assert "Preserve" in native_agent.python_verifier_write_issue(folder, item, "edit_file", args)
        (folder / ".local-ai/USER-REQUESTS.md").write_text("Use pytest for the project test suite")
        assert native_agent.python_function_candidate(item) is None
        assert not native_agent.python_verifier_write_issue(folder, item, "write_file", args)


def test_checker_first_guard_leaves_io_and_non_json_functions_on_normal_test_route():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        item["brief"] = "Fix the function in code.py"
        source = Path(item["folder"]) / "code.py"
        for body in ("return {n}", "yield n", "return range(n)", "return b'bytes'",
                     "return open('file.txt').read()"):
            source.write_text("def calculate(n):\n    " + body + "\n")
            assert native_agent.python_function_candidate(item) is None, body


def test_large_valid_python_case_data_returns_complete_parseable_verdict():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        (Path(item["folder"]) / "code.py").write_text("def count(text): return len(text)\n")
        result = run_typed_python_check(item, {"path": "code.py", "function": "count",
            "cases": [{"args": [chr(97+i) * 7000], "expected": 7000} for i in range(7)]})
        assert result["passed"] and result["case_count"] == 7 and result["passed_cases"] == 7
        assert result["diagnostics_limited"] and len(result["cases"]) == 4
        assert result["verifier_check_passed"]


def test_python_suite_diagnostics_follow_failing_function_and_clear_across_files():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        first = folder / "first.py"
        first.write_text("def double(n): return n*2\n")
        (folder / "second.py").write_text("def increment(n): return n+1\n")
        assert run_typed_python_check(item, {"path": "first.py", "function": "double",
            "cases": [{"args": [2], "expected": 4}]})["passed"]
        first.write_text("def double(n): return n*3\n")
        args = {"path": "second.py", "function": "increment", "cases": [{"args": [3], "expected": 4}]}
        failed = run_typed_python_check(item, args)
        task_context.record_action(folder, "check_python_function", json.dumps(failed), args)
        diagnostic = task_context.checker_failures(folder)[0]
        assert diagnostic["path"] == ".local-ai/cale-python-checks.json"
        assert "first.py:double" in diagnostic["details"] and '"args": [2]' in diagnostic["details"]
        first.write_text("def double(n): return n*2\n")
        passed = run_typed_python_check(item, args)
        assert passed["passed"] and passed["case_count"] == 2
        task_context.record_action(folder, "check_python_function", json.dumps(passed), args)
        assert not task_context.checker_failures(folder)


def test_structured_step_plans_before_source_exists_and_checks_real_function():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        arguments = {"path": "calc.py", "function": "double", "cases": [{"args": [2], "expected": 4}]}
        queue = task_context.plan_steps(folder, [{"phase": "Core", "title": "Implement double",
            "check_tool": "check_python_function", "check_arguments": arguments}])
        assert queue["active"]["check_arguments"] == arguments
        source = folder / "calc.py"
        source.write_text("def double(n): return n*3\n")
        state = folder / ".local-ai/test-supervisor"
        state.mkdir()
        with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
            failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": queue["active"]["id"]}))
            assert not failed["passed"] and failed["done"] == 0
            source.write_text("def double(n): return n*2\n")
            before = long_tasks.fingerprint(folder, include_verifier=False)
            passed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": queue["active"]["id"]}))
            assert passed["passed"] and passed["done"] == 1 and passed["verifier_check_passed"]
            assert long_tasks.fingerprint(folder, include_verifier=False) == before
        assert (folder / ".local-ai/verify.ps1").is_file()


def test_direct_structured_check_advances_matching_step_with_valid_json_reply():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        (folder / "calc.py").write_text("def double(n): return n*2\n")
        arguments = {"path": "calc.py", "function": "double", "cases": [{"args": [2], "expected": 4}]}
        task_context.plan_steps(folder, [{"phase": "Core", "title": "Implement double",
            "check_tool": "check_python_function", "check_arguments": arguments},
            {"phase": "Integration", "title": "Final integration", "check_tool": "run_verifier"}])
        response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "check", "function": {
            "name": "check_python_function", "arguments": json.dumps(arguments)}}]}}]}
        state_folder = folder / ".local-ai/test-supervisor"
        state_folder.mkdir()
        with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state_folder), \
             patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=lambda _: response)
        assert task_context.step_status(folder)["done"] == 1
        reply = next(message for message in state["messages"] if message.get("role") == "tool")
        parsed = json.loads(reply["content"])
        assert parsed["passed"] and parsed["step_progress"]["done"] == 1
        assert state["verify_passed"]


def test_wrong_step_expectation_is_revised_in_sqlite_without_changing_correct_source():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        folder = Path(item["folder"])
        source = folder / "calc.py"
        source.write_text("def double(n): return n*2\n")
        original = source.read_bytes()
        step = {"phase": "Core", "title": "Check double", "check_tool": "check_python_function",
                "check_arguments": {"path": "calc.py", "function": "double",
                                    "cases": [{"args": [2], "expected": 6}]}}
        queue = task_context.plan_steps(folder, [step])
        step_id = queue["active"]["id"]
        state = folder / ".local-ai/test-supervisor"
        state.mkdir()
        with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
            failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": step_id}))
            assert not failed["passed"]
            assert f"step_id={step_id}" in failed["next_action"] and "SQLite" in failed["next_action"]
            assert "authoritative" in task_context.snapshot(folder)
            step["check_arguments"]["cases"][0]["expected"] = 4
            revised = json.loads(native_agent.execute_tool(folder, "task_steps",
                                {"action": "revise", "step_id": step_id, "steps": [step]}))
            assert revised["active"]["check_arguments"]["cases"][0]["expected"] == 4
            passed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": step_id}))
            assert passed["passed"] and passed["verifier_check_passed"]
        assert source.read_bytes() == original


def test_real_failed_step_checks_route_deeper_after_history_compaction_and_reset_on_pass():
    from auto_route import repeated_tool_failures, task_size

    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        item["brief"] = "Fix the small function in calc.py."
        folder = Path(item["folder"])
        source = folder / "calc.py"
        source.write_text("def double(n): return n*3\n")
        check = {"path": "calc.py", "function": "double", "cases": [{"args": [2], "expected": 4}]}
        queue = task_context.plan_steps(folder, [{"phase": "Core", "title": "Fix double",
                "check_tool": "check_python_function", "check_arguments": check}])
        step_id = queue["active"]["id"]
        messages = [{"role": "system", "content": native_agent.prompt(item)}]
        for number in range(8):
            messages += [{"role": "assistant", "tool_calls": [{"id": str(number),
                         "function": {"name": "read_file", "arguments": '{"path":"calc.py"}'}}]},
                         {"role": "tool", "tool_call_id": str(number), "content": "Inspected source"}]
        state_folder = folder / ".local-ai/test-supervisor"
        state_folder.mkdir()
        with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state_folder):
            for attempt in range(2):
                failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": step_id}))
                assert not failed["passed"]
                fresh = native_agent.fresh_messages(messages, folder, project=item)
                assert sum(message.get("role") == "tool" for message in fresh) == 2
                assert repeated_tool_failures(fresh) is (attempt == 1)
            assert task_size(fresh) == "deep"
            # A long checkpoint is clipped, but its controller counters stay
            # before every untrusted project excerpt.
            fresh[1]["content"] += "\nUntrusted notes: " + "old notes " * 15000
            compacted = native_agent.compact_messages(fresh, folder, 12000)
            assert repeated_tool_failures(compacted)
            source.write_text("def double(n): return n*2\n")
            passed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": step_id}))
            assert passed["passed"]
            recovered = native_agent.fresh_messages(messages, folder, project=item)
            assert not repeated_tool_failures(recovered)
            assert task_size(recovered) == "balanced"


def test_step_strings_decode_strict_json_and_report_format_errors_without_creating_work(tmp_path):
    import pytest

    item = project(tmp_path / "steps")
    folder = Path(item["folder"])
    step = {"phase": "Check", "title": "Run checks", "check_tool": "run_verifier"}
    queue = task_context.plan_steps(folder, json.dumps([step]))
    assert queue["total"] == 1 and queue["active"]["title"] == "Run checks"
    for bad in ('[{\\"phase\\":\\"Check\\"}]', '{"phase":"Check"}', '[]'):
        with pytest.raises(ValueError):
            task_context.revise_step(folder, queue["active"]["id"], bad)
        assert task_context.step_status(folder)["total"] == 1
    with pytest.raises(ValueError, match="Malformed steps tool argument"):
        task_context.revise_step(folder, queue["active"]["id"], '[{\\"phase\\":\\"Check\\"}]')
    with pytest.raises(ValueError, match="top level"):
        task_context.revise_step(folder, queue["active"]["id"], [{**step, "step_id": 1}])


def test_step_tool_advertises_real_array_while_legacy_json_strings_keep_exact_quotes(tmp_path):
    import pytest
    from jsonschema import ValidationError, validate

    tool = next(tool["function"] for tool in native_agent.TOOLS if tool["function"]["name"] == "task_steps")
    command = 'python -c "print(\'quoted value\')"'
    step = {"phase": "Check", "title": "Keep quoting", "check_command": command}
    validate({"action": "plan", "steps": [step]}, tool["parameters"])
    with pytest.raises(ValidationError):
        validate({"action": "plan", "steps": json.dumps([step])}, tool["parameters"])
    folder = Path(project(tmp_path / "legacy-quoted")["folder"])
    active = task_context.plan_steps(folder, json.dumps([step]))["active"]
    assert active["check_command"] == command


def test_failed_initial_plan_blocks_completion_and_survives_fresh_context_until_real_plan(tmp_path):
    item = project(tmp_path / "planned")
    folder = Path(item["folder"])
    (folder / "hello.txt").write_text('Hello')
    (folder / ".local-ai/verify.ps1").write_text('exit 0\n')
    (folder / ".local-ai/PLAN.md").write_text('- [x] Finished\n')
    (folder / ".local-ai/PROGRESS.md").write_text('Work completed and the saved verifier passed.')
    native_agent.save_state(item, {"status": "continue", "verify_passed": True})
    bad = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "plan", "function": {
        "name": "task_steps", "arguments": json.dumps({"action": "plan", "steps": [{"title": "Missing phase"}]})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: bad)
    assert state["status"] == "continue" and state["uncreated_step_plan"]
    assert "initial SQLite plan was not created" in native_agent.completion_issue(folder, item, state)
    fresh = native_agent.fresh_messages(state["messages"], folder, state, item)
    assert "nonempty phase" in fresh[1]["content"]
    final = {"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        rejected = native_agent.run_slice(item, complete=lambda _: final)
    assert rejected["status"] == "continue"
    good = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "fixed-plan", "function": {
        "name": "task_steps", "arguments": json.dumps({"action": "plan", "steps": [
            {"phase": "Verify", "title": "Run checks", "check_tool": "run_verifier"},
            {"phase": "Handoff", "title": "Check final work", "check_tool": "run_verifier"}]})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        repaired = native_agent.run_slice(item, complete=lambda _: good)
    assert "uncreated_step_plan" not in repaired and task_context.step_status(folder)["total"] == 2
    pending = {"uncreated_step_plan": {"request_hash": task_context.request_identity(folder), "error": "old"}}
    native_agent.invalidate_checks(pending)
    assert "uncreated_step_plan" not in pending


def test_local_repair_feedback_survives_reads_restart_aliases_and_resets_on_verified_progress():
    state = {}
    identity = "f" * 64
    for model in ("main", "fast"):
        native_agent.remember_local_tool_feedback(state, model, "a" * 64, identity, failed=True, recovered=False)
    copied = json.loads(json.dumps(state))
    native_agent.remember_local_tool_feedback(copied, "fast", "a" * 64, identity, failed=False, recovered=False)
    assert native_agent.repair_avoid_models(copied, identity) == [{"model": "fast", "weights": "a" * 64}]
    assert native_agent.repair_avoid_models(copied, "e" * 64) == []
    native_agent.remember_local_tool_feedback(copied, None, "", identity, failed=False, recovered=True)
    assert not native_agent.repair_avoid_models(copied, identity)
    native_agent.remember_local_tool_feedback(copied, "fast", "a" * 64, identity, failed=True, recovered=False)
    native_agent.invalidate_checks(copied)
    assert "local_repair_feedback" not in copied


def test_native_bare_step_completion_runs_real_check_and_never_advances_second_unit_in_batch(tmp_path):
    item = project(tmp_path / "current-step")
    folder = Path(item["folder"])
    queue = task_context.plan_steps(folder, [
        {"phase": "Build", "title": "First unit", "check_command": "exit 0"},
        {"phase": "Build", "title": "Second unit", "check_command": "exit 0"}])
    first = queue["active"]["id"]
    def complete(payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": str(i), "function": {"name": "task_steps", "arguments": '{"action":"complete"}'}}
            for i in range(2)]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=complete)
    queue = task_context.step_status(folder)
    assert queue["done"] == 1 and queue["remaining"] == 1 and queue["active"]["id"] != first
    replies = [message["content"] for message in state["messages"] if message.get("role") == "tool"]
    assert json.loads(replies[0])["checked_step_id"] == first
    assert "step queue advanced" in replies[1]
    # Direct/public dispatch still requires a deliberate target ID.
    assert native_agent.execute_tool(folder, "task_steps", {"action": "complete"}).startswith("Tool error:")


def test_native_bare_step_completion_cannot_pass_failed_check_or_correct_wrong_explicit_id(tmp_path):
    for suffix, args in [("failed", {"action": "complete"}), ("wrong-id", {"action": "complete", "step_id": 999})]:
        item = project(tmp_path / suffix)
        folder = Path(item["folder"])
        task_context.plan_steps(folder, [{"phase": "Build", "title": "Failing check", "check_command": "exit 7"}])
        def complete(payload):
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": suffix,
                "function": {"name": "task_steps", "arguments": json.dumps(args)}}]}}]}
        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=complete)
        queue = task_context.step_status(folder)
        assert queue["done"] == 0 and queue["remaining"] == 1
        assert queue["active"]["attempts"] == (1 if suffix == "failed" else 0)
        reply = next(message["content"] for message in reversed(state["messages"]) if message.get("role") == "tool")
        assert not json.loads(reply)["passed"] if suffix == "failed" else "step_id" in reply


def test_bare_step_binding_rejects_revised_acceptance_and_new_requests(tmp_path):
    import pytest

    folder = Path(project(tmp_path / "binding")["folder"])
    original = {"phase": "Build", "title": "First", "check_command": "exit 0"}
    active = task_context.plan_steps(folder, [original])["active"]
    request = task_context.request_identity(folder)
    bound = native_agent.bind_step_completion(folder, {"action": "complete"}, active, request)
    task_context.revise_step(folder, active["id"], [{**original, "check_command": "exit 9"}])
    with pytest.raises(ValueError, match="acceptance check changed"):
        native_agent.bind_step_completion(folder, {"action": "complete"}, active, request)
    rejected = native_agent.execute_tool(folder, "task_steps", bound)
    assert "acceptance check changed" in rejected
    assert task_context.step_status(folder)["active"]["attempts"] == 0
    (folder / ".local-ai/USER-REQUESTS.md").write_text("New instructions")
    with pytest.raises(ValueError, match="unchanged active step"):
        native_agent.bind_step_completion(folder, {"action": "complete"}, active, request)


def test_step_acceptance_revised_during_check_cannot_receive_older_success(tmp_path):
    folder = Path(project(tmp_path / "racing-check")["folder"])
    original = {"phase": "Build", "title": "First", "check_command": "exit 0"}
    active = task_context.plan_steps(folder, [original])["active"]
    dispatch = native_agent.execute_tool
    def revise_while_running(folder, name, arguments):
        if name == "run_command":
            task_context.revise_step(folder, active["id"], [{**original, "check_command": "exit 9"}])
            return "Exit code: 0\nOld acceptance passed"
        return dispatch(folder, name, arguments)
    with patch.object(native_agent, "execute_tool", side_effect=revise_while_running):
        result = native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]})
    assert "acceptance check changed while it ran" in result
    queue = task_context.step_status(folder)
    assert queue["done"] == 0 and queue["active"]["attempts"] == 0
    assert queue["active"]["check_command"] == "exit 9"


def test_native_bare_step_accepts_a_source_edit_but_rejects_acceptance_revision_during_inference(tmp_path):
    for revised in (False, True):
        item = project(tmp_path / ("revised" if revised else "source-edit"))
        folder = Path(item["folder"])
        step = {"phase": "Build", "title": "Write greeting", "check_command": "findstr /x World hello.txt"}
        active = task_context.plan_steps(folder, [step])["active"]
        def complete(payload):
            if revised:
                task_context.revise_step(folder, active["id"], [{**step, "check_command": "exit 9"}])
            calls = [{"id": "write", "function": {"name": "write_file", "arguments":
                json.dumps({"path": "hello.txt", "content": "World\n"})}},
                {"id": "check", "function": {"name": "task_steps", "arguments": '{"action":"complete"}'}}]
            return {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}
        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=complete)
        queue = task_context.step_status(folder)
        assert queue["done"] == (0 if revised else 1)
        assert (folder / "hello.txt").read_text() == "World\n"
        if revised:
            assert queue["active"]["attempts"] == 0
            assert "acceptance check changed" in state["messages"][-1]["content"]


def test_auto_recorded_acceptance_cannot_advance_a_check_revised_during_execution(tmp_path):
    item = project(tmp_path / "auto-check-race")
    folder = Path(item["folder"])
    step = {"phase": "Build", "title": "First", "check_command": "exit 0"}
    active = task_context.plan_steps(folder, [step])["active"]
    def complete(payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "check",
            "function": {"name": "run_command", "arguments": '{"command":"exit 0"}'}}]}}]}
    dispatch = native_agent.execute_tool
    def revise(folder, name, arguments):
        if name == "run_command":
            task_context.revise_step(folder, active["id"], [{**step, "check_command": "exit 9"}])
            return "Exit code: 0\nOld check passed"
        return dispatch(folder, name, arguments)
    with (patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1),
          patch.object(native_agent, "execute_tool", side_effect=revise)):
        state = native_agent.run_slice(item, complete=complete)
    assert task_context.step_status(folder)["done"] == 0
    assert "acceptance check changed while it ran" in state["messages"][-1]["content"]


def test_native_local_repair_feedback_changes_next_router_call_across_slices(tmp_path):
    item = project(tmp_path / "routing")
    folder = Path(item["folder"])
    (folder / "hello.txt").write_text('Hello')
    (folder / ".local-ai/verify.ps1").write_text('exit 0\n')
    observed = []
    class RouterStub:
        def complete(self, payload, task_id, **kwargs):
            avoid = kwargs.get("repair_avoid", [])
            self.last_local_model = "alternate" if avoid else "fast"
            self.last_local_weights = ("b" if avoid else "a") * 64
            number = len(observed)
            observed.append((self.last_local_model, avoid))
            name, arguments = (("read_file", {"path": "missing-one.txt"}),
                               ("read_file", {"path": "AGENTS.md"}),
                               ("read_file", {"path": "missing-two.txt"}),
                               ("run_verifier", {}))[min(number, 3)]
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": str(number),
                "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}
    state_folder = folder / ".local-ai/test-supervisor"
    state_folder.mkdir()
    with patch("cloud_ai.Router", RouterStub), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 3):
        state = native_agent.run_slice(item)
    assert len(observed) == 3 and state["local_repair_feedback"]
    assert observed[1][1] == []  # A successful read did not clear the first failure.
    with (patch("cloud_ai.Router", RouterStub), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1),
          patch.object(task_context, "checker_failures", return_value=[{"path": "other.html", "passed": False}]),
          patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state_folder)):
        pending = native_agent.run_slice(item)
    assert pending["local_repair_feedback"]  # An unrelated outstanding check prevents reset.
    with (patch("cloud_ai.Router", RouterStub), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1),
          patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state_folder)):
        recovered = native_agent.run_slice(item)
    assert observed[3] == ("alternate", [{"model": "fast", "weights": "a" * 64}])
    assert "local_repair_feedback" not in recovered


def test_legacy_step_database_migration_preserves_commands_and_failed_attempts():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "code")["folder"])
        project_memory.save(folder, "decision", "Keep architecture", "Retain earlier design decisions")
        with project_memory.connection(folder) as connection:
            connection.execute("""CREATE TABLE task_steps(id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_hash TEXT NOT NULL,position INTEGER NOT NULL,phase TEXT NOT NULL,title TEXT NOT NULL,
                check_command TEXT NOT NULL,shell TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                evidence TEXT NOT NULL DEFAULT '',updated REAL NOT NULL)""")
            connection.execute("""INSERT INTO task_steps(request_hash,position,phase,title,check_command,shell,status,attempts,updated)
                VALUES(?,1,'Core','Old step','exit 3','default','active',2,?)""", (task_context.request_identity(folder), time.time()))
        active = task_context.step_status(folder)["active"]
        assert active["check_command"] == "exit 3" and active["attempts"] == 2
        assert active["check_tool"] == "run_command" and active["check_arguments"] == {}
        assert project_memory.search(folder, "architecture")


def test_structured_step_rejects_unknown_execution_and_ambiguous_checks():
    import pytest

    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "code")["folder"])
        for fields in ({"check_tool": "run_command", "check_arguments": {"command": "bad"}},
                       {"check_tool": "call_local_tool", "check_arguments": {}},
                       {"check_tool": "check_python_function", "check_arguments": {}},
                       {"check_tool": "run_verifier", "check_command": "exit 0"}):
            with pytest.raises(ValueError):
                task_context.plan_steps(folder, [{"phase": "Core", "title": "A step", **fields}])


def test_resumed_worker_refreshes_owned_instructions_and_retains_work_history():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "code")
        item["brief"] = "Repair double(n) in calc.py"
        folder = Path(item["folder"])
        (folder / "calc.py").write_text("def double(n): return n*2\n")
        native_agent.save_state(item, {"status": "continue", "turns": 5, "messages": [
            {"role": "system", "content": "You are Cale's native project worker. Obsolete old instructions"},
            {"role": "assistant", "content": "A useful completed observation"}]})
        seen = []
        def complete(payload):
            seen.append(payload)
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "read", "function": {
                "name": "read_file", "arguments": '{"path":"calc.py"}'}}]}}]}
        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            native_agent.run_slice(item, complete=complete)
        assert "check_python_function" in seen[0]["messages"][0]["content"]
        assert item["brief"] in seen[0]["messages"][0]["content"]
        assert "Obsolete old instructions" not in str(seen)
        assert "A useful completed observation" in str(seen)


def test_native_worker_writes_files_and_checkpoints_tool_results():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        (Path(item["folder"]) / ".local-ai/verify.ps1").write_text("exit 0")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "write-1", "type": "function", "function": {
                    "name": "write_file", "arguments": '{"path":"index.html","content":"<h1>Ready</h1>"}'
                }}]}}]},
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "check-1", "type": "function", "function": {
                    "name": "run_command",
                    "arguments": '{"command":"powershell -File .local-ai/verify.ps1"}',
                },
            }]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Page is ready."}}]},
        ])
        original = native_agent.execute_tool

        def tool(root, name, args):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args)

        with patch.object(native_agent, "execute_tool", side_effect=tool):
            state = native_agent.run_slice(item, complete=lambda _: next(responses))
        assert state["status"] == "done"
        assert (Path(item["folder"]) / "index.html").read_text() == "<h1>Ready</h1>"
        assert any(message.get("tool_call_id") == "write-1" for message in state["messages"])
        assert native_agent.read_state(item)["status"] == "done"


def test_worker_cannot_finish_before_its_verifier_passes():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        (folder / ".local-ai/PLAN.md").write_text("# Plan\n\n- [x] Repair the page\n")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "content": "Finished."}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Finished."}}]},
        ])
        state = native_agent.run_slice(item, complete=lambda _: next(responses))
        assert state["status"] == "continue"
        assert not state.get("verify_passed")
        assert "A created verifier is not evidence" in state["messages"][-1]["content"]


def test_run_command_timeout_stops_child_process_and_returns():
    """A shell child must not hold the worker's output pipes open forever."""
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "hang.py").write_text(
            "import os, time\n"
            "open('child.pid', 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n", encoding="utf-8")
        started = time.monotonic()
        with patch.object(native_agent, "PROJECT_COMMAND_TIMEOUT", 1):
            result = native_agent.execute_tool(
                folder, "run_command", {"command": "python hang.py"})
        assert "timed out after 1 seconds" in result
        assert time.monotonic() - started < 8
        import psutil

        child_pid = int((folder / "child.pid").read_text())
        assert not psutil.pid_exists(child_pid)


def test_run_command_accepts_quoted_executable_path_and_shell_operators():
    if os.name != "nt":
        return  # This regression concerns cmd.exe quoting on Windows.
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        spaced = folder / "program with spaces"
        spaced.mkdir()
        batch = spaced / "hello.cmd"
        batch.write_text("@echo off\necho quoted path works\n", encoding="utf-8")
        version = native_agent.execute_tool(
            folder, "run_command", {"command": f'"{sys.executable}" -V'})
        quoted = native_agent.execute_tool(
            folder, "run_command", {"command": f'"{batch}"'})
        chained = native_agent.execute_tool(
            folder, "run_command", {"command": "echo first && echo second"})
    assert version.startswith("Exit code: 0\nPython ")
    assert quoted.startswith("Exit code: 0\nquoted path works")
    assert chained.startswith("Exit code: 0")
    assert "first" in chained and "second" in chained


def test_powershell_commands_preserve_literals_pipelines_and_project_directory():
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory(prefix="Cale command space ") as temporary:
        folder = Path(temporary)
        (folder / "source.txt").write_text("source", encoding="utf-8")
        with patch.dict(os.environ, {"PSModulePath": str(folder / "missing-modules")}):
            result = native_agent.execute_tool(folder, "run_command", {
                "shell": "powershell",
                "command": "$value='literal $value | café'; Write-Output $value; "
                           "Get-FileHash -LiteralPath 'source.txt' | Select-Object -ExpandProperty Hash",
            })
        assert result.startswith("Exit code: 0\n"), result
        assert "literal $value | café" in result
        assert hashlib.sha256(b"source").hexdigest().upper() in result


def test_powershell_cmdlet_errors_and_native_exit_codes_are_failures():
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        missing = native_agent.execute_tool(folder, "run_command", {
            "shell": "powershell", "command": "Get-Item 'does-not-exist.txt'; Set-Content 'false-success.txt' 'bad'"})
        assert missing.startswith("Exit code: 1\n"), missing
        assert not (folder / "false-success.txt").exists()
        assert "#< CLIXML" not in missing
        failed = native_agent.execute_tool(folder, "run_command", {
            "shell": "powershell",
            "command": "& '" + sys.executable.replace("'", "''") + "' -c 'import sys; sys.exit(7)'"})
        assert failed.startswith("Exit code: 7\n"), failed


def test_powershell_parser_errors_are_readable_and_explain_shell_mismatch():
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory() as temporary:
        result = native_agent.execute_tool(Path(temporary), "run_command", {
            "shell": "powershell", "command": "Write-Output first && Write-Output second"})
        assert result.startswith("Exit code: 1\n"), result
        assert "not a valid statement separator" in result
        assert "#< CLIXML" not in result
        assert "_x000D_" not in result
        assert "semicolons instead of &&" in result


def test_partial_powershell_error_xml_is_preserved_instead_of_hidden():
    output = "#< CLIXML\n<Objs><S S='Error'>incomplete"
    assert native_agent.readable_powershell_error(output) == output
    assert native_agent.readable_powershell_error("ordinary stderr") == "ordinary stderr"


def test_powershell_error_emoji_and_literal_escape_text_remain_checkpoint_safe():
    serialized = ('#< CLIXML\n<Objs><S S="Error">Bad &amp; token '
                  '_xD83D__xDE80_ _x005F_x000A_ _xD800_</S></Objs>')
    message = native_agent.readable_powershell_error(serialized)
    assert message == "Bad & token \U0001f680 _x000A_ \ufffd"
    assert json.dumps({"content": message}, ensure_ascii=False).encode("utf-8")


def test_run_command_rejects_unknown_shell_before_execution():
    with tempfile.TemporaryDirectory() as temporary:
        with patch.object(native_agent.subprocess, "Popen") as launch:
            result = native_agent.execute_tool(Path(temporary), "run_command", {
                "shell": "unknown", "command": "echo hello"})
        assert result.startswith("Tool error: ValueError: Choose shell"), result
        launch.assert_not_called()


def test_legacy_verifier_command_rejects_output_changes():
    """A verifier cannot pass by silently regenerating the requested file."""
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        notes = folder / ".local-ai"
        notes.mkdir()
        output = folder / "result.txt"
        output.write_text("before", encoding="utf-8")
        verifier = notes / "verify.ps1"
        verifier.write_text(
            "Set-Content -Path result.txt -Value after\nexit 0\n", encoding="utf-8")
        changed = native_agent.execute_tool(
            folder, "run_command", {"command": "powershell -File .local-ai/verify.ps1"})
        assert changed.startswith("Exit code: 1\nProject files changed")
        assert output.read_text(encoding="utf-8").strip() == "after"
        verifier.write_text(
            "if ((Get-Content result.txt) -ne 'after') { exit 1 }\nexit 0\n",
            encoding="utf-8")
        checked = native_agent.execute_tool(
            folder, "run_command", {"command": "powershell -File .local-ai/verify.ps1"})
        assert checked.startswith("Exit code: 0\n")


def test_csv_task_prompt_recommends_a_parser_and_read_only_verifier():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "table")
        item["brief"] = "Clean a CSV file"
        instruction = native_agent.prompt(item)
        assert "use transform_csv" in instruction
        assert "Python's csv module" in instruction
        assert "verifier must only inspect files" in instruction


def test_transform_csv_cleans_rows_without_changing_source():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        source = folder / "contacts.csv"
        original = ("name,phone,city\n"
                    '"  avery LEE  ","(517) 555-0101"," lansing "\n'
                    '"Avery Lee","5175550101","Lansing"\n'
                    '"Sam Patel","bad","Ann Arbor"\n')
        source.write_text(original, encoding="utf-8")
        result = native_agent.execute_tool(folder, "transform_csv", {
            "input_path": "contacts.csv", "output_path": "cleaned_contacts.csv",
            "transforms": {"name": "title", "phone": "digits", "city": "title"},
            "exact_lengths": {"phone": 10}, "unique_by": ["phone"],
        })
        assert json.loads(result)["kept"] == 1
        with (folder / "cleaned_contacts.csv").open(newline="", encoding="utf-8") as stream:
            assert list(csv.DictReader(stream)) == [
                {"name": "Avery Lee", "phone": "5175550101", "city": "Lansing"}]
        assert source.read_text(encoding="utf-8") == original
        verify_command = {"command": "powershell -File .local-ai/verify.ps1"}
        assert native_agent.execute_tool(folder, "run_command", verify_command).startswith(
            "Exit code: 0\n")
        helper = folder / ".local-ai" / "verify-transform-csv.py"
        assert helper.is_file()
        prior_hash = hashlib.sha256((folder / "cleaned_contacts.csv").read_bytes()).hexdigest().upper()
        wrong_row = "name,phone,city\nWrong Name,5175550101,Lansing\n"
        (folder / "cleaned_contacts.csv").write_text(wrong_row, encoding="utf-8")
        # Even if the expected output hash is updated, the separate check
        # must reject rows that violate the saved transform rules.
        new_hash = hashlib.sha256((folder / "cleaned_contacts.csv").read_bytes()).hexdigest().upper()
        helper.write_text(helper.read_text(encoding="utf-8").replace(prior_hash, new_hash),
                          encoding="utf-8")
        semantic_result = subprocess.run([sys.executable, str(helper)],
                                         capture_output=True, text=True)
        assert semantic_result.returncode != 0
        assert "do not match" in semantic_result.stderr
        (folder / "cleaned_contacts.csv").write_text("corrupted", encoding="utf-8")
        assert native_agent.execute_tool(folder, "run_command", verify_command).startswith(
            "Exit code: 1\n")


def test_transform_csv_failure_preserves_existing_output():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        (folder / "contacts.csv").write_text("name,phone\nAvery,123,extra\n", encoding="utf-8")
        output = folder / "cleaned_contacts.csv"
        output.write_text("saved result", encoding="utf-8")
        result = native_agent.execute_tool(folder, "transform_csv", {
            "input_path": "contacts.csv", "output_path": "cleaned_contacts.csv",
            "transforms": {"phone": "digits"},
        })
        assert result.startswith("Tool error: ValueError: CSV row 1")
        assert output.read_text(encoding="utf-8") == "saved result"


def test_transform_csv_verifier_handles_nested_paths_and_preserves_custom_check():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        source = folder / "data" / "team's.csv"
        source.parent.mkdir()
        source.write_text("name,phone\navery,517-555-0101\n", encoding="utf-8-sig")
        arguments = {
            "input_path": "data/team's.csv",
            "output_path": "results/cleaned's.csv",
            "transforms": {"name": "title", "phone": "digits"},
            "exact_lengths": {"phone": 10},
        }
        result = native_agent.execute_tool(folder, "transform_csv", arguments)
        assert json.loads(result)["kept"] == 1
        verifier = folder / ".local-ai" / "verify.ps1"
        assert native_agent.execute_tool(folder, "run_command", {
            "command": "powershell -File .local-ai/verify.ps1"}).startswith("Exit code: 0\n")
        verifier.write_text("Write-Output 'my custom check'\nexit 0\n", encoding="utf-8")
        native_agent.execute_tool(folder, "transform_csv", arguments)
        assert verifier.read_text(encoding="utf-8") == "Write-Output 'my custom check'\nexit 0\n"


def test_simple_csv_request_uses_structured_tool_before_general_worker():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "table")
        folder = Path(item["folder"])
        item["brief"] = ("Create cleaned.csv from contacts.csv. Trim and title-case "
                         "names and cities, normalize phones to 10 digits, and "
                         "keep the first duplicate phone.")
        (folder / "contacts.csv").write_text(
            "name,phone,city\navery lee,(517) 555-0101,lansing\n",
            encoding="utf-8")
        calls = []

        def complete(payload):
            calls.append(payload)
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "csv-1", "function": {"name": "transform_csv", "arguments": json.dumps({
                    "input_path": "contacts.csv", "output_path": "cleaned.csv",
                    "transforms": {"name": "title", "phone": "digits", "city": "title"},
                    "exact_lengths": {"phone": 10}, "unique_by": ["phone"],
                })}}]}}]}

        messages, state = native_agent.start_csv_cleanup(
            item, folder, complete, [], {"turns": 0})
        assert len(calls) == 1
        assert [tool["function"]["name"] for tool in calls[0]["tools"]] == ["transform_csv"]
        assert state["verify_passed"] is True
        assert state["turns"] == 1
        assert "verifier returned: Exit code: 0" in messages[-1]["content"]
        assert "Avery Lee,5175550101,Lansing" in (
            folder / "cleaned.csv").read_text(encoding="utf-8")


def test_csv_fast_path_skips_unsupported_or_existing_work():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "table")
        folder = Path(item["folder"])
        (folder / "source.csv").write_text("name\nAvery\n", encoding="utf-8")
        item["brief"] = "Create output.csv from source.csv and sort by name."
        assert native_agent.csv_cleanup_candidate(item, folder) is None
        item["brief"] = "Create output.csv from source.csv and clean the names."
        assert native_agent.csv_cleanup_candidate(item, folder) == (
            "output.csv", "source.csv")
        (folder / "output.csv").write_text("existing", encoding="utf-8")
        assert native_agent.csv_cleanup_candidate(item, folder) is None
        (folder / "output.csv").unlink()
        (folder / "AGENTS.md").write_text("Never change files.\n", encoding="utf-8")
        assert native_agent.csv_cleanup_candidate(item, folder) is None
        (folder / "AGENTS.md").unlink()
        (folder / ".local-ai/USER-REQUESTS.md").write_text(
            "Actually, sort the result.\n", encoding="utf-8")
        assert native_agent.csv_cleanup_candidate(item, folder) is None


def test_csv_fast_path_provider_failure_returns_to_general_worker():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "table")
        folder = Path(item["folder"])
        item["brief"] = "Create output.csv from source.csv and clean the names."
        (folder / "source.csv").write_text("name\navery\n", encoding="utf-8")
        messages = [{"role": "user", "content": "Begin"}]

        def fail(_payload):
            raise RuntimeError("Provider unavailable")

        result_messages, state = native_agent.start_csv_cleanup(
            item, folder, fail, messages, {"turns": 0})
        assert result_messages == messages
        assert state["turns"] == 0
        assert not (folder / "output.csv").exists()


def test_interactive_html_completion_requires_fresh_browser_check():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        item["brief"] = "Build a single-file index.html app with an input that updates a total."
        assert "call check_webapp" in native_agent.prompt(item)
        page = folder / "index.html"
        page.write_text("<input id='number'><p id='total'>1</p>", encoding="utf-8")
        (folder / ".local-ai/verify.ps1").write_text("exit 0", encoding="utf-8")
        (folder / ".local-ai/PLAN.md").write_text("- [x] Build and verify\n", encoding="utf-8")
        state = {"verify_passed": True}
        assert "Run check_webapp" in native_agent.completion_issue(folder, item, state)
        state.update(browser_verified=True, browser_checked_path="index.html",
                     browser_checked_hash=hashlib.sha256(page.read_bytes()).hexdigest(),
                     browser_interactions=[{"case": 1, "targets": ["text:total"], "actions": ["set"]}])
        assert native_agent.completion_issue(folder, item, state) == ""
        page.write_text(page.read_text(encoding="utf-8") + "<!-- changed -->", encoding="utf-8")
        assert "Run check_webapp" in native_agent.completion_issue(folder, item, state)


def test_interactive_page_cannot_replace_generated_browser_verifier():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        item["brief"] = "Build a single-file index.html app with inputs that update a total."
        folder = Path(item["folder"])
        verifier = folder / ".local-ai/verify.ps1"
        arguments = {"path": ".local-ai/verify.ps1", "content": "exit 0"}
        issue = native_agent.browser_verifier_write_issue(
            folder, item, "write_file", arguments)
        assert "Run check_webapp" in issue
        verifier.write_text("# Generated by Cale check_webapp\nexit 1", encoding="utf-8")
        assert "Preserve" in native_agent.browser_verifier_write_issue(
            folder, item, "edit_file", arguments)
        verifier.write_text("# User's custom verifier\nexit 1", encoding="utf-8")
        assert native_agent.browser_verifier_write_issue(
            folder, item, "edit_file", arguments) == ""


def test_native_worker_rejects_premature_browser_verifier_write():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        item["brief"] = "Build a single-file index.html app with inputs that update a total."
        response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": "early-verifier", "function": {"name": "write_file", "arguments": json.dumps({
                "path": ".local-ai/verify.ps1", "content": "exit 0",
            })},
        }]}}]}
        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=lambda _: response)
        assert not (Path(item["folder"]) / ".local-ai/verify.ps1").exists()
        assert any("Run check_webapp" in message.get("content", "")
                   for message in state["messages"] if message["role"] == "tool")


def test_browser_check_evidence_accepts_json_encoded_cases():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        page = folder / "index.html"
        page.write_text("<input id='bill'><p id='total'>24.00</p>", encoding="utf-8")
        arguments = {"path": "index.html", "cases": (
            '[{"set":{"bill":"80"},"expect":{"total":"24.00"}},'
            '{"set":{"bill":"0"},"visible":["error"]}]')}
        evidence = native_agent.browser_check_evidence(
            folder, arguments, json.dumps({"passed": True,
                "page_sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
                "interactions": [{"case": 1, "actions": ["set"], "targets": ["text:total"]}]}))
        assert evidence["browser_verified"] is True
        assert evidence["browser_checked_hash"] == hashlib.sha256(page.read_bytes()).hexdigest()
        assert native_agent.browser_check_evidence(folder, arguments, '{"passed":false}') == {}


def test_html_game_cannot_finish_with_static_hud_or_input_only_claims(tmp_path):
    item = project(tmp_path / "game")
    item["brief"] = "Make a simple space shooter game"
    folder=Path(item["folder"])
    page=folder/"game.html"
    page.write_text('<canvas></canvas><p id="score">0</p><p id="position">center</p>')
    (folder/".local-ai/verify.ps1").write_text("exit 0")
    (folder/".local-ai/PLAN.md").write_text("- [x] Built\n")
    assert native_agent.game_browser_required(item)
    state={"verify_passed": True, "browser_verified": True, "browser_checked_path": "game.html",
           "browser_checked_hash": hashlib.sha256(page.read_bytes()).hexdigest()}
    assert "Run check_webapp" in native_agent.completion_issue(folder,item,state)
    state["browser_interactions"]=[{"case":1,"targets":["text:score"],"actions":["keys"]}]
    assert "at least two" in native_agent.completion_issue(folder,item,state)
    state["browser_interactions"].append({"case":2,"targets":["visibility:score"],"actions":["click"]})
    assert "at least two" in native_agent.completion_issue(folder,item,state)
    state["browser_interactions"][1]["targets"]=["text:position"]
    assert native_agent.completion_issue(folder,item,state)==""
    item["brief"]="Write a game design document"
    assert not native_agent.game_browser_required(item)


def test_browser_evidence_requires_measured_changes_matching_current_page(tmp_path):
    page = tmp_path / "game.html"
    page.write_text('<p id="score">0</p>')
    arguments={"path":"game.html","cases":[
        {"keys":[{"key":"Space"}],"expect":{"score":"0"}},
        {"keys":[{"key":"Enter"}],"expect":{"score":"0"}}]}
    verdict={"passed":True,"page_sha256":hashlib.sha256(page.read_bytes()).hexdigest(),"interactions":[]}
    assert native_agent.browser_check_evidence(tmp_path,arguments,json.dumps(verdict))=={}
    verdict["interactions"]=[{"case":1,"targets":["text:other"],"actions":["keys"]}]
    assert native_agent.browser_check_evidence(tmp_path,arguments,json.dumps(verdict))=={}
    verdict["interactions"][0]["targets"]=["text:score"]
    assert native_agent.browser_check_evidence(tmp_path,arguments,json.dumps(verdict))["browser_verified"]
    page.write_text('<p id="score">broken</p>')
    assert native_agent.browser_check_evidence(tmp_path,arguments,json.dumps(verdict))=={}


def test_new_instruction_invalidates_saved_browser_and_verifier_checks():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        item["brief"] = "Build a single-file index.html app with an input that updates a total."
        folder = Path(item["folder"])
        page = folder / "index.html"
        page.write_text("<input id='bill'><p id='total'>30</p>", encoding="utf-8")
        (folder / ".local-ai/verify.ps1").write_text("exit 0", encoding="utf-8")
        (folder / ".local-ai/PLAN.md").write_text("- [x] Build and verify\n", encoding="utf-8")
        native_agent.save_state(item, {"status": "done", "verify_passed": True,
                                       "browser_verified": True, "browser_checked_path": "index.html",
                                       "browser_checked_hash": hashlib.sha256(page.read_bytes()).hexdigest()})
        with patch.object(long_tasks, "update"):
            assert native_agent.apply_request(item, "note", "Also add a reset button") == "Applied"
        state = native_agent.read_state(item)
        assert state["status"] == "continue"
        assert state["verify_passed"] is False
        assert state["browser_verified"] is False
        assert "Run check_webapp" in native_agent.completion_issue(folder, item, state)


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
        native_agent.save_state(item, {"status": "continue", "verify_passed": True})
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
        (Path(item["folder"]) / "index.html").write_text("<h1>Ready</h1>")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "content": "Finished."}}]},
            {"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "verify-1", "type": "function", "function": {
                    "name": "write_file", "arguments":
                    '{"path":".local-ai/verify.ps1","content":"if (-not (Test-Path index.html)) { exit 1 }"}'
                }}]}}]},
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "run-verify-1", "function": {"name": "run_command",
                    "arguments": '{"command":"powershell -File .local-ai/verify.ps1"}'}}]}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Finished."}}]},
        ])
        seen = []
        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return next(responses)
        state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "done"
        assert len(seen) == 4
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
        native_agent.save_state(item, {"status": "continue", "verify_passed": True})
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


def test_direct_verifier_tool_runs_registered_project_and_hands_off():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0", encoding="utf-8")
        (notes / "PLAN.md").write_text("- [x] Build and check the page", encoding="utf-8")
        (notes / "PROGRESS.md").write_text(
            "Page created and the saved project checks passed successfully.", encoding="utf-8")
        calls = []

        def complete(_payload):
            calls.append(1)
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "verify-1", "function": {"name": "run_verifier", "arguments": "{}"}}]}}]}

        with (patch.object(long_tasks, "projects", return_value=[item]),
              patch.object(long_tasks, "verify_project", return_value=(True, "real checks passed")) as verify):
            state = native_agent.run_slice(item, complete=complete)
        verify.assert_called_once_with(item)
        assert state["status"] == "done"
        assert len(calls) == 1
        assert "Exit code: 0" in str(state["messages"])


def test_failed_direct_verifier_cannot_certify_a_task():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 1", encoding="utf-8")
        reply = {"choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": "verify-1", "function": {"name": "run_verifier", "arguments": "{}"}}]}}]}
        with (patch.object(long_tasks, "projects", return_value=[item]),
              patch.object(long_tasks, "verify_project", return_value=(False, "check failed")),
              patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1)):
            state = native_agent.run_slice(item, complete=lambda _payload: reply)
        assert state["status"] == "continue"
        assert state["verify_passed"] is False
        assert "Exit code: 1" in str(state["messages"])


def test_echoed_verifier_command_does_not_count_as_a_check():
    assert not native_agent.verifier_command_passed(
        "run_command", {"command": "echo powershell -File .local-ai/verify.ps1"},
        "Completed")
    assert native_agent.verifier_command_passed(
        "run_command", {"command": "powershell -NoProfile -File .local-ai/verify.ps1"},
        "Completed")


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


def test_source_edit_after_verification_requires_another_check():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        (notes / "PLAN.md").write_text("- [ ] Edit and check the page\n")
        (notes / "PROGRESS.md").write_text("Working on the page.\n")
        (folder / "index.html").write_text("<h1>Old</h1>", encoding="utf-8")
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "verify-1", "function": {"name": "run_command",
                    "arguments": '{"command":"powershell -File .local-ai/verify.ps1"}'}}]}}]},
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "edit-1", "function": {"name": "edit_file", "arguments": json.dumps({
                    "path": "index.html", "old_text": "Old", "new_text": "New",
                })}}]}}]},
        ])
        original = native_agent.execute_tool

        def tool(root, name, args):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args)

        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), \
                patch.object(native_agent, "execute_tool", side_effect=tool):
            state = native_agent.run_slice(item, complete=lambda _: next(responses))
        assert state["status"] == "continue"
        assert state["verify_passed"] is False
        assert (folder / "index.html").read_text() == "<h1>New</h1>"


def test_valid_model_turn_resets_consecutive_worker_failures():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        (Path(item["folder"]) / ".local-ai/verify.ps1").write_text("exit 0")
        native_agent.save_state(item, {"status": "continue", "messages": [],
                                       "failures": 2, "verify_passed": True})
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
        edit = native_agent.execute_tool(folder, "edit_file", {
            "path": "../outside.txt", "old_text": "secret", "new_text": "changed",
        })
        assert edit.startswith("Tool error:")


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


def test_edit_file_changes_one_block_and_refreshes_search_without_full_rewrite():
    import project_memory

    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        source = folder / "app.py"
        source.write_bytes(b"# keep this header\r\ndef old_widget():\r\n    return 1\r\n")
        prior = hashlib.sha256(source.read_bytes()).hexdigest()
        edited = native_agent.execute_tool(folder, "edit_file", {
            "path": "app.py", "old_text": "def old_widget():\n    return 1",
            "new_text": "def new_widget():\n    return 2", "expected_sha256": prior,
        })
        assert edited.startswith("Edited app.py (sha256 ")
        assert source.read_bytes() == b"# keep this header\r\ndef new_widget():\r\n    return 2\r\n"
        assert project_memory.search(folder, "new_widget", kind="file")
        assert not project_memory.search(folder, "old_widget", kind="file")
        stale = native_agent.execute_tool(folder, "edit_file", {
            "path": "app.py", "old_text": "return 2", "new_text": "return 3",
            "expected_sha256": prior,
        })
        assert "file changed since that hash" in stale
        assert b"return 2" in source.read_bytes()


def test_edit_file_rejects_ambiguous_block_and_preserves_powershell_bom():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        script = folder / "verify.ps1"
        script.write_bytes(b"Write-Output 'old'\r\nWrite-Output 'old'\r\n")
        ambiguous = native_agent.execute_tool(folder, "edit_file", {
            "path": "verify.ps1", "old_text": "Write-Output 'old'",
            "new_text": "Write-Output 'new'",
        })
        assert "found 2" in ambiguous
        assert script.read_bytes() == b"Write-Output 'old'\r\nWrite-Output 'old'\r\n"
        edited = native_agent.execute_tool(folder, "edit_file", {
            "path": "verify.ps1", "old_text": "Write-Output 'old'\nWrite-Output 'old'",
            "new_text": "Write-Output '✓'",
        })
        assert edited.startswith("Edited verify.ps1")
        assert script.read_bytes() == b"\xef\xbb\xbfWrite-Output '\xe2\x9c\x93'\r\n"


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


def test_first_native_turn_includes_short_rules_and_saved_checkpoint():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / "AGENTS.md").write_text("Use a blue header.", encoding="utf-8")
        (folder / ".local-ai/PLAN.md").write_text("- [ ] Build the page\n", encoding="utf-8")
        seen = []

        def complete(payload):
            seen.append(payload)
            return {"choices": [{"message": {"role": "assistant", "content": "Working."}}]}

        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            native_agent.run_slice(item, complete=complete)
        assert "Use a blue header." in seen[0]["messages"][0]["content"]
        assert "Build the page" in seen[0]["messages"][1]["content"]
        assert "Take the next useful action" in seen[0]["messages"][2]["content"]
        assert "Read AGENTS.md, the original request" not in seen[0]["messages"][0]["content"]


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


def test_failed_checker_details_survive_event_trimming_and_fresh_context(tmp_path):
    item = project(tmp_path / "page")
    folder = Path(item["folder"])
    (folder/"index.html").write_text('<p id="total">wrong</p>')
    args={"path":"index.html","cases":[{"set":{"bill":"80","tip":"25"},"expect":{"total":"24.00"}}]}
    failure=json.dumps({"passed":False,"cases":[{"case":1,"passed":False,
        "failures":["#total displayed 'wrong'; expected '24.00'"]}]})
    task_context.record_action(folder,"check_webapp",failure,args)
    for _ in range(105):
        task_context.record_action(folder,"read_file","private file contents",{"path":"index.html"})
    fresh=native_agent.fresh_messages([{"role":"system","content":"Original brief"}],folder)
    assert "#total displayed 'wrong'" in fresh[1]["content"]
    assert "expected '24.00'" in fresh[1]["content"]
    assert '"tip": "25"' in fresh[1]["content"]
    assert "checker case can itself be mistaken" in fresh[1]["content"]
    assert "private file contents" not in fresh[1]["content"]
    task_context.record_action(folder,"check_webapp",
        'Tool error: This exact call failed twice and was skipped.',args)
    assert "expected '24.00'" in task_context.snapshot(folder)
    with project_memory.connection(folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]==100
    task_context.record_action(folder,"check_webapp",'{"passed":true}',args)
    assert "Unresolved checker diagnostics" not in task_context.snapshot(folder)


def test_checker_repeated_problem_survives_edits_and_resets_for_new_request(tmp_path):
    from auto_route import repeated_tool_failures

    item = project(tmp_path / "page")
    folder = Path(item["folder"])
    page = folder / "index.html"
    page.write_text('old')
    requests = folder / ".local-ai/USER-REQUESTS.md"
    requests.write_text('Use blue')
    args={"path":"index.html"}
    failure='{"passed":false,"javascript_errors":["total is not defined"]}'
    identity=task_context.request_identity(folder)
    for _ in range(3):
        task_context.record_action(folder,"check_webapp",failure,args)
    assert "same check/result failed 3 times" in task_context.snapshot(folder)
    assert "Do not repeat the same failed check" in task_context.snapshot(folder)
    assert repeated_tool_failures([{"role": "system", "content": task_context.snapshot(folder)}])
    page.write_text('edited')
    task_context.record_action(folder,"check_webapp",failure,args)
    assert "same check/result failed 4 times" in task_context.snapshot(folder)
    requests.write_text('Use gold')
    task_context.record_action(folder,"check_webapp",failure,args,request_hash=identity)
    assert "Unresolved checker diagnostics" not in task_context.snapshot(folder)
    assert not repeated_tool_failures([{"role": "system", "content": task_context.snapshot(folder)}])
    assert task_context.checker_failures(folder,"index.html")==[]
    task_context.record_action(folder,"check_webapp",failure,args)
    assert "total is not defined" in task_context.snapshot(folder)
    assert task_context.checker_failures(folder,"index.html")[0]["repeats"] == 1
    assert not repeated_tool_failures([{"role": "system", "content": task_context.snapshot(folder)}])
    recalled=json.loads(native_agent.execute_tool(folder,"search_project_memory",{
        "query":"index.html","kind":"check"}))
    assert recalled[0]["path"]=="index.html"
    assert "total is not defined" in recalled[0]["details"]


def test_document_checker_memory_is_bounded_and_does_not_save_input_documents(tmp_path):
    item = project(tmp_path / "report")
    folder = Path(item["folder"])
    (folder/"summary.md").write_text('Draft')
    args={"report_path":"summary.md","facts":[{"source_text":"private complete input", "report_text":"final"}]}
    error="Fact 1: report text was not found.\n"+"x"*5000
    task_context.record_action(folder,"check_document_report",json.dumps({"passed":False,"errors":[error]}),args)
    checkpoint=task_context.snapshot(folder)
    assert "Fact 1: report text was not found" in checkpoint
    assert "private complete input" not in checkpoint
    assert "x"*300 not in checkpoint
    assert "\\n" in checkpoint
    task_context.record_action(folder,"check_document_report",'{"passed":true}',{"report_path":"../other.md"})
    assert "Fact 1: report text was not found" in task_context.snapshot(folder)


def test_verifier_exit_zero_does_not_overrule_missing_gameplay_evidence(tmp_path):
    item = project(tmp_path / "game")
    folder = Path(item["folder"])
    item["brief"]="Create a simple space shooter game"
    (folder/"game.html").write_text('<canvas></canvas>')
    (folder/".local-ai/verify.ps1").write_text('exit 0')
    fresh=native_agent.fresh_messages([{"role":"system","content":"Build the game"}],folder,
        {"verify_passed":True},item)
    assert "completion requirements remain unresolved" in fresh[1]["content"]
    assert "Run check_webapp" in fresh[1]["content"]
    assert "Do not recreate files or rerun unchanged checks" not in fresh[1]["content"]


def test_edit_whitespace_mismatch_gives_exact_candidate_without_fuzzy_application(tmp_path):
    page = tmp_path / "code.py"
    original = "if ready:\n  run()\n"
    page.write_text(original)
    import pytest
    with pytest.raises(ValueError) as failure:
        native_agent.edit_project_file(tmp_path,"code.py","   run()","   stop()")
    assert page.read_text()==original
    hint = str(failure.value).split("exact old_text: ")[1]
    assert json.loads(hint)=="  run()"
    native_agent.edit_project_file(tmp_path,"code.py",json.loads(hint),"  stop()")
    assert page.read_text()=="if ready:\n  stop()\n"
    page.write_text("if ready:\n  run()\nelse:\n run()\n")
    with pytest.raises(ValueError, match="found 0"):
        native_agent.edit_project_file(tmp_path,"code.py","   run()","   stop()")


def test_bounded_checker_memory_prioritizes_unresolved_failures(tmp_path):
    item = project(tmp_path / "project")
    folder = Path(item["folder"])
    task_context.record_action(folder,"check_document_report",
        '{"passed":false,"errors":["Fact 1: report text was not found"]}',{"report_path":"important.md"})
    for number in range(130):
        task_context.record_action(folder,"check_document_report",'{"passed":true}',
            {"report_path":f"passed-{number}.md"})
    with project_memory.connection(folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_check_results").fetchone()[0]==128
    recalled=task_context.checker_failures(folder,"important.md")
    assert len(recalled)==1 and "Fact 1" in recalled[0]["details"]


def test_small_html_repair_focus_restores_other_capabilities_periodically(tmp_path):
    item = project(tmp_path / "page")
    folder = Path(item["folder"])
    (folder/"index.html").write_text('<p id="total">wrong</p>')
    task_context.record_action(folder,"check_webapp",'{"passed":false,"javascript_errors":["total is not defined"]}',
        {"path":"index.html"})
    state={}
    for _ in range(4):
        names={tool["function"]["name"] for tool in native_agent.repair_tools(item,state)}
        assert "edit_file" in names and "check_webapp" in names
        assert "search_project_memory" not in names
    assert native_agent.repair_tools(item,state) is native_agent.TOOLS
    task_context.record_action(folder,"check_webapp",'{"passed":true}',{"path":"index.html"})
    assert native_agent.repair_tools(item,state) is native_agent.TOOLS


def test_checker_validation_error_survives_a_fresh_context(tmp_path):
    item = project(tmp_path / "page")
    folder = Path(item["folder"])
    task_context.record_action(folder,"check_webapp",
        'Tool error: A case cannot require the same target to be both hidden and visible.',
        {"path":"index.html"})
    assert "both hidden and visible" in task_context.snapshot(folder)


def test_checkpoint_uses_newest_root_notes_then_returns_to_updated_canonical_files():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        for name, old, new in (
                ("PLAN.md", "- [ ] Stale plan", "- [ ] Build the actual page"),
                ("PROGRESS.md", "Next: Stale progress", "Next: Verify the actual page")):
            canonical = folder / ".local-ai" / name
            alternate = folder / name
            canonical.write_text(old, encoding="utf-8")
            alternate.write_text(new, encoding="utf-8")
            stamp = time.time_ns()
            os.utime(canonical, ns=(stamp, stamp))
            os.utime(alternate, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
            assert task_context.document_path(folder, name) == alternate
        summary = task_context.snapshot(folder)
        assert "Next step: Verify the actual page" in summary
        assert "Build the actual page" in summary
        assert "[Saved at project root: PROGRESS.md]" in summary
        assert "Stale" not in summary
        # A later worker resumes the standard location; no stale DB excerpt
        # or root file should override its newer checkpoint.
        canonical = folder / ".local-ai/PLAN.md"
        canonical.write_text("- [ ] Publish the checked page", encoding="utf-8")
        newer = (folder / "PROGRESS.md").stat().st_mtime_ns + 2_000_000_000
        os.utime(canonical, ns=(newer, newer))
        assert "Next step: Publish the checked page" in task_context.snapshot(folder)
        # Equal timestamps prefer the canonical file too.
        alternate = folder / "PLAN.md"
        os.utime(alternate, ns=(newer, newer))
        assert task_context.document_path(folder, "PLAN.md") == canonical


def test_root_user_requests_never_replace_the_queue_backed_request_file():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / "USER-REQUESTS.md").write_text("Unrelated root requests", encoding="utf-8")
        canonical = folder / ".local-ai/USER-REQUESTS.md"
        canonical.write_text("The actual queued request", encoding="utf-8")
        summary = task_context.snapshot(folder)
        assert "The actual queued request" in summary
        assert "Unrelated root requests" not in summary


def test_unchanged_task_checkpoint_reads_cached_sqlite_document():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        plan = folder / ".local-ai/PLAN.md"
        plan.write_text("- [ ] Build the page")
        assert "Build the page" in task_context.snapshot(folder)
        with patch.object(Path, "read_text", side_effect=AssertionError("file reread")):
            assert "Build the page" in task_context.snapshot(folder)


def test_checkpoint_reads_same_size_user_instruction_edit_with_preserved_timestamp():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai/USER-REQUESTS.md"
        notes.write_text("Use blue text", encoding="utf-8")
        assert "Use blue text" in task_context.snapshot(folder)
        before = notes.stat()
        notes.write_text("Use gold text", encoding="utf-8")
        os.utime(notes, ns=(before.st_atime_ns, before.st_mtime_ns))
        updated = task_context.snapshot(folder)
        assert "Use gold text" in updated
        assert "Use blue text" not in updated


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
        assert "Next step: Step two" in task_context.snapshot(folder)


def test_inflight_user_instruction_prevents_early_completion():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        notes = Path(item["folder"]) / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        native_agent.save_state(item, {"status": "continue", "verify_passed": True,
                                       "browser_verified": True})
        seen = []

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            if len(seen) == 1:
                (notes / "USER-REQUESTS.md").write_text("Make the title blue")
                return {"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}
            assert "Make the title blue" in str(payload["messages"])
            return {"choices": [{"message": {"role": "assistant", "content": "Blue title finished"}}]}

        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2):
            state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "continue"
        assert state["verify_passed"] is False
        assert state["browser_verified"] is False
        assert len(seen) == 2


def test_new_instruction_during_model_call_discards_stale_tool_plan():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "USER-REQUESTS.md").write_text("", encoding="utf-8")
        (notes / "verify.ps1").write_text("exit 0", encoding="utf-8")
        native_agent.save_state(item, {"status": "continue", "verify_passed": True,
                                       "browser_verified": True})

        def complete(_payload):
            (notes / "USER-REQUESTS.md").write_text("Do not create old.txt", encoding="utf-8")
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": "stale-write", "function": {"name": "write_file", "arguments":
                    '{"path":"old.txt","content":"stale plan"}'}}]}}]}

        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "continue"
        assert not (folder / "old.txt").exists()
        assert state["verify_passed"] is False
        assert state["browser_verified"] is False
        assert "before acting" in str(state["messages"])


def test_instruction_between_turns_reaches_next_model_call():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        notes = Path(item["folder"]) / ".local-ai"
        (notes / "USER-REQUESTS.md").write_text("", encoding="utf-8")
        (notes / "verify.ps1").write_text("exit 0", encoding="utf-8")
        native_agent.save_state(item, {"status": "continue", "verify_passed": True,
                                       "browser_verified": True})
        original_read = native_agent.read_state
        reads = []
        seen = []

        def read_with_between_turn_note(project_item):
            reads.append(1)
            if len(reads) == 5:
                (notes / "USER-REQUESTS.md").write_text("Use blue instead", encoding="utf-8")
            return original_read(project_item)

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return {"choices": [{"message": {
                "role": "assistant", "tool_calls": [
                    {"id": f"list-{len(seen)}", "function": {
                        "name": "list_files", "arguments": "{}"}},
                ],
            }}]}

        with (patch.object(native_agent, "read_state", side_effect=read_with_between_turn_note),
              patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2)):
            state = native_agent.run_slice(item, complete=complete)
        assert len(seen) == 2
        assert "Use blue instead" in str(seen[1]["messages"])
        assert state["verify_passed"] is False
        assert state["browser_verified"] is False


def test_task_action_ledger_names_files_without_storing_content():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        task_context.record_action(folder, "read_file", "secret document text",
                                   {"path": "src/page.py"})
        summary = task_context.snapshot(folder)
        assert "read_file src/page.py: Completed" in summary
        assert "secret document text" not in summary


def test_csv_action_ledger_keeps_counts_without_private_rows():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "table")
        folder = Path(item["folder"])
        task_context.record_action(folder, "transform_csv", json.dumps({
            "output": "cleaned.csv", "kept": 3, "dropped_invalid": 1,
            "dropped_duplicate": 2, "preview": [{"name": "private person"}],
        }), {"output_path": "cleaned.csv"})
        summary = task_context.snapshot(folder)
        assert "transform_csv cleaned.csv (3 kept, 1 invalid, 2 duplicate): Completed" in summary
        assert "private person" not in summary


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


def test_checker_memory_requires_explicit_pass_and_preserves_failed_file_identity():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(project(Path(temporary) / "page")["folder"])
        for tool, arguments in (("check_webapp", {"path": "index.html"}),
                                ("check_document_report", {"report_path": "summary.md"})):
            task_context.record_action(folder, tool, json.dumps({
                "ok": True, "passed": False, "detail": "private report or page text",
            }), arguments)
            assert task_context.action_outcome(tool, '{"passed":true}') == "Completed"
            for invalid in ('{"ok":true}', '{"passed":"true"}', '[]', '{"passed":'):
                assert task_context.action_outcome(tool, invalid) == "Failed"
        summary = task_context.snapshot(folder)
        assert "check_webapp index.html: Failed" in summary
        assert "check_document_report summary.md: Failed" in summary
        assert "private report" not in summary


def test_repeated_unchanged_read_batches_get_guidance_after_complete_tool_replies():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / "game.js").write_text("// code\n" * 460, encoding="utf-8")
        seen = []

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            number = len(seen)
            calls = [{"id": f"read-{number}-{line}", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "game.js", "start_line": line})}}
                for line in (1, 151, 301)] if number < 3 else [{
                    "id": "list-after-guidance", "type": "function", "function": {
                        "name": "list_files", "arguments": "{}"}}]
            return {"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]}

        original = native_agent.execute_tool
        executed_reads = []
        def execute(root, name, args):
            if name == "read_file":
                executed_reads.append(args)
            return original(root, name, args)

        with (patch.object(native_agent, "MAX_TURNS_PER_SLICE", 3),
              patch.object(native_agent, "execute_tool", side_effect=execute)):
            state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "continue"
        assert len(executed_reads) == 6  # Guidance never denies a legitimate read.
        messages = seen[2]["messages"]
        notice = next(index for index, message in enumerate(messages)
                      if "Several unchanged file sections" in str(message.get("content", "")))
        assert [message["role"] for message in messages[notice - 3:notice]] == ["tool"] * 3
        assert state["read_cycle_nudged"]
        assert all(len(digest) == 64 for digest in state["recent_read_hashes"])


def test_read_cycle_ignores_new_content_and_resets_after_source_edit_or_user_change():
    state = {}
    args = {"path": "game.js", "start_line": 1}
    for number in range(5):
        assert not native_agent.remember_read_cycle(state, "read_file", args, f"Changed {number}", "Completed")
    for _ in range(2):
        assert not native_agent.remember_read_cycle(state, "read_file", args, "Changed 4", "Completed")
    assert native_agent.remember_read_cycle(state, "read_file", args, "Changed 4", "Completed")
    native_agent.remember_read_cycle(state, "write_file", {"path": ".local-ai/PROGRESS.md"}, "Saved", "Completed")
    assert state["read_cycle_nudged"]
    native_agent.remember_read_cycle(state, "edit_file", args, "Saved", "Completed")
    assert "recent_read_hashes" not in state
    assert not native_agent.remember_read_cycle(state, "read_file", args, "Changed 4", "Completed")
    native_agent.invalidate_checks(state)
    assert "recent_read_hashes" not in state


def test_repeated_identical_tool_failure_prompts_a_different_approach():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        native_agent.save_state(item, {"status": "continue", "verify_passed": True})
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


def test_repeated_failed_browser_verdict_reaches_worker_repair_guidance():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        native_agent.save_state(item, {"status": "continue", "verify_passed": True})
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": call_id, "function": {"name": "check_webapp", "arguments":
                    '{"path":"index.html","cases":[{"expect":{"total":"24.00"}}]}'},
            }]}}]} for call_id in ("browser-1", "browser-2")
        ] + [{"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}] * 2)
        seen = []

        def complete(payload):
            seen.append(copy.deepcopy(payload))
            return next(responses)

        with patch.object(native_agent, "execute_tool", return_value='{"ok":true,"passed":false}'):
            state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "continue"
        assert state["verify_passed"] is False
        assert "Do not retry it unchanged" in str(seen[2]["messages"])
        assert "check_webapp index.html: Failed" in task_context.snapshot(folder)


def test_failed_check_survives_restart_and_old_verifier_pass_until_same_file_passes():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        state = {"verify_passed": True}
        native_agent.remember_checker_verdict(state, "check_webapp", {"path": "index.html"}, '{"passed":false}')
        native_agent.save_state(item, state)
        recovered = native_agent.read_state(item)
        recovered["verify_passed"] = True  # An older script still exits zero.
        assert "index.html" in native_agent.completion_issue(folder, item, recovered)
        native_agent.remember_checker_verdict(recovered, "check_webapp", {"path": "other.html"}, '{"passed":true}')
        assert "index.html" in native_agent.completion_issue(folder, item, recovered)
        native_agent.remember_checker_verdict(recovered, "check_webapp", {"path": "./index.html"}, '{"passed":true}')
        assert not recovered["failed_checks"]


def test_third_identical_failed_tool_call_is_not_executed_again():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        (folder / ".local-ai/verify.ps1").write_text("exit 0")
        native_agent.save_state(item, {"status": "continue", "verify_passed": True})
        responses = iter([
            {"choices": [{"message": {"role": "assistant", "tool_calls": [{
                "id": f"read-{number}", "function": {"name": "read_file",
                    "arguments": '{"path":"missing.txt"}'}}]}}]}
            for number in range(3)
        ] + [{"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}])
        with patch.object(native_agent, "execute_tool", return_value="Tool error: missing") as tool:
            state = native_agent.run_slice(item, complete=lambda _payload: next(responses))
        assert state["status"] == "done"
        assert tool.call_count == 2
        assert "exact call failed twice and was skipped" in str(state["messages"])
        assert str(state["messages"]).count("Do not retry it unchanged") == 1


def test_instruction_during_tool_call_follows_complete_tool_batch():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        notes = folder / ".local-ai"
        (notes / "verify.ps1").write_text("exit 0")
        native_agent.save_state(item, {"status": "continue", "verify_passed": True,
                                       "browser_verified": True})
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

        with (patch.object(native_agent, "execute_tool", side_effect=tool_with_note),
              patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2)):
            state = native_agent.run_slice(item, complete=complete)
        assert state["status"] == "continue"
        assert state["verify_passed"] is False
        assert state["browser_verified"] is False
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
