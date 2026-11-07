"""Behavior checks for Cale's native, restartable project worker."""

import copy
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import pytest

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


def test_concurrent_checkpoint_saves_publish_complete_independent_files(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    item = project(tmp_path / "concurrent-checkpoint")
    barrier = threading.Barrier(2)
    read_barrier = threading.Barrier(2)
    ready = threading.local()
    prepared = []
    replace = native_agent.os.replace
    states = [{"status": "continue", "messages": [], "current": value * 16000} for value in ("A", "B")]

    def publish(source, target):
        if not getattr(ready, "prepared", False):
            barrier.wait(timeout=5)
            prepared.append(json.loads(Path(source).read_text(encoding="utf-8")))
            read_barrier.wait(timeout=5)
            ready.prepared = True
        replace(source, target)

    with patch.object(native_agent.os, "replace", side_effect=publish):
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [workers.submit(native_agent.save_state, item, state) for state in states]
            for future in pending:
                future.result(timeout=10)
    assert native_agent.read_state(item) in states
    assert {state["current"] for state in prepared} == {state["current"] for state in states}
    assert not list(native_agent.state_file(item).parent.glob("*.tmp"))


def test_checkpoint_read_retries_contention_without_losing_history(tmp_path):
    item = project(tmp_path / "busy-checkpoint")
    previous = {"status": "continue", "messages": [{"role": "user", "content": "Keep this history."}], "turns": 8}
    native_agent.save_state(item, previous)
    read_text = Path.read_text
    attempts = []

    def read(path, *args, **kwargs):
        if path == native_agent.state_file(item):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("Checkpoint is being replaced")
        return read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", new=read), patch.object(native_agent.time, "sleep"):
        if os.name == "nt":
            assert native_agent.read_state(item) == previous
            assert len(attempts) == 2
        else:
            with pytest.raises(PermissionError):
                native_agent.read_state(item)


def test_persistent_checkpoint_denial_never_becomes_a_new_task(tmp_path):
    item = project(tmp_path / "denied-checkpoint")
    with patch.object(Path, "read_text", side_effect=PermissionError("Access denied")) as read, \
            patch.object(native_agent.time, "sleep"):
        with pytest.raises(PermissionError, match="Access denied"):
            native_agent.read_state(item)
    assert read.call_count == (5 if os.name == "nt" else 1)


def test_failed_checkpoint_publication_keeps_prior_state_and_cleans_temporary_file(tmp_path):
    item = project(tmp_path / "locked-checkpoint")
    previous = {"status": "continue", "messages": [], "current": "Saved progress"}
    native_agent.save_state(item, previous)
    with patch.object(native_agent.os, "replace", side_effect=PermissionError("Checkpoint unavailable")) as replace, \
            patch.object(native_agent.time, "sleep"):
        with pytest.raises(PermissionError, match="Checkpoint unavailable"):
            native_agent.save_state(item, {**previous, "current": "Unsaved progress"})
    assert replace.call_count == (5 if os.name == "nt" else 1)
    assert native_agent.read_state(item) == previous
    assert not list(native_agent.state_file(item).parent.glob("*.tmp"))


@pytest.mark.parametrize("status", ["paused", "stopped", "done"])
def test_worker_error_keeps_a_newer_control_checkpoint(tmp_path, status):
    item = project(tmp_path / "controlled-worker")
    checkpoint = {"status": status, "messages": [{"role": "user", "content": "Keep the saved progress."}],
                  "current": "A newer task state", "turns": 8}

    def interrupted(_project):
        native_agent.save_state(item, checkpoint)
        raise OSError("The old operation failed during shutdown")

    with patch.object(long_tasks, "projects", return_value=[item]), \
            patch.object(native_agent, "run_slice", side_effect=interrupted):
        assert native_agent.worker_main(item["id"]) == 0
    assert native_agent.read_state(item) == checkpoint


def test_worker_error_still_records_a_running_tasks_actual_failure(tmp_path):
    item = project(tmp_path / "failed-worker")
    previous = {"status": "running", "messages": [], "failures": 1, "turns": 8}
    native_agent.save_state(item, previous)
    with patch.object(long_tasks, "projects", return_value=[item]), \
            patch.object(native_agent, "run_slice", side_effect=OSError("Actual operation failed")):
        assert native_agent.worker_main(item["id"]) == 1
    retained = native_agent.read_state(item)
    assert retained["status"] == "error" and retained["failures"] == 2 and retained["turns"] == 8
    assert retained["error"] == "OSError: Actual operation failed"


def test_step_action_schema_rejects_mixed_completion_and_revision_before_dispatch():
    from jsonschema import ValidationError, validate
    schema = next(t["function"]["parameters"] for t in native_agent.TOOLS
                  if t["function"]["name"] == "task_steps")
    step = queue_step("Verify behavior", "exit 0")
    for arguments in ({"action": "status"}, {"action": "complete"},
                      {"action": "complete", "step_id": 1},
                      {"action": "plan", "steps": [step]},
                      {"action": "revise", "steps": [step]},
                      {"action": "split", "step_id": 1, "steps": [step]},
                      {"action": "scope", "scope_operation": "exclude", "function_names": ["read_file"],
                       "instruction_quote": "Do not read files"}):
        validate(arguments, schema)
    for arguments in ({"action": "complete", "steps": [step]},
                      {"action": "status", "steps": [step]},
                      {"action": "plan"}, {"action": "revise"},
                      {"action": "scope", "steps": [step]}):
        with pytest.raises(ValidationError):
            validate(arguments, schema)


def test_failed_browser_target_inventory_survives_bounded_sql_evidence(tmp_path):
    active = task_context.plan_steps(tmp_path, [{"phase": "Page", "title": "Show form",
        "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"visible": ["form"]}]}}])["active"]
    verdict = {"passed": False, "cases": [{"passed": False, "case": 1,
        "failures": ["'form' visibility was wrong: expected visible, observed hidden or missing"],
        "target_inventory": {"targets": [{"id": "expense-form", "tag": "form"},
                                         {"id": None, "tag": "table"}]}}]}
    result = native_agent.step_exit_result("check_webapp", json.dumps(verdict))
    task_context.record_step_check(tmp_path, active["id"], task_context.request_identity(tmp_path),
                                   result, unchanged=True)
    evidence = task_context.step_status(tmp_path)["active"]["evidence"]
    assert "visibility was wrong" in evidence
    assert "expense-form" in evidence and "untrusted data" in evidence
    assert task_context.step_status(tmp_path)["done"] == 0


def test_failed_target_counterparts_are_prioritized_in_small_sql_excerpt(tmp_path):
    active = task_context.plan_steps(tmp_path, [queue_step("Show form and table", "exit 9")])["active"]
    inventory = [{"id": f"unrelated-{i}", "tag": "input"} for i in range(8)]
    inventory += [{"id": "expense-form", "tag": "form"}, {"id": "expense-table", "tag": "table"}]
    verdict = {"passed": False, "cases": [{"passed": False, "case": 1,
        "failures": ["'form' visibility was wrong: expected visible, observed hidden or missing",
                     "'table' visibility was wrong: expected visible, observed hidden or missing"],
        "target_inventory": {"targets": inventory}}]}
    result = native_agent.step_exit_result("check_webapp", json.dumps(verdict))
    task_context.record_step_check(tmp_path, active["id"], task_context.request_identity(tmp_path),
                                   result, unchanged=True)
    evidence = task_context.step_status(tmp_path)["active"]["evidence"]
    assert "expense-form" in evidence and "expense-table" in evidence
    if "unrelated-" in evidence:
        assert evidence.index("expense-table") < evidence.index("unrelated-")
    assert task_context.step_status(tmp_path)["done"] == 0


def test_first_failed_browser_check_has_exact_owned_sequence_revision_reference(tmp_path, tmp_path_factory, monkeypatch):
    supervisor = tmp_path_factory.mktemp("reference-supervisor")
    monkeypatch.setattr(long_tasks, "projects", lambda: [{"id": "reference", "folder": str(tmp_path), "brief": "Verify form and table"}])
    monkeypatch.setattr(long_tasks, "STATE", supervisor)
    (tmp_path / "index.html").write_text('<h1 id="heading">Expenses</h1><form id="expense-form">'
        '<button>Add expense</button></form><table id="expense-table"><tr><th>Amount</th></tr></table>')
    first = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", {
        "path": "index.html", "viewport_width": 900, "cases": [{"visible": ["heading"]}]}))
    assert first["passed"], first
    failed = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", {
        "path": "index.html", "cases": [{"visible": ["form", "table"]}]}))
    assert failed["passed"] is False and failed["failed_cases_retained"]
    reference = failed["assertion_revision_reference"]
    saved_path = tmp_path / ".local-ai/webapp-check.json"
    suite = json.loads(saved_path.read_text())
    assert reference["saved_sequence"] == 2 and reference["saved_case"] == 1
    assert reference["suite_sha256"] == hashlib.sha256(saved_path.read_bytes()).hexdigest()
    assert suite["checks"][1]["cases"][0]["visible"] == ["form", "table"]
    corrected = json.loads(native_agent.execute_tool(tmp_path, "check_webapp", {
        **reference, "cases": [{"visible": ["expense-form", "expense-table"]}]}))
    assert corrected["passed"] and corrected["verifier_check_passed"]
    suite = json.loads(saved_path.read_text())
    assert len(suite["checks"]) == 2
    assert suite["checks"][0]["cases"][0]["visible"] == ["heading"]


def step_notice_fixture(folder):
    folder.mkdir()
    source = '<form id="expense-form"><input value="PRIVATE-FIELD-DATA"><button>Add expense</button></form>' \
             '<table id="expense-table"><tr><th>Amount</th></tr></table>'
    (folder / "index.html").write_text(source)
    active = task_context.plan_steps(folder, [{"phase": "Layout", "title": "Verify form and table",
        "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"visible": ["form", "table"]}]}}])["active"]
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert failed["passed"] is False
    return {"id": "notice", "folder": str(folder), "brief": "Build a page with a form and table."}, source


def test_active_step_notice_uses_current_ids_and_owned_reference_without_changing_progress(tmp_path):
    item, source = step_notice_fixture(tmp_path / "notice")
    folder = Path(item["folder"])
    before = task_context.step_status(folder)
    notice = native_agent.active_step_repair_notice(item, {"step_focus_turns": 1})
    assert "Compare the ORIGINAL request" in notice["content"]
    assert "not proof of visibility" in notice["content"]
    assert "PRIVATE-FIELD-DATA" not in notice["content"]
    data = json.loads(notice["content"].rsplit("\n", 1)[1])
    assert data["candidate_static_tag_ids"] == {"form": "expense-form", "table": "expense-table"}
    assert data["assertion_revision_reference"]["suite_sha256"] == hashlib.sha256(
        (folder / ".local-ai/webapp-check.json").read_bytes()).hexdigest()
    assert task_context.step_status(folder) == before
    assert (folder / "index.html").read_text() == source
    assert native_agent.active_step_repair_notice(item, {"step_focus_turns": 0}) is None
    (folder / "index.html").write_text(source.replace("expense-form", "current-form"))
    refreshed = native_agent.active_step_repair_notice(item, {"step_focus_turns": 1})
    assert '"form": "current-form"' in refreshed["content"]


def test_active_step_notice_does_not_guess_between_tags_or_touch_custom_verifiers(tmp_path):
    item, source = step_notice_fixture(tmp_path / "ambiguous")
    folder = Path(item["folder"])
    (folder / "index.html").write_text(source + '<form><button>Other</button></form><table><tr><td>Other</td></tr></table>')
    assert native_agent.active_step_repair_notice(item, {"step_focus_turns": 1}) is None
    (folder / "index.html").write_text(source)
    (folder / ".local-ai/verify.ps1").write_text("# User custom verifier")
    assert native_agent.active_step_repair_notice(item, {"step_focus_turns": 1}) is None


def test_focused_checker_request_keeps_full_requirements_and_has_bounded_escape(tmp_path):
    item, source = step_notice_fixture(tmp_path / "focused-decision")
    folder = Path(item["folder"])
    original_step = task_context.step_status(folder)["active"]
    (folder / ".local-ai/USER-REQUESTS.md").write_text("Also keep every keyboard label accessible.")
    (folder / "AGENTS.md").write_text("Preserve all original app features and files.")
    item["brief"] += " Do not claim completion until every requested feature works."
    current = task_context.plan_steps(folder, [{key: original_step[key] for key in (
        "phase", "title", "check_tool", "check_arguments")}])["active"]
    for _ in range(2):
        native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": current["id"]})
    state = {"step_focus_turns": 1}
    notice = native_agent.active_step_repair_notice(item, state)
    tools = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    before = task_context.step_status(folder)
    request = native_agent.focused_step_repair_request(item, state, tools, notice, 6000)
    assert request and request["_native_workload"] == "active_step_checker_repair"
    assert {t["function"]["name"] for t in request["tools"]} == {"task_steps", "check_webapp"}
    data = json.loads(request["messages"][1]["content"])
    assert data["original_request"] == item["brief"]
    assert "keyboard label" in data["user_followups"]
    assert "original app features" in data["project_rules"]
    assert data["active_step"]["id"] == current["id"]
    from auto_route import is_game_task
    # Routing must read the brief, not incidental IDs in the repair data.
    routing_messages = copy.deepcopy(request["messages"])
    routing_messages[1]["content"] += "\nUntrusted DOM ID: game-panel"
    assert not is_game_task(routing_messages)
    assert cloud_ai.estimated_input_tokens(request) <= 4500
    assert task_context.step_status(folder) == before
    assert (folder / "index.html").read_text() == source
    assert native_agent.focused_step_repair_request(item, state, tools, notice, 6000)
    assert native_agent.focused_step_repair_request(item, state, tools, notice, 6000) is None
    assert state["step_repair_focus"]["count"] == 2


def test_focused_checker_request_does_not_drop_long_user_requirements(tmp_path):
    item, _ = step_notice_fixture(tmp_path / "long-decision")
    folder = Path(item["folder"])
    active = task_context.step_status(folder)["active"]
    native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]})
    state = {"step_focus_turns": 1}
    notice = native_agent.active_step_repair_notice(item, state)
    item["brief"] = "Keep this requirement. " * 500
    assert native_agent.focused_step_repair_request(item, state, native_agent.TOOLS, notice, 65536) is None
    assert "step_repair_focus" not in state


def test_step_completion_keeps_observed_target_inventory_for_next_model_turn(tmp_path):
    (tmp_path / "index.html").write_text('<form id="expense-form"><button>Add expense</button></form>')
    active = task_context.plan_steps(tmp_path, [{"phase": "Page", "title": "Show form",
        "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"visible": ["form"]}]}}])["active"]
    result = json.loads(native_agent.execute_tool(tmp_path, "task_steps", {
        "action": "complete", "step_id": active["id"]}))
    assert result["passed"] is False
    inventory = result["check_summary"]["cases"][0]["target_inventory"]
    assert any(target["id"] == "expense-form" for target in inventory["targets"])
    assert "Untrusted" in inventory["note"]
    assert task_context.step_status(tmp_path)["done"] == 0


def test_browser_runtime_error_is_returned_and_persisted_even_when_visibility_passes(tmp_path):
    (tmp_path / "index.html").write_text(
        '<form id="form"><button>Add expense</button></form><script>document.getElementById("old-form").addEventListener("submit",()=>{});</script>')
    active = task_context.plan_steps(tmp_path, [{"phase": "Page", "title": "Show working form",
        "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"visible": ["form"]}]}}])["active"]
    result = json.loads(native_agent.execute_tool(tmp_path, "task_steps", {
        "action": "complete", "step_id": active["id"]}))
    assert result["passed"] is False
    assert result["check_summary"]["cases"] == []
    assert any("addEventListener" in error for error in result["check_summary"]["errors"])
    saved = task_context.step_status(tmp_path)
    assert saved["done"] == 0
    assert "addEventListener" in saved["active"]["evidence"]
    assert "Acceptance check error" in saved["active"]["evidence"]


@pytest.mark.parametrize("check", [
    {"check_command": "exit 9"},
    {"check_tool": "check_webapp", "check_arguments": {
        "path": "index.html", "cases": [{"visible": ["heading"]}]}},
])
def test_noop_step_revision_keeps_failure_evidence_and_does_not_touch_database(tmp_path, check):
    step = {"phase": "Core", "title": "Verify behavior", **check}
    active = task_context.plan_steps(tmp_path, [step])["active"]
    task_context.record_step_check(tmp_path, active["id"], task_context.request_identity(tmp_path),
                                   "Exit code: 9\nMissing heading", unchanged=True)
    before = task_context.step_status(tmp_path)
    with pytest.raises(ValueError, match="saved step is unchanged"):
        task_context.revise_step(tmp_path, active["id"], [step])
    assert task_context.step_status(tmp_path) == before
    corrected = {**step, "title": "Verify corrected behavior"}
    after = task_context.revise_step(tmp_path, active["id"], [corrected])
    assert after["active"]["title"] == corrected["title"]
    assert after["active"]["attempts"] == before["active"]["attempts"]
    assert "not yet passed" in after["active"]["evidence"]


@pytest.mark.parametrize("check", [
    {"check_command": "Write-Output checked"},
    {"check_tool": "run_verifier"},
    {"check_tool": "check_python_function", "check_arguments": {"path": "logic.py", "function": "identity",
        "cases": [{"args": [2], "expected": 2}]}},
    {"check_tool": "check_javascript_function", "check_arguments": {"path": "logic.js", "function": "identity",
        "cases": [{"args": [2], "expected": 2}]}},
    {"check_tool": "check_webapp", "check_arguments": {"path": "index.html", "cases": [{"visible": ["heading"]}]}},
])
def test_typed_plan_contract_accepts_supported_repeatable_checks(tmp_path, check):
    from jsonschema import ValidationError, validate
    arguments = {"action": "plan", "steps": [{"phase": "Check", "title": "Verify behavior", **check}]}
    schema = next(tool["function"]["parameters"] for tool in native_agent.TOOLS if tool["function"]["name"] == "task_steps")
    validate(arguments, schema)
    result = json.loads(native_agent.execute_tool(tmp_path, "task_steps", arguments))
    assert result["total"] == 1 and result["done"] == 0
    if check.get("check_tool") not in (None, "run_verifier"):
        once = copy.deepcopy(arguments)
        once["steps"][0]["check_arguments"]["saved_case"] = 1
        once["steps"][0]["check_arguments"]["suite_sha256"] = "0" * 64
        with pytest.raises(ValidationError):
            validate(once, schema)


@pytest.mark.parametrize("command", ["write_file", "browser_open_project", "run_verifier", " WRITE_FILE ", "'write_file'", "write_file()"])
def test_shell_tool_labels_cannot_be_saved_as_acceptance_commands(tmp_path, command):
    with pytest.raises(ValueError, match="not a shell acceptance check"):
        task_context.plan_steps(tmp_path, [queue_step("Build page", command)])
    assert task_context.step_status(tmp_path)["total"] == 0


def test_legacy_shell_tool_label_is_blocked_before_process_and_can_be_repaired(tmp_path):
    (tmp_path / "logic.py").write_text("def identity(value): return value\n")
    queued = task_context.plan_steps(tmp_path, [queue_step("Check identity", "Write-Output checked")])
    active = queued["active"]["id"]
    with project_memory.connection(tmp_path) as conn:
        conn.execute("UPDATE task_steps SET check_command='write_file' WHERE id=?", (active,))
    assert "Repair the invalid saved acceptance definition" in task_context.snapshot(tmp_path)
    project = {"folder": str(tmp_path)}
    original_tools = copy.deepcopy(native_agent.TOOLS)
    from jsonschema import ValidationError, validate
    state = {}
    for _ in range(4):
        available = native_agent.step_tools(project, state, native_agent.TOOLS)
        focused = native_agent.acceptance_repair_tools(project, state, available)
        schema = next(tool["function"]["parameters"] for tool in focused if tool["function"]["name"] == "task_steps")
        assert schema["properties"]["action"]["enum"] == ["revise"]
        with pytest.raises(ValidationError):
            validate({"action": "complete", "steps": [queue_step("Wrong completion", "exit 0")]}, schema)
        assert "read_file" in {tool["function"]["name"] for tool in focused}
    available = native_agent.step_tools(project, state, native_agent.TOOLS)
    assert native_agent.acceptance_repair_tools(project, state, available) is available
    assert native_agent.TOOLS == original_tools
    original = native_agent.execute_tool
    def guarded(folder, name, arguments, **kwargs):
        assert name != "run_command", "The invalid legacy checker must not launch a shell"
        return original(folder, name, arguments, **kwargs)
    with patch.object(native_agent, "execute_tool", side_effect=guarded):
        error = native_agent.execute_tool(tmp_path, "task_steps", {"action": "complete", "step_id": active})
    assert "not a shell acceptance check" in error
    unchanged = task_context.step_status(tmp_path)
    assert unchanged["done"] == 0 and unchanged["active"]["attempts"] == 0
    corrected = {"phase": "Check", "title": "Check identity", "check_tool": "check_python_function",
        "check_arguments": {"path": "logic.py", "function": "identity", "cases": [{"args": [2], "expected": 2}]}}
    validate({"action": "revise", "step_id": active, "steps": [corrected]}, schema)
    assert json.loads(original(tmp_path, "task_steps", {"action": "revise", "step_id": active, "steps": [corrected]}))["remaining"] == 1
    assert json.loads(original(tmp_path, "task_steps", {"action": "complete", "step_id": active}))["done"] == 1
    assert (tmp_path / "logic.py").read_text() == "def identity(value): return value\n"
    assert "Repair the invalid saved acceptance definition" not in task_context.snapshot(tmp_path)
    assert native_agent.acceptance_repair_tools(project, {"step_focus_turns": 1}, native_agent.TOOLS) is native_agent.TOOLS


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
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(native_agent, "checker_fingerprint", return_value="before-fix") as checker:
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 1 and task_context.step_status(folder)["done"] == 0
        assert state["local_repair_feedback"]
        state = native_agent.read_state(item)
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 1
        # A software checker fix retries an unchanged failed unit once; it
        # never turns the old failure into a pass without running the checker.
        checker.return_value = "after-fix"
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 2 and task_context.step_status(folder)["done"] == 0
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 2
        # A revised acceptance identity retries even with unchanged source.
        corrected = {**spec, "cases": [{"args": [2], "expected": 3}]}
        task_context.revise_step(folder, active["id"], [{**step, "check_arguments": corrected}])
        assert native_agent.advance_checked_steps(item, state, [])
        assert state["automatic_step_checks"] == 3 and task_context.step_status(folder)["done"] == 0
        source.write_text("function calculate(x){return x+1}")
        assert native_agent.advance_checked_steps(item, state, [])
    assert state["automatic_step_checks"] == 4 and task_context.step_status(folder)["done"] == 1
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


def test_named_api_coverage_blocks_omissions_and_stale_suite_but_preserves_custom_checks(tmp_path):
    for javascript in (False, True):
        item = project(tmp_path / ("api-js" if javascript else "api-py"))
        folder = Path(item["folder"])
        path = "logic.js" if javascript else "logic.py"
        item["brief"] = f"Repair the functions in {path}.\nfirst(x) adds one.\nsecond(x) doubles x.\n"
        source = ("function first(x){return x+1}\nfunction second(x){return x*2}" if javascript else
                  "def first(x): return x+1\ndef second(x): return x*2")
        (folder / path).write_text(source)
        run = run_typed_javascript_check if javascript else run_typed_python_check
        run(item, {"path": path, "function": "first", "cases": [{"args": [2], "expected": 3}]})
        coverage = native_agent.function_coverage(item)
        assert coverage["covered_functions"] == ["first"] and coverage["missing_functions"] == ["second"]
        issue = native_agent.completion_issue(folder, item, {"verify_passed": True})
        assert "omits explicitly requested APIs" in issue and "second" in issue
        copy_path = "test-copy.js" if javascript else "test-copy.py"
        (folder / copy_path).write_text(source)
        run(item, {"path": copy_path, "function": "second", "cases": [{"args": [2], "expected": 4}]})
        assert native_agent.function_coverage(item)["missing_functions"] == ["second"]
        fresh = native_agent.fresh_messages([{"role": "system", "content": "brief"}], folder, {}, item)
        assert "Explicit API coverage" in fresh[1]["content"] and '"missing_functions": ["second"]' in fresh[1]["content"]
        run(item, {"path": path, "function": "second", "cases": [{"args": [2], "expected": 4}]})
        assert native_agent.completion_issue(folder, item, {"verify_passed": True}) == ""
        target = folder / (".local-ai/cale-javascript-checks.json" if javascript else ".local-ai/cale-python-checks.json")
        target.write_bytes(target.read_bytes() + b' ')
        assert "not pinned" in native_agent.completion_issue(folder, item, {"verify_passed": True})
        (folder / ".local-ai/verify.ps1").write_text("# User integration suite\nexit 0\n")
        assert native_agent.completion_issue(folder, item, {"verify_passed": True}) == ""


def test_api_declaration_contract_ignores_ambiguous_modules_calls_and_documentation(tmp_path):
    item = project(tmp_path / "contract-parser")
    item["brief"] = "Repair logic.mjs.\n`first(x,y)` adds.\n- second(value) doubles."
    assert native_agent.declared_function_contract(item) == ("logic.mjs", ["first", "second"])
    for brief in ("Explain how to implement logic.js.\nfirst(x)\nsecond(y)",
                  "Repair logic.js and other.js.\nfirst(x)\nsecond(y)",
                  "Repair logic.js. Examples: first(1), second(2).",
                  "Repair ../outside.js.\nfirst(x)\nsecond(y)"):
        item["brief"] = brief
        assert native_agent.declared_function_contract(item) is None


def test_fresh_step_brief_reads_live_bounded_source_not_cached_or_unrelated_files(tmp_path):
    item = project(tmp_path / "live-step-source")
    folder = Path(item["folder"])
    source = folder / "logic.js"
    source.write_text("function calculate(x){return x+1}")
    (folder / "private.txt").write_text("unrelated private phrase")
    (folder / ".local-ai/PLAN.md").write_text("- [ ] Calculate\n" + "older plan detail\n" * 300)
    task_context.plan_steps(folder, [{"phase": "Logic", "title": "Calculate", "check_tool": "check_javascript_function",
        "check_arguments": {"path": "logic.js", "function": "calculate", "cases": [{"args": [2], "expected": 3}]}}])
    messages = [{"role": "system", "content": "brief"}]
    first = native_agent.fresh_messages(messages, folder, {}, item)[1]["content"]
    assert '"source": "function calculate(x){return x+1}"' in first
    assert first.index("Small-step execution queue") < first.index("Live acceptance source") < first.index("PLAN.md:")
    assert "unrelated private phrase" not in first
    stamp = source.stat()
    source.write_text("function calculate(x){return x+2}")
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = native_agent.fresh_messages(messages, folder, {}, item)[1]["content"]
    assert '"source": "function calculate(x){return x+2}"' in second
    assert '"source": "function calculate(x){return x+1}"' not in second
    source.write_text("function calculate(x){return x}" + "/**/" * 2000)
    large = native_agent.fresh_messages(messages, folder, {}, item)[1]["content"]
    assert "Live acceptance source" not in large


def test_native_game_starter_is_project_scoped_available_during_steps_and_not_claimed_verified(tmp_path):
    item = project(tmp_path / "starter-tool")
    folder = Path(item["folder"])
    result = json.loads(native_agent.execute_tool(folder, "create_game_starter", {"path": "game.html", "title": "Flight test"}))
    assert result["path"] == "game.html" and result["tests_run"] is False
    assert result["suggested_check"]["path"] == "game.html" and len(result["suggested_check"]["cases"]) == 3
    original = (folder / "game.html").read_bytes()
    assert native_agent.execute_tool(folder, "create_game_starter", {"path": "game.html"}).startswith("Tool error:")
    assert (folder / "game.html").read_bytes() == original
    assert native_agent.execute_tool(folder, "create_game_starter", {"path": "../outside.html"}).startswith("Tool error:")
    task_context.plan_steps(folder, [{"phase": "Game", "title": "Build", "check_tool": "check_webapp",
        "check_arguments": result["suggested_check"]}])
    available = native_agent.step_tools(item, {}, native_agent.TOOLS)
    assert "create_game_starter" in {tool["function"]["name"] for tool in available}
    other = project(tmp_path / "starter-state")
    native_agent.save_state(other, {"status": "continue", "verify_passed": True})
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "starter", "function": {
        "name": "create_game_starter", "arguments": json.dumps({"path": "index.html"})}}]}}]}
    with patch.object(long_tasks, "projects", return_value=[other]), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(other, complete=lambda _: response)
    assert state["verify_passed"] is False and state["status"] == "continue"


def test_focused_game_starter_selects_one_tool_and_saves_real_baseline_checks(tmp_path):
    item = project(tmp_path / "starter-selector")
    item["brief"] = "Build a playable offline HTML space shooter in index.html."
    folder = Path(item["folder"])
    state = {"status": "running", "turns": 0}
    native_agent.save_state(item, state)
    response = {"choices": [{"message": {"tool_calls": [{"id": "build", "type": "function", "function": {
        "name": "create_game_starter", "arguments": json.dumps({"path": "index.html", "title": "Star Watch"})}}]}}]}
    seen = []
    def choose(payload):
        seen.append(payload)
        return response
    messages, state = native_agent.start_game_starter(item, folder, choose, [], state)
    assert seen[0]["tool_choice"]["function"]["name"] == "create_game_starter"
    assert len(seen[0]["tools"]) == 1 and (folder / "index.html").is_file()
    assert state["turns"] == 1 and state["verify_passed"] is False
    assert task_context.step_status(folder)["remaining"] == 2
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.advance_checked_steps(item, state, messages)
    assert task_context.step_status(folder)["done"] == 2 and state["browser_verified"]
    assert state["verify_passed"] and state["status"] == "running"


def test_game_starter_selection_respects_new_instructions_frameworks_and_existing_files(tmp_path):
    import pytest

    for index, brief in enumerate(("Build a space shooter from scratch in index.html.",
                                  "Build a React space shooter.", "Write a review of a playable space shooter.",
                                  "Build a 3D space shooter.", "Build a space shooter without templates.")):
        item = project(tmp_path / str(index))
        item["brief"] = brief
        assert native_agent.game_starter_candidate(item) is None
    item = project(tmp_path / "selection-race")
    item["brief"] = "Create an offline space shooter in index.html."
    folder = Path(item["folder"])
    native_agent.save_state(item, {"status": "running"})
    old_request = task_context.request_identity(folder)
    def change_request(_):
        (folder / ".local-ai/USER-REQUESTS.md").write_text("Do not make the game now.")
        return {"choices": [{"message": {"tool_calls": [{"id": "build", "function": {
            "name": "create_game_starter", "arguments": '{"path":"index.html"}'}}]}}]}
    native_agent.start_game_starter(item, folder, change_request, [], {"status": "running"})
    assert not (folder / "index.html").exists() and task_context.step_status(folder)["total"] == 0
    assert native_agent.game_starter_candidate(item) is None
    with pytest.raises(ValueError, match="instructions changed"):
        task_context.plan_steps(folder, [{"phase": "Game", "title": "Old plan", "check_command": "echo check"}], expected_request=old_request)
    (folder / "index.html").write_text("user's existing game")
    assert native_agent.game_starter_candidate(item) is None


def test_owned_game_verifier_checks_request_and_never_auto_executes_forged_shell(tmp_path):
    import pytest

    item = project(tmp_path / "owned-game")
    folder = Path(item["folder"])
    native_agent.execute_tool(folder, "create_game_starter", {"path": "index.html"})
    native_agent.prepare_game_verifier(folder, "index.html")
    assert native_agent.owned_game_verifier(folder)
    spec = folder / ".local-ai/space-shooter-check.json"
    original = spec.read_bytes()
    spec.write_bytes(original + b' ')
    assert not native_agent.owned_game_verifier(folder)
    spec.write_bytes(original)
    (folder / ".local-ai/USER-REQUESTS.md").write_text("A new instruction")
    assert not native_agent.owned_game_verifier(folder)
    (folder / ".local-ai/USER-REQUESTS.md").write_text("")
    verifier = folder / ".local-ai/verify.ps1"
    verifier.write_text("# Generated by Cale check_game_starter v1\nSet-Content should-not-run.txt bad\nexit 0\n")
    assert not native_agent.owned_game_verifier(folder)
    task_context.plan_steps(folder, [{"phase": "Game", "title": "Verify", "check_tool": "run_verifier"}])
    state = {"status": "running", "turns": 1}
    native_agent.save_state(item, state)
    with patch.object(native_agent, "execute_tool") as execute:
        assert native_agent.advance_checked_steps(item, state, [])
    execute.assert_not_called()
    assert not (folder / "should-not-run.txt").exists()
    native_agent.prepare_game_verifier(folder, "index.html")
    assert native_agent.owned_game_verifier(folder) and not (folder / "should-not-run.txt").exists()
    verifier.write_text("# User custom integration\nexit 0\n")
    with pytest.raises(ValueError, match="Preserve"):
        native_agent.prepare_game_verifier(folder, "index.html")


def test_verified_starter_review_hands_off_only_when_scope_is_clear_and_current(tmp_path):
    for mode in ("ready", "missing", "new_instruction", "custom_feature"):
        item = project(tmp_path / mode)
        item["brief"] = "Build a playable offline HTML space shooter in index.html."
        folder = Path(item["folder"])
        (folder / ".local-ai/PLAN.md").write_text(long_tasks.INITIAL_PLAN, encoding="utf-8")
        (folder / ".local-ai/PROGRESS.md").write_text(long_tasks.INITIAL_PROGRESS, encoding="utf-8")
        state = {"status": "running", "turns": 0}
        native_agent.save_state(item, state)
        response = {"choices": [{"message": {"tool_calls": [{"id": "build", "function": {
            "name": "create_game_starter", "arguments": '{"path":"index.html"}'}}]}}]}
        messages, state = native_agent.start_game_starter(item, folder, lambda _: response, [], state)
        with patch.object(long_tasks, "projects", return_value=[item]):
            assert native_agent.advance_checked_steps(item, state, messages)
        assert state["verify_passed"] and state["browser_verified"]
        calls = []
        def review(payload):
            calls.append(payload)
            if mode == "new_instruction":
                (folder / ".local-ai/USER-REQUESTS.md").write_text("Change the game first")
            return {"choices": [{"message": {"content": json.dumps({
                "missing_features": ["A requested feature needs implementation"] if mode == "missing" else [],
                "reviewed_requirements": [item["brief"]]})}}]}
        if mode == "custom_feature":
            item["brief"] += " Add a boss and 5 lives."
        handed_off = native_agent.review_verified_game_starter(item, state, messages, review)
        assert handed_off is (mode == "ready")
        if mode == "ready":
            assert state["status"] == "done" and (folder / ".local-ai/PLAN.md").is_file()
        else:
            assert state["status"] == "running" and (folder / ".local-ai/PLAN.md").read_text(encoding="utf-8") == long_tasks.INITIAL_PLAN
        if mode == "custom_feature":
            assert not calls


def test_api_scope_requires_latest_user_provenance_and_expires_when_omitted_work_is_mentioned(tmp_path):
    import pytest

    item = project(tmp_path / "scope-provenance")
    folder = Path(item["folder"])
    item["brief"] = "Repair logic.js.\nfirst(x) adds.\nsecond(x) doubles."
    requests = folder / ".local-ai/USER-REQUESTS.md"
    requests.write_text("## Request 1\nSkip second; I no longer need it.\n")
    task_context.save_api_scope(folder, item["brief"], ["first", "second"], "exclude", ["second"],
                               "Skip second; I no longer need it.")
    assert native_agent.declared_function_contract(item)[1] == ["first"]
    # Unrelated steering keeps the proven omission. Mentioning it reopens scope.
    requests.write_text(requests.read_text() + "## Request 2\nMake first faster.\n")
    assert native_agent.declared_function_contract(item)[1] == ["first"]
    requests.write_text(requests.read_text() + "## Request 3\nActually restore second.\n")
    assert native_agent.declared_function_contract(item)[1] == ["first", "second"]
    with pytest.raises(ValueError, match="latest"):
        task_context.save_api_scope(folder, item["brief"], ["first", "second"], "exclude", ["second"],
                                   "Skip second; I no longer need it.")
    requests.write_text("Do not skip second; it must be tested.")
    with pytest.raises(ValueError, match="not to omit"):
        task_context.save_api_scope(folder, item["brief"], ["first", "second"], "exclude", ["second"], requests.read_text())
    requests.write_text("Only repair first now.")
    with patch.object(long_tasks, "projects", return_value=[item]):
        saved = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "scope", "scope_operation": "only",
            "function_names": ["first"], "instruction_quote": requests.read_text()}))
    assert saved["scope_saved"] and saved["function_coverage"]["required_functions"] == ["first"]


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


def test_native_split_and_revise_shorthand_preserve_completed_and_later_units(tmp_path):
    for action in ("split", "revise"):
        item = project(tmp_path / action)
        folder = Path(item["folder"])
        first = {"phase": "Build", "title": "Done", "check_command": "exit 0"}
        queue = task_context.plan_steps(folder, [first, {**first, "title": "Current"}, {**first, "title": "Later"}])
        task_context.record_step_check(folder, queue["active"]["id"], task_context.request_identity(folder),
                                       "Exit code: 0\n", unchanged=True)
        children = [{**first, "title": "Small one", "check_command": "exit 3"}]
        if action == "split":
            children.append({**first, "title": "Small two", "check_command": "exit 4"})
        def complete(payload):
            return {"choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "queue", "function": {"name": "task_steps", "arguments": json.dumps({"action": action, "steps": children})}},
                {"id": "stale-write", "function": {"name": "write_file", "arguments": '{"path":"stale.txt","content":"wrong step"}'}}]}}]}
        with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
            state = native_agent.run_slice(item, complete=complete)
        queue = task_context.step_status(folder)
        assert queue["done"] == 1 and queue["active"]["title"] == "Small one"
        assert queue["remaining"] == len(children) + 1 and queue["active"]["attempts"] == 0
        assert not (folder / "stale.txt").exists()
        assert "queue advanced" in state["messages"][-1]["content"]
        with project_memory.connection(folder) as conn:
            later = conn.execute("SELECT title,status FROM task_steps WHERE title='Later'").fetchone()
            assert tuple(later) == ("Later", "pending")


def test_shorthand_queue_mutations_recheck_seen_state_inside_sql_transaction(tmp_path):
    import pytest

    for action in ("split", "revise"):
        for race in ("check", "request", "advance"):
            folder = Path(project(tmp_path / (action + race))["folder"])
            step = {"phase": "Build", "title": "Original", "check_command": "exit 0"}
            active = task_context.plan_steps(folder, [step, {**step, "title": "Later"}])["active"]
            request = task_context.request_identity(folder)
            children = [{**step, "title": "Replacement"}]
            if action == "split":
                children.append({**step, "title": "Replacement two"})
            args = {"action": action, "steps": children}
            bound = native_agent.bind_step_completion(folder, args, active, request)
            if race == "check":
                task_context.revise_step(folder, active["id"], [{**step, "check_command": "exit 9"}])
            elif race == "request":
                (folder / ".local-ai/USER-REQUESTS.md").write_text("Changed instructions")
            else:
                task_context.record_step_check(folder, active["id"], request, "Exit code: 0\n", unchanged=True)
            with pytest.raises(ValueError):
                native_agent.bind_step_completion(folder, args, active, request)
            rejected = native_agent.execute_tool(folder, "task_steps", bound)
            assert rejected.startswith("Tool error:")
            with project_memory.connection(folder) as conn:
                assert conn.execute("SELECT COUNT(*) FROM task_steps WHERE title LIKE 'Replacement%'").fetchone()[0] == 0
                if race == "check":
                    assert conn.execute("SELECT check_command FROM task_steps WHERE id=?", (active["id"],)).fetchone()[0] == "exit 9"
                else:
                    assert conn.execute("SELECT title FROM task_steps WHERE id=?", (active["id"],)).fetchone()[0] == "Original"


def test_queue_shorthand_does_not_retarget_explicit_ids_or_public_dispatch(tmp_path):
    folder = Path(project(tmp_path / "strict-queue")["folder"])
    step = {"phase": "Build", "title": "Original", "check_command": "exit 0"}
    active = task_context.plan_steps(folder, [step])["active"]
    request = task_context.request_identity(folder)
    for action in ("split", "revise"):
        children = [step, step] if action == "split" else [step]
        wrong = {"action": action, "steps": children, "step_id": active["id"] + 100}
        assert native_agent.bind_step_completion(folder, wrong, active, request) == wrong
        assert native_agent.execute_tool(folder, "task_steps", wrong).startswith("Tool error:")
        assert "step_id" in native_agent.execute_tool(folder, "task_steps", {"action": action, "steps": children})
    assert task_context.step_status(folder)["active"]["title"] == "Original"


def test_browser_suite_revision_cannot_be_stored_or_replayed_as_step_acceptance(tmp_path):
    import pytest

    folder = Path(project(tmp_path / "revision-replay")["folder"])
    args = {"path": "index.html", "cases": [{"expect": {"error": "Numeric"}}]}
    step = {"phase": "Build", "title": "Validation", "check_tool": "check_webapp", "check_arguments": args}
    active = task_context.plan_steps(folder, [step])["active"]
    for key, value in (("saved_case", 1), ("saved_sequence", 1), ("suite_sha256", "a" * 64)):
        revised = {**step, "check_arguments": {**args, key: value}}
        with pytest.raises(ValueError, match="one-time checker action"):
            task_context.revise_step(folder, active["id"], [revised])
        assert task_context.step_status(folder)["active"]["check_arguments"] == args
    # Simulate a record saved by an earlier release. Completion must not
    # dispatch its one-time assertion mutation or count it as an attempted check.
    legacy = {**args, "saved_sequence": 1, "saved_case": 1, "suite_sha256": "a" * 64}
    with project_memory.connection(folder) as conn:
        conn.execute("UPDATE task_steps SET check_arguments=? WHERE id=?", (json.dumps(legacy), active["id"]))
    dispatch = native_agent.execute_tool
    called = []
    def observed(folder, name, arguments):
        called.append(name)
        return dispatch(folder, name, arguments)
    with patch.object(native_agent, "execute_tool", side_effect=observed):
        result = native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]})
    assert "one-time checker action" in result
    assert called == ["task_steps"]
    assert task_context.step_status(folder)["active"]["attempts"] == 0


def bound_browser_revision_fixture(tmp_path):
    """Create one actual failed browser acceptance with its owned revision ref."""
    item = project(tmp_path)
    folder = Path(item["folder"])
    source = '<form id="expense-form"><input id="amount"></form><table id="expense-table"><tr><td>Ready</td></tr></table>'
    (folder / "index.html").write_text(source)
    step = {"phase": "Layout", "title": "Show form and table", "check_tool": "check_webapp",
            "check_arguments": {"path": "index.html", "cases": [{"visible": ["form", "table"]}]}}
    active = task_context.plan_steps(folder, [step])["active"]
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not failed["passed"] and failed["assertion_revision_reference"]
    revision = {**failed["assertion_revision_reference"], "cases": [{"visible": ["expense-form", "expense-table"]}]}
    return item, folder, step, task_context.step_status(folder)["active"], revision, source


@pytest.mark.parametrize("bound", [False, True])
def test_explicit_browser_revision_aligns_only_worker_bound_sql_then_actual_complete_passes(tmp_path, bound):
    item, folder, step, active, revision, source = bound_browser_revision_fixture(tmp_path / str(bound))
    args = (native_agent.bind_browser_assertion_revision(folder, revision, active, task_context.request_identity(folder))
            if bound else revision)
    state = tmp_path / "state"
    state.mkdir()
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        result = json.loads(native_agent.execute_tool(folder, "check_webapp", args))
        assert result["passed"] and result["verifier_check_passed"]
        current = task_context.step_status(folder)
        assert current["done"] == 0 and current["active"]["attempts"] == 1
        assert all(current["active"][key] == step[key] for key in ("phase", "title", "check_tool"))
        if bound:
            assert result["active_step_check_aligned"] is True
            assert current["active"]["check_arguments"]["cases"] == revision["cases"]
            completed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
            assert completed["passed"] and completed["done"] == 1 and completed["remaining"] == 0
            assert "already used" in native_agent.execute_tool(folder, "check_webapp", args)
        else:
            assert "active_step_check_aligned" not in result
            assert current["active"]["check_arguments"] == step["check_arguments"]
    assert (folder / "index.html").read_text() == source


@pytest.mark.parametrize("race", ["acceptance", "control", "request"])
def test_bound_browser_revision_never_overwrites_newer_acceptance_or_request(tmp_path, race):
    item, folder, step, active, revision, source = bound_browser_revision_fixture(tmp_path / race)
    request = task_context.request_identity(folder)
    bound = native_agent.bind_browser_assertion_revision(folder, revision, active, request)
    if race == "acceptance":
        task_context.revise_step(folder, active["id"], [{**step, "title": "Newer worker acceptance"}])
    elif race == "request":
        (folder / ".local-ai/USER-REQUESTS.md").write_text("Changed user instructions")
    state = tmp_path / "state"
    state.mkdir()
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        result = native_agent.execute_tool(folder, "check_webapp", bound,
            before_step_commit=(lambda: False) if race == "control" else None)
    if race == "request":
        assert result.startswith("Tool error:") and "instructions changed" in result
    else:
        decoded = json.loads(result)
        assert decoded["passed"] and decoded["active_step_check_aligned"] is False
    with project_memory.connection(folder) as conn:
        row = conn.execute("SELECT title,check_arguments,status FROM task_steps WHERE id=?", (active["id"],)).fetchone()
    assert json.loads(row["check_arguments"]) == step["check_arguments"] and row["status"] == "active"
    assert row["title"] == ("Newer worker acceptance" if race == "acceptance" else step["title"])
    assert (folder / "index.html").read_text() == source
    with pytest.raises(ValueError):
        native_agent.bind_browser_assertion_revision(folder, {**revision, "_active_browser_revision": "forged"}, active, request)


def test_browser_revision_link_requires_same_actions_viewport_and_retained_assertions(tmp_path):
    item = project(tmp_path / "revision-scope")
    folder = Path(item["folder"])
    (folder / "index.html").write_text("<p>Ready</p>")
    old = {"path": "index.html", "cases": [{"set": {"amount": "2"}, "expect": {"total": "2", "status": "Ready"},
                                              "visible": ["form", "table"]}]}
    active = {"check_arguments": {"path": "index.html", "cases": [{"set": {"amount": "2"},
        "expect": {"total": "2"}, "visible": ["form"]}]}}
    assert native_agent.browser_revision_matches_step(folder, active, old)
    for altered in ({**old, "viewport_width": 800}, {**old, "path": "other.html"},
                    {**old, "cases": [{**old["cases"][0], "set": {"amount": "3"}}]},
                    {**old, "cases": [{**old["cases"][0], "expect": {"total": "9"}}]},
                    {**old, "cases": [{**old["cases"][0], "visible": ["table"]}]}):
        assert not native_agent.browser_revision_matches_step(folder, active, altered)
    assert "missing, expired or already used" in native_agent.execute_tool(folder, "check_webapp", {
        "path": "index.html", "saved_sequence": 1, "saved_case": 1, "suite_sha256": "0" * 64,
        "cases": [{"visible": ["form"]}], "_active_browser_revision": "forged"})


def test_bound_browser_revision_creates_native_batch_boundary_without_completing_step(tmp_path):
    item, folder, step, active, revision, source = bound_browser_revision_fixture(tmp_path / "browser-revision-boundary")
    state = tmp_path / "state"
    state.mkdir()
    def complete(payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "explicit-repair", "function": {"name": "check_webapp", "arguments": json.dumps(revision)}},
            {"id": "stale", "function": {"name": "write_file", "arguments": '{"path":"stale.txt","content":"wrong check"}'}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        native_agent.run_slice(item, complete=complete)
    queue = task_context.step_status(folder)
    assert queue["done"] == 0 and queue["active"]["check_arguments"]["cases"] == revision["cases"]
    assert not (folder / "stale.txt").exists()
    assert (folder / "index.html").read_text() == source


def test_worker_pause_during_explicit_browser_revision_blocks_sql_alignment(tmp_path):
    import webapp_check
    item, folder, step, active, revision, source = bound_browser_revision_fixture(tmp_path / "pause-browser-revision")
    state_folder = tmp_path / "state"
    state_folder.mkdir()
    check = webapp_check.check
    def pause_after_check(*args, **kwargs):
        result = check(*args, **kwargs)
        state = native_agent.read_state(item)
        state["status"] = "paused"
        native_agent.save_state(item, state)
        return result
    def complete(payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "explicit-repair", "function": {"name": "check_webapp", "arguments": json.dumps(revision)}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state_folder), patch.object(webapp_check, "check", side_effect=pause_after_check):
        state = native_agent.run_slice(item, complete=complete)
    assert state["status"] == "paused"
    queue = task_context.step_status(folder)
    assert queue["done"] == 0 and queue["active"]["check_arguments"] == step["check_arguments"]
    assert (folder / "index.html").read_text() == source


def failed_typed_revision_fixture(tmp_path):
    """Persist a real failed Python acceptance attempt for narrow repair tests."""
    item = project(tmp_path)
    folder = Path(item["folder"])
    (folder / "logic.py").write_text("def identity(value): return value\n")
    step = {"phase": "Check", "title": "Identity examples", "check_tool": "check_python_function",
            "check_arguments": {"path": "logic.py", "function": "identity", "cases": [{"args": [2], "expected": 3}]}}
    active = task_context.plan_steps(folder, [step])["active"]
    result = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert result["passed"] is False
    return item, folder, step, task_context.step_status(folder)["active"]


def test_failed_browser_step_can_revise_only_typed_arguments_then_pass_real_check(tmp_path):
    from jsonschema import ValidationError, validate
    item = project(tmp_path / "browser-argument-repair")
    folder = Path(item["folder"])
    source = '<form id="expense-form"><input id="amount"></form><table id="expense-table"><tr><td>Ready</td></tr></table>'
    (folder / "index.html").write_text(source)
    original = {"phase": "Layout", "title": "Show the expense form and table", "check_tool": "check_webapp",
                "check_arguments": {"path": "index.html", "cases": [{"visible": ["form", "table"]}]}}
    active = task_context.plan_steps(folder, [original])["active"]
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not failed["passed"] and failed["active"]["attempts"] == 1
    assert failed["check_summary"]["cases"][0]["target_inventory"]["targets"]
    global_tools = copy.deepcopy(native_agent.TOOLS)
    focused = native_agent.acceptance_repair_tools(item, {"step_focus_turns": 1}, native_agent.TOOLS)
    schema = next(tool["function"]["parameters"] for tool in focused if tool["function"]["name"] == "task_steps")
    corrected = {"action": "revise_check", "check_arguments": {
        "path": "index.html", "cases": [{"visible": ["expense-form", "expense-table"]}]}}
    validate(corrected, schema)
    validate({"action": "complete", "step_id": active["id"]}, schema)
    for invalid in ({**corrected, "step_id": active["id"] + 1},
                    {**corrected, "steps": [original]},
                    {"action": "complete", "check_arguments": corrected["check_arguments"]},
                    {**corrected, "check_arguments": {**corrected["check_arguments"], "path": "different.html"}}):
        with pytest.raises(ValidationError):
            validate(invalid, schema)
    seen = task_context.step_status(folder)["active"]
    bound = native_agent.bind_step_completion(folder, corrected, seen, task_context.request_identity(folder))
    revised = json.loads(native_agent.execute_tool(folder, "task_steps", bound))
    assert revised["done"] == 0 and revised["active"]["attempts"] == 1
    assert all(revised["active"][key] == original[key] for key in ("phase", "title", "check_tool"))
    assert revised["active"]["check_arguments"] == corrected["check_arguments"]
    assert "not yet passed" in revised["active"]["evidence"]
    assert native_agent.acceptance_repair_tools(item, {"step_focus_turns": 1}, native_agent.TOOLS) is native_agent.TOOLS
    (tmp_path / "state").mkdir()
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", tmp_path / "state"):
        # SQL acceptance and the independent retained suite are distinct. The
        # shortcut deliberately does not rewrite already-retained assertions.
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")
        assert task_context.step_status(folder)["done"] == 0
        repaired_suite = json.loads(native_agent.execute_tool(folder, "check_webapp", {
            **failed["assertion_revision_reference"], "cases": corrected["check_arguments"]["cases"]}))
        assert repaired_suite["passed"]
        completed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert completed["passed"] and completed["done"] == 1 and completed["remaining"] == 0, completed
    assert (folder / "index.html").read_text() == source
    assert native_agent.TOOLS == global_tools


def test_narrow_check_revision_rejects_noop_foreign_target_and_early_attempt(tmp_path):
    item, folder, step, active = failed_typed_revision_fixture(tmp_path / "narrow-rules")
    base = {"action": "revise_check", "step_id": active["id"], "check_arguments": step["check_arguments"]}
    assert "saved step is unchanged" in native_agent.execute_tool(folder, "task_steps", base)
    for field, value in (("path", "other.py"), ("function", "other")):
        changed = {**base, "check_arguments": {**base["check_arguments"], field: value}}
        assert "ordinary revise" in native_agent.execute_tool(folder, "task_steps", changed)
    assert "only the active check_arguments" in native_agent.execute_tool(folder, "task_steps", {
        **base, "check_tool": "check_webapp"})
    assert task_context.step_status(folder)["active"]["check_arguments"] == step["check_arguments"]
    # Deliberate target changes remain available through the ordinary repair
    # contract; the narrow shortcut cannot silently retarget acceptance.
    replacement = {**step, "check_arguments": {**step["check_arguments"], "path": "other.py"}}
    assert json.loads(native_agent.execute_tool(folder, "task_steps", {
        "action": "revise", "step_id": active["id"], "steps": [replacement]}))["done"] == 0
    assert "failed structured acceptance attempt" in native_agent.execute_tool(folder, "task_steps", base)
    assert native_agent.acceptance_repair_tools(item, {"step_focus_turns": 0}, native_agent.TOOLS) is native_agent.TOOLS


@pytest.mark.parametrize("race", ["check", "request", "advance"])
def test_narrow_revision_binds_explicit_and_implicit_ids_and_rejects_races(tmp_path, race):
    for explicit in (False, True):
        item, folder, step, active = failed_typed_revision_fixture(tmp_path / (race + str(explicit)))
        request = task_context.request_identity(folder)
        corrected = {**step["check_arguments"], "cases": [{"args": [2], "expected": 2}]}
        args = {"action": "revise_check", "check_arguments": corrected}
        if explicit:
            args["step_id"] = active["id"]
        bound = native_agent.bind_step_completion(folder, args, active, request)
        if race == "check":
            task_context.revise_step(folder, active["id"], [{**step, "title": "Newer acceptance"}])
        elif race == "request":
            (folder / ".local-ai/USER-REQUESTS.md").write_text("Changed user request")
        else:
            task_context.record_step_check(folder, active["id"], request, "Exit code: 0\n", unchanged=True)
        with pytest.raises(ValueError):
            native_agent.bind_step_completion(folder, args, active, request)
        assert native_agent.execute_tool(folder, "task_steps", bound).startswith("Tool error:")
        with project_memory.connection(folder) as conn:
            saved = json.loads(conn.execute("SELECT check_arguments FROM task_steps WHERE id=?", (active["id"],)).fetchone()[0])
            assert saved == step["check_arguments"]


def test_narrow_revision_repeats_seen_check_comparison_inside_sql_transaction(tmp_path):
    item, folder, step, active = failed_typed_revision_fixture(tmp_path / "revision-sql-race")
    request = task_context.request_identity(folder)
    corrected = {**step["check_arguments"], "cases": [{"args": [2], "expected": 2}]}
    bound = native_agent.bind_step_completion(folder, {"action": "revise_check", "check_arguments": corrected}, active, request)
    original_revise = task_context.revise_step
    def raced(folder, step_id, steps, **kwargs):
        original_revise(folder, step_id, [{**step, "title": "Other worker revised acceptance"}])
        return original_revise(folder, step_id, steps, **kwargs)
    with patch.object(task_context, "revise_step", side_effect=raced):
        result = native_agent.execute_tool(folder, "task_steps", bound)
    assert "acceptance check changed before revising" in result
    current = task_context.step_status(folder)["active"]
    assert current["title"] == "Other worker revised acceptance"
    assert current["check_arguments"] == step["check_arguments"]


def test_narrow_revision_is_a_queue_boundary_and_does_not_apply_stale_next_tool(tmp_path):
    item, folder, step, active = failed_typed_revision_fixture(tmp_path / "revision-boundary")
    corrected = {**step["check_arguments"], "cases": [{"args": [2], "expected": 2}]}
    def complete(payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "repair", "function": {"name": "task_steps", "arguments": json.dumps({
                "action": "revise_check", "check_arguments": corrected})}},
            {"id": "stale", "function": {"name": "write_file", "arguments": '{"path":"stale.txt","content":"wrong step"}'}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        native_agent.run_slice(item, complete=complete)
    assert task_context.step_status(folder)["done"] == 0
    assert task_context.step_status(folder)["active"]["check_arguments"] == corrected
    assert not (folder / "stale.txt").exists()


def test_automatic_browser_unit_progress_does_not_keep_stale_global_verified_state(tmp_path):
    item, source, first, future, active = future_browser_phase_fixture(tmp_path / "automatic-phase-global-state",
        [{"visible": ["expense-form", "expense-table"]}])
    folder = Path(item["folder"])
    state = {"status": "running", "verify_passed": True, "browser_verified": True}
    native_agent.save_state(item, state)
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        assert native_agent.advance_checked_steps(item, state, [])
    queue = task_context.step_status(folder)
    assert queue["done"] == 1 and queue["active"]["phase"] == "Validation"
    assert state["verify_passed"] is False and state["browser_verified"] is False
    assert state["failed_checks"]


def test_complete_cannot_silently_ignore_replacement_steps(tmp_path):
    folder = Path(project(tmp_path / "complete-with-replacement")["folder"])
    step = {"phase": "Build", "title": "Failing", "check_command": "exit 9"}
    active = task_context.plan_steps(folder, [step])["active"]
    result = native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"],
        "steps": [{**step, "check_command": "exit 0"}]})
    assert "Use action=revise" in result
    assert task_context.step_status(folder)["active"]["attempts"] == 0
    assert task_context.step_status(folder)["active"]["check_command"] == "exit 9"


def test_structured_step_summary_keeps_failures_without_quoted_verifier_transcripts(tmp_path):
    folder = Path(project(tmp_path / "structured-summary")["folder"])
    (folder / "logic.py").write_text("def double(x): return x\n")
    step = {"phase": "Fix", "title": "Double value", "check_tool": "check_python_function",
            "check_arguments": {"path": "logic.py", "function": "double", "cases": [{"args": [4], "expected": 8}]}}
    active = task_context.plan_steps(folder, [step])["active"]
    result = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not result["passed"] and "check_output" not in result
    assert result["check_summary"]["cases"][0]["inputs"] == {"args": [4], "kwargs": {}}
    assert "8" in str(result["check_summary"]["cases"][0]["failures"])
    assert "verification" not in result["check_summary"]
    # An unstructured tool failure still supplies its concrete cause.
    dispatch = native_agent.execute_tool
    def unavailable(folder, name, arguments):
        if name == "check_python_function":
            return "Tool error: checker runtime unavailable"
        return dispatch(folder, name, arguments)
    with patch.object(native_agent, "execute_tool", side_effect=unavailable):
        failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not failed["passed"] and "runtime unavailable" in str(failed["check_summary"]["errors"])


def test_structured_step_preserves_failed_click_diagnostic(tmp_path):
    folder = Path(project(tmp_path / "click-summary")["folder"])
    (folder / "index.html").write_text('<button id="delete">Delete</button><p id="total">10</p>')
    step = {"phase": "Fix", "title": "Delete updates total", "check_tool": "check_webapp",
            "check_arguments": {"path": "index.html", "cases": [{"click": ["delete"], "expect": {"total": "0.00"}}]}}
    active = task_context.plan_steps(folder, [step])["active"]
    result = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not result["passed"]
    assert result["check_summary"]["cases"], json.dumps(result)
    diagnostic = result["check_summary"]["cases"][0]["click_diagnostics"]
    assert diagnostic["replay_confirmed"]
    assert diagnostic["observations"][0]["project_listener_count"] == 0
    assert "0 project-file-backed listeners" in task_context.checker_failures(folder, "index.html")[0]["details"]


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

        def tool(root, name, args, **kwargs):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args, **kwargs)

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
        prepare_existing_html_gate_fixture(folder, "index.html")
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
        folder = Path(item["folder"])
        (folder / "index.html").write_text("<input id='number'><p id='total'>1</p>")
        prepare_existing_html_gate_fixture(folder, "index.html", complete=False)
        with project_memory.connection(folder) as conn:
            original_steps = [dict(row) for row in conn.execute("SELECT * FROM task_steps").fetchall()]
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
        with project_memory.connection(folder) as conn:
            assert [dict(row) for row in conn.execute("SELECT * FROM task_steps").fetchall()] == original_steps


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
    prepare_existing_html_gate_fixture(folder, "game.html")
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
        prepare_existing_html_gate_fixture(folder, "index.html")
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
        assert last.startswith("20002: last line\nFile sha256: ")
        assert last.endswith(hashlib.sha256(source.read_bytes()).hexdigest())


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

        def tool(root, name, args, **kwargs):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args, **kwargs)

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

        def tool(root, name, args, **kwargs):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args, **kwargs)

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

        def tool(root, name, args, **kwargs):
            return "Exit code: 0\nPASS" if name == "run_command" else original(root, name, args, **kwargs)

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


def test_reread_recovers_stale_edit_hash_from_actual_bytes():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        source = folder / "app.py"
        source.write_bytes(b"\xef\xbb\xbf# first\r\nvalue = 1\r\n")
        assert native_agent.execute_tool(folder, "read_file", {"path": "app.py"}).startswith("1: # first\n")
        first = native_agent.execute_tool(folder, "read_file", {"path": "app.py", "start_line": 2})
        old_hash = re.search(r"File sha256: ([a-f0-9]{64})", first)[1]
        source.write_bytes(b"\xef\xbb\xbf# changed elsewhere\r\nvalue = 1\r\n")
        arguments = {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2", "expected_sha256": old_hash}
        assert "file changed since that hash" in native_agent.execute_tool(folder, "edit_file", arguments)
        reread = native_agent.execute_tool(folder, "read_file", {"path": "app.py", "start_line": 2})
        arguments["expected_sha256"] = re.search(r"File sha256: ([a-f0-9]{64})", reread)[1]
        assert arguments["expected_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest() != old_hash
        assert native_agent.execute_tool(folder, "edit_file", arguments).startswith("Edited app.py")
        assert source.read_bytes() == b"\xef\xbb\xbf# changed elsewhere\r\nvalue = 2\r\n"


def test_read_hash_describes_displayed_snapshot_even_if_live_file_changes():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        source = folder / "app.py"
        original = b"value = 1\n"
        source.write_bytes(original)
        bounded = native_agent._bounded_line
        def mutate(stream, limit):
            source.write_bytes(b"value = 3\n")
            return bounded(stream, limit)
        with patch.object(native_agent, "_bounded_line", side_effect=mutate):
            read = native_agent.execute_tool(folder, "read_file", {"path": "app.py"})
        digest = re.search(r"File sha256: ([a-f0-9]{64})", read)[1]
        assert "value = 1" in read and "value = 3" not in read
        assert digest == hashlib.sha256(original).hexdigest()
        rejected = native_agent.execute_tool(folder, "edit_file", {
            "path": "app.py", "old_text": "value = 1", "new_text": "value = 2", "expected_sha256": digest})
        assert "file changed since that hash" in rejected
        assert source.read_bytes() == b"value = 3\n"


def test_large_read_is_bounded_without_supplying_an_unusable_edit_hash():
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        folder = Path(item["folder"])
        source = folder / "large.txt"
        with source.open("wb") as stream:
            stream.write(b"x" * 10_000_001)
        read = native_agent.execute_tool(folder, "read_file", {"path": "large.txt", "max_lines": 1})
        assert "[line truncated]" in read and len(read) < 5000
        assert "No edit hash" in read and "File sha256:" not in read


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


def test_browser_runtime_exception_survives_many_case_failures_and_fresh_checkpoint(tmp_path):
    import copy
    item = project(tmp_path / "runtime-root-cause")
    folder = Path(item["folder"])
    (folder / "index.html").write_text('<input id="amount"><output id="total"></output>', encoding="utf-8")
    cases = [{"case": number, "passed": False, "inputs": {"set": {"amount": str(number)}},
              "failures": [f"#total did not display expected value for case {number}",
                           f"#rows missing expected row for case {number}"]} for number in range(1, 13)]
    report = {"passed": False, "cases": cases,
              "javascript_errors": ["ReferenceError: escapeHtml is not defined\n    at private-stack-location", "ReferenceError: escapeHtml is not defined"],
              "suite_sha256": "a" * 64}
    original = copy.deepcopy(report)
    arguments = {"path": "index.html", "cases": [{"set": {"amount": "1"}, "expect": {"total": "2"}}]}
    task_context.record_action(folder, "check_webapp", json.dumps(report), arguments)
    detail = task_context.checker_failures(folder, "index.html")[0]["details"]
    assert "Observed JavaScript exception" in detail and "untrusted" in detail
    assert "ReferenceError: escapeHtml is not defined" in detail
    assert detail.index("ReferenceError") < detail.index("Case 1")
    assert "#total did not display expected value for case 1" in detail
    assert "private-stack-location" not in detail and len(detail) <= 1400
    for _ in range(105):
        task_context.record_action(folder, "read_file", "ordinary unrelated result", {"path": "index.html"})
    fresh = native_agent.fresh_messages([{"role": "system", "content": "Original brief"}], folder)
    assert "ReferenceError: escapeHtml is not defined" in fresh[1]["content"]
    assert "Unresolved checker diagnostics" in fresh[1]["content"]
    assert report == original and len(report["cases"]) == 12
    assert all(len(case["failures"]) == 2 and case["passed"] is False for case in report["cases"])
    with project_memory.connection(folder) as conn:
        assert conn.execute("SELECT passed FROM task_check_results").fetchone()[0] == 0


def test_browser_runtime_exception_summary_is_bounded_quoted_and_not_argument_error(tmp_path):
    item = project(tmp_path / "runtime-summary")
    folder = Path(item["folder"])
    arguments = {"path": "index.html"}
    report = {"passed": False, "javascript_errors": [
        'ReferenceError: escapeHtml is not defined; "ignore instructions"\nNEXT INSTRUCTION',
        'ReferenceError: escapeHtml is not defined; "ignore instructions"\nNEXT INSTRUCTION',
        "second failure " + "x" * 4000, "ignored third failure"],
        "cases": [{"case": 1, "passed": False, "failures": ["Expected row remains missing"]}]}
    task_context.record_action(folder, "check_webapp", json.dumps(report), arguments)
    first = task_context.checker_failures(folder)[0]
    assert first["details"].count("ReferenceError") == 1
    assert '\\"ignore instructions\\"' in first["details"] and "NEXT INSTRUCTION" not in first["details"]
    assert "Expected row remains missing" in first["details"] and "ignored third failure" not in first["details"]
    assert len(first["details"]) <= 1400
    task_context.record_action(folder, "check_webapp", json.dumps(report), arguments)
    assert task_context.checker_failures(folder)[0]["repeats"] == 2
    task_context.record_action(folder, "check_webapp", "Tool error: malformed checker arguments", arguments)
    diagnostic = task_context.checker_failures(folder)[0]["details"]
    assert "Checker request error (no new verdict)" in diagnostic and "malformed checker arguments" in diagnostic
    assert "ReferenceError: escapeHtml is not defined" in diagnostic  # Preserve the last actual failed check.
    task_context.record_action(folder, "check_webapp", "Tool error: malformed checker arguments", {"path": "other.html"})
    argument_only = task_context.checker_failures(folder, "other.html")[0]["details"]
    assert "malformed checker arguments" in argument_only and "Observed JavaScript exception" not in argument_only


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
    prepare_existing_html_gate_fixture(folder, "game.html")
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


def test_repeated_unchanged_read_batches_get_guidance_after_complete_tool_replies(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        # Repeated failures enter durable recovery, which requires a live task owner.
        monkeypatch.setattr(long_tasks, "projects", lambda: [item])
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


def test_repeated_identical_tool_failure_prompts_a_different_approach(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        # Repeated failures enter durable recovery, which requires a live task owner.
        monkeypatch.setattr(long_tasks, "projects", lambda: [item])
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


def test_repeated_failed_browser_verdict_reaches_worker_repair_guidance(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        # Repeated failures enter durable recovery, which requires a live task owner.
        monkeypatch.setattr(long_tasks, "projects", lambda: [item])
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


def test_third_identical_failed_tool_call_is_not_executed_again(monkeypatch):
    with tempfile.TemporaryDirectory() as temporary:
        item = project(Path(temporary) / "page")
        # Repeated failures enter durable recovery, which requires a live task owner.
        monkeypatch.setattr(long_tasks, "projects", lambda: [item])
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


@pytest.mark.parametrize(("tool", "boundary", "recoverable"), [
    ("write_file", "after", True),
    ("edit_file", "after", True),
    ("write_file", "before", False),
    ("edit_file", "replaced", False),
])
def test_worker_recovers_only_the_exact_published_file(tmp_path, monkeypatch, tool, boundary, recoverable):
    folder = tmp_path / "notes"
    item = project(folder)
    item["brief"] = "Maintain the existing notes."
    output = folder / "notes.txt"
    output.write_text("Old note", encoding="utf-8")
    native_agent.save_state(item, {"status": "continue", "turns": 1,
        "messages": [{"role": "system", "content": "Maintain the notes."},
                     {"role": "user", "content": "Update the wording."}],
        "verify_passed": True, "browser_verified": True})
    arguments = ({"path": "notes.txt", "content": "New note"} if tool == "write_file"
                 else {"path": "notes.txt", "old_text": "Old note", "new_text": "New note"})
    replace = os.replace

    class WorkerInterrupted(BaseException):
        pass

    def stop_at_publication(source, destination):
        if Path(destination) != output:
            return replace(source, destination)
        if boundary != "before":
            replace(source, destination)
        raise WorkerInterrupted()

    monkeypatch.setattr(native_agent, "MAX_TURNS_PER_SLICE", 1)
    with monkeypatch.context() as crash:
        crash.setattr(native_agent.os, "replace", stop_at_publication)
        with pytest.raises(WorkerInterrupted):
            native_agent.run_slice(item, complete=lambda _: initial_planning_reply(tool, arguments))
    if boundary == "replaced":
        # Identical bytes on a different file are not this worker's publication.
        replacement = folder / "replacement.txt"
        replacement.write_bytes(output.read_bytes())
        replace(replacement, output)
    checkpoint = native_agent.read_state(item)
    checkpoint["messages"].append({"role": "user", "content": "Keep the next change small."})
    native_agent.save_state(item, checkpoint)

    def resumed(payload):
        replies = [msg["content"] for msg in payload["messages"] if msg["role"] == "tool"]
        expected = "Recovered saved file notes.txt" if recoverable else "Inspect the project state"
        assert any(expected in reply for reply in replies)
        return initial_planning_reply("read_file", {"path": "notes.txt"})

    resumed_state = native_agent.run_slice(item, complete=resumed)
    assert output.read_text(encoding="utf-8") == ("Old note" if boundary == "before" else "New note")
    assert not resumed_state["verify_passed"] and not resumed_state["browser_verified"]
    messages = resumed_state["messages"]
    steering = next(index for index, msg in enumerate(messages)
                    if msg.get("content") == "Keep the next change small.")
    assert [msg["role"] for msg in messages[steering-2:steering+1]] == ["assistant", "tool", "user"]


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



def test_requested_game_lives_followups_override_and_conflicts_are_not_guessed(tmp_path):
    item = project(tmp_path / "requested-lives")
    folder = Path(item["folder"])
    item["brief"] = "Build a space shooter. reset starts with score0/lives5/wave1."
    assert native_agent.requested_game_lives(item, folder) == 5
    item["brief"] = "A hit removes one life. Build a game with five starting lives."
    assert native_agent.requested_game_lives(item, folder) == 5
    item["brief"] = "Build a game with one life."
    assert native_agent.requested_game_lives(item, folder) == 1
    (folder / ".local-ai/USER-REQUESTS.md").write_text("Start and restart with 7 lives.")
    assert native_agent.requested_game_lives(item, folder) == 7
    (folder / ".local-ai/USER-REQUESTS.md").write_text("Start with 3 lives and 5 lives.")
    assert native_agent.requested_game_lives(item, folder) is None


def test_real_lives_gate_rejects_false_complete_with_default_verifier_and_prose(tmp_path):
    import design_tools

    item = project(tmp_path / "five-lives")
    item["brief"] = "Build an offline space shooter with 5 lives on start and restart."
    folder = Path(item["folder"])
    design_tools.create_space_shooter(str(folder / "index.html"))
    prepare_existing_html_gate_fixture(folder, "index.html")
    state = {"game_starter": {"path": "index.html"}, "verify_passed": True}
    # Even a claimed verifier pass and checked prose cannot pass the live gate.
    (folder / ".local-ai/PLAN.md").write_text("# Plan\n- [x] Five lives verified.")
    (folder / ".local-ai/PROGRESS.md").write_text("# Progress\nComplete with lives=5.")
    assert "5 starting/reset lives" in native_agent.starter_lives_issue(folder, item, state)
    assert "5 starting/reset lives" in native_agent.completion_issue(folder, item, state)
    with patch("webapp_check.check", side_effect=AssertionError("Unchanged failed source was rechecked")):
        assert native_agent.starter_lives_issue(folder, item, state)
    source = (folder / "index.html").read_text(encoding="utf-8")
    (folder / "index.html").write_text(source.replace("lives:3", "lives:5").replace("this.lives=3", "this.lives=5"), encoding="utf-8")
    assert native_agent.starter_lives_issue(folder, item, state) == ""
    with patch("webapp_check.check", side_effect=AssertionError("Unchanged passing source was rechecked")):
        assert native_agent.starter_lives_issue(folder, item, state) == ""
    (folder / ".local-ai/USER-REQUESTS.md").write_text("Change starting lives to 7.")
    assert "7 starting/reset lives" in native_agent.starter_lives_issue(folder, item, state)
    native_agent.prepare_game_verifier(folder, "index.html", project=item)
    spec = json.loads((folder / ".local-ai/space-shooter-check.json").read_text())
    assert spec["version"] == 2 and spec["starting_lives"] == 7



def test_verified_custom_lives_uses_bounded_review_and_preserves_completed_plan(tmp_path):
    item = project(tmp_path / "custom-review")
    item["brief"] = "Build a playable offline HTML space shooter in index.html with 5 lives."
    folder = Path(item["folder"])
    (folder / ".local-ai/PLAN.md").write_text(long_tasks.INITIAL_PLAN, encoding="utf-8")
    (folder / ".local-ai/PROGRESS.md").write_text(long_tasks.INITIAL_PROGRESS, encoding="utf-8")
    state = {"status": "running", "turns": 0}
    native_agent.save_state(item, state)
    response = {"choices": [{"message": {"tool_calls": [{"id": "build", "function": {
        "name": "create_game_starter", "arguments": '{"path":"index.html"}'}}]}}]}
    messages, state = native_agent.start_game_starter(item, folder, lambda _: response, [], state)
    path = folder / "index.html"
    source = path.read_text(encoding="utf-8")
    path.write_text(source.replace("this.lives=3", "this.lives=5"), encoding="utf-8")
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.advance_checked_steps(item, state, messages)
    assert task_context.step_status(folder)["done"] == 2 and state["verify_passed"]
    plan = "# Plan\n- [x] Implement five lives and retain the game controls.\n"
    (folder / ".local-ai/PLAN.md").write_text(plan, encoding="utf-8")
    (folder / ".local-ai/PROGRESS.md").write_text("# Progress\nRequested game verified.\n", encoding="utf-8")
    seen = []
    def review(payload):
        seen.append(payload)
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "review_game_requirements", "arguments": json.dumps({
                "missing_features": [], "reviewed_requirements": ["5 lives", "offline HTML space shooter"]})}}]}}]}
    assert native_agent.review_verified_game_starter(item, state, messages, review)
    assert state["status"] == "done"
    assert seen[0]["tool_choice"]["function"]["name"] == "review_game_requirements"
    assert (folder / ".local-ai/PLAN.md").read_text(encoding="utf-8") == plan



def test_ordinary_starter_tool_also_registers_requested_value_completion_gate(tmp_path):
    item = project(tmp_path / "ordinary-starter")
    item["brief"] = "Create a game starter component with 5 lives."
    response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "starter", "function": {
        "name": "create_game_starter", "arguments": '{"path":"index.html"}'}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        state = native_agent.run_slice(item, complete=lambda _: response)
    assert state["game_starter"]["path"] == "index.html"
    assert not state["verify_passed"]
    assert "5 starting/reset lives" in native_agent.starter_lives_issue(Path(item["folder"]), item, state)



def test_focused_function_planning_binds_named_inputs_and_appends_completed_phases(tmp_path):
    item = project(tmp_path / "phases")
    item["brief"] = "Repair logic.js.\nsubtract(x,y) returns x-y.\ndouble(x) returns x*2."
    folder = Path(item["folder"])
    (folder / "logic.js").write_text("function subtract(x,y){return x-y}\nfunction double(x){return x*2}")
    state = {"status": "running", "turns": 0}
    native_agent.save_state(item, state)
    seen = []
    def plan(payload):
        seen.append(payload)
        first = "subtract(x,y)" in payload["messages"][-1]["content"].split("Plan only")[-1].split("Other APIs")[0]
        # Deliberately reverse dictionary key order; the checker still gets x,y.
        cases = [{"inputs": {"y": 2, "x": 7}, "expected": 5}] if first else [{"inputs": {"x": 3}, "expected": 6}]
        return {"choices": [{"message": {"tool_calls": [{"id": "phase", "function": {
            "name": "plan_function_step", "arguments": json.dumps({"cases": [{"reason": "Derived from the requested arithmetic", **case} for case in cases]})}}]}}]}
    messages = []
    assert native_agent.plan_declared_function_step(item, state, messages, plan)
    queue = task_context.step_status(folder)
    assert queue["total"] == 1 and queue["active"]["check_arguments"]["cases"][0]["args"] == [7, 2]
    assert not native_agent.plan_declared_function_step(item, state, messages, plan)
    assert "Existing source for interface compatibility ONLY" in seen[0]["messages"][-1]["content"]
    assert len(seen) == 1  # Active work never gets replanned.
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.advance_checked_steps(item, state, messages)
        assert native_agent.plan_declared_function_step(item, state, messages, plan)
        assert task_context.step_status(folder)["total"] == 2 and task_context.step_status(folder)["done"] == 1
        assert native_agent.advance_checked_steps(item, state, messages)
    assert task_context.step_status(folder)["done"] == 2
    assert not native_agent.plan_declared_function_step(item, state, messages, plan)
    assert len(seen) == 2 and all(p["_native_workload"] == "function_step_planning" for p in seen)
    assert all(m["role"] != "tool" or m.get("name") != "plan_function_step" for m in messages)


def test_function_planning_preserves_existing_acceptance_and_honors_new_instructions(tmp_path):
    item = project(tmp_path / "planning-controls")
    item["brief"] = "Repair logic.js.\nfirst(x) returns x.\nsecond(x) returns x."
    folder = Path(item["folder"])
    (folder / "logic.js").write_text("function first(x){return x}\nfunction second(x){return x}")
    state = {"status": "running"}
    native_agent.save_state(item, state)
    task_context.plan_steps(folder, [queue_step("Existing integration", "echo verify")])
    def call(_):
        raise AssertionError("Existing plan must be preserved")
    assert not native_agent.plan_declared_function_step(item, state, [], call)
    # A separate clean request can be interrupted while a case plan is in flight.
    other = project(tmp_path / "planning-new-request")
    other["brief"] = item["brief"]
    new_folder = Path(other["folder"])
    native_agent.save_state(other, {"status": "running"})
    def changed(_):
        (new_folder / ".local-ai/USER-REQUESTS.md").write_text("Only explain; do not edit files.")
        return {"choices": [{"message": {"tool_calls": [{"id": "old", "function": {
            "name": "plan_function_step", "arguments": '{"cases":[{"reason":"Identity preserves x","inputs":{"x":1},"expected":1}]}'}}]}}]}
    assert not native_agent.plan_declared_function_step(other, {"status": "running"}, [], changed)
    assert task_context.step_status(new_folder)["total"] == 0


def test_invalid_function_plan_falls_back_once_and_dom_modules_use_normal_workflow(tmp_path):
    item = project(tmp_path / "planning-invalid")
    item["brief"] = "Repair logic.js.\nfirst(x) returns x.\nsecond(x) returns x."
    folder = Path(item["folder"])
    state = {"status": "running"}
    native_agent.save_state(item, state)
    calls = []
    def invalid(payload):
        calls.append(payload)
        return {"choices": [{"message": {"content": "I will plan later"}}]}
    assert not native_agent.plan_declared_function_step(item, state, [], invalid)
    assert not native_agent.plan_declared_function_step(item, state, [], invalid)
    assert len(calls) == 1 and not task_context.step_status(folder)["total"]
    state.pop("function_planning_attempted")
    (folder / "logic.js").write_text("const button=document.querySelector('button');")
    assert not native_agent.plan_declared_function_step(item, state, [], invalid)
    assert len(calls) == 1


def test_development_router_defers_live_media_but_ignores_dead_or_reused_pids(tmp_path):
    import importlib.util
    import os

    import psutil
    import pytest

    benchmark = Path(__file__).resolve().parents[1] / "benchmarks/browser_assertion_repair.py"
    if not benchmark.is_file():
        pytest.skip("Development benchmark is distributed in source archives, not the runtime manifest")
    spec = importlib.util.spec_from_file_location("benchmark_media_guard", benchmark)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "runtime"
    marker = root / "LongTasks" / f"music-gpu-active-{os.getpid()}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    with pytest.raises(RuntimeError, match="active music worker"):
        module.require_idle_media(root)
    # An old marker with a PID since reused cannot block development indefinitely.
    old = psutil.Process().create_time() - 10
    os.utime(marker, (old, old))
    module.require_idle_media(root)
    marker.rename(marker.with_name("image-gpu-active-2147483647.json"))
    module.require_idle_media(root)


def test_python_phase_acceptance_and_pause_during_planning(tmp_path):
    item = project(tmp_path / "python-phases")
    item["brief"] = "Repair helpers.py.\ndifference(left,right) returns left-right.\nidentity(value) returns value."
    folder = Path(item["folder"])
    (folder / "helpers.py").write_text("def difference(left, right): return left - right\ndef identity(value): return value\n")
    state = {"status": "running"}
    native_agent.save_state(item, state)
    def plan(_):
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "plan_function_step", "arguments": json.dumps({"cases": [
                {"reason": "7 minus 2 equals 5", "inputs": {"right": 2, "left": 7}, "expected": 5}]})}}]}}]}
    messages = []
    assert native_agent.plan_declared_function_step(item, state, messages, plan)
    assert task_context.step_status(folder)["active"]["check_tool"] == "check_python_function"
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.advance_checked_steps(item, state, messages)
    assert task_context.step_status(folder)["done"] == 1
    def paused(payload):
        native_agent.save_state(item, {**state, "status": "paused"})
        return {"choices": [{"message": {"tool_calls": [{"function": {
            "name": "plan_function_step", "arguments": json.dumps({"cases": [
                {"reason": "Identity preserves 2", "inputs": {"value": 2}, "expected": 2}]})}}]}}]}
    assert not native_agent.plan_declared_function_step(item, state, messages, paused)
    assert task_context.step_status(folder)["total"] == 1
    assert native_agent.read_state(item)["status"] == "paused"


def test_sqlite_acceptance_evidence_keeps_mismatch_before_large_source_and_inputs(tmp_path):
    item = project(tmp_path / "diagnostics")
    folder = Path(item["folder"])
    task_context.plan_steps(folder, [queue_step("Check arithmetic", "echo verify")])
    step = task_context.step_status(folder)["active"]
    mismatch = "Expected 35, got 30. Check the requested clamp."
    result = json.dumps({"passed": False, "cases": [{"path": "logic.js", "function": "move",
        "case": 6, "inputs": {"args": ["x" * 1000]}, "function_signature": "function move" + "x" * 2000,
        "passed": False, "failures": [mismatch]}]})
    translated = native_agent.step_exit_result("check_javascript_function", result)
    # Exercise the actual bounded SQL storage, not merely the formatter output.
    task_context.record_step_check(folder, step["id"], task_context.request_identity(folder), translated, unchanged=True)
    evidence = task_context.step_status(folder)["active"]["evidence"]
    assert mismatch in evidence
    assert "function move" not in evidence
    assert native_agent.step_exit_result("check_python_function", '{"passed":true}').startswith("Exit code: 0\n")
    assert "Acceptance check failed" in native_agent.step_exit_result("check_python_function", "bad json")


def test_native_worker_recovers_schema_rejection_without_executing_invalid_call(tmp_path):
    from jsonschema import ValidationError
    item = project(tmp_path / "syntax-recovery")
    seen = []
    def call(payload):
        seen.append(payload)
        if len(seen) == 1:
            raise cloud_ai.ToolArgumentError("task_steps", ValidationError("wrong type", validator="type", validator_value="array", instance="[]"))
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "read", "type": "function",
            "function": {"name": "list_files", "arguments": "{}"}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2):
        state = native_agent.run_slice(item, complete=call)
    assert len(seen) == 2
    assert any("No progress was accepted" in m.get("content", "") for m in seen[1]["messages"])
    assert state["turns"] == 2
    assert "argument_correction_failures" not in state
    assert task_context.step_status(item["folder"])["total"] == 0


def test_native_schema_recovery_is_bounded_and_honors_stop(tmp_path):
    import pytest
    from jsonschema import ValidationError
    item = project(tmp_path / "syntax-bound")
    def rejected(_):
        raise cloud_ai.ToolArgumentError("task_steps", ValidationError("wrong type", validator="type", validator_value="array", instance="[]"))
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 4), pytest.raises(cloud_ai.ToolArgumentError):
        native_agent.run_slice(item, complete=rejected)
    assert native_agent.read_state(item)["argument_correction_failures"] == 2
    other = project(tmp_path / "syntax-stop")
    def stopped(payload):
        native_agent.save_state(other, {"status": "stopped"})
        return rejected(payload)
    assert native_agent.run_slice(other, complete=stopped)["status"] == "stopped"


def test_function_notes_follow_real_acceptance_and_survive_restart(tmp_path):
    item = project(tmp_path / "owned-notes")
    item["brief"] = "Repair helpers.py.\nfirst(value) returns value.\nsecond(value) returns value."
    folder = Path(item["folder"])
    (folder / "helpers.py").write_text("def first(value): return value\ndef second(value): return value\n")
    state = {"status": "running"}
    native_agent.save_state(item, state)
    def plan(_):
        return {"choices": [{"message": {"tool_calls": [{"function": {"name": "plan_function_step",
            "arguments": json.dumps({"cases": [{"reason": "Identity preserves 2", "inputs": {"value": 2}, "expected": 2}]})}}]}}]}
    messages = []
    assert not native_agent.sync_function_notes(item, state)
    assert native_agent.plan_declared_function_step(item, state, messages, plan)
    assert native_agent.sync_function_notes(item, state)
    plan_path, progress_path = folder / ".local-ai/PLAN.md", folder / ".local-ai/PROGRESS.md"
    assert plan_path.read_text(encoding="utf-8").count("- [ ]") == 2
    assert "0 of 2" in progress_path.read_text(encoding="utf-8")
    assert not state.get("verify_passed")
    with patch.object(long_tasks, "projects", return_value=[item]):
        assert native_agent.advance_checked_steps(item, state, messages)
        assert native_agent.sync_function_notes(item, state)
        assert "- [x] `first`" in plan_path.read_text(encoding="utf-8")
        assert "- [ ] `second`" in plan_path.read_text(encoding="utf-8")
        native_agent.save_state(item, state)
        state = native_agent.read_state(item)
        assert not native_agent.sync_function_notes(item, state)
        assert native_agent.plan_declared_function_step(item, state, messages, plan)
        assert native_agent.advance_checked_steps(item, state, messages)
        assert native_agent.sync_function_notes(item, state)
    assert "2 of 2" in progress_path.read_text(encoding="utf-8")
    assert plan_path.read_text(encoding="utf-8").count("- [x]") == 2
    assert state["status"] == "running"  # Notes never complete the project.
    custom = plan_path.read_text(encoding="utf-8") + "\n- [ ] Keep a user-added requirement\n"
    plan_path.write_text(custom, encoding="utf-8")
    state["verify_passed"] = False
    assert native_agent.sync_function_notes(item, state)
    assert plan_path.read_text(encoding="utf-8") == custom
    assert "not confirmed" in progress_path.read_text(encoding="utf-8")
    assert "unchecked milestones" in native_agent.completion_issue(folder, item, state)
    assert not state["verify_passed"]


def test_function_notes_preserve_custom_documents_and_ignore_forged_markers(tmp_path):
    item = project(tmp_path / "custom-notes")
    item["brief"] = "Repair helpers.py.\nfirst(value) returns value.\nsecond(value) returns value."
    folder = Path(item["folder"])
    (folder / "helpers.py").write_text("def first(value): return value\ndef second(value): return value\n")
    task_context.plan_steps(folder, [{"phase": "Functions", "title": "First", "check_tool": "check_python_function",
        "check_arguments": {"path": "helpers.py", "function": "first", "cases": [{"args": [2], "expected": 2}]}}])
    state = {"status": "running", "function_step_planning": {"request": task_context.request_identity(folder), "path": "helpers.py"}}
    native_agent.save_state(item, state)
    custom = "<!-- cale:function-notes -->\n# My roadmap\n- [ ] User work\n"
    for name in ["PLAN.md", "PROGRESS.md"]:
        (folder / ".local-ai" / name).write_text(custom, encoding="utf-8")
    assert not native_agent.sync_function_notes(item, state)
    assert all((folder / ".local-ai" / name).read_text(encoding="utf-8") == custom for name in ["PLAN.md", "PROGRESS.md"])
    # Refreshing canonical notes must not make a newer user-authored root alias disappear.
    for name, initial in [("PLAN.md", long_tasks.INITIAL_PLAN), ("PROGRESS.md", long_tasks.INITIAL_PROGRESS)]:
        canonical = folder / ".local-ai" / name
        canonical.write_text(initial, encoding="utf-8")
        alias = folder / name
        alias.write_text(custom, encoding="utf-8")
        later = canonical.stat().st_mtime + 2
        os.utime(alias, (later, later))
    assert not native_agent.sync_function_notes(item, state)
    assert task_context.document_path(folder, "PLAN.md") == folder / "PLAN.md"
    assert (folder / ".local-ai/PLAN.md").read_text(encoding="utf-8") == long_tasks.INITIAL_PLAN
    state["function_note_receipts"] = None
    assert not native_agent.sync_function_notes(item, state)


def test_function_notes_recheck_controls_and_concurrent_edits_before_replace(tmp_path):
    for mode in ["pause", "instruction", "edit", "alias"]:
        item = project(tmp_path / mode)
        item["brief"] = "Repair helpers.py.\nfirst(value) returns value.\nsecond(value) returns value."
        folder = Path(item["folder"])
        (folder / "helpers.py").write_text("def first(value): return value\ndef second(value): return value\n")
        task_context.plan_steps(folder, [{"phase": "Functions", "title": "First", "check_tool": "check_python_function",
            "check_arguments": {"path": "helpers.py", "function": "first", "cases": [{"args": [2], "expected": 2}]}}])
        state = {"status": "running", "function_step_planning": {"request": task_context.request_identity(folder), "path": "helpers.py"}}
        native_agent.save_state(item, state)
        note = folder / ".local-ai/PLAN.md"
        note.write_text(long_tasks.INITIAL_PLAN, encoding="utf-8")
        original = native_agent.tempfile.mkstemp
        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            if mode == "pause":
                native_agent.save_state(item, {**state, "status": "paused"})
            elif mode == "instruction":
                (folder / ".local-ai/USER-REQUESTS.md").write_text("New request: explain only.")
            elif mode == "alias":
                alias = folder / "PLAN.md"
                alias.write_text("# A newer root roadmap\n", encoding="utf-8")
                later = note.stat().st_mtime + 2
                os.utime(alias, (later, later))
            else:
                note.write_text("# User edited this while the refresh was running\n", encoding="utf-8")
            return result
        with patch.object(native_agent.tempfile, "mkstemp", side_effect=interrupted):
            native_agent.sync_function_notes(item, state)
        expected = "# User edited this while the refresh was running\n" if mode == "edit" else long_tasks.INITIAL_PLAN
        assert note.read_text(encoding="utf-8") == expected
        assert "PLAN.md" not in state.get("function_note_receipts", {})
        assert not list((folder / ".local-ai").glob(".cale-notes-*.tmp"))
        if mode == "pause":
            assert native_agent.read_state(item)["status"] == "paused"


def test_automatic_function_acceptance_planning_can_reason_instead_of_quick_mode(tmp_path):
    item = project(tmp_path / "reasoned-planning")
    item["brief"] = "Repair helpers.py.\nfirst(value) returns value.\nsecond(value) returns value."
    seen = []
    def complete(payload, task_id, **options):
        seen.append((payload, options))
        return {"choices": [{"message": {"tool_calls": [{"function": {"name": "plan_function_step",
            "arguments": json.dumps({"cases": [{"reason": "Identity preserves 2", "inputs": {"value": 2}, "expected": 2}]})}}]}}]}
    with patch("cloud_ai.Router", return_value=SimpleNamespace(complete=complete)), patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        native_agent.run_slice(item)
    assert len(seen) == 1
    assert seen[0][1]["size_hint"] == "balanced"
    assert seen[0][0]["reasoning_effort"] == "low"
    assert seen[0][0]["max_tokens"] == 4096
    assert task_context.step_status(item["folder"])["total"] == 1


def test_execution_steps_have_a_finite_output_budget_and_keep_the_original_request(tmp_path):
    item = project(tmp_path / "bounded-execution")
    item["brief"] = "Work in small steps. " + "Keep the original requirement. " * 80 + "Final requirement: preserve the blue heading."
    seen = []
    def call(payload):
        seen.append(payload)
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "files", "function": {
            "name": "list_files", "arguments": "{}"}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1):
        native_agent.run_slice(item, complete=call)
    assert len(seen) == 1
    assert seen[0]["max_tokens"] == 4096
    assert "Final requirement: preserve the blue heading." in seen[0]["messages"][0]["content"]


def test_focused_existing_html_uses_edits_but_keeps_new_files_and_notes(tmp_path):
    from jsonschema import ValidationError, validate
    item = project(tmp_path / "focused-html-write")
    folder = Path(item["folder"])
    (folder / "index.html").write_text('<p id="total">Wrong</p>', encoding="utf-8")
    active = {"check_tool": "check_webapp", "check_arguments": {"path": "./index.html", "cases": []}}
    original = copy.deepcopy(native_agent.TOOLS)
    available = native_agent.focused_html_write_tools(item, native_agent.TOOLS, active)
    tool = next(tool for tool in available if tool["function"]["name"] == "write_file")
    schema = tool["function"]["parameters"]
    for path in ("index.html", "./index.html", ".\\index.html", "././INDEX.HTML"):
        with pytest.raises(ValidationError):
            validate({"path": path, "content": "whole replacement"}, schema)
    for path in ("new.html", ".local-ai/PROGRESS.md", "assets/index.html"):
        validate({"path": path, "content": "new content"}, schema)
    assert any(tool["function"]["name"] == "edit_file" for tool in available)
    assert native_agent.TOOLS == original
    assert len(json.dumps(available)) - len(json.dumps(native_agent.TOOLS)) < 1000
    # The normal router validation rejects the whole-page write before dispatch.
    response = {"choices": [{"message": {"tool_calls": [{"id": "write", "function": {
        "name": "write_file", "arguments": json.dumps({"path": "./index.html", "content": "replacement"})}}]}}]}
    with pytest.raises(cloud_ai.ToolArgumentError):
        cloud_ai.valid_completion(response, {"tools": available})


def test_focused_html_restriction_restores_fifth_turn_and_honors_rewrite_brief(tmp_path):
    from jsonschema import ValidationError, validate
    item = project(tmp_path / "html-escape")
    folder = Path(item["folder"])
    (folder / "index.html").write_text('<p>Wrong</p>', encoding="utf-8")
    task_context.plan_steps(folder, [{"phase": "Repair", "title": "Fix visible total",
        "check_tool": "check_webapp", "check_arguments": {"path": "index.html", "cases": [{"expect": {"total": "10"}}]}}])
    state = {}
    for _ in range(4):
        tools = native_agent.step_tools(item, state, native_agent.TOOLS)
        schema = next(tool["function"]["parameters"] for tool in tools if tool["function"]["name"] == "write_file")
        with pytest.raises(ValidationError):
            validate({"path": "index.html", "content": "replacement"}, schema)
    assert native_agent.step_tools(item, state, native_agent.TOOLS) is native_agent.TOOLS
    for brief in ("Rewrite the entire HTML file", "Replace index.html from scratch", "An entire page replacement is required"):
        item["brief"] = brief
        assert native_agent.focused_html_write_tools(item, native_agent.TOOLS,
            task_context.step_status(folder)["active"]) is native_agent.TOOLS


def test_focused_write_does_not_guess_missing_large_or_non_html_targets(tmp_path):
    item = project(tmp_path / "html-unknown")
    folder = Path(item["folder"])
    active = {"check_tool": "check_webapp", "check_arguments": {"path": "index.html", "cases": []}}
    assert native_agent.focused_html_write_tools(item, native_agent.TOOLS, active) is native_agent.TOOLS
    (folder / "index.html").write_text("x" * 25000, encoding="utf-8")
    # An unbound large page is not guessed from the project directory.
    assert native_agent.focused_html_write_tools(item, native_agent.TOOLS) is native_agent.TOOLS
    for active in ({"check_command": "python verify.py"},
                   {"check_tool": "check_python_function", "check_arguments": {"path": "index.html"}},
                   {"check_tool": "check_webapp", "check_arguments": {"path": "../outside.html"}}):
        assert native_agent.focused_html_write_tools(item, native_agent.TOOLS, active) is native_agent.TOOLS


def test_repair_and_step_focus_share_the_same_unrestricted_escape_turn(tmp_path):
    from jsonschema import ValidationError, validate
    item = project(tmp_path / "combined-html-focus")
    folder = Path(item["folder"])
    (folder / "index.html").write_text('<p id="total">wrong</p>', encoding="utf-8")
    task_context.plan_steps(folder, [{"phase": "Repair", "title": "Fix total",
        "check_tool": "check_webapp", "check_arguments": {"path": "index.html", "cases": [{"expect": {"total": "10"}}]}}])
    task_context.record_action(folder, "check_webapp", '{"passed":false}', {"path": "index.html"})
    state = {}
    for turn in range(5):
        available = native_agent.step_tools(item, state, native_agent.repair_tools(item, state))
        schema = next(tool["function"]["parameters"] for tool in available if tool["function"]["name"] == "write_file")
        arguments = {"path": "index.html", "content": "replacement"}
        if turn < 4:
            with pytest.raises(ValidationError):
                validate(arguments, schema)
        else:
            validate(arguments, schema)
            assert available is native_agent.TOOLS


@pytest.mark.parametrize('repair_count', range(5))
@pytest.mark.parametrize('step_count', range(5))
def test_persisted_misaligned_focus_cycles_reach_true_baseline_within_five_turns(tmp_path,repair_count,step_count):
    item=project(tmp_path/'misaligned-focus')
    folder=Path(item['folder'])
    (folder/'index.html').write_text('<p id="total">wrong</p>')
    task_context.plan_steps(folder,[{'phase':'Repair','title':'Fix total',
        'check_tool':'check_webapp','check_arguments':{'path':'index.html','cases':[{'expect':{'total':'10'}}]}}])
    task_context.record_action(folder,'check_webapp','{"passed":false}',{'path':'index.html'})
    original_queue=task_context.step_status(folder)
    original_request=task_context.request_identity(folder)
    native_agent.save_state(item,{'status':'continue','messages':[],
        'repair_focus_turns':repair_count,'step_focus_turns':step_count,
        'step_repair_focus':{'count':2},'automatic_check_count':5})
    offers=[]
    for _ in range(10):
        # A restart must preserve the cycle rather than relying on RAM alignment.
        state=native_agent.read_state(item)
        available=native_agent.step_tools(item,state,native_agent.repair_tools(item,state))
        available=native_agent.acceptance_repair_tools(item,state,available)
        offers.append(available is native_agent.TOOLS)
        assert state['step_repair_focus']=={'count':2} and state['automatic_check_count']==5
        native_agent.save_state(item,state)
    assert all(any(offers[start:start+5]) for start in range(6)),offers
    assert task_context.step_status(folder)==original_queue
    assert task_context.request_identity(folder)==original_request


def test_large_active_html_excerpt_reaches_live_handlers_and_failed_ids_without_document_values(tmp_path):
    item = project(tmp_path / "large-live-html-excerpt")
    folder = Path(item["folder"])
    page = folder / "index.html"
    prose = "\n".join(f"<p>PRIVATE_PROSE_{number} unrelated personal document content</p>" for number in range(140))
    text = '''<!doctype html><body>
''' + prose + '''
<script type="application/json">
{"private":"PRIVATE_JSON_DOCUMENT"}
</script>
<form id="expense-form"><input id="amount" value="PRIVATE_FIELD_VALUE"></form>
<table id="expense-table"><tr><td>PRIVATE_TABLE_TEXT</td></tr></table>
<p id="error"></p>
<script>
const form = document.getElementById('expense-form');
const amount = document.getElementById('amount');
const error = document.getElementById('error');
function validateInputs() {
  if (Number(amount.value) <= 0) {
    error.textContent = 'Enter a positive amount';
    return false;
  }
  return true;
}
form.addEventListener('submit', event => {
  event.preventDefault();
  if (!validateInputs()) return;
  error.textContent = '';
});
</script></body>'''
    page.write_text(text, encoding="utf-8")
    assert 6000 < page.stat().st_size <= 24000
    (folder / "private.csv").write_text("PRIVATE_UNRELATED_CSV")
    active = task_context.plan_steps(folder, [{"phase": "Layout", "title": "Show form and table", "check_tool": "check_webapp",
        "check_arguments": {"path": "index.html", "cases": [{"visible": ["form", "table"]}]}}])["active"]
    task_context.record_step_check(folder, active["id"], task_context.request_identity(folder),
        'Exit code: 1\nAcceptance check failed; observed UI targets: [{"id":"expense-form"},{"id":"expense-table"}]', unchanged=True)
    messages = [{"role": "system", "content": "Original requirement: show the working expense form and table."}]
    checkpoint = native_agent.fresh_messages(messages, folder, {}, item)[1]["content"]
    line = next(line for line in checkpoint.splitlines() if line.startswith("Live acceptance source"))
    excerpt = json.loads(line.split(": ", 1)[1])
    assert excerpt["partial"] is True and excerpt["sha256"] == hashlib.sha256(page.read_bytes()).hexdigest()
    assert excerpt["line_count"] == len(text.splitlines())
    assert 'id="expense-form"' in excerpt["source"] and 'id="expense-table"' in excerpt["source"]
    assert "function validateInputs()" in excerpt["source"]
    assert "form.addEventListener('submit'" in excerpt["source"]
    assert "event.preventDefault()" in excerpt["source"]
    assert re.search(r"L1[4-9][0-9]:", excerpt["source"])  # Useful code is beyond read_file's default first120 lines.
    assert "PRIVATE_" not in excerpt["source"]
    assert len(excerpt["source"]) <= 5600 and "read_file" in excerpt["read_more"]
    stamp = page.stat()
    page.write_text(text.replace("return false;", "return Boolean(amount.value);"), encoding="utf-8")
    os.utime(page, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    updated = native_agent.fresh_messages(messages, folder, {}, item)[1]["content"]
    changed = json.loads(next(line for line in updated.splitlines() if line.startswith("Live acceptance source")).split(": ", 1)[1])
    assert changed["sha256"] != excerpt["sha256"]
    assert "return Boolean(amount.value);" in changed["source"] and "return false;" not in changed["source"]


@pytest.mark.parametrize("suffix,checker,declaration", [
    (".py", "check_python_function", "def calculate(value):\n    return value * 2\n"),
    (".js", "check_javascript_function", "function calculate(value) {\n  return value * 2;\n}\n"),
])
def test_large_active_function_excerpt_is_bounded_refreshed_and_context_compacts(tmp_path, suffix, checker, declaration):
    item = project(tmp_path / ("large-function-" + suffix[1:]))
    folder = Path(item["folder"])
    source = folder / ("logic" + suffix)
    comment = "# " if suffix == ".py" else "// "
    text = (comment + "unrelated header commentary " * 4 + "\n") * 70 + declaration
    source.write_text(text, encoding="utf-8")
    assert 6000 < source.stat().st_size <= 24000
    task_context.plan_steps(folder, [{"phase": "Logic", "title": "Calculate", "check_tool": checker,
        "check_arguments": {"path": source.name, "function": "calculate", "cases": [{"args": [2], "expected": 4}]}}])
    messages = [{"role": "system", "content": "Original request: calculate must double the supplied value."}]
    refreshed = native_agent.fresh_messages(messages, folder, {}, item)
    entry = json.loads(next(line for line in refreshed[1]["content"].splitlines()
        if line.startswith("Live acceptance source")).split(": ", 1)[1])
    assert entry["partial"] and "L71:" in entry["source"] and "return value * 2" in entry["source"]
    assert "unrelated header commentary" not in entry["source"] and len(entry["source"]) <= 5600
    compact = native_agent.compact_messages(refreshed, folder, 6000)
    assert cloud_ai.estimated_input_tokens({"messages": compact, "tools": native_agent.TOOLS}) <= 4500
    assert "Original request: calculate" in compact[0]["content"]
    source.write_text(text + "\n" + "x" * 24001, encoding="utf-8")
    too_large = native_agent.fresh_messages(messages, folder, {}, item)[1]["content"]
    assert "Live acceptance source" not in too_large  # Ordinary bounded read_file remains the escape.


def future_browser_phase_fixture(folder, first_cases):
    """Retain a later, genuinely broken validation requirement before layout passes."""
    import webapp_check
    item = project(folder)
    source = '''<form id="expense-form"><input id="amount"><button id="add">Add</button></form>
        <table id="expense-table"><tr><td>Ready</td></tr></table>
        <p id="total">0</p><p id="error"></p><button id="dummy">No action</button>
        <script>document.getElementById('expense-form').onsubmit=e=>{e.preventDefault();
        document.getElementById('total').textContent=document.getElementById('amount').value;};</script>'''
    (folder / "index.html").write_text(source)
    first = {"path": "index.html", "cases": first_cases}
    future = {"path": "index.html", "cases": [
        {"set": {"amount": "-1"}, "click": ["add"], "expect": {"error": "positive amount"}}]}
    steps = [{"phase": phase, "title": title, "check_tool": "check_webapp", "check_arguments": args}
             for phase, title, args in (("Layout", "Show the working page", first),
                                       ("Validation", "Reject negative amounts", future))]
    active = task_context.plan_steps(folder, steps)["active"]
    webapp_check.save_verifier(folder, future, accumulate=True)
    return item, source, first, future, active


@pytest.mark.parametrize("first_cases,needs_effect", [
    ([{"visible": ["expense-form", "expense-table"]}], False),
    ([{"set": {"amount": "2"}, "click": ["add"], "expect": {"total": "2"}}], True),
])
def test_own_browser_phase_advances_while_future_retained_failure_keeps_final_gate_closed(tmp_path, first_cases, needs_effect):
    item, source, first, future, active = future_browser_phase_fixture(tmp_path / "phase-order", first_cases)
    folder = Path(item["folder"])
    state = tmp_path / "state"
    state.mkdir()
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        result = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
        assert result["passed"] and result["done"] == 1 and result["remaining"] == 1
        assert result["active"]["phase"] == "Validation" and result["active"]["attempts"] == 0
        assert result["own_batch_passed"] and result["global_check_passed"] is False
        assert result["check_verdict"]["passed"] is False and not result["verifier_check_passed"]
        assert not result["browser_evidence"]
        assert "positive amount" in json.dumps(result["check_summary"])
        if needs_effect:
            assert result["check_summary"]["input_effect_proven"] is True
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")
        assert native_agent.completion_issue(folder, item, {"verify_passed": False})
    assert (folder / "index.html").read_text() == source


def test_own_browser_unit_still_requires_actual_input_effect_despite_future_failures(tmp_path):
    item, source, first, future, active = future_browser_phase_fixture(tmp_path / "input-effect",
        [{"click": ["dummy"], "visible": ["expense-form"]}])
    folder = Path(item["folder"])
    state = tmp_path / "state"
    state.mkdir()
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        result = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not result["passed"] and result["done"] == 0
    assert not result["own_batch_passed"] and not result["global_check_passed"]
    assert result["check_summary"]["input_effect_proven"] is False
    assert "Input-step coverage failed" in result["active"]["evidence"]
    assert "positive amount" not in result["active"]["evidence"]  # Future failures are not this unit's cause.
    assert "positive amount" in json.dumps(result["check_summary"])


@pytest.mark.parametrize("fault", ["own_assertion", "javascript", "source_change"])
def test_own_browser_phase_cannot_advance_failed_assertion_js_or_changed_source(tmp_path, fault):
    import webapp_check
    item, source, first, future, active = future_browser_phase_fixture(tmp_path / fault,
        [{"visible": ["missing" if fault == "own_assertion" else "expense-form"]}])
    folder = Path(item["folder"])
    if fault == "javascript":
        (folder / "index.html").write_text(source + '<script>throw new Error("Own batch JS failure")</script>')
    check = webapp_check.check
    def changed(*args, **kwargs):
        result = check(*args, **kwargs)
        if fault == "source_change":
            with (folder / "index.html").open("a") as stream:
                stream.write("<!-- changed after this actual check -->")
        return result
    state = tmp_path / "state"
    state.mkdir()
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state), patch.object(webapp_check, "check", side_effect=changed):
        result = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not result["passed"] and not result["own_batch_passed"] and result["done"] == 0
    assert task_context.step_status(folder)["active"]["id"] == active["id"]


def test_explicit_browser_alignment_with_future_failure_stops_stale_batch_but_not_final_gate(tmp_path):
    item, source, first, future, active = future_browser_phase_fixture(tmp_path / "alignment-future",
        [{"visible": ["form", "table"]}])
    folder = Path(item["folder"])
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    revision = {**failed["assertion_revision_reference"], "cases": [{"visible": ["expense-form", "expense-table"]}]}
    state = tmp_path / "state"
    state.mkdir()
    def complete(payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "explicit-repair", "function": {"name": "check_webapp", "arguments": json.dumps(revision)}},
            {"id": "stale", "function": {"name": "write_file", "arguments": '{"path":"stale.txt","content":"wrong check"}'}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        result = native_agent.run_slice(item, complete=complete)
        repair = json.loads(next(message["content"] for message in result["messages"]
            if message.get("tool_call_id") == "explicit-repair"))
        assert repair["own_batch_passed"] and repair["active_step_check_aligned"]
        assert repair["passed"] is False and repair["verifier_check_passed"] is False
        assert result["verify_passed"] is False and result["browser_verified"] is False
        assert result["failed_checks"]
        queue = task_context.step_status(folder)
        assert queue["done"] == 0 and queue["active"]["check_arguments"]["cases"] == revision["cases"]
        progressed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
        assert progressed["passed"] and progressed["done"] == 1 and not progressed["verifier_check_passed"]
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")
    assert not (folder / "stale.txt").exists() and (folder / "index.html").read_text() == source


def test_opaque_browser_binding_nonce_does_not_bypass_repeated_public_call_guard(tmp_path):
    item, folder, step, active, revision, source = bound_browser_revision_fixture(tmp_path / "nonce-call-guard")
    original = native_agent.execute_tool
    attempts = []
    def unavailable(folder, name, arguments, **kwargs):
        if name == "check_webapp" and "_active_browser_revision" in arguments:
            attempts.append(arguments["_active_browser_revision"])
            return "Tool error: simulated checker unavailable"
        return original(folder, name, arguments, **kwargs)
    def complete(payload):
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "same-repair", "function": {"name": "check_webapp", "arguments": json.dumps(revision)}}]}}]}
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 3), patch.object(native_agent, "execute_tool", side_effect=unavailable), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert any("This exact call failed twice and was skipped" in message.get("content", "")
               for message in state["messages"] if message.get("role") == "tool")
    assert task_context.step_status(folder)["done"] == 0


def scoped_repair_integration_fixture(tmp_path, name):
    """Use actual failed browser/SQLite state, while replacing only inference."""
    item, source = step_notice_fixture(tmp_path / name)
    folder = Path(item["folder"])
    active = task_context.step_status(folder)["active"]
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not failed["passed"] and failed["active"]["attempts"] == 2
    (folder / "AGENTS.md").write_text("Preserve working source and all user requirements.")
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    return item, folder, source, runtime_state


def test_scoped_repair_turn_cannot_execute_unoffered_source_write(tmp_path):
    item, folder, source, runtime_state = scoped_repair_integration_fixture(tmp_path, "scope-unoffered-write")
    seen = []
    def complete(payload):
        seen.append(payload)
        assert payload["_native_workload"] == "active_step_checker_repair"
        assert {tool["function"]["name"] for tool in payload["tools"]} == {"check_webapp", "task_steps"}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "unoffered-write", "function": {"name": "write_file", "arguments": json.dumps({
                "path": "should-not-exist.txt", "content": "A scoped decision must not write source."})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(seen) == 1 and not (folder / "should-not-exist.txt").exists()
    assert (folder / "index.html").read_text() == source
    assert task_context.step_status(folder)["done"] == 0
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "unoffered-write")
    assert reply.startswith("Tool error:") and "not available" in reply


def test_scoped_repair_cannot_change_an_allowed_tools_operation_contract(tmp_path):
    item, folder, source, runtime_state = scoped_repair_integration_fixture(tmp_path, "scope-operation")
    before = task_context.step_status(folder)
    def complete(payload):
        assert payload["_native_workload"] == "active_step_checker_repair"
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": "wrong-operation", "function": {"name": "task_steps", "arguments": json.dumps({
                "action": "plan", "steps": [queue_step("Unexpected extra unit", "exit 0")]})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    after = task_context.step_status(folder)
    assert after["total"] == before["total"] and after["done"] == 0
    assert task_context.step_check_identity(after["active"]) == task_context.step_check_identity(before["active"])
    assert (folder / "index.html").read_text() == source
    reply = next(m["content"] for m in state["messages"] if m.get("tool_call_id") == "wrong-operation")
    assert "do not match the offered schema" in reply


def test_scoped_repair_explanation_restores_normal_tools_without_finishing_project(tmp_path):
    item, folder, source, runtime_state = scoped_repair_integration_fixture(tmp_path, "scope-explanation")
    seen = []
    def complete(payload):
        seen.append(payload)
        if len(seen) == 1:
            assert payload["_native_workload"] == "active_step_checker_repair"
            return {"choices": [{"message": {"role": "assistant", "content":
                "The source needs inspection; this decision has not changed or completed anything."}}]}
        assert payload.get("_native_workload") != "active_step_checker_repair"
        assert "read_file" in {tool["function"]["name"] for tool in payload["tools"]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "normal-inspection", "function": {"name": "read_file", "arguments": '{"path":"index.html"}'}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(seen) == 2 and state["status"] == "continue"
    assert state["step_repair_focus"]["count"] == 2 and not state.get("premature_finishes")
    assert task_context.step_status(folder)["done"] == 0
    assert any(message.get("tool_call_id") == "normal-inspection" for message in state["messages"])
    assert (folder / "index.html").read_text() == source


def test_scoped_repair_discards_inflight_calls_after_project_rules_change(tmp_path):
    item, folder, source, runtime_state = scoped_repair_integration_fixture(tmp_path, "scope-rules-race")
    saved = folder / ".local-ai/webapp-check.json"
    before = saved.read_bytes()
    original_step = task_context.step_status(folder)["active"]
    seen = []
    def complete(payload):
        seen.append(payload)
        assert payload["_native_workload"] == "active_step_checker_repair"
        data = json.loads(payload["messages"][1]["content"])
        assert "Preserve working source" in data["project_rules"]
        reference = data["observations"]["assertion_revision_reference"]
        (folder / "AGENTS.md").write_text("The user now requires the literal form and table IDs. Do not revise those assertions.")
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "stale-rule-repair", "function": {"name": "check_webapp", "arguments": json.dumps({
                **reference, "cases": [{"visible": ["expense-form", "expense-table"]}]})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(seen) == 1 and state["status"] == "continue"
    assert saved.read_bytes() == before and (folder / "index.html").read_text() == source
    current = task_context.step_status(folder)
    assert current["done"] == 0 and current["active"]["check_arguments"] == original_step["check_arguments"]
    assert not any(message.get("tool_call_id") == "stale-rule-repair" for message in state["messages"])


def test_scoped_repair_reserves_persistent_turn_before_transport_exception_and_escapes_on_restart(tmp_path):
    item, folder, source, runtime_state = scoped_repair_integration_fixture(tmp_path, "scope-transport-count")
    stages = []
    def complete(payload):
        stage = payload.get("_native_workload")
        stages.append(stage)
        if stage == "active_step_checker_repair":
            persisted = native_agent.read_state(item)["step_repair_focus"]
            assert persisted["count"] == len(stages)
            raise OSError("Simulated transport loss after the reserved inference begins")
        assert "read_file" in {tool["function"]["name"] for tool in payload["tools"]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "normal-after-outage", "function": {"name": "read_file", "arguments": '{"path":"index.html"}'}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        for expected in (1, 2):
            with pytest.raises(OSError, match="Simulated transport loss"):
                native_agent.run_slice(item, complete=complete)
            assert native_agent.read_state(item)["step_repair_focus"]["count"] == expected
        state = native_agent.run_slice(item, complete=complete)
    assert stages == ["active_step_checker_repair", "active_step_checker_repair", None]
    assert state["status"] == "continue" and state["step_repair_focus"]["count"] == 2
    assert task_context.step_status(folder)["done"] == 0 and (folder / "index.html").read_text() == source


def invalid_definition_scope_fixture(tmp_path, name):
    """Keep an actually completed phase before a legacy invalid next check."""
    item = project(tmp_path / name)
    item["brief"] = "Build an expense page. Reject negative amounts with a visible positive-amount error."
    folder = Path(item["folder"])
    source = '''<form id="expense-form" novalidate>
        <input id="amount" type="number" value="PRIVATE_FIELD_VALUE"><button id="add">Add</button>
        </form><p id="error"></p><script>
        document.getElementById('expense-form').onsubmit=e=>e.preventDefault();
        </script>'''
    (folder / "index.html").write_text(source)
    task_context.plan_steps(folder, [
        {"phase": "Prepare", "title": "Prepare the workspace", "check_command": "exit 0"},
        {"phase": "Validation", "title": "Reject negative amounts", "check_command": "exit 7"}])
    first = task_context.step_status(folder)["active"]
    completed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": first["id"]}))
    assert completed["passed"] and completed["done"] == 1
    active = task_context.step_status(folder)["active"]
    with project_memory.connection(folder) as conn:
        conn.execute("UPDATE task_steps SET check_command='write_file' WHERE id=?", (active["id"],))
    active = task_context.step_status(folder)["active"]
    assert active["attempts"] == 0 and active["check_arguments"] == {}
    replacement = {"phase": active["phase"], "title": active["title"], "check_tool": "check_webapp",
        "check_arguments": {"path": "index.html", "cases": [
            {"set": {"amount": "-1"}, "click": ["add"], "expect": {"error": "positive amount"}}]}}
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    return item, folder, source, active, replacement, runtime_state


def test_invalid_definition_scope_starts_at_zero_attempts_and_pins_every_step_alternative(tmp_path):
    from jsonschema import ValidationError, validate
    item, folder, source, active, replacement, runtime_state = invalid_definition_scope_fixture(tmp_path, "invalid-scope-schema")
    original_tools = copy.deepcopy(native_agent.TOOLS)
    state = {"step_focus_turns": 1}
    available = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    request = native_agent.focused_step_repair_request(item, state, available, None, 65536)
    assert request and request["_native_workload"] == "active_step_checker_repair"
    assert [tool["function"]["name"] for tool in request["tools"]] == ["task_steps"]
    data = json.loads(request["messages"][1]["content"])
    assert "not a shell acceptance check" in data["observations"]["invalid_acceptance"]
    assert data["observations"]["page"] == "index.html"
    assert {"id": "amount", "tag": "input", "type": "number"} in data["observations"]["static_controls"]
    assert "PRIVATE_FIELD_VALUE" not in json.dumps(data)
    schema = request["tools"][0]["function"]["parameters"]
    assert schema["properties"]["action"]["enum"] == ["revise"]
    assert schema["properties"]["step_id"]["const"] == active["id"]
    assert schema["properties"]["steps"]["minItems"] == schema["properties"]["steps"]["maxItems"] == 1
    for branch in schema["properties"]["steps"]["items"]["anyOf"]:
        for field in ("phase", "title"):
            assert branch["properties"][field]["const"] == active[field] and field in branch["required"]
    valid = {"action": "revise", "step_id": active["id"], "steps": [replacement]}
    validate(valid, schema)
    for invalid in ({**valid, "action": "plan"}, {"action": "complete", "step_id": active["id"]},
                    {**valid, "steps": [{**replacement, "phase": "A rebuilt project"}]},
                    {**valid, "steps": [{**replacement, "title": "Something else"}]},
                    {**valid, "step_id": active["id"] + 1}):
        with pytest.raises(ValidationError):
            validate(invalid, schema)
    assert task_context.step_status(folder)["done"] == 1
    assert task_context.step_status(folder)["active"]["check_command"] == "write_file"
    assert native_agent.TOOLS == original_tools and (folder / "index.html").read_text() == source


def test_scoped_invalid_definition_revises_explicit_active_id_without_completing_or_editing_source(tmp_path):
    item, folder, source, active, replacement, runtime_state = invalid_definition_scope_fixture(tmp_path, "invalid-scope-repair")
    seen = []
    def complete(payload):
        seen.append(payload)
        assert payload["_native_workload"] == "active_step_checker_repair"
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["task_steps"]
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "repair-invalid-definition", "function": {"name": "task_steps", "arguments": json.dumps({
                "action": "revise", "step_id": active["id"], "steps": [replacement]})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "configured_context", return_value=65536), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
        queue = task_context.step_status(folder)
        assert len(seen) == 1 and state["status"] == "continue"
        assert queue["done"] == 1 and queue["remaining"] == 1 and queue["active"]["id"] == active["id"]
        assert queue["active"]["phase"] == active["phase"] and queue["active"]["title"] == active["title"]
        assert queue["active"]["check_tool"] == "check_webapp" and queue["active"]["check_arguments"] == replacement["check_arguments"]
        assert queue["active"]["attempts"] == 0 and "not yet passed" in queue["active"]["evidence"]
        checked = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
        assert not checked["passed"] and checked["done"] == 1 and checked["active"]["attempts"] == 1
        assert "positive amount" in json.dumps(checked["check_summary"])
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")
        assert native_agent.completion_issue(folder, item, {"verify_passed": False})
    assert (folder / "index.html").read_text() == source


def test_scoped_invalid_definition_explicit_id_cannot_overwrite_newer_check_during_inference(tmp_path):
    item, folder, source, active, replacement, runtime_state = invalid_definition_scope_fixture(tmp_path, "invalid-scope-race")
    request = task_context.request_identity(folder)
    newer = {**replacement, "check_arguments": {**replacement["check_arguments"], "cases": [
        {"set": {"amount": "-2"}, "click": ["add"], "expect": {"error": "A newer validation contract"}}]}}
    def complete(payload):
        assert payload["_native_workload"] == "active_step_checker_repair"
        task_context.revise_step(folder, active["id"], [newer])
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "stale-invalid-repair", "function": {"name": "task_steps", "arguments": json.dumps({
                "action": "revise", "step_id": active["id"], "steps": [replacement]})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "configured_context", return_value=65536), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    queue = task_context.step_status(folder)
    assert queue["done"] == 1 and queue["active"]["check_arguments"] == newer["check_arguments"]
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "stale-invalid-repair")
    assert reply.startswith("Tool error:") and "changed" in reply
    ordinary = {"action": "revise", "step_id": active["id"] + 99, "steps": [replacement]}
    assert native_agent.bind_step_completion(folder, ordinary, active, request) == ordinary
    assert (folder / "index.html").read_text() == source


def retained_input_revision_native_fixture(tmp_path, name, *, multiple_cases=False, nonfillable_later=False, accessibility=False):
    """Keep a bad no-submit case alongside a real future validation failure."""
    import webapp_check
    item = project(tmp_path / name)
    item["brief"] = "Build an expense page that adds named expenses and rejects negative amounts with a visible positive-amount error."
    folder = Path(item["folder"])
    source = '''<form id="expense-form" novalidate>
        <label for="description">Description</label><input id="description">
        <input id="amount" type="number"><button id="add">Add expense</button></form>
        <table><tbody id="rows"></tbody></table><p id="count">0</p><p id="error"></p><script>
        document.getElementById('expense-form').onsubmit=e=>{e.preventDefault();
          if(Number(document.getElementById('amount').value)>0) {
            const row=document.createElement('tr');const cell=document.createElement('td');
            cell.textContent=document.getElementById('description').value;row.appendChild(cell);
            document.getElementById('rows').appendChild(row);
            document.getElementById('count').textContent=document.getElementById('rows').children.length;
          }
        };</script>'''
    page = folder / "index.html"
    page.write_text(source)
    bad = {"path": "index.html", "cases": [{"set": {"description": "Coffee", "amount": "2"},
                                                "expect": {"rows": "1 row"}}]}
    if accessibility:
        bad['cases'][0]['accessibility'] = {'add': {'role': 'button', 'name': 'Add expense'}}
    if multiple_cases:
        bad["cases"].append({"set": {"description-label" if nonfillable_later else "description": "Tea", "amount": "3"},
                             "expect": {"rows": "2 rows"}})
    future = {"path": "index.html", "cases": [{"set": {"description": "Invalid", "amount": "-1"},
        "click": ["add"], "expect": {"error": "positive amount"}}]}
    active = task_context.plan_steps(folder, [{"phase": "Entry", "title": "Submit one expense", "check_tool": "check_webapp",
                                             "check_arguments": bad}])["active"]
    webapp_check.save_verifier(folder, future, accumulate=True)
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not failed["passed"] and failed["assertion_revision_reference"]
    revision = {**failed["assertion_revision_reference"], "revise_inputs": True,
        "cases": [{"set": {"description": "Coffee", "amount": "2"}, "click": ["add"], "expect": {"rows": "Coffee"}}]}
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    return item, folder, page.read_bytes(), bad, future, task_context.step_status(folder)["active"], revision, runtime_state


@pytest.mark.parametrize("bound", [False, True])
def test_retained_input_repair_updates_only_bound_active_sql_and_keeps_future_global_failure(tmp_path, bound):
    item, folder, source, bad, future, active, revision, runtime_state = retained_input_revision_native_fixture(tmp_path, str(bound))
    saved = folder / ".local-ai/webapp-check.json"
    old_suite = json.loads(saved.read_text())
    selected = revision["saved_sequence"] - 1
    arguments = (native_agent.bind_browser_assertion_revision(folder, revision, active, task_context.request_identity(folder))
                 if bound else revision)
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        result = json.loads(native_agent.execute_tool(folder, "check_webapp", arguments))
        assert result["own_batch_passed"] and result["verifier_saved"]
        assert result["passed"] is False and result["verifier_check_passed"] is False
        assert result["revision"]["replace_sequence"] == revision["saved_sequence"]
        revised_suite = json.loads(saved.read_text())
        assert len(revised_suite["checks"]) == len(old_suite["checks"]) == 2
        assert revised_suite["checks"][selected]["cases"] == revision["cases"]
        assert [check for index, check in enumerate(revised_suite["checks"]) if index != selected] == [
            check for index, check in enumerate(old_suite["checks"]) if index != selected]
        queue = task_context.step_status(folder)
        assert queue["done"] == 0 and queue["active"]["id"] == active["id"]
        if bound:
            assert result["active_step_check_aligned"] is True
            assert queue["active"]["check_arguments"]["cases"] == revision["cases"]
            assert queue["active"]["phase"] == active["phase"] and queue["active"]["title"] == active["title"]
            assert "revise_inputs" not in queue["active"]["check_arguments"]
            assert "not yet passed" in queue["active"]["evidence"]
            completed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
            assert completed["passed"] and completed["done"] == 1
            assert completed["check_summary"]["input_effect_proven"] is True
            assert completed["check_verdict"]["passed"] is False and completed["verifier_check_passed"] is False
        else:
            assert "active_step_check_aligned" not in result
            assert queue["active"]["check_arguments"] == bad
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")
        assert native_agent.completion_issue(folder, item, {"verify_passed": False})
    assert (folder / "index.html").read_bytes() == source


@pytest.mark.parametrize("fault", ["candidate", "stale_checksum", "request_during_check"])
def test_retained_input_repair_cannot_publish_failed_stale_or_new_request_candidate(tmp_path, fault):
    import webapp_check
    item, folder, source, bad, future, active, revision, runtime_state = retained_input_revision_native_fixture(tmp_path, fault)
    saved = folder / ".local-ai/webapp-check.json"
    before = saved.read_bytes()
    if fault == "candidate":
        revision = {**revision, "cases": [{**revision["cases"][0], "expect": {"rows": "Impossible row"}}]}
    elif fault == "stale_checksum":
        revision = {**revision, "suite_sha256": "0" * 64}
    arguments = native_agent.bind_browser_assertion_revision(folder, revision, active, task_context.request_identity(folder))
    check = webapp_check.check
    def request_changes(*args, **kwargs):
        result = check(*args, **kwargs)
        if fault == "request_during_check":
            (folder / ".local-ai/USER-REQUESTS.md").write_text("New request: preserve the earlier acceptance cases unchanged.")
        return result
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state), patch.object(webapp_check, "check", side_effect=request_changes):
        result = native_agent.execute_tool(folder, "check_webapp", arguments)
    if fault == "stale_checksum":
        assert result.startswith("Tool error:") and "changed" in result
    else:
        rejected = json.loads(result)
        assert rejected["passed"] is False and rejected["own_batch_passed"] is False
        assert not rejected.get("verifier_saved") and not rejected.get("active_step_check_aligned")
    assert saved.read_bytes() == before and (folder / "index.html").read_bytes() == source
    with project_memory.connection(folder) as conn:
        row = conn.execute("SELECT check_arguments,status FROM task_steps WHERE id=?", (active["id"],)).fetchone()
    assert json.loads(row["check_arguments"]) == bad and row["status"] == "active"


def test_retained_input_repair_flag_cannot_be_saved_as_repeatable_step_acceptance(tmp_path):
    from jsonschema import ValidationError, validate
    arguments = {"path": "index.html", "revise_inputs": True, "cases": [{"visible": ["form"]}]}
    step = {"phase": "Entry", "title": "Submit expense", "check_tool": "check_webapp", "check_arguments": arguments}
    schema = next(tool["function"]["parameters"] for tool in native_agent.TOOLS if tool["function"]["name"] == "task_steps")
    with pytest.raises(ValidationError):
        validate({"action": "plan", "steps": [step]}, schema)
    with pytest.raises(ValueError):
        task_context.plan_steps(tmp_path, [step])
    assert task_context.step_status(tmp_path)["total"] == 0


@pytest.mark.parametrize("nonfillable_later", [False, True])
def test_retained_input_repair_can_fix_bad_cases_one_at_a_time_with_real_prefix_state(tmp_path, nonfillable_later):
    item, folder, source, bad, future, active, revision, runtime_state = retained_input_revision_native_fixture(
        tmp_path, str(nonfillable_later), multiple_cases=True, nonfillable_later=nonfillable_later)
    if nonfillable_later:
        page = folder / "index.html"
        page.write_text(page.read_text().replace('<label for="description">', '<label id="description-label">'))
        source = page.read_bytes()  # Reproduce the earlier model's nonassociated-label regression.
    saved = folder / ".local-ai/webapp-check.json"
    before = json.loads(saved.read_text())
    selected = revision["saved_sequence"] - 1
    arguments = native_agent.bind_browser_assertion_revision(folder, revision, active, task_context.request_identity(folder))
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        first = json.loads(native_agent.execute_tool(folder, "check_webapp", arguments))
        assert first["repaired_case_passed"] is True and first["verifier_saved"] is True
        assert first["own_batch_passed"] is False and first["passed"] is False
        assert first["active_step_check_aligned"] is True and first["verifier_check_passed"] is False
        persisted = json.loads(saved.read_text())
        assert len(persisted["checks"]) == len(before["checks"])
        assert persisted["checks"][selected]["cases"][0] == revision["cases"][0]
        assert persisted["checks"][selected]["cases"][1] == bad["cases"][1]
        assert [check for index, check in enumerate(persisted["checks"]) if index != selected] == [
            check for index, check in enumerate(before["checks"]) if index != selected]
        queue = task_context.step_status(folder)
        assert queue["done"] == 0 and queue["active"]["check_arguments"]["cases"] == persisted["checks"][selected]["cases"]
        still_failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
        assert not still_failed["passed"] and still_failed["done"] == 0
        next_revision = {**still_failed["assertion_revision_reference"], "revise_inputs": True,
            "cases": [{"set": {"description": "Tea", "amount": "3"}, "click": ["add"],
                       "expect": {"rows": "Tea", "count": "2"}}]}
        assert next_revision["saved_case"] == 2
        active = task_context.step_status(folder)["active"]
        arguments = native_agent.bind_browser_assertion_revision(folder, next_revision, active, task_context.request_identity(folder))
        second = json.loads(native_agent.execute_tool(folder, "check_webapp", arguments))
        assert second["repaired_case_passed"] and second["own_batch_passed"] and second["active_step_check_aligned"]
        assert not second["passed"] and not second["verifier_check_passed"]  # Future negative validation still fails.
        completed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
        assert completed["passed"] and completed["done"] == 1
        assert completed["check_summary"]["input_effect_proven"] is True
        assert completed["check_verdict"]["passed"] is False and not completed["verifier_check_passed"]
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")
    assert (folder / "index.html").read_bytes() == source


def submission_decision_fixture(tmp_path, name):
    """Keep one actual no-submit failure, including a separate future failure."""
    item, folder, source, bad, future, active, revision, runtime_state = retained_input_revision_native_fixture(tmp_path, name)
    (folder / "AGENTS.md").write_text("Preserve the original requirements and working application source.")
    return item, folder, source, bad, active, revision, runtime_state


def test_submission_decision_hypothesis_pins_one_case_without_editing_acceptance(tmp_path):
    from jsonschema import ValidationError, validate
    item, folder, source, bad, active, revision, runtime_state = submission_decision_fixture(tmp_path, "submission-schema")
    saved = folder / ".local-ai/webapp-check.json"
    original_suite = saved.read_bytes()
    original_tools = copy.deepcopy(native_agent.TOOLS)
    state = {"step_focus_turns": 1}
    assert active["attempts"] == 1
    notice = native_agent.active_step_repair_notice(item, state)
    assert notice and "possible checker mistake, not proof" in notice["content"]
    observations = json.loads(notice["content"].rsplit("\n", 1)[1])
    assert observations["candidate_form_submission"] == {
        "form_id": "expense-form", "input_ids": ["description", "amount"], "submit_ids": ["add"]}
    request = native_agent.focused_step_repair_request(item, state, native_agent.TOOLS, notice, 65536)
    assert request and [tool["function"]["name"] for tool in request["tools"]] == ["check_webapp"]
    data = json.loads(request["messages"][1]["content"])
    assert data["original_request"] == item["brief"] and "working application source" in data["project_rules"]
    schema = request["tools"][0]["function"]["parameters"]
    validate(revision, schema)
    for field in ("path", "saved_sequence", "saved_case", "suite_sha256"):
        assert schema["properties"][field]["const"] == revision[field]
        assert field in schema["required"]
    for invalid in ({**revision, "saved_case": 99}, {**revision, "path": "other.html"},
                    {**revision, "revise_inputs": False}, {**revision, "cases": revision["cases"] * 2}):
        with pytest.raises(ValidationError):
            validate(invalid, schema)
    assert native_agent.focused_step_repair_request(item, state, native_agent.TOOLS, notice, 65536)
    assert native_agent.focused_step_repair_request(item, state, native_agent.TOOLS, notice, 65536) is None
    assert task_context.step_status(folder)["active"]["check_arguments"] == bad
    assert saved.read_bytes() == original_suite and (folder / "index.html").read_bytes() == source
    assert native_agent.TOOLS == original_tools


@pytest.mark.parametrize("operation", ["source_write", "foreign_reference", "changed_inputs", "state_assignment", "wrong_submit"])
def test_submission_decision_host_rejects_source_write_or_changed_reference(tmp_path, operation):
    item, folder, source, bad, active, revision, runtime_state = submission_decision_fixture(tmp_path, operation)
    saved = folder / ".local-ai/webapp-check.json"
    original_suite = saved.read_bytes()
    attempted_revisions = []
    execute = native_agent.execute_tool
    def observed_execute(task_folder, name, arguments, **kwargs):
        if name == "check_webapp" and "saved_sequence" in arguments:
            attempted_revisions.append(arguments)
        return execute(task_folder, name, arguments, **kwargs)
    def complete(payload):
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["check_webapp"]
        name, args = "check_webapp", copy.deepcopy(revision)
        if operation == "source_write":
            name, args = "write_file", {"path": "forbidden.txt", "content": "Do not write this"}
        elif operation == "foreign_reference":
            args["saved_case"] = 99
        elif operation == "changed_inputs":
            args["cases"][0]["set"]["amount"] = "999"
        elif operation == "state_assignment":
            args["cases"][0]["state"] = args["cases"][0].pop("set")
        else:
            args["cases"][0]["click"] = ["unknown-submit"]
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "submission-disallowed", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "configured_context", return_value=65536), patch.object(native_agent, "execute_tool", side_effect=observed_execute), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "submission-disallowed")
    assert reply.startswith("Tool error:")
    assert "not available" in reply if operation == "source_write" else "offered schema" in reply
    assert attempted_revisions == []  # Host rejects malformed scope before running the browser repair.
    assert not (folder / "forbidden.txt").exists() and saved.read_bytes() == original_suite
    queue = task_context.step_status(folder)
    assert queue["done"] == 0 and queue["active"]["check_arguments"] == bad
    assert (folder / "index.html").read_bytes() == source


def test_submission_decision_real_click_aligns_sql_without_completing_or_hiding_future_failure(tmp_path):
    item, folder, source, bad, active, revision, runtime_state = submission_decision_fixture(tmp_path, "submission-real-click")
    seen = []
    def complete(payload):
        seen.append(payload)
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["check_webapp"]
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "submission-real-click", "function": {"name": "check_webapp", "arguments": json.dumps(revision)}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "configured_context", return_value=65536), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
        result = json.loads(next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "submission-real-click"))
        assert result["repaired_case_passed"] and result["verifier_saved"] and result["active_step_check_aligned"]
        assert result["own_batch_passed"] and not result["passed"] and not result["verifier_check_passed"]
        assert native_agent.execute_tool(folder, "run_verifier", {}).startswith("Exit code: 1")
    queue = task_context.step_status(folder)
    assert len(seen) == 1 and state["status"] == "continue" and queue["done"] == 0
    assert queue["active"]["check_arguments"]["cases"] == revision["cases"]
    assert queue["active"]["phase"] == active["phase"] and queue["active"]["title"] == active["title"]
    assert not state.get("verify_passed") and not state.get("browser_verified")
    assert (folder / "index.html").read_bytes() == source


def test_submission_decision_explanation_restores_normal_tools_without_mutation(tmp_path):
    item, folder, source, bad, active, revision, runtime_state = submission_decision_fixture(tmp_path, "submission-explain")
    stages = []
    def complete(payload):
        stages.append(payload.get("_native_workload"))
        names = {tool["function"]["name"] for tool in payload["tools"]}
        if len(stages) == 1:
            assert names == {"check_webapp"}
            return {"choices": [{"message": {"role": "assistant", "content": "The source may have a real submission bug; restore ordinary tools to inspect it."}}]}
        assert "read_file" in names and "write_file" in names
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "ordinary-source-read", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "index.html"})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), patch.object(native_agent, "configured_context", return_value=65536), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert stages == ["active_step_checker_repair", None] and state["status"] == "continue"
    queue = task_context.step_status(folder)
    assert queue["done"] == 0 and queue["active"]["check_arguments"] == bad
    assert (folder / "index.html").read_bytes() == source


@pytest.mark.parametrize("missing", ["form", "submit_id"])
def test_submission_decision_skips_unidentified_form_or_submit_control(tmp_path, missing):
    item, folder, source, bad, active, revision, runtime_state = submission_decision_fixture(tmp_path, "submission-skip-" + missing)
    page = folder / "index.html"
    changed = source.decode().replace('id="expense-form"', '') if missing == "form" else source.decode().replace('id="add"', '')
    page.write_text(changed)
    assert native_agent.active_step_repair_notice(item, {"step_focus_turns": 1}) is None
    assert task_context.step_status(folder)["active"]["check_arguments"] == bad


def test_submission_decision_skips_successful_auto_calculated_form(tmp_path):
    item = project(tmp_path / "submission-auto-calculated")
    folder = Path(item["folder"])
    (folder / "index.html").write_text('''<form id="calc"><input id="amount"><button id="submit">Submit</button></form>
        <p id="result">0</p><script>document.getElementById('amount').oninput=e=>document.getElementById('result').textContent=e.target.value;</script>''')
    active = task_context.plan_steps(folder, [{"phase": "Calculate", "title": "Update immediately", "check_tool": "check_webapp",
        "check_arguments": {"path": "index.html", "cases": [{"set": {"amount": "7"}, "expect": {"result": "7"}}]}}])["active"]
    checked = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert checked["passed"] and checked["done"] == 1
    assert native_agent.active_step_repair_notice(item, {"step_focus_turns": 1}) is None


@pytest.mark.parametrize("escape_turn", [False, True])
@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
@pytest.mark.parametrize("alias", [".local-ai/webapp-check.json", "scratch/../.local-ai/webapp-check.json"])
def test_owned_browser_suite_normal_and_escape_writes_reach_integrity_guard(tmp_path, escape_turn, tool, alias):
    import webapp_check
    item = project(tmp_path / "owned-suite-guard")
    item["brief"] = "Organize project notes and preserve the existing files."
    assert not native_agent.browser_check_required(item)
    folder = Path(item["folder"])
    (folder / "scratch").mkdir()
    page = folder / "index.html"
    page.write_text('<p id="message">Preserve this source.</p>')
    original_source = page.read_bytes()
    webapp_check.save_verifier(folder, {"path": "index.html", "cases": [{"visible": ["message"]}]}, accumulate=True)
    suite = folder / ".local-ai/webapp-check.json"
    original_suite = suite.read_bytes()
    task_context.plan_steps(folder, [queue_step("Preserve existing notes", "exit 1")])
    with project_memory.connection(folder) as conn:
        original_sql = [dict(row) for row in conn.execute("SELECT * FROM task_steps").fetchall()]
    native_agent.save_state(item, {"status": "continue", "turns": 1, "messages": [], "failures": 0,
        "step_focus_turns": 4 if escape_turn else 0})
    guarded = []
    dispatched_writes = []
    guard = native_agent.browser_verifier_write_issue
    execute = native_agent.execute_tool
    def observe_guard(task_folder, task_project, name, arguments):
        result = guard(task_folder, task_project, name, arguments)
        if name == tool and arguments.get("path") == alias:
            guarded.append(result)
        return result
    def observe_execute(task_folder, name, arguments, **kwargs):
        if name in {"write_file", "edit_file"}:
            dispatched_writes.append((name, arguments))
        return execute(task_folder, name, arguments, **kwargs)
    def complete(payload):
        assert payload.get("_native_workload") != "active_step_checker_repair"
        offered = next(entry for entry in payload["tools"] if entry["function"]["name"] == tool)
        from jsonschema import validate
        arguments = {"path": alias, "content": '{}'} if tool == "write_file" else {
            "path": alias, "old_text": '"suite_version": 1', "new_text": '"suite_version": 99',
            "expected_sha256": hashlib.sha256(original_suite).hexdigest()}
        validate(arguments, offered["function"]["parameters"])  # The normal tool is actually available.
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "owned-suite-write",
            "function": {"name": tool, "arguments": json.dumps(arguments)}}]}}]}
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "browser_verifier_write_issue", side_effect=observe_guard), patch.object(native_agent, "execute_tool", side_effect=observe_execute), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "owned-suite-write")
    assert guarded and all("Preserve the generated browser suite" in result for result in guarded)
    assert "Preserve the generated browser suite" in reply and dispatched_writes == []
    assert suite.read_bytes() == original_suite and page.read_bytes() == original_source
    with project_memory.connection(folder) as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM task_steps").fetchall()] == original_sql
    assert state["step_focus_turns"] == (0 if escape_turn else 1)


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
def test_custom_browser_suite_json_is_editable_through_ordinary_worker_tools(tmp_path, tool):
    item = project(tmp_path / "custom-suite-guard")
    item["brief"] = "Update the user-owned project notes."
    assert not native_agent.browser_check_required(item)
    folder = Path(item["folder"])
    page = folder / "index.html"
    page.write_text('<p id="message">Keep the app source.</p>')
    original_source = page.read_bytes()
    (folder / ".local-ai/verify.ps1").write_text("# User custom verifier\nexit 1\n")
    suite = folder / ".local-ai/webapp-check.json"
    suite.write_text('{"note":"before"}')
    task_context.plan_steps(folder, [queue_step("Preserve notes metadata", "exit 1")])
    with project_memory.connection(folder) as conn:
        original_sql = [dict(row) for row in conn.execute("SELECT * FROM task_steps").fetchall()]
    native_agent.save_state(item, {"status": "continue", "turns": 1, "messages": [], "failures": 0, "step_focus_turns": 4})
    arguments = {"path": ".local-ai/webapp-check.json", "content": '{"note":"after"}'} if tool == "write_file" else {
        "path": ".local-ai/webapp-check.json", "old_text": "before", "new_text": "after",
        "expected_sha256": hashlib.sha256(suite.read_bytes()).hexdigest()}
    def complete(payload):
        assert payload.get("_native_workload") != "active_step_checker_repair"
        assert tool in {entry["function"]["name"] for entry in payload["tools"]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "custom-suite-write",
            "function": {"name": tool, "arguments": json.dumps(arguments)}}]}}]}
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "custom-suite-write")
    assert not reply.startswith("Tool error:") and json.loads(suite.read_text()) == {"note": "after"}
    assert page.read_bytes() == original_source
    with project_memory.connection(folder) as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM task_steps").fetchall()] == original_sql


def initial_planning_integration_fixture(tmp_path, name):
    item = project(tmp_path / name)
    item["experimental_initial_plan"] = True
    item["brief"] = "Build a single-file index.html calculator app with a labeled units input and Compute button that shows units multiplied by five in total."
    folder = Path(item["folder"])
    (folder / "AGENTS.md").write_text("Keep arithmetic exact and verify actual form input behavior.")
    steps = [
        {"phase": "Interface", "title": "Show the labeled calculator controls", "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"visible": ["calculator", "units", "compute", "total"]}]}},
        {"phase": "Calculation", "title": "Compute entered units times five", "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"set": {"units": "7.2"}, "click": ["compute"], "expect": {"total": "36"}}]}}]
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    return item, folder, steps, runtime_state


def initial_planning_reply(name, arguments, call_id="initial-plan-action"):
    return {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": call_id,
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}


@pytest.mark.parametrize("operation", ["write_file", "shell_plan"])
def test_initial_planning_stage_rejects_implementation_and_untyped_browser_plan(tmp_path, operation):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, operation)
    seen = []
    def complete(payload):
        seen.append(payload)
        assert payload["_native_workload"] == "initial_step_planning"
        assert not {"write_file", "edit_file", "run_command", "check_webapp"} & {
            tool["function"]["name"] for tool in payload["tools"]}
        if operation == "write_file":
            return initial_planning_reply("write_file", {"path": "index.html", "content": "Do not create this before planning."})
        return initial_planning_reply("task_steps", {"action": "plan", "steps": [
            queue_step("Fake first check", "exit 0"), queue_step("Fake second check", "exit 0")]})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "initial-plan-action")
    assert reply.startswith("Tool error:") and len(seen) == 1
    assert not (folder / "index.html").exists() and task_context.step_status(folder)["total"] == 0
    assert state["status"] == "continue"


def test_initial_planning_saves_typed_queue_then_restores_implementation_tools(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "valid-first-plan")
    seen = []
    def complete(payload):
        seen.append(payload)
        if len(seen) == 1:
            assert payload["_native_workload"] == "initial_step_planning"
            assert not (folder / "index.html").exists()
            return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
        assert payload.get("_native_workload") != "initial_step_planning"
        assert "write_file" in {tool["function"]["name"] for tool in payload["tools"]}
        assert task_context.step_status(folder)["total"] == 2
        return initial_planning_reply("write_file", {"path": "index.html", "content": "<!doctype html><p>Implementation begins after planning.</p>"})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        first = native_agent.run_slice(item, complete=complete)
        queue = task_context.step_status(folder)
        assert queue["total"] == 2 and queue["done"] == 0 and queue["active"]["check_arguments"] == steps[0]["check_arguments"]
        assert not (folder / "index.html").exists() and first["status"] == "continue"
        second = native_agent.run_slice(item, complete=complete)
    assert (folder / "index.html").read_text().endswith("Implementation begins after planning.</p>")
    assert len(seen) == 2 and second["status"] == "continue" and task_context.step_status(folder)["done"] == 0


def test_initial_planning_ignored_instruction_cannot_finish_unplanned_app(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "ignored-first-plan")
    def complete(payload):
        assert payload["_native_workload"] == "initial_step_planning"
        return {"choices": [{"message": {"role": "assistant", "content": "Finished. The calculator is ready."}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert state["status"] == "continue" and task_context.step_status(folder)["total"] == 0
    assert not (folder / "index.html").exists() and not state.get("verify_passed")


@pytest.mark.parametrize("exemption", ["existing_queue", "checker_maintenance", "explanation"])
def test_initial_planning_preserves_existing_queue_and_nonbuild_workflows(tmp_path, exemption):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, exemption)
    if exemption == "existing_queue":
        task_context.plan_steps(folder, [queue_step("Preserve the existing phase", "exit 1")])
    elif exemption == "checker_maintenance":
        item["brief"] = "Repair the mistaken saved checker arguments for the existing index.html calculator input; preserve app source."
        assert not native_agent.initial_plan_required(item)
    else:
        item["brief"] = "Explain how to build a single-file index.html app with an input and a button."
        assert not native_agent.initial_plan_required(item)
    before = task_context.step_status(folder)
    def complete(payload):
        assert payload.get("_native_workload") != "initial_step_planning"
        assert "write_file" in {tool["function"]["name"] for tool in payload["tools"]}
        return initial_planning_reply("write_file", {"path": "notes.txt", "content": "Ordinary work remains available."})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        native_agent.run_slice(item, complete=complete)
    assert (folder / "notes.txt").read_text() == "Ordinary work remains available."
    after = task_context.step_status(folder)
    assert (after["total"], after["done"]) == (before["total"], before["done"])


@pytest.mark.parametrize("race", ["rules", "followup", "brief", "stop", "completed_competing_plan"])
def test_initial_planning_rejects_requirements_control_and_completed_competing_plan_races(tmp_path, race):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, race)
    competitor_id = None
    def complete(payload):
        nonlocal competitor_id
        assert payload["_native_workload"] == "initial_step_planning"
        if race == "rules":
            (folder / "AGENTS.md").write_text("Changed requirement: inspect the revised rules before planning.")
        elif race == "followup":
            (folder / ".local-ai/USER-REQUESTS.md").write_text("New user requirement: support a different calculation.")
        elif race == "brief":
            item["brief"] += " Also preserve this newly added requirement."
        elif race == "stop":
            checkpoint = native_agent.read_state(item)
            checkpoint["status"] = "stopped"
            native_agent.save_state(item, checkpoint)
        else:
            competitor = task_context.plan_steps(folder, [queue_step("Competing completed preparation", "exit 0")])
            competitor_id = competitor["active"]["id"]
            checked = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": competitor_id}))
            assert checked["passed"] and checked["remaining"] == 0 and checked["total"] == 1
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    with project_memory.connection(folder) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM task_steps ORDER BY id").fetchall()]
    if race == "completed_competing_plan":
        assert len(rows) == 1 and rows[0]["id"] == competitor_id and rows[0]["status"] == "done"
    else:
        assert rows == []
    assert not (folder / "index.html").exists()
    assert state["status"] == ("stopped" if race == "stop" else "continue")


@pytest.mark.parametrize("history_complete,followup,requires_plan", [
    (True, "Change the Compute button color to blue.", False),
    (True, "Repair only the mistaken saved checker assertions; preserve the app source.", False),
    (False, "Change the Compute button color to blue.", True),
    (False, "Repair only the mistaken saved checker assertions; preserve the app source.", True),
    (True, "Build a new single-file index.html app with inputs and a button for currency conversion.", True),
])
def test_initial_planning_uses_latest_numbered_followup_only_after_historical_phases_finish(tmp_path, history_complete, followup, requires_plan):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "history-followup")
    task_context.plan_steps(folder, [queue_step("Prepare the original interface", "exit 0"),
                                     queue_step("Verify the original calculation", "exit 0")])
    first = task_context.step_status(folder)["active"]
    assert json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": first["id"]}))["passed"]
    if history_complete:
        second = task_context.step_status(folder)["active"]
        assert json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": second["id"]}))["passed"]
    (folder / ".local-ai/USER-REQUESTS.md").write_text(
        "## Request 1\nBuild the original calculator app with inputs.\n\n## Request 2\n" + followup + "\n")
    history = task_context.historical_step_status(folder)
    assert history["total"] == 2 and bool(history["remaining"]) is not history_complete
    assert task_context.step_status(folder)["total"] == 0  # Follow-up has a fresh request identity.
    assert native_agent.initial_plan_required(item) is requires_plan
    seen = []
    def complete(payload):
        seen.append(payload)
        assert (payload.get("_native_workload") == "initial_step_planning") is requires_plan
        return initial_planning_reply("read_file", {"path": "AGENTS.md"})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(seen) == 1 and state["status"] == "continue"
    assert task_context.historical_step_status(folder) == history and not (folder / "index.html").exists()


@pytest.mark.parametrize("race", ["rules", "stop"])
def test_initial_planning_transaction_window_change_after_insert_rolls_back_and_preserves_stop(tmp_path, race):
    from contextlib import contextmanager
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "insert-window-" + race)
    real_plan = task_context.plan_steps
    real_connection = project_memory.connection
    inserted = []
    callbacks = []
    @contextmanager
    def traced_connection(*args, **kwargs):
        with real_connection(*args, **kwargs) as conn:
            conn.set_trace_callback(lambda sql: inserted.append(sql) if sql.lstrip().upper().startswith("INSERT INTO TASK_STEPS") else None)
            yield conn
    def plan_with_window_change(*args, **kwargs):
        original_commit_guard = kwargs["before_commit"]
        def change_after_inserts():
            callbacks.append(len(inserted))
            if len(callbacks) == 2:
                assert len(inserted) == 2  # Both actual SQLite INSERTs ran in the open transaction.
                if race == "rules":
                    (folder / "AGENTS.md").write_text("Revised during the SQL transaction: inspect new requirements before planning.")
                else:
                    checkpoint = native_agent.read_state(item)
                    checkpoint["status"] = "stopped"
                    native_agent.save_state(item, checkpoint)
            return original_commit_guard()
        return real_plan(*args, **{**kwargs, "before_commit": change_after_inserts})
    def complete(payload):
        assert payload["_native_workload"] == "initial_step_planning"
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(task_context, "plan_steps", side_effect=plan_with_window_change), patch.object(project_memory, "connection", side_effect=traced_connection), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert callbacks == [0, 2]
    with project_memory.connection(folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_steps").fetchone()[0] == 0
    assert not (folder / "index.html").exists()
    assert state["status"] == ("stopped" if race == "stop" else "continue")
    assert native_agent.read_state(item)["status"] == state["status"]


def test_initial_planning_retry_refreshes_system_rules_before_second_inference(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "planning-rule-retry")
    marker = "NEW_RULE: Calculator must show the words Updated rules applied."
    seen = []
    def complete(payload):
        seen.append(payload)
        if len(seen) == 1:
            assert marker not in payload["messages"][0]["content"]
            (folder / "AGENTS.md").write_text(marker)
        else:
            assert marker in payload["messages"][0]["content"]
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(seen) == 2 and state["status"] == "continue"
    assert task_context.step_status(folder)["total"] == 2


def test_initial_planning_basis_capture_uses_matching_fresh_system_rules(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "planning-rule-capture")
    marker = "NEW_CAPTURE_RULE: Keep the revised output requirement in every plan."
    basis = native_agent.planning_basis
    captures = []
    def change_before_hash(project):
        if not captures:
            (folder / "AGENTS.md").write_text(marker)
        value = basis(project)
        captures.append(value)
        return value
    def complete(payload):
        assert marker in payload["messages"][0]["content"]
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "planning_basis", side_effect=change_before_hash), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        native_agent.run_slice(item, complete=complete)
    assert captures and task_context.step_status(folder)["total"] == 2


@pytest.mark.parametrize("window", ["inference", "after_insert"])
def test_initial_planning_preserves_concurrent_done_checkpoint(tmp_path, window):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "planning-done-" + window)
    real_plan = task_context.plan_steps
    callbacks = []
    def mark_done():
        state = native_agent.read_state(item)
        state.update(status="done", answer="Concurrent owner completed the task.")
        native_agent.save_state(item, state)
    def plan_with_done_window(*args, **kwargs):
        commit = kwargs["before_commit"]
        def before_commit():
            callbacks.append(True)
            if len(callbacks) == 2:
                mark_done()
            return commit()
        return real_plan(*args, **{**kwargs, "before_commit": before_commit})
    def complete(payload):
        if window == "inference":
            mark_done()
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(task_context, "plan_steps", side_effect=plan_with_done_window), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert task_context.step_status(folder)["total"] == 0
    if window == "after_insert":
        assert len(callbacks) == 2
    assert state["status"] == "done" and native_agent.read_state(item)["status"] == "done"
    assert state["answer"] == "Concurrent owner completed the task."


@pytest.mark.parametrize("followup", [
    "Create an installation guide for the index.html app with forms.",
    "Rewrite README to document the index.html app and its input forms.",
])
def test_initial_planning_completed_history_documentation_followup_is_exempt(tmp_path, followup):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "planning-doc-followup")
    active = task_context.plan_steps(folder, [queue_step("Completed original build", "exit 0")])["active"]
    assert json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))["passed"]
    (folder / ".local-ai/USER-REQUESTS.md").write_text("## Request 1\nOriginal build.\n\n## Request 2\n" + followup)
    assert not native_agent.initial_plan_required(item)


@pytest.mark.parametrize("brief,requires_plan", [
    ("Create a tutorial about index.html forms and input buttons.", False),
    ("For this task, use SQLite steps to repair the calculator before editing implementation.", True),
    ("Plan the app in SQLite steps before writing code.", True),
])
def test_initial_planning_classifies_documentation_and_explicit_plan_requests(tmp_path, brief, requires_plan):
    item = project(tmp_path / "planning-intent")
    item["brief"] = brief
    assert native_agent.initial_plan_required(item) is requires_plan


def prepare_existing_html_gate_fixture(folder, filename, *, complete=True):
    """Satisfy only SQL preparation, leaving every app/interaction gate unproven."""
    assert (folder / filename).is_file()
    command = f"if (Test-Path -LiteralPath '{filename}' -PathType Leaf) {{ exit 0 }} else {{ exit 1 }}"
    active = task_context.plan_steps(folder, [{"phase": "Fixture preparation",
        "title": "Confirm the existing HTML fixture file is available", "check_command": command,
        "shell": "powershell"}])["active"]
    if complete:
        checked = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
        assert checked["passed"] and checked["done"] == 1, checked
    return active


def noneditable_repair_integration_fixture(tmp_path, name, *, tag_hint=False, target="display", user_editable=False):
    item = project(tmp_path / name)
    item["brief"] = ("Repair the saved acceptance checker. The user requires an editable total-display input; inspect source before removing that requirement."
        if user_editable else "Repair the mistaken saved acceptance checker without changing this working calculator. Keep total-display a noneditable calculated display.")
    folder = Path(item["folder"])
    markup = {'display': '<div id="total-display">0</div>', 'unknown': '<div id="other-display">0</div>',
        'label': '<label id="total-display" for="units">Units</label>',
        'contenteditable': '<div id="total-display" contenteditable="true">0</div>'}[target]
    source = '''<form id="calculator"><input id="units"><button id="compute">Compute</button></form>''' + markup + '''
        <p id="result">0</p><script>document.getElementById('calculator').onsubmit=e=>{e.preventDefault();
        document.getElementById('result').textContent=String(Number(document.getElementById('units').value)*5);};</script>'''
    (folder / "index.html").write_text(source)
    bad_case = {"set": {"units": "7.2", "total-display": "0"}, "click": ["compute"], "expect": {"result": "36"}}
    if target in {"label", "contenteditable"}:
        bad_case["expect"] = {"result": "Impossible expected output"}  # Actual fill succeeds; a different assertion fails.
    if tag_hint:
        bad_case["visible"] = ["form"]
    bad = {"path": "index.html", "cases": [bad_case]}
    active = task_context.plan_steps(folder, [{"phase": "Calculator", "title": "Verify submitted calculation", "check_tool": "check_webapp", "check_arguments": bad}])["active"]
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert not failed["passed"]
    active = task_context.step_status(folder)["active"]
    notice = native_agent.active_step_repair_notice(item, {"step_focus_turns": 1})
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    return item, folder, source, bad, active, notice, runtime_state


def test_noneditable_repair_actual_bad_fill_recovers_definition_without_source_edit_or_completion(tmp_path):
    item, folder, source, bad, active, notice, runtime_state = noneditable_repair_integration_fixture(tmp_path, "bad-fill-repair")
    observations = json.loads(notice["content"].rsplit("\n", 1)[1])
    assert observations["candidate_noneditable_inputs"] == {"total-display": "div"}
    revision = {**observations["assertion_revision_reference"], "revise_inputs": True,
        "cases": [{"set": {"units": "7.2"}, "click": ["compute"], "expect": {"result": "36"}}]}
    def complete(payload):
        assert [tool["function"]["name"] for tool in payload["tools"]] == ["check_webapp"]
        assert payload["_native_workload"] == "active_step_checker_repair"
        return initial_planning_reply("check_webapp", revision, "bad-fill-real-repair")
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "configured_context", return_value=65536), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    result = json.loads(next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "bad-fill-real-repair"))
    assert result["repaired_case_passed"] and result["verifier_saved"] and result["active_step_check_aligned"]
    queue = task_context.step_status(folder)
    assert queue["done"] == 0 and queue["active"]["id"] == active["id"]
    assert queue["active"]["check_arguments"]["cases"] == revision["cases"]
    assert (folder / "index.html").read_text() == source


def test_noneditable_repair_host_preserves_valid_original_inputs_and_clicks(tmp_path):
    item, folder, source, bad, active, notice, runtime_state = noneditable_repair_integration_fixture(tmp_path, "bad-fill-preserve")
    saved = folder / ".local-ai/webapp-check.json"
    before = saved.read_bytes()
    observations = json.loads(notice["content"].rsplit("\n", 1)[1])
    def complete(payload):
        parameters = payload["tools"][0]["function"]["parameters"]
        case_schema = parameters["properties"]["cases"]["items"]
        assert case_schema["properties"]["set"]["const"] == {"units": "7.2"}
        assert case_schema["properties"]["click"]["const"] == ["compute"]
        return initial_planning_reply("check_webapp", {**observations["assertion_revision_reference"], "revise_inputs": True,
            "cases": [{"set": {"units": "999"}, "click": ["compute"], "expect": {"result": "4995"}}]}, "bad-fill-changed-input")
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "configured_context", return_value=65536), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "bad-fill-changed-input")
    assert "offered schema" in reply and saved.read_bytes() == before
    assert task_context.step_status(folder)["active"]["check_arguments"] == bad
    assert (folder / "index.html").read_text() == source


@pytest.mark.parametrize("target", ["unknown", "label", "contenteditable"])
def test_noneditable_repair_does_not_remove_unknown_associated_label_or_contenteditable_targets(tmp_path, target):
    item, folder, source, bad, active, notice, runtime_state = noneditable_repair_integration_fixture(tmp_path, "bad-fill-skip-" + target, target=target)
    observations = json.loads(notice["content"].rsplit("\n", 1)[1]) if notice else {}
    assert "candidate_noneditable_inputs" not in observations
    assert task_context.step_status(folder)["active"]["check_arguments"] == bad
    assert (folder / "index.html").read_text() == source


def test_noneditable_repair_observed_bad_fill_takes_priority_over_tag_alias_hint(tmp_path):
    item, folder, source, bad, active, notice, runtime_state = noneditable_repair_integration_fixture(tmp_path, "bad-fill-priority", tag_hint=True)
    observations = json.loads(notice["content"].rsplit("\n", 1)[1])
    assert observations["candidate_noneditable_inputs"] == {"total-display": "div"}
    assert "candidate_static_tag_ids" not in observations
    request = native_agent.focused_step_repair_request(item, {"step_focus_turns": 1}, native_agent.TOOLS, notice, 65536)
    assert [tool["function"]["name"] for tool in request["tools"]] == ["check_webapp"]
    assert request["tools"][0]["function"]["parameters"]["properties"]["revise_inputs"]["const"] is True
    assert task_context.step_status(folder)["active"]["check_arguments"] == bad


def test_noneditable_repair_user_required_editable_control_explanation_restores_normal_tools(tmp_path):
    item, folder, source, bad, active, notice, runtime_state = noneditable_repair_integration_fixture(tmp_path, "bad-fill-user-requirement", user_editable=True)
    stages = []
    def complete(payload):
        stages.append(payload.get("_native_workload"))
        if len(stages) == 1:
            assert [tool["function"]["name"] for tool in payload["tools"]] == ["check_webapp"]
            assert "user requires an editable total-display input" in payload["messages"][0]["content"]
            return {"choices": [{"message": {"role": "assistant", "content": "The original request requires an editable control. This is a source bug; preserve that requirement and restore ordinary tools."}}]}
        assert "edit_file" in {tool["function"]["name"] for tool in payload["tools"]}
        return initial_planning_reply("read_file", {"path": "index.html"})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), patch.object(native_agent, "configured_context", return_value=65536), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert stages == ["active_step_checker_repair", None] and state["status"] == "continue"
    assert task_context.step_status(folder)["done"] == 0 and task_context.step_status(folder)["active"]["check_arguments"] == bad
    assert (folder / "index.html").read_text() == source


def test_initial_planning_completed_html_followup_allows_explicit_sqlite_python_cli_acceptance(tmp_path):
    from jsonschema import validate
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "html-to-python-cli")
    original = task_context.plan_steps(folder, [queue_step("Complete previous HTML preparation", "exit 0")])["active"]
    assert json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": original["id"]}))["passed"]
    (folder / ".local-ai/USER-REQUESTS.md").write_text(
        "## Request 1\nBuild the original index.html calculator.\n\n## Request 2\n"
        "Build a Python CLI calculator in cli.py. Use SQLite steps and verify add(left,right) and multiply(left,right).")
    assert native_agent.initial_plan_required(item)
    python_steps = [{"phase": "Python CLI", "title": title, "check_tool": "check_python_function", "check_arguments": {
        "path": "cli.py", "function": function, "cases": [{"args": [2, 3], "expected": expected}]}}
        for title, function, expected in (("Add supplied values", "add", 5), ("Multiply supplied values", "multiply", 6))]
    offered = native_agent.initial_plan_tools(item, native_agent.TOOLS)
    schema = next(tool["function"]["parameters"] for tool in offered if tool["function"]["name"] == "task_steps")
    arguments = {"action": "plan", "steps": python_steps}
    validate(arguments, schema)
    def complete(payload):
        assert payload["_native_workload"] == "initial_step_planning"
        return initial_planning_reply("task_steps", arguments)
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        native_agent.run_slice(item, complete=complete)
    queue = task_context.step_status(folder)
    assert queue["total"] == 2 and queue["done"] == 0 and queue["active"]["check_tool"] == "check_python_function"
    assert not (folder / "cli.py").exists()


def test_initial_planning_completed_latest_workflow_ignores_abandoned_older_request_units(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "abandoned-request-history")
    abandoned = task_context.plan_steps(folder, [queue_step("Old abandoned request A", "exit 1")])["active"]
    requests = folder / ".local-ai/USER-REQUESTS.md"
    requests.write_text("## Request 1\nBuild replacement app B.\n")
    replacement = task_context.plan_steps(folder, [queue_step("Complete replacement request B", "exit 0")])["active"]
    assert json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": replacement["id"]}))["passed"]
    requests.write_text("## Request 1\nBuild replacement app B.\n\n## Request 2\nChange the button color to blue.\n")
    with project_memory.connection(folder) as conn:
        before = [dict(row) for row in conn.execute("SELECT * FROM task_steps ORDER BY id").fetchall()]
    assert len(before) == 2 and before[0]["id"] == abandoned["id"] and before[0]["status"] == "active"
    assert before[1]["id"] == replacement["id"] and before[1]["status"] == "done"
    assert task_context.step_status(folder)["total"] == 0
    assert not native_agent.initial_plan_required(item)
    def complete(payload):
        assert payload.get("_native_workload") != "initial_step_planning"
        return initial_planning_reply("write_file", {"path": "color-note.txt", "content": "A small follow-up can proceed."})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert state["status"] == "continue" and (folder / "color-note.txt").is_file()
    with project_memory.connection(folder) as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM task_steps ORDER BY id").fetchall()] == before


@pytest.mark.parametrize("field", ["visible", "hidden"])
@pytest.mark.parametrize("target", ['<input id="units">', 'role=alert', 'units\n'])
def test_initial_planning_visibility_rejects_markup_and_role_selectors_without_sql_mutation(tmp_path, field, target):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "planned-visibility-ids")
    steps[0]["check_arguments"]["cases"] = [{field: [target]}]
    original_tools = copy.deepcopy(native_agent.TOOLS)
    def complete(payload):
        assert payload["_native_workload"] == "initial_step_planning"
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps}, "bad-planned-visibility")
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "bad-planned-visibility")
    assert "offered schema" in reply and task_context.step_status(folder)["total"] == 0
    assert not (folder / "index.html").exists() and native_agent.TOOLS == original_tools


def test_initial_planning_visibility_accepts_ids_and_preserves_literal_markup_expectations(tmp_path):
    from jsonschema import validate
    import webapp_check
    item, folder, original_steps, runtime_state = initial_planning_integration_fixture(tmp_path, "planned-literal-markup")
    item["brief"] = "Build a single-file index.html app with an input editor and Show button that displays the entered HTML markup as literal text in preview."
    literal = '<input id="sample">'
    steps = [
        {"phase": "Interface", "title": "Show the editor controls", "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"visible": ["editor", "show", "preview"], "hidden": ["validation-error"]}]}},
        {"phase": "Preview", "title": "Display entered markup as literal text", "check_tool": "check_webapp", "check_arguments": {
            "path": "index.html", "cases": [{"set": {"editor": literal}, "click": ["show"], "expect": {"preview": literal}}]}}]
    original_tools = copy.deepcopy(native_agent.TOOLS)
    public = next(tool["function"]["parameters"] for tool in native_agent.TOOLS if tool["function"]["name"] == "check_webapp")
    validate({"path": "index.html", "cases": [{"visible": [literal, "role=alert"]}]}, public)
    def complete(payload):
        assert payload["_native_workload"] == "initial_step_planning"
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    queue = task_context.step_status(folder)
    assert queue["total"] == 2 and queue["done"] == 0 and state["status"] == "continue"
    assert queue["active"]["check_arguments"]["cases"] == steps[0]["check_arguments"]["cases"]
    with project_memory.connection(folder) as conn:
        saved = json.loads(conn.execute("SELECT check_arguments FROM task_steps WHERE title=?", (steps[1]["title"],)).fetchone()[0])
    assert saved["cases"][0]["expect"]["preview"] == literal
    (folder / "index.html").write_text("""<input id="editor"><button id="show">Show</button><output id="preview"></output>
        <p id="validation-error" hidden>Error</p><script>document.getElementById('show').onclick=()=>{
        document.getElementById('preview').textContent=document.getElementById('editor').value;};</script>""")
    assert webapp_check.check(folder, saved)["passed"]  # Literal markup remains actual visible text.
    assert native_agent.TOOLS == original_tools and task_context.step_status(folder)["done"] == 0


@pytest.mark.parametrize("defect", ["no_action", "action_only", "empty_action", "empty_outcome", "across_units"])
def test_initial_planning_interaction_requirement_rejects_inert_sequences_before_sql(tmp_path, defect):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "inert-plan-" + defect)
    if defect == "no_action":
        steps[1]["check_arguments"]["cases"] = [{"visible": ["total"]}]
    elif defect == "action_only":
        steps[1]["check_arguments"]["cases"] = [{"click": ["compute"]}]
    elif defect == "empty_action":
        steps[1]["check_arguments"]["cases"] = [{"set": {}, "click": [], "keys": [], "reload": False, "expect": {"total": "36"}}]
    elif defect == "empty_outcome":
        steps[1]["check_arguments"]["cases"] = [{"set": {"units": "7.2"}, "expect": {}, "state": {}, "visible": [], "hidden": []}]
    else:
        steps[0]["check_arguments"]["cases"] = [{"click": ["compute"]}]
        steps[1]["check_arguments"]["cases"] = [{"visible": ["total"]}]
    original_tools = copy.deepcopy(native_agent.TOOLS)
    def complete(payload):
        assert payload["_native_workload"] == "initial_step_planning"
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps}, "inert-planned-sequence")
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "inert-planned-sequence")
    assert "offered schema" in reply and task_context.step_status(folder)["total"] == 0
    assert not (folder / "index.html").exists() and native_agent.TOOLS == original_tools


@pytest.mark.parametrize("adjacent", [False, True])
def test_initial_planning_interaction_accepts_static_layout_then_real_sequence_and_adjacent_observation(tmp_path, adjacent):
    import webapp_check
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "interactive-plan-" + str(adjacent))
    if adjacent:
        steps[1]["check_arguments"]["cases"] = [
            {"set": {"units": "7.2"}, "click": ["compute"]}, {"expect": {"total": "36"}}]
    def complete(payload):
        assert payload["_native_workload"] == "initial_step_planning"
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    queue = task_context.step_status(folder)
    assert queue["total"] == 2 and queue["done"] == 0 and state["status"] == "continue"
    assert queue["active"]["check_arguments"]["cases"] == steps[0]["check_arguments"]["cases"]
    with project_memory.connection(folder) as conn:
        saved = json.loads(conn.execute("SELECT check_arguments FROM task_steps WHERE title=?", (steps[1]["title"],)).fetchone()[0])
    assert webapp_check.normalize_spec(saved)["cases"] == [
        {"set": {"units": "7.2"}, "click": ["compute"], "expect": {"total": "36"}}]
    (folder / "index.html").write_text("""<form id="calculator"><input id="units"><button id="compute">Compute</button></form>
        <output id="total">0</output><script>document.getElementById('calculator').onsubmit=e=>{e.preventDefault();
        document.getElementById('total').textContent=String(Number(document.getElementById('units').value)*5);};</script>""")
    assert webapp_check.check(folder, saved)["passed"]
    assert task_context.step_status(folder)["done"] == 0


def test_initial_planning_interaction_contract_preserves_each_action_outcome_and_nonbrowser_tools(tmp_path):
    from jsonschema import validate
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "plan-contract-scope")
    original_tools = copy.deepcopy(native_agent.TOOLS)
    tools = native_agent.initial_plan_tools(item, native_agent.TOOLS)
    schema = next(tool["function"]["parameters"] for tool in tools if tool["function"]["name"] == "task_steps")
    for action in ({"set": {"units": "7.2"}}, {"click": ["compute"]}, {"keys": [{"key": "Enter"}]}, {"reload": True}):
        for outcome in ({"expect": {"total": "36"}}, {"state": {"app.ready": True}}, {"visible": ["total"]}, {"hidden": ["error"]}):
            candidate = copy.deepcopy(steps)
            candidate[1]["check_arguments"]["cases"] = [{**action, **outcome}]
            validate({"action": "plan", "steps": candidate}, schema)
    other = {**item, "brief": "Use SQLite steps to build a Python CLI calculator."}
    nonbrowser_tools = native_agent.initial_plan_tools(other, native_agent.TOOLS)
    nonbrowser = next(tool["function"]["parameters"] for tool in nonbrowser_tools if tool["function"]["name"] == "task_steps")
    assert "contains" not in nonbrowser["properties"]["steps"]
    validate({"action": "plan", "steps": [{"phase": "Logic", "title": "Add arguments", "check_tool": "check_python_function",
        "check_arguments": {"path": "cli.py", "function": "add", "cases": [{"args": [2, 3], "expected": 5}]}}]}, nonbrowser)
    public = next(tool["function"]["parameters"] for tool in native_agent.TOOLS if tool["function"]["name"] == "task_steps")
    inert = copy.deepcopy(steps)
    inert[1]["check_arguments"]["cases"] = [{"visible": ["total"]}]
    validate({"action": "plan", "steps": inert}, public)
    assert native_agent.TOOLS == original_tools


def test_initial_planning_contains_failure_explains_missing_interaction_without_repeating_values(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "semantic-plan-diagnostic")
    steps[1]["check_arguments"]["cases"] = [{"expect": {"total": "PRIVATE_UNTRUSTED_MODEL_VALUE"}}]
    request = {"tools": native_agent.initial_plan_tools(item, native_agent.TOOLS)}
    schema = next(tool["function"]["parameters"] for tool in request["tools"] if tool["function"]["name"] == "task_steps")
    hint = schema["properties"]["steps"]["contains"]["description"]
    assert "nonempty set/click/keys or reload:true" in hint and "same unit's cases" in hint
    response = initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with pytest.raises(cloud_ai.ToolArgumentError) as caught:
        cloud_ai.valid_completion(response, request)
    error = caught.value
    assert error.path == ["steps"] and error.problem == "Schema rule contains: " + hint + "."
    assert "PRIVATE_UNTRUSTED_MODEL_VALUE" not in str(error)
    assert task_context.step_status(folder)["total"] == 0


def test_initial_planning_contains_feedback_guides_semantic_retry_then_saves_valid_plan(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "semantic-contains-retry")
    calls = []
    def complete(payload):
        calls.append(payload)
        if len(calls) == 1:
            inert = copy.deepcopy(steps)
            inert[1]["check_arguments"]["cases"] = [{"expect": {"total": "PRIVATE_MODEL_VALUE"}}]
            # Exercise the same schema rejection that the actual Router raises.
            return cloud_ai.valid_completion(initial_planning_reply("task_steps", {"action": "plan", "steps": inert}), payload)
        feedback = next(message["content"] for message in reversed(payload["messages"])
            if "last inference was rejected" in message.get("content", ""))
        assert "nonempty set/click/keys or reload:true" in feedback
        assert "same unit's cases" in feedback and "Preserve the declared JSON types" in feedback
        assert "quoted JSON" not in feedback and "argument syntax" not in feedback
        assert "PRIVATE_MODEL_VALUE" not in feedback
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(calls) == 2 and state["status"] == "continue"
    assert task_context.step_status(folder)["total"] == 2 and task_context.step_status(folder)["done"] == 0
    assert not (folder / "index.html").exists()


@pytest.mark.parametrize("flag", [None, False, "true", 1])
def test_default_source_first_build_allows_source_then_sql_without_enabling_experimental_gate(tmp_path, flag):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-source-first-" + str(flag))
    item.pop("experimental_initial_plan")
    if flag is not None:
        item["experimental_initial_plan"] = flag
    source = '<form id="calculator"><input id="units"><button id="compute">Compute</button></form><output id="total">0</output>'
    calls = []
    def complete(payload):
        calls.append(payload)
        assert payload.get("_native_workload") != "initial_step_planning"
        assert payload["_native_plan_basis"] == native_agent.planning_basis(item)
        names = {tool["function"]["name"] for tool in payload["tools"]}
        assert {"write_file", "read_file", "task_steps"} <= names
        assert any("source skeleton first" in message.get("content", "") for message in payload["messages"])
        if len(calls) == 1:
            return initial_planning_reply("write_file", {"path": "index.html", "content": source})
        assert (folder / "index.html").read_text() == source
        assert task_context.step_status(folder)["total"] == 0
        assert "SQLite" in native_agent.completion_issue(folder, item, {"verify_passed": True})
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 2), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    queue = task_context.step_status(folder)
    assert len(calls) == 2 and state["status"] == "continue" and (folder / "index.html").read_text() == source
    assert queue["total"] == 2 and queue["done"] == 0
    assert "unfinished work" in native_agent.completion_issue(folder, item, {"verify_passed": True})


@pytest.mark.parametrize("brief", [
    "Build a single-file index.html app with an input that updates a total.",
    "For this task, use SQLite steps to build a Python CLI calculator.",
])
def test_default_source_first_cannot_finish_without_required_sql_queue(tmp_path, brief):
    item = project(tmp_path / "default-unplanned-finish")
    item["brief"] = brief
    folder = Path(item["folder"])
    assert native_agent.initial_plan_required(item)
    runtime_state = tmp_path / "state"
    runtime_state.mkdir()
    def complete(payload):
        assert payload.get("_native_workload") != "initial_step_planning"
        return {"choices": [{"message": {"role": "assistant", "content": "Everything is finished and verified."}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert state["status"] == "continue" and task_context.step_status(folder)["total"] == 0
    assert "SQLite" in native_agent.completion_issue(folder, item, {"verify_passed": True, "browser_verified": True})
    assert not state.get("verify_passed")


def test_default_source_first_preserves_uncreated_plan_failure_and_browser_completion_gates(tmp_path):
    item = project(tmp_path / "default-quality-gates")
    item["brief"] = "Explain this existing calculator and verify its notes."
    folder = Path(item["folder"])
    state = {"verify_passed": True, "uncreated_step_plan": {
        "request_hash": task_context.request_identity(folder), "error": "Typed plan was rejected"}}
    assert "initial SQLite plan was not created" in native_agent.completion_issue(folder, item, state)
    item["brief"] = "Build a single-file index.html app with an input that updates a total."
    (folder / "index.html").write_text('<input id="units"><output id="total">0</output>')
    prepare_existing_html_gate_fixture(folder, "index.html")
    (folder / ".local-ai/verify.ps1").write_text("exit 0")
    assert "Run check_webapp" in native_agent.completion_issue(folder, item, {"verify_passed": True})


def test_default_source_first_uses_latest_python_sql_followup_without_new_html_guidance(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-html-to-python-followup")
    item.pop("experimental_initial_plan")
    active = task_context.plan_steps(folder, [queue_step("Completed previous HTML workflow", "exit 0")])["active"]
    assert json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))["passed"]
    (folder / ".local-ai/USER-REQUESTS.md").write_text(
        "## Request 1\nBuild the original HTML app.\n\n## Request 2\n"
        "Build a Python CLI in cli.py using SQLite steps and verify the calculation.")
    assert native_agent.initial_plan_required(item) and "Python CLI" in native_agent.planning_intent(item)
    def complete(payload):
        assert payload.get("_native_workload") != "initial_step_planning"
        assert {"write_file", "check_python_function", "task_steps"} <= {tool["function"]["name"] for tool in payload["tools"]}
        assert not any("source skeleton first" in message.get("content", "") for message in payload["messages"])
        return initial_planning_reply("write_file", {"path": "cli.py", "content": "def calculate(value): return value * 5\n"})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert state["status"] == "continue" and (folder / "cli.py").is_file()
    assert task_context.step_status(folder)["total"] == 0
    assert "SQLite" in native_agent.completion_issue(folder, item, {"verify_passed": True})


@pytest.mark.parametrize("changed", ["rules", "brief", "followup"])
def test_default_source_first_basis_rejects_stale_inference_before_source_write(tmp_path, changed):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-stale-" + changed)
    item.pop("experimental_initial_plan")
    marker = "Fresh requirement must be read before acting."
    def complete(payload):
        assert payload["_native_plan_basis"] == native_agent.planning_basis(item)
        assert payload.get("_native_workload") != "initial_step_planning"
        if changed == "rules":
            (folder / "AGENTS.md").write_text(marker)
        elif changed == "brief":
            item["brief"] += " " + marker
        else:
            (folder / ".local-ai/USER-REQUESTS.md").write_text(marker)
        return initial_planning_reply("write_file", {"path": "index.html", "content": "Stale response must not run."})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert not (folder / "index.html").exists() and task_context.step_status(folder)["total"] == 0
    assert state["status"] == "continue" and state["current"] == "Reading changed project requirements"
    if changed == "followup":
        # Follow-ups remain authoritative in the durable request document;
        # fresh_messages adds them to the next checkpoint, not the system brief.
        assert (folder / ".local-ai/USER-REQUESTS.md").read_text() == marker
    else:
        assert marker in state["messages"][0]["content"]


@pytest.mark.parametrize("status", ["stopped", "done"])
def test_default_source_first_preserves_task_control_changed_during_inference(tmp_path, status):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-control-" + status)
    item.pop("experimental_initial_plan")
    def complete(payload):
        assert payload["_native_plan_basis"]
        checkpoint = native_agent.read_state(item)
        checkpoint.update(status=status, answer="Concurrent control owner")
        native_agent.save_state(item, checkpoint)
        return initial_planning_reply("write_file", {"path": "index.html", "content": "Do not run after task control."})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert state["status"] == native_agent.read_state(item)["status"] == status
    assert state["answer"] == "Concurrent control owner" and not (folder / "index.html").exists()


@pytest.mark.parametrize("finished", [False, True])
def test_default_source_first_late_plan_cannot_append_after_competing_initial_queue(tmp_path, finished):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-competing-plan-" + str(finished))
    item.pop("experimental_initial_plan")
    competitor_id = None
    def complete(payload):
        nonlocal competitor_id
        assert payload.get("_native_workload") != "initial_step_planning" and payload["_native_plan_basis"]
        active = task_context.plan_steps(folder, [queue_step("Competing real preparation", "exit 0")])["active"]
        competitor_id = active["id"]
        if finished:
            assert json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": competitor_id}))["passed"]
        return initial_planning_reply("task_steps", {"action": "plan", "steps": steps}, "default-stale-plan")
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    with project_memory.connection(folder) as conn:
        rows = conn.execute("SELECT id,status FROM task_steps").fetchall()
    assert [tuple(row) for row in rows] == [(competitor_id, "done" if finished else "active")]
    reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "default-stale-plan")
    assert "initial plan already exists" in reply


@pytest.mark.parametrize("operation", ["status", "complete"])
def test_default_source_first_attaches_initial_plan_metadata_only_to_plan_action(tmp_path, operation):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-bookkeeping-" + operation)
    item.pop("experimental_initial_plan")
    execute, dispatched = native_agent.execute_tool, []
    def observed_execute(task_folder, name, arguments, **kwargs):
        if name == "task_steps":
            dispatched.append(arguments)
        return execute(task_folder, name, arguments, **kwargs)
    def complete(payload):
        assert payload["_native_plan_basis"] and payload.get("_native_workload") != "initial_step_planning"
        arguments = {"action": operation}
        if operation == "complete":
            arguments["step_id"] = 999
        return initial_planning_reply("task_steps", arguments)
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "execute_tool", side_effect=observed_execute), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert len(dispatched) == 1
    assert "_initial_plan" not in dispatched[0] and "_expected_request" not in dispatched[0]
    assert state["status"] == "continue" and task_context.step_status(folder)["total"] == 0


@pytest.mark.parametrize("change", ["rules", "stopped", "done"])
def test_default_source_first_per_tool_preflight_blocks_later_write_after_read_change(tmp_path, change):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-midbatch-" + change)
    item.pop("experimental_initial_plan")
    execute = native_agent.execute_tool
    def observed_execute(task_folder, name, arguments, **kwargs):
        result = execute(task_folder, name, arguments, **kwargs)
        if name == "read_file":
            if change == "rules":
                (folder / "AGENTS.md").write_text("New rules arrived during the first read.")
            else:
                state = native_agent.read_state(item)
                state["status"] = change
                native_agent.save_state(item, state)
        return result
    def complete(payload):
        assert payload["_native_plan_basis"] and {"read_file", "write_file"} <= {tool["function"]["name"] for tool in payload["tools"]}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "first-read", "function": {"name": "read_file", "arguments": json.dumps({"path": "AGENTS.md"})}},
            {"id": "later-write", "function": {"name": "write_file", "arguments": json.dumps({"path": "index.html", "content": "Stale later write"})}}]}}]}
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "execute_tool", side_effect=observed_execute), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert not (folder / "index.html").exists() and task_context.step_status(folder)["total"] == 0
    if change == "rules":
        reply = next(message["content"] for message in state["messages"] if message.get("tool_call_id") == "later-write")
        assert "Project requirements changed" in reply
    else:
        assert state["status"] == native_agent.read_state(item)["status"] == change


def test_inspect_webapp_is_available_in_html_discovery_without_check_side_effects(tmp_path):
    item = project(tmp_path / "grounded-inspection")
    item["brief"] = "Build an interactive index.html calculator using SQLite steps."
    folder = Path(item["folder"])
    (folder / "index.html").write_text('<input id="amount"><output id="total"></output>', encoding="utf-8")
    def names(tools):
        return {tool["function"]["name"] for tool in tools}
    assert "inspect_webapp" in names(native_agent.initial_plan_tools(item, native_agent.TOOLS))
    python = {**item, "brief": "Use SQLite steps to build a Python CLI calculator."}
    assert "inspect_webapp" not in names(native_agent.initial_plan_tools(python, native_agent.TOOLS))
    task_context.plan_steps(folder, [{"phase": "App", "title": "Calculate", "check_tool": "check_webapp",
        "check_arguments": {"path": "index.html", "cases": [{"set": {"amount": "2"}, "expect": {"total": "4"}}]}}])
    task_context.record_action(folder, "check_webapp", '{"passed":false}', {"path": "index.html"})
    before = task_context.step_status(folder)
    assert "inspect_webapp" in names(native_agent.repair_tools(item, {}))
    assert "inspect_webapp" in names(native_agent.step_tools(item, {}, native_agent.TOOLS))
    state = {}
    native_agent.remember_checker_verdict(state, "inspect_webapp", {"path": "index.html"}, '{"dom_contract":{}}')
    assert not state and task_context.step_status(folder) == before
    assert "unfinished work" in native_agent.completion_issue(folder, item, {"verify_passed": True})


def test_default_source_first_guidance_survives_existing_narrower_tools_note(tmp_path):
    item, folder, steps, runtime_state = initial_planning_integration_fixture(tmp_path, "default-guide-order")
    item.pop("experimental_initial_plan")
    available = [tool for tool in native_agent.TOOLS if tool["function"]["name"] in {"read_file", "write_file", "task_steps"}]
    def complete(payload):
        assert payload.get("_native_workload") != "initial_step_planning" and payload["_native_plan_basis"]
        assert any("source skeleton first" in message.get("content", "") for message in payload["messages"])
        assert any("Use the current checkpoint's active step" in message.get("content", "") for message in payload["messages"])
        return initial_planning_reply("read_file", {"path": "AGENTS.md"})
    with patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1), patch.object(native_agent, "repair_tools", return_value=available), patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", runtime_state):
        state = native_agent.run_slice(item, complete=complete)
    assert state["status"] == "continue" and not (folder / "index.html").exists()


def test_row_counts_host_schema_supports_public_planning_and_repair_counts(tmp_path):
    from jsonschema import ValidationError, validate
    item = project(tmp_path / "count-schema")
    item["brief"] = "Build an index.html expense app using SQLite steps."
    folder = Path(item["folder"])
    schema = next(tool["function"]["parameters"] for tool in native_agent.TOOLS if tool["function"]["name"] == "check_webapp")
    arguments = {"path": "index.html", "cases": [{"click": ["add"], "row_counts": {"rows": 2}}]}
    validate(arguments, schema)
    for counts in ({"rows": True}, {"rows": -1}, {"rows": 10001}, {"rows": 1.5}, {"rows.length": 1}, {"rows\n": 1}):
        with pytest.raises(ValidationError):
            validate({**arguments, "cases": [{"row_counts": counts}]}, schema)
    planned = [{"phase": "Layout", "title": "Empty table", "check_tool": "check_webapp", "check_arguments": {
        "path": "index.html", "cases": [{"row_counts": {"rows": 0}}]}},
        {"phase": "Add", "title": "Add one expense", "check_tool": "check_webapp", "check_arguments": arguments}]
    focused = native_agent.initial_plan_tools(item, native_agent.TOOLS)
    plan_schema = next(tool["function"]["parameters"] for tool in focused if tool["function"]["name"] == "task_steps")
    validate({"action": "plan", "steps": planned}, plan_schema)
    active = task_context.plan_steps(folder, planned)["active"]
    task_context.record_step_check(folder, active["id"], task_context.request_identity(folder), 'Exit code: 1\nExpected rows', unchanged=True)
    repair_tools = native_agent.acceptance_repair_tools(item, {"step_focus_turns": 1}, native_agent.TOOLS)
    repair = next(tool["function"]["parameters"] for tool in repair_tools if tool["function"]["name"] == "task_steps")
    validate({"action": "revise_check", "check_arguments": planned[0]["check_arguments"]}, repair)
    description = next(tool["function"]["description"] for tool in native_agent.TOOLS if tool["function"]["name"] == "check_webapp")
    assert "row_counts:{tbodyId:2}" in description and "table includes headers" in description


def test_row_counts_host_revision_keeps_original_count_assertions(tmp_path):
    folder = tmp_path
    (folder / "index.html").write_text("<table id='rows'></table>", encoding="utf-8")
    planned = {"path": "index.html", "cases": [{"click": ["add"], "row_counts": {"rows": 1}}]}
    active = {"check_arguments": planned}
    assert native_agent.browser_revision_matches_step(folder, active, copy.deepcopy(planned))
    for case in ({"click": ["add"]}, {"click": ["add"], "row_counts": {"rows": 2}},
                 {"click": ["other"], "row_counts": {"rows": 1}}):
        assert not native_agent.browser_revision_matches_step(folder, active, {"path": "index.html", "cases": [case]})


def test_row_counts_host_effect_credit_requires_asserted_changed_target_and_fresh_page(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<button id='add'>Add</button><table id='rows'></table>", encoding="utf-8")
    arguments = {"path": "index.html", "cases": [{"row_counts": {"rows": 0}}, {"click": ["add"], "row_counts": {"rows": 1}}]}
    verdict = {"passed": True, "page_sha256": hashlib.sha256(page.read_bytes()).hexdigest(),
               "interactions": [{"case": 2, "targets": ["rows:rows"]}]}
    evidence = native_agent.browser_check_evidence(tmp_path, arguments, json.dumps(verdict))
    assert evidence["browser_verified"] and evidence["browser_interactions"] == [{"case": 2, "targets": ["rows:rows"], "actions": ["click"]}]
    wrong_target = {**verdict, "interactions": [{"case": 2, "targets": ["rows:unasserted"]}]}
    assert native_agent.browser_check_evidence(tmp_path, arguments, json.dumps(wrong_target)) == {}
    assert native_agent.browser_check_evidence(tmp_path, arguments, json.dumps({**verdict, "passed": False})) == {}
    assert native_agent.browser_check_evidence(tmp_path, arguments, json.dumps({**verdict, "interactions": []})) == {}
    page.write_text("Changed after evidence", encoding="utf-8")
    assert native_agent.browser_check_evidence(tmp_path, arguments, json.dumps(verdict)) == {}


def test_row_counts_host_invalid_acceptance_prompt_teaches_counts_without_loosening_scope(tmp_path):
    from jsonschema import ValidationError, validate
    item, folder, source, active, replacement, runtime_state = invalid_definition_scope_fixture(tmp_path, "row-count-repair")
    before = task_context.step_status(folder)
    state = {"step_focus_turns": 1}
    available = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    request = native_agent.focused_step_repair_request(item, state, available, None, 65536)
    assert request and [tool["function"]["name"] for tool in request["tools"]] == ["task_steps"]
    encoded = json.dumps(request)
    assert "row_counts:{tbodyId:2}" in encoded and "table includes headers" in encoded and "tbody data only" in encoded
    assert "JS expressions" in encoded
    counted = {**replacement, "check_arguments": {"path": "index.html", "cases": [{"row_counts": {"expense-table": 1}}]}}
    schema = request["tools"][0]["function"]["parameters"]
    valid = {"action": "revise", "step_id": active["id"], "steps": [counted]}
    validate(valid, schema)
    for invalid in ({**valid, "action": "complete"}, {**valid, "step_id": active["id"] + 1},
                    {**valid, "steps": [{**counted, "title": "Changed requirement"}]}):
        with pytest.raises(ValidationError):
            validate(invalid, schema)
    assert task_context.step_status(folder) == before and (folder / "index.html").read_text() == source


def test_row_counts_host_actual_table_header_counts_and_current_source_evidence(tmp_path):
    import webapp_check
    page = tmp_path / "index.html"
    page.write_text("<button id='add' onclick=\"document.querySelector('tbody').insertRow().insertCell().textContent='Expense'\">Add</button>"
                    "<table id='ledger'><thead><tr><th>Expense</th></tr></thead><tbody id='entries'></tbody></table>", encoding="utf-8")
    arguments = {"path": "index.html", "cases": [
        {"row_counts": {"entries": 0, "ledger": 1}},
        {"click": ["add"], "row_counts": {"entries": 1, "ledger": 2}},
        {"click": ["add"], "row_counts": {"entries": 2, "ledger": 3}}]}
    verdict = webapp_check.check(tmp_path, arguments)
    raw = json.dumps(verdict)
    assert verdict["passed"] and verdict["cases"][2]["row_counts"]["ledger"]["actual"] == 3
    assert verdict["cases"][2]["row_counts"]["entries"]["actual"] == 2
    evidence = native_agent.browser_check_evidence(tmp_path, arguments, raw)
    assert evidence["browser_verified"] and {item["case"] for item in evidence["browser_interactions"]} == {2, 3}
    assert all(set(item["targets"]) == {"rows:entries", "rows:ledger"} for item in evidence["browser_interactions"])
    page.write_text(page.read_text().replace("Expense'", "Changed'"), encoding="utf-8")
    assert native_agent.browser_check_evidence(tmp_path, arguments, raw) == {}


def test_row_counts_host_repair_budget_renews_only_when_base_checker_contract_changes(tmp_path):
    item, folder, source, active, replacement, runtime_state = invalid_definition_scope_fixture(tmp_path, "contract-aware-budget")
    before = task_context.step_status(folder)
    state = {"step_focus_turns": 1}
    available = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    assert native_agent.focused_step_repair_request(item, state, available, None, 65536)
    assert native_agent.focused_step_repair_request(item, state, available, None, 65536)
    assert state["step_repair_focus"]["count"] == 2
    old_key = state["step_repair_focus"]["key"]
    assert native_agent.focused_step_repair_request(item, state, available, None, 65536) is None
    # Narrowed per-turn descriptions are not new host capabilities.
    dynamic = copy.deepcopy(available)
    dynamic[0]["function"]["description"] += " Dynamic turn note."
    assert native_agent.focused_step_repair_request(item, state, dynamic, None, 65536) is None
    changed = copy.deepcopy(native_agent.TOOLS)
    check = next(tool for tool in changed if tool["function"]["name"] == "check_webapp")
    check["function"]["parameters"]["properties"]["new_read_only_capability"] = {"type": "boolean"}
    with patch.object(native_agent, "TOOLS", changed):
        offered = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
        assert native_agent.focused_step_repair_request(item, state, offered, None, 65536)
        assert state["step_repair_focus"]["count"] == 1 and state["step_repair_focus"]["key"] != old_key
        assert native_agent.focused_step_repair_request(item, state, offered, None, 65536)
        assert native_agent.focused_step_repair_request(item, state, offered, None, 65536) is None
    assert task_context.step_status(folder) == before and (folder / "index.html").read_text() == source


def invalid_context_fixture(tmp_path):
    item = project(tmp_path / "invalid-context")
    item["brief"] = "Build a form that adds valid amounts and rejects negative amounts."
    folder = Path(item["folder"])
    (folder / "index.html").write_text('<form id="form"><input id="amount" type="number"><button id="add">Add</button></form><p id="error"></p>')
    (folder / "AGENTS.md").write_text("Preserve existing application behavior.")
    (folder / ".local-ai/USER-REQUESTS.md").write_text("Also keep keyboard submission working.")
    active = task_context.plan_steps(folder, [{"phase": "Add", "title": "Add valid input", "check_command": "exit 1"}])["active"]
    broken = {"path": "index.html", "cases": [{"visible": ["STALE_ID_SENTINEL"],
        "expect": {"rows.length": "MALFORMED_EXPECT_VALUE_SENTINEL"}}]}
    with project_memory.connection(folder) as conn:
        conn.execute("UPDATE task_steps SET check_tool='check_webapp',check_command=?,check_arguments=?,evidence=? WHERE id=?",
            ("MALFORMED_COMMAND_SENTINEL", json.dumps(broken), "MALFORMED_EVIDENCE_SENTINEL", active["id"]))
    return item, folder, task_context.step_status(folder)["active"]


def test_invalid_acceptance_context_omits_corrupt_arguments_but_keeps_requirements(tmp_path):
    item, folder, active = invalid_context_fixture(tmp_path)
    before = task_context.step_status(folder)
    source = (folder / "index.html").read_bytes()
    state = {"step_focus_turns": 1}
    offered = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    request = native_agent.focused_step_repair_request(item, state, offered, None, 65536)
    assert request
    messages = json.dumps(request["messages"])
    assert all(token not in messages for token in ("MALFORMED_EXPECT_VALUE_SENTINEL", "MALFORMED_COMMAND_SENTINEL", "MALFORMED_EVIDENCE_SENTINEL", "STALE_ID_SENTINEL"))
    data = json.loads(request["messages"][1]["content"])
    assert data["original_request"] == item["brief"]
    assert "keyboard submission" in data["user_followups"] and "existing application behavior" in data["project_rules"]
    assert data["active_step"]["id"] == active["id"] and data["active_step"]["title"] == active["title"]
    assert data["active_step"]["phase"] == active["phase"] and data["active_step"]["check_tool"] == "check_webapp"
    assert "purpose" in data["active_step"] and "check_arguments" not in data["active_step"] and "check_command" not in data["active_step"]
    assert "rows.length" in data["observations"]["invalid_acceptance"]
    assert {"id": "amount", "tag": "input", "type": "number"} in data["observations"]["static_controls"]
    assert task_context.step_status(folder) == before and (folder / "index.html").read_bytes() == source


def test_invalid_acceptance_context_new_format_renews_exhausted_budget_once(tmp_path):
    item, folder, active = invalid_context_fixture(tmp_path)
    before = task_context.step_status(folder)
    state = {"step_focus_turns": 1}
    offered = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    with patch.object(native_agent, "_INVALID_ACCEPTANCE_CONTEXT_FORMAT", ""):
        assert native_agent.focused_step_repair_request(item, state, offered, None, 65536)
        assert native_agent.focused_step_repair_request(item, state, offered, None, 65536)
        old_key = state["step_repair_focus"]["key"]
        assert native_agent.focused_step_repair_request(item, state, offered, None, 65536) is None
    assert native_agent.focused_step_repair_request(item, state, offered, None, 65536)
    assert state["step_repair_focus"]["key"] != old_key and state["step_repair_focus"]["count"] == 1
    assert native_agent.focused_step_repair_request(item, state, offered, None, 65536)
    assert native_agent.focused_step_repair_request(item, state, offered, None, 65536) is None
    assert task_context.step_status(folder) == before


def test_invalid_acceptance_context_does_not_change_valid_input_repair_payload_or_budget(tmp_path):
    item, folder, source, bad, active, notice, runtime_state = noneditable_repair_integration_fixture(tmp_path, "valid-input-context")
    before = task_context.step_status(folder)
    state = {"step_focus_turns": 1}
    offered = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    first = native_agent.focused_step_repair_request(item, state, offered, notice, 65536)
    data = json.loads(first["messages"][1]["content"])
    assert data["active_step"]["check_arguments"] == bad
    assert data["active_step"]["check_arguments"]["cases"][0]["set"]["units"] == "7.2"
    key = state["step_repair_focus"]["key"]
    with patch.object(native_agent, "_INVALID_ACCEPTANCE_CONTEXT_FORMAT", "future-invalid-only-version"):
        assert native_agent.focused_step_repair_request(item, state, offered, notice, 65536)
        assert state["step_repair_focus"]["key"] == key and state["step_repair_focus"]["count"] == 2
        assert native_agent.focused_step_repair_request(item, state, offered, notice, 65536) is None
    assert task_context.step_status(folder) == before and (folder / "index.html").read_text() == source


def test_retarget_host_schema_is_one_time_and_never_repeatable_acceptance(tmp_path):
    from jsonschema import ValidationError, validate
    mapping = {"from": "text:bad-count", "to": "rows:items"}
    args = {"path": "index.html", "saved_sequence": 1, "saved_case": 1, "suite_sha256": "a" * 64,
            "retarget_assertion": mapping, "cases": [{"row_counts": {"items": 1}}]}
    schema = next(tool["function"]["parameters"] for tool in native_agent.TOOLS if tool["function"]["name"] == "check_webapp")
    validate(args, schema)
    plain = {"path": "index.html", "cases": [{"expect": {"status": "Ready"}}]}
    for one_time in ({"retarget_assertion": mapping}, {**args, "path": "index.html"}):
        with pytest.raises(ValueError, match="one-time checker action"):
            task_context.plan_steps(tmp_path, [{"phase": "Check", "title": "Retarget", "check_tool": "check_webapp",
                "check_arguments": {**plain, **one_time}}])
    assert task_context.step_status(tmp_path)["total"] == 0
    steps_schema = next(tool["function"]["parameters"] for tool in native_agent.TOOLS if tool["function"]["name"] == "task_steps")
    with pytest.raises(ValidationError):
        validate({"action": "plan", "steps": [{"phase": "Check", "title": "Retarget", "check_tool": "check_webapp", "check_arguments": args}]}, steps_schema)
    for bad in ({"path": "index.html", "retarget_assertion": mapping, "cases": args["cases"]}, {**args, "revise_inputs": False}):
        assert "complete saved-case reference" in native_agent.execute_tool(tmp_path, "check_webapp", bad)


def test_retarget_host_only_changes_owned_assertion_and_preserves_all_other_case_data(tmp_path):
    old = {"path": "index.html", "viewport_width": 375, "cases": [
        {"set": {"amount": "2"}, "click": ["add"], "expect": {"wrong": "1", "status": "Ready"}, "hidden": ["error"]},
        {"reload": True, "row_counts": {"items": 1}}]}
    active = {"check_arguments": copy.deepcopy(old)}
    corrected = copy.deepcopy(old)
    del corrected["cases"][0]["expect"]["wrong"]
    corrected["cases"][0]["row_counts"] = {"items": 1}
    revision = {"saved_case": 1, "retarget_assertion": {"from": "text:wrong", "to": "rows:items"}}
    assert native_agent.browser_retarget_preserves_step(active, old, corrected, revision)
    altered = []
    for key, value in (("set", {"amount": "9"}), ("click", []), ("hidden", []), ("expect", {}), ("reload", True)):
        candidate = copy.deepcopy(corrected); candidate["cases"][0][key] = value; altered.append(candidate)
    candidate = copy.deepcopy(corrected); candidate["cases"][1]["row_counts"]["items"] = 0; altered.append(candidate)
    candidate = copy.deepcopy(corrected); candidate["viewport_width"] = 800; altered.append(candidate)
    assert all(not native_agent.browser_retarget_preserves_step(active, old, case, revision) for case in altered)
    subset = copy.deepcopy(active); del subset["check_arguments"]["cases"][0]["expect"]["wrong"]
    assert native_agent.browser_revision_matches_step(tmp_path, subset, old)
    assert not native_agent.browser_retarget_preserves_step(subset, old, corrected, revision)


def retarget_host_fixture(tmp_path, name, *, extra_only=False):
    import webapp_check
    item = project(tmp_path / name); folder = Path(item["folder"])
    source = ("<input id='amount'><button id='add' onclick=\"document.querySelector('tbody').insertRow().insertCell().textContent=document.querySelector('input').value\">Add</button>"
              "<table><tbody id='items'></tbody></table><p id='status'>Ready</p><p id='error' hidden></p>")
    (folder / "index.html").write_text(source, encoding="utf-8")
    retained = {"path": "index.html", "cases": [{"set": {"amount": "2"}, "click": ["add"],
        "expect": {"wrong-count-id": "1", "status": "Ready"}, "hidden": ["error"]}]}
    planned = copy.deepcopy(retained)
    if extra_only: del planned["cases"][0]["expect"]["wrong-count-id"]
    active = task_context.plan_steps(folder, [{"phase": "Add", "title": "Add an item", "check_tool": "check_webapp", "check_arguments": planned}])["active"]
    future = {"path": "index.html", "cases": [{"expect": {"status": "Future failure remains"}}]}
    webapp_check.save_verifier(folder, future, accumulate=True)
    webapp_check.save_verifier(folder, retained, accumulate=True)
    failed = json.loads(native_agent.execute_tool(folder, "task_steps", {"action": "complete", "step_id": active["id"]}))
    assert failed["passed"] is False
    if extra_only:
        # The active subset has no measured outcome change and may fail its
        # unit-strength gate before reporting the unrelated retained failure.
        # Explicitly address that actual retained assertion, not its SQL unit.
        saved = (folder / ".local-ai/webapp-check.json").read_bytes()
        checks = json.loads(saved)["checks"]
        selected = next(index for index, check in enumerate(checks, 1)
                        if "wrong-count-id" in check["cases"][0].get("expect", {}))
        ref = {"path": "index.html", "saved_sequence": selected, "saved_case": 1, "suite_sha256": hashlib.sha256(saved).hexdigest()}
    else:
        ref = failed["assertion_revision_reference"]
    arguments = {**ref, "retarget_assertion": {"from": "text:wrong-count-id", "to": "rows:items"}, "cases": [{"row_counts": {"items": 1}}]}
    state = tmp_path / (name + "-state"); state.mkdir()
    return item, folder, source, planned, task_context.step_status(folder)["active"], arguments, state


@pytest.mark.parametrize("bound", [False, True])
def test_retarget_host_actual_count_repair_preserves_inputs_and_future_failure_without_completing(tmp_path, bound):
    item, folder, source, planned, active, args, state = retarget_host_fixture(tmp_path, "count-retarget-" + str(bound))
    before = json.loads((folder / ".local-ai/webapp-check.json").read_text())
    actual = native_agent.bind_browser_assertion_revision(folder, args, active, task_context.request_identity(folder)) if bound else args
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        result = json.loads(native_agent.execute_tool(folder, "check_webapp", actual))
    assert result["repaired_case_passed"] and result["verifier_saved"]
    assert result["passed"] is False and result["verifier_check_passed"] is False
    after = json.loads((folder / ".local-ai/webapp-check.json").read_text())
    selected = args["saved_sequence"] - 1
    old_case = before["checks"][selected]["cases"][0]; new_case = after["checks"][selected]["cases"][0]
    assert new_case["set"] == old_case["set"] and new_case["click"] == old_case["click"]
    assert new_case["expect"] == {"status": "Ready"} and new_case["row_counts"] == {"items": 1}
    assert new_case["hidden"] == old_case["hidden"]
    assert [case for n,case in enumerate(before["checks"]) if n != selected] == [case for n,case in enumerate(after["checks"]) if n != selected]
    current = task_context.step_status(folder)
    assert current["done"] == 0 and current["active"]["id"] == active["id"]
    assert (folder / "index.html").read_text() == source
    if bound:
        assert result["active_step_check_aligned"] and current["active"]["check_arguments"]["cases"][0] == new_case
        assert "already used" in native_agent.execute_tool(folder, "check_webapp", actual)
    else:
        assert "active_step_check_aligned" not in result and current["active"]["check_arguments"] == planned


def test_retarget_host_extra_retained_assertion_does_not_authorize_sql_rewrite(tmp_path):
    item, folder, source, planned, active, args, state = retarget_host_fixture(tmp_path, "extra-retained-target", extra_only=True)
    actual = native_agent.bind_browser_assertion_revision(folder, args, active, task_context.request_identity(folder))
    with patch.object(long_tasks, "projects", return_value=[item]), patch.object(long_tasks, "STATE", state):
        result = json.loads(native_agent.execute_tool(folder, "check_webapp", actual))
    assert result["repaired_case_passed"] and result["verifier_saved"]
    assert result["active_step_check_aligned"] is False
    assert task_context.step_status(folder)["active"]["check_arguments"] == planned
    assert task_context.step_status(folder)["done"] == 0 and (folder / "index.html").read_text() == source


def test_retarget_host_state_assertions_keep_boolean_and_numeric_types_distinct(tmp_path):
    old = {"path": "index.html", "cases": [{"click": ["add"], "expect": {"wrong": "1"}, "state": {"app.ready": True}}]}
    active = {"check_arguments": copy.deepcopy(old)}
    corrected = copy.deepcopy(old); del corrected["cases"][0]["expect"]["wrong"]
    corrected["cases"][0]["row_counts"] = {"items": 1}
    revision = {"saved_case": 1, "retarget_assertion": {"from": "text:wrong", "to": "rows:items"}}
    assert native_agent.browser_retarget_preserves_step(active, old, corrected, revision)
    changed = copy.deepcopy(corrected); changed["cases"][0]["state"]["app.ready"] = 1
    assert not native_agent.browser_retarget_preserves_step(active, old, changed, revision)
    retained = copy.deepcopy(old); retained["cases"][0]["state"]["app.ready"] = 1
    assert not native_agent.browser_revision_matches_step(tmp_path, active, retained)
    retained_correction = copy.deepcopy(corrected); retained_correction["cases"][0]["state"]["app.ready"] = 1
    with patch.object(task_context, "revise_step", side_effect=AssertionError("Mismatched SQL must not be rewritten")):
        aligned, note = native_agent.align_browser_step_revision(tmp_path, {"active": active}, retained,
            retained_correction, {"own_batch_passed": True, "verifier_saved": True, "revision": revision})
    assert aligned is False and "not the active acceptance sequence" in note
    from_state = {"saved_case": 1, "retarget_assertion": {"from": "state:app.ready", "to": "rows:items"}}
    new = copy.deepcopy(old); del new["cases"][0]["state"]["app.ready"]; new["cases"][0]["row_counts"] = {"items": 1}
    numeric_owner = copy.deepcopy(active); numeric_owner["check_arguments"]["cases"][0]["state"]["app.ready"] = 1
    assert not native_agent.browser_retarget_preserves_step(numeric_owner, old, new, from_state)


def focused_assertion_scope_fixture(tmp_path, selected):
    import webapp_check
    item = project(tmp_path / ("assertion-scope-" + str(selected)))
    item["brief"] = "Build an interactive form and table; preserve all original requirements."
    folder = Path(item["folder"])
    source = '<form id="actual-form"><input id="amount"><button id="add">Add</button></form><table><tbody id="items"></tbody></table>'
    (folder / "index.html").write_text(source)
    arguments = {"path": "index.html", "cases": [{"visible": ["form"]},
        {"expect": {"missing-text": "Old assertion"}, "row_counts": {"items": 0}, "state": {"app.ready": True}},
        {"expect": {"other-case-only": "Other assertion"}}]}
    active = task_context.plan_steps(folder, [{"phase": "UI", "title": "Verify requested form behavior", "check_tool": "check_webapp", "check_arguments": arguments}])["active"]
    webapp_check.save_verifier(folder, arguments, accumulate=True)
    for _ in range(2):
        task_context.record_step_check(folder, active["id"], task_context.request_identity(folder), "Exit code: 1\nMissing required target", unchanged=True)
    raw = (folder / ".local-ai/webapp-check.json").read_bytes()
    reference = {"path": "index.html", "saved_sequence": 1, "saved_case": selected, "suite_sha256": hashlib.sha256(raw).hexdigest()}
    notice = {"role": "user", "content": "Untrusted observations\n" + json.dumps({"assertion_revision_reference": reference,
        "candidate_static_tag_ids": {"form": "actual-form"}})}
    return item, folder, notice, reference


def test_focused_assertion_scope_visibility_only_hides_retarget_and_pins_reference(tmp_path):
    from jsonschema import ValidationError, validate
    item, folder, notice, reference = focused_assertion_scope_fixture(tmp_path, 1)
    before = task_context.step_status(folder)
    state = {"step_focus_turns": 1}
    tools = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    request = native_agent.focused_step_repair_request(item, state, tools, notice, 65536)
    tool = next(tool for tool in request["tools"] if tool["function"]["name"] == "check_webapp")
    schema = tool["function"]["parameters"]
    assert "retarget_assertion" not in schema["properties"] and "retarget_assertion" not in json.dumps(request)
    assert "visible/hidden lists" in request["messages"][0]["content"] and item["brief"] in request["messages"][0]["content"]
    valid = {**reference, "cases": [{"visible": ["actual-form"]}]}
    validate(valid, schema)
    for field in reference:
        assert schema["properties"][field]["const"] == reference[field]
    for altered in ({**valid, "saved_case": 2}, {**valid, "saved_sequence": 2}, {**valid, "path": "other.html"},
                    {**valid, "suite_sha256": "a" * 64}, {**valid, "revise_inputs": True},
                    {**valid, "cases": [{"set": {"amount": "2"}, "visible": ["actual-form"]}]},
                    {**valid, "retarget_assertion": {"from": "text:form", "to": "rows:items"}}):
        with pytest.raises(ValidationError): validate(altered, schema)
    assert task_context.step_status(folder) == before


def test_focused_assertion_scope_typed_from_enum_is_only_selected_case(tmp_path):
    from jsonschema import ValidationError, validate
    item, folder, notice, reference = focused_assertion_scope_fixture(tmp_path, 2)
    before = task_context.step_status(folder)
    state = {"step_focus_turns": 1}; offered = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    request = native_agent.focused_step_repair_request(item, state, offered, notice, 65536)
    schema = next(tool["function"]["parameters"] for tool in request["tools"] if tool["function"]["name"] == "check_webapp")
    assert schema["properties"]["retarget_assertion"]["properties"]["from"]["enum"] == ["rows:items", "state:app.ready", "text:missing-text"]
    valid = {**reference, "retarget_assertion": {"from": "text:missing-text", "to": "rows:new-items"}, "cases": [{"row_counts": {"new-items": 1}}]}
    validate(valid, schema)
    for value in ("text:form", "text:other-case-only", "text:invented"):
        with pytest.raises(ValidationError): validate({**valid, "retarget_assertion": {**valid["retarget_assertion"], "from": value}}, schema)
    with pytest.raises(ValidationError): validate({**valid, "saved_case": 3}, schema)
    with pytest.raises(ValidationError): validate({**valid, "cases": [{"click": ["add"], "row_counts": {"new-items": 1}}]}, schema)
    assert task_context.step_status(folder) == before


def test_focused_assertion_scope_format_renews_only_two_generic_attempts(tmp_path):
    item, folder, notice, reference = focused_assertion_scope_fixture(tmp_path, 1)
    before = task_context.step_status(folder); state = {"step_focus_turns": 1}
    offered = native_agent.acceptance_repair_tools(item, state, native_agent.TOOLS)
    with patch.object(native_agent, "_GENERIC_ASSERTION_SCOPE_FORMAT", ""):
        for _ in range(2): assert native_agent.focused_step_repair_request(item, state, offered, notice, 65536)
        old = state["step_repair_focus"]["key"]
        assert native_agent.focused_step_repair_request(item, state, offered, notice, 65536) is None
    assert native_agent.focused_step_repair_request(item, state, offered, notice, 65536)
    assert state["step_repair_focus"]["key"] != old and state["step_repair_focus"]["count"] == 1
    assert native_agent.focused_step_repair_request(item, state, offered, notice, 65536)
    assert native_agent.focused_step_repair_request(item, state, offered, notice, 65536) is None
    assert task_context.step_status(folder) == before


def capability_recovery_invoice_fixture(folder):
    """Developer-seeded broken local module for deterministic and real-router smoke.

    No helper implementation, model answers, verifier mocks or later expected
    outputs are injected. Original user tax/format requirements own both units.
    """
    item=project(folder)
    item['brief']='Finish the existing invoice module: apply tax to each subtotal and round half up to cents; preserve invoice_total and format_invoice in billing.py and pass both original saved acceptance phases.'
    item['ai_route']='local_only'
    import hashlib
    item['id']=hashlib.sha256(str(folder.resolve()).encode()).hexdigest()[:12]
    folder=Path(item['folder'])
    (folder/'billing.py').write_text('from invoice_math import apply_tax\ndef invoice_total(subtotal,tax_rate):\n    return apply_tax(subtotal,tax_rate)\ndef format_invoice(subtotal,tax_rate):\n    return "Total: %.2f" % invoice_total(subtotal,tax_rate)\n')
    (folder/'.local-ai/USER-REQUESTS.md').write_text(item['brief'])
    original=[{'phase':'Calculate','title':'Invoice totals','check_tool':'check_python_function','check_arguments':
        {'path':'billing.py','function':'invoice_total','cases':[{'args':[100,.06],'expected':106},
            {'args':[0,.06],'expected':0},{'args':[19.99,.085],'expected':21.69}]}},
        {'phase':'Format','title':'Invoice text','check_tool':'check_python_function','check_arguments':
        {'path':'billing.py','function':'format_invoice','cases':[{'args':[100,.06],'expected':'Total: 106.00'}]}}]
    task_context.plan_steps(folder,original)
    return item


def test_durable_capability_recovery_builds_real_local_module_then_resumes_original_checks(tmp_path):
    import sys
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice')
    folder=Path(item['folder'])
    def response(calls):
        return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':str(i),
            'function':{'name':name,'arguments':json.dumps(args)}} for i,(name,args) in enumerate(calls)]}}]}
    runtime=tmp_path/'runtime'
    with patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]),patch.object(long_tasks,'STATE',runtime),patch.object(native_agent,'capability_local_search',wraps=getattr(native_agent,'capability_local_search',lambda query:[]),create=True) as search:
        native_agent.run_slice(item,complete=lambda payload:response([('task_steps',{'action':'complete'})]))
        assert task_context.step_status(folder)['done']==0
        assert search.call_count==1  # Baseline truly ran/fails the typed check but never searches.
        active=task_context.step_status(folder)['active']
        episode=task_context.recovery_episode(folder,native_agent.capability_goal_hash(item),active)
        assert episode and isinstance(json.loads(episode['catalog']),list) and episode['turns']==0 and episode['status']!='recovered'
        assert search.call_count==1 and task_context.step_status(folder)['done']==0
        # New run_slice reloads persisted native state + SQL episode. It does not
        # ask the user or repeat local discovery; the model supplies a real module.
        code='from decimal import Decimal, ROUND_HALF_UP\ndef apply_tax(subtotal,tax_rate):\n    return float((Decimal(str(subtotal))*(Decimal("1")+Decimal(str(tax_rate)))).quantize(Decimal(".01"),rounding=ROUND_HALF_UP))\n'
        command='"'+sys.executable+'" -B -c "from invoice_math import apply_tax; assert apply_tax(100,0.06)==106; assert apply_tax(19.99,0.085)==21.69"'
        def build(payload):
            assert payload.get('_native_workload')=='capability_recovery'
            assert 'discover_local_tools' in {tool['function']['name'] for tool in payload['tools']}
            return response([('write_file',{'path':'invoice_math.py','content':code}),('run_command',{'command':command})])
        native_agent.run_slice(item,complete=build)
        episode=task_context.recovery_episode(folder,native_agent.capability_goal_hash(item),active)
        assert (folder/'invoice_math.py').is_file() and episode['status']!='recovered'
        assert any(method['method']=='check_script' and method['outcome']=='Completed' for method in episode['methods'])
        assert task_context.step_status(folder)['done']==0 and search.call_count==1
        # The host now reruns the ORIGINAL exact unit, then the ORIGINAL next unit.
        # A final narrative isn't what advances either acceptance check.
        native_agent.run_slice(item,complete=lambda payload:{'choices':[{'message':{'role':'assistant','content':'The saved checks passed.'}}]})
        assert task_context.step_status(folder)['done']==2
        with project_memory.connection(folder) as conn:
            final=dict(conn.execute('SELECT * FROM capability_recoveries WHERE id=?',(episode['id'],)).fetchone())
        assert final['status']=='recovered' and json.loads(final['proof'])['check_hash']==episode['original_check']
        assert search.call_count==1


def test_capability_claim_needs_real_unfinished_work_and_is_not_a_quoted_refusal(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice')
    assert native_agent.capability_claim_with_unmet_goal(item,'I cannot access files or tools in this environment.')
    assert not native_agent.capability_claim_with_unmet_goal(item,'"I cannot access files," said the character in the story.')
    assert not native_agent.capability_claim_with_unmet_goal(item,'The narrator says I cannot access files here.')
    assert native_agent.capability_claim_with_unmet_goal(item,
        'I can provide a blueprint here, but I cannot render files or create tasks in this chat.')
    assert not native_agent.capability_claim_with_unmet_goal(item,'I cannot execute this because policy prohibits it.')
    ordinary=project(tmp_path/'story');ordinary['brief']='Write a short story in your reply.'
    assert not native_agent.capability_claim_with_unmet_goal(ordinary,'I cannot access files here.')


def test_no_tool_inability_claim_enters_durable_action_lane_without_new_user_prompt(tmp_path):
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    with patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]),patch.object(long_tasks,'STATE',tmp_path/'runtime'),patch.object(native_agent,'capability_local_search',return_value=[]) as search:
        native_agent.run_slice(item,complete=lambda payload:{'choices':[{'message':{'role':'assistant','content':'I cannot access files or execute tools in this environment.'}}]})
        active=task_context.step_status(folder)['active'];episode=task_context.recovery_episode(folder,native_agent.capability_goal_hash(item),active)
        assert episode['trigger']=='capability_claim' and episode['status']!='recovered' and search.call_count==1
        def use_tool(payload):
            assert payload['_native_workload']=='capability_recovery' and payload['tool_choice']=='required'
            return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'read','function':{'name':'read_file','arguments':'{"path":"billing.py"}'}}]}}]}
        native_agent.run_slice(item,complete=use_tool)
        assert task_context.step_status(folder)['done']==0 and search.call_count==1


def test_recovery_check_revisions_do_not_reset_budget_or_prove_original_acceptance(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='missing_capability',component='invoice_math')
    task_context.recovery_catalog(folder,episode['id'],[])
    for _ in range(6):assert task_context.reserve_recovery_turn(folder,episode['id'],goal,episode['current_check'])
    changed=json.loads(json.dumps(active));changed['check_arguments']['cases']=changed['check_arguments']['cases'][:1]
    revision=task_context.recovery_episode(folder,goal,changed)
    assert revision['id']==episode['id'] and revision['turns']==6 and revision['original_check']!=revision['current_check']
    assert not task_context.reserve_recovery_turn(folder,revision['id'],goal,revision['current_check'])
    task_context.recovery_attempt(folder,episode['id'],'acceptance','task_steps',{},'{"passed":true}','Completed',proof={
        'goal_hash':goal,'request_hash':task_context.request_identity(folder),'check_hash':revision['current_check'],
        'passed':True,'source_before':'a'*64,'source_after':'a'*64})
    assert task_context.recovery_episode(folder,goal,changed)['status']!='recovered'


def test_recovery_uses_full_brief_rules_goal_and_supersedes_old_unit_without_success(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];old_goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,old_goal,active,trigger='read_stall')
    changed={**item,'brief':item['brief']+' Also add a discount.'}
    assert native_agent.capability_goal_hash(changed)!=old_goal
    assert task_context.recovery_episode(folder,native_agent.capability_goal_hash(changed),active) is None
    with project_memory.connection(folder) as conn:
        assert conn.execute('SELECT status FROM capability_recoveries WHERE id=?',(episode['id'],)).fetchone()[0]=='superseded'
    (folder/'AGENTS.md').write_text('Preserve rounding and private data.')
    assert native_agent.capability_goal_hash(item)!=old_goal


def test_recovery_rejects_stale_goal_response_and_never_counts_helper_success_as_recovered(tmp_path):
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    task_context.recovery_attempt(folder,episode['id'],'existing_tool','call_local_tool',{},'{"registered":true,"passed":true}','Completed')
    assert task_context.recovery_episode(folder,goal,active)['status']!='recovered'
    def stale(payload):
        assert payload.get('_native_capability_recovery')
        (folder/'AGENTS.md').write_text('A new authoritative instruction arrived.')
        return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'bad','function':{'name':'write_file','arguments':'{"path":"stale.txt","content":"must not write"}'}}]}}]}
    with patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]):
        native_agent.run_slice(item,complete=stale)
    assert not (folder/'stale.txt').exists()


def test_recovery_source_changes_and_done_rows_without_observed_proof_are_not_recovery(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    before=native_agent.capability_recovery_before(item,'task_steps',{'action':'complete'},active)
    (folder/'changed.py').write_text('SOURCE_CHANGED=True')
    verdict=json.dumps({'passed':True,'checked_step_id':active['id'],'check_sha256':task_context.step_check_identity(active)})
    native_agent.capability_recovery_observe(item,{},'task_steps',{'action':'complete'},verdict,'Completed',before)
    assert task_context.recovery_episode(folder,goal,active)['status']!='recovered'
    with project_memory.connection(folder) as conn:
        conn.execute("UPDATE task_steps SET status='done' WHERE id=?",(active['id'],))
    assert task_context.recovery_episode(folder,goal,active)['status']!='recovered'


def test_recovery_private_binding_is_not_forwarded_to_router(tmp_path):
    from types import SimpleNamespace
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    seen=[]
    def complete(payload,task_id,**options):
        assert not any(key.startswith('_native') for key in payload)
        assert task_id==item['id'];seen.append(payload)
        return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'read','function':{'name':'read_file','arguments':'{"path":"billing.py"}'}}]}}]}
    router=SimpleNamespace(complete=complete,last_local_model=None,last_local_weights='')
    with patch('cloud_ai.Router',return_value=router),patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]):
        native_agent.run_slice(item)
    assert len(seen)==1


def test_recovery_local_catalog_search_never_starts_backend_or_network(tmp_path):
    with (patch.object(native_agent,'ROOT',tmp_path),patch('subprocess.run',side_effect=AssertionError('No backend process')),
          patch('urllib.request.urlopen',side_effect=AssertionError('No network'))):
        rows=native_agent.capability_local_search('build 3d scene')
    assert any(item['name']=='local.build_3d_scene' for item in rows)


def test_recovery_does_not_treat_quoted_case_failure_as_missing_capability(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    before=native_agent.capability_recovery_before(item,'task_steps',{'action':'complete'})
    result=json.dumps({'passed':False,'check_summary':{'errors':[], 'cases':[
        {'failures':["Expected story says No module named secret_module, actual story differs"]}]}})
    with patch.object(native_agent,'capability_local_search',side_effect=AssertionError('No missing-capability search')):
        native_agent.capability_recovery_observe(item,{},'task_steps',{'action':'complete'},result,'Failed',before)
    assert task_context.recovery_episode(folder,native_agent.capability_goal_hash(item),before['active']) is None


def test_recovery_budget_notice_preserves_unfinished_goal_and_does_not_request_human(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    for _ in range(6):assert task_context.reserve_recovery_turn(folder,episode['id'],goal,episode['current_check'])
    messages=[];state={}
    with patch('long_tasks.projects',return_value=[item]):
        assert native_agent.capability_recovery_request(item,state,messages,native_agent.TOOLS) is None
    assert 'original goal is still unfinished' in messages[-1]['content'] and 'another eligible approach' in messages[-1]['content']
    assert task_context.step_status(folder)['done']==0 and 'status' not in state


def test_recovery_pause_during_inference_blocks_returned_source_edit(tmp_path):
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    def paused(payload):
        assert payload.get('_native_capability_recovery')
        state=native_agent.read_state(item);state['status']='paused';native_agent.save_state(item,state)
        return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'edit','function':{'name':'write_file','arguments':'{"path":"paused.txt","content":"do not write"}'}}]}}]}
    with patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]):
        result=native_agent.run_slice(item,complete=paused)
    assert result['status']=='paused' and not (folder/'paused.txt').exists()


def test_recovery_stage_keeps_original_check_schema_small_and_immutable(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    with patch('long_tasks.projects',return_value=[item]):
        payload=native_agent.capability_recovery_request(item,{},[],native_agent.TOOLS)
    tool=next(t for t in payload['tools'] if t['function']['name']=='task_steps')
    assert tool['function']['parameters']['properties']['action']['enum']==['complete','status']
    assert tool['function']['parameters']['properties']['step_id']['const']==active['id']
    assert len(json.dumps(tool))<1000
    assert 'revise_check' not in json.dumps(tool)
    assert next(t for t in native_agent.TOOLS if t['function']['name']=='task_steps')['function']['parameters']!=tool['function']['parameters']


def test_recovery_rechecks_live_registered_owner_after_model_response(tmp_path):
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    owners=[item]
    def deleted(payload):
        assert payload.get('_native_capability_recovery');owners.clear()
        return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':'edit','function':{'name':'write_file','arguments':'{"path":"after-delete.txt","content":"must not write"}'}}]}}]}
    with patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',side_effect=lambda:list(owners)):
        native_agent.run_slice(item,complete=deleted)
    assert not (folder/'after-delete.txt').exists()


def test_recovery_claim_requires_environmental_unavailability_not_policy_refusal(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice')
    assert not native_agent.capability_claim_with_unmet_goal(item,'I cannot create ransomware or help you harm another person.')
    assert not native_agent.capability_claim_with_unmet_goal(item,'I cannot run malware in this environment.')
    assert native_agent.capability_claim_with_unmet_goal(item,'I cannot access local files or tools in this environment.')


def test_recovery_merges_static_tools_when_existing_catalog_is_stale_empty(tmp_path):
    import capability_index
    path=capability_index.database_path(tmp_path);path.parent.mkdir();path.touch()
    with patch.object(native_agent,'ROOT',tmp_path),patch.object(capability_index,'search',return_value=[]):
        rows=native_agent.capability_local_search('build 3d scene')
    assert any(row['name']=='local.build_3d_scene' for row in rows)


def test_no_queue_recovery_replays_real_original_checker_and_reopens_same_budget_after_failure(tmp_path):
    import long_tasks
    item=project(tmp_path/'direct');item['brief']='Repair double_value in logic.py using its existing acceptance.'
    folder=Path(item['folder']);(folder/'.local-ai/USER-REQUESTS.md').write_text(item['brief'])
    (folder/'logic.py').write_text('from local_math import double\ndef double_value(x):\n    return double(x)\n')
    args={'path':'logic.py','function':'double_value','cases':[{'args':[2],'expected':4},{'args':[0],'expected':0}]}
    with patch.object(long_tasks,'projects',return_value=[item]),patch.object(long_tasks,'STATE',tmp_path/'runtime'),patch.object(native_agent,'capability_local_search',return_value=[]) as search:
        before=native_agent.capability_recovery_before(item,'check_python_function',args)
        failed=native_agent.execute_tool(folder,'check_python_function',args)
        assert task_context.action_outcome('check_python_function',failed)=='Failed'
        native_agent.capability_recovery_observe(item,{},'check_python_function',args,failed,'Failed',before)
        episode=task_context.recovery_episode(folder,native_agent.capability_goal_hash(item),None)
        payload=native_agent.capability_recovery_request(item,{},[],native_agent.TOOLS)
        assert 'check_python_function' in {t['function']['name'] for t in payload['tools']}
        (folder/'local_math.py').write_text('def double(x):\n    return x*2\n')
        before=native_agent.capability_recovery_before(item,'check_python_function',args)
        passed=native_agent.execute_tool(folder,'check_python_function',args)
        assert json.loads(passed)['passed'] is True
        native_agent.capability_recovery_observe(item,{},'check_python_function',args,passed,'Completed',before)
        recovered=task_context.recovery_episode(folder,native_agent.capability_goal_hash(item),None)
        assert recovered['status']=='recovered'
        (folder/'local_math.py').write_text('import another_missing_local\ndef double(x):\n    return x*2\n')
        before=native_agent.capability_recovery_before(item,'check_python_function',args)
        failed_again=native_agent.execute_tool(folder,'check_python_function',args)
        assert task_context.action_outcome('check_python_function',failed_again)=='Failed'
        native_agent.capability_recovery_observe(item,{},'check_python_function',args,failed_again,'Failed',before)
        reopened=task_context.recovery_episode(folder,native_agent.capability_goal_hash(item),None)
        assert reopened['status']!='recovered' and reopened['id']==episode['id'] and reopened['turns']==recovered['turns']
        assert search.call_count==1


def test_recovery_response_cannot_execute_unbounded_tool_batch(tmp_path):
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    active=task_context.step_status(folder)['active'];goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='read_stall');task_context.recovery_catalog(folder,episode['id'],[])
    def many(payload):
        return {'choices':[{'message':{'role':'assistant','tool_calls':[{'id':str(i),'function':{'name':'write_file',
            'arguments':json.dumps({'path':str(i)+'.txt','content':'bounded'})}} for i in range(25)]}}]}
    with patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]):
        state=native_agent.run_slice(item,complete=many)
    assert len(list(folder.glob('*.txt')))<=4
    replies=[message for message in state['messages'] if message.get('role')=='tool']
    assert len(replies)==25 and any('recovery action budget' in message['content'] for message in replies)


def test_recovery_claim_preserves_permission_and_account_authentication_barriers(tmp_path):
    item=capability_recovery_invoice_fixture(tmp_path/'invoice')
    barriers=[
        'I cannot access local files because permissions were denied.',
        'I cannot access local files until you authenticate.',
        'I cannot access local files until authentication finishes.',
        'I cannot use tools here because your account requires authorization.',
        'I cannot access local files until you log in.',
        'I cannot use the browser in this environment until you sign-in.',
        'I cannot access local files without valid credentials.',
    ]
    for message in barriers:
        assert not native_agent.capability_claim_with_unmet_goal(item,message),message
    for message in [
        'I cannot access local files or tools in this environment.',
        'I cannot render audio in this chat because no tool runtime is available.',
        'I cannot use local tools because the file access adapter is missing.',
    ]:
        assert native_agent.capability_claim_with_unmet_goal(item,message),message


def test_recovery_preserves_real_focused_browser_specialist_without_spending_turn(tmp_path):
    import long_tasks
    item,folder,source,bad,active,revision,runtime=submission_decision_fixture(tmp_path,'recovery-specialist')
    suite=folder/'.local-ai/webapp-check.json';saved=suite.read_bytes()
    state=native_agent.read_state(item);state.update(step_focus_turns=1,status='continue')
    native_agent.save_state(item,state)
    notice=native_agent.active_step_repair_notice(item,copy.deepcopy(state))
    specialist=native_agent.focused_step_repair_request(item,copy.deepcopy(state),native_agent.TOOLS,notice,65536)
    assert [tool['function']['name'] for tool in specialist['tools']]==['check_webapp']
    goal=native_agent.capability_goal_hash(item)
    episode=task_context.recovery_episode(folder,goal,active,trigger='repeated_failure')
    task_context.recovery_catalog(folder,episode['id'],[])
    seen=[]
    def complete(payload):
        seen.append(payload)
        return {'choices':[{'message':{'role':'assistant','content':'I will preserve the original acceptance.'}}]}
    with patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]),patch.object(long_tasks,'STATE',runtime):
        native_agent.run_slice(item,complete=complete)
    assert len(seen)==1
    assert [tool['function']['name'] for tool in seen[0]['tools']]==['check_webapp']
    assert not seen[0].get('_native_capability_recovery')
    assert task_context.recovery_episode(folder,goal,active)['turns']==0
    assert suite.read_bytes()==saved and (folder/'index.html').read_bytes()==source
    assert task_context.step_status(folder)['done']==0

def missing_note_feedback_turn(tmp_path, calls, prior_failures=0):
    from types import SimpleNamespace
    import long_tasks
    item=capability_recovery_invoice_fixture(tmp_path/'invoice');folder=Path(item['folder'])
    for name in ('PLAN.md','PROGRESS.md'):(folder/'.local-ai'/name).unlink(missing_ok=True)
    if prior_failures:
        state=native_agent.read_state(item)
        for _ in range(prior_failures):native_agent.remember_local_tool_feedback(state,'cale-qwen36','b'*64,
            task_context.request_identity(folder),failed=True,recovered=False)
        native_agent.save_state(item,state)
    def complete(payload,task_id,**options):
        return {'choices':[{'message':{'role':'assistant','tool_calls':[
            {'id':str(i),'function':{'name':tool,'arguments':json.dumps(args)}} for i,(tool,args) in enumerate(calls)]}}]}
    router=SimpleNamespace(complete=complete,last_local_model='cale-qwen36',last_local_weights='b'*64)
    with patch('cloud_ai.Router',return_value=router),patch.object(native_agent,'MAX_TURNS_PER_SLICE',1),patch.object(long_tasks,'projects',return_value=[item]),patch.object(long_tasks,'STATE',tmp_path/'runtime'):
        state=native_agent.run_slice(item)
    return item,folder,state


def test_expected_missing_worker_notes_keep_failures_without_discrediting_model(tmp_path):
    item,folder,state=missing_note_feedback_turn(tmp_path,[('read_file',{'path':'.local-ai/PLAN.md'}),
        ('read_file',{'path':'.local-ai/PROGRESS.md'})])
    replies=[m for m in state['messages'] if m.get('role')=='tool']
    assert len(replies)==2 and all('FileNotFoundError' in m['content'] for m in replies)
    assert state['same_tool_failures']>=1 and task_context.step_status(folder)['done']==0
    assert native_agent.repair_avoid_models(state,task_context.request_identity(folder))==[]
    assert not (folder/'.local-ai/PLAN.md').exists() and not (folder/'.local-ai/PROGRESS.md').exists()
    assert state['status']!='done'


def test_missing_worker_notes_do_not_reset_existing_model_quality_failure(tmp_path):
    item,folder,state=missing_note_feedback_turn(tmp_path,[('read_file',{'path':'.local-ai/PLAN.md'}),
        ('read_file',{'path':'.local-ai/PROGRESS.md'})],prior_failures=1)
    assert state['local_repair_feedback']['models'][0]['failures']==1
    assert state['local_repair_feedback']['models'][0]['weights']=='b'*64


@pytest.mark.parametrize('kind',['schema','source_patch','requested_output','escaping_path'])
def test_ordinary_model_tool_failures_still_defer_weights(tmp_path,kind):
    calls=([('read_file',{'path':42})]*2 if kind=='schema' else
        [('edit_file',{'path':'billing.py','old_text':'UNMATCHED_SOURCE','new_text':'replacement'})]*2 if kind=='source_patch' else
        [('read_file',{'path':'requested-report.txt'})]*2 if kind=='requested_output' else
        [('read_file',{'path':'../escape.txt'})]*2)
    item,folder,state=missing_note_feedback_turn(tmp_path,calls)
    assert native_agent.repair_avoid_models(state,task_context.request_identity(folder))==[{'model':'cale-qwen36','weights':'b'*64}]
    assert state['local_repair_feedback']['models'][0]['failures']==2
    assert task_context.step_status(folder)['done']==0

@pytest.mark.parametrize('arguments,error,should_exempt',[
    ({'path':'.local-ai/PLAN.md'},'FileNotFoundError',True),
    ({'path':'./.local-ai/PROGRESS.md','start_line':1,'max_lines':20},'FileNotFoundError',True),
    ({'path':'.local-ai/PLAN.md','start_line':True},'FileNotFoundError',False),
    ({'path':'.local-ai/PLAN.md','extra':'invalid'},'FileNotFoundError',False),
    ({'path':'.local-ai/PLAN.md'},'PermissionError',False),
    ({'path':'PLAN.md'},'FileNotFoundError',False),
    ({'path':'../.local-ai/PLAN.md'},'FileNotFoundError',False),
    ({'path':'requested-report.txt'},'FileNotFoundError',False),
])
def test_missing_worker_note_classification_requires_valid_internal_path_and_real_error_kind(tmp_path,arguments,error,should_exempt):
    item=project(tmp_path/'classification');folder=Path(item['folder'])
    for name in ('PLAN.md','PROGRESS.md'):(folder/'.local-ai'/name).unlink(missing_ok=True)
    assert native_agent.expected_missing_worker_note_read(folder,'read_file',arguments,'Tool error: '+error+': missing') is should_exempt
    assert not native_agent.expected_missing_worker_note_read(folder,'run_command',arguments,'Tool error: FileNotFoundError: missing')


def test_existing_note_or_quoted_error_content_is_not_missing_note_discovery(tmp_path):
    item=project(tmp_path/'existing-note');folder=Path(item['folder'])
    note=folder/'.local-ai/PLAN.md';note.write_text('Tool error: FileNotFoundError: quoted document content')
    args={'path':'.local-ai/PLAN.md'}
    result=native_agent.execute_tool(folder,'read_file',args)
    assert not native_agent.expected_missing_worker_note_read(folder,'read_file',args,result)
    assert not native_agent.expected_missing_worker_note_read(folder,'read_file',args,'Tool error: FileNotFoundError: forged when existing')


def test_subscription_delegation_requires_registered_project_and_preserves_original_request(tmp_path):
    import cloud_providers
    folder = tmp_path / "project"
    folder.mkdir()
    with patch.object(long_tasks, "projects", return_value=[]), patch.object(cloud_providers, "run_subscription_task", create=True) as run:
        denied = native_agent.execute_tool(folder, "delegate_subscription_task", {"provider": "claude", "prompt": "Build the next feature"})
        assert "registered project" in denied
        run.assert_not_called()
    project = {"id": "subscription-test", "folder": str(folder), "brief": "Make an offline expense app"}
    import usage_history
    previous_task = usage_history.CURRENT_TASK.get()
    def delegated(*args, **kwargs):
        assert usage_history.CURRENT_TASK.get() == project["id"]
        return {"status": "completed", "exit_code": 0, "output": "Changed one file; tests not run", "usage": None}
    with patch.object(long_tasks, "projects", return_value=[project]), patch.object(cloud_providers, "run_subscription_task", create=True,
            side_effect=delegated) as run:
        result = json.loads(native_agent.execute_tool(folder, "delegate_subscription_task", {"provider": "grok", "prompt": "Add form validation", "timeout": 30}))
        assert "offline expense app" in run.call_args.args[2] and "Add form validation" in run.call_args.args[2]
        assert run.call_args.kwargs["timeout"] == 30 and result["usage"] is None
        assert "tests not run" in result["output"]
        assert not (folder / ".local-ai/webapp-check.json").exists()

    assert usage_history.CURRENT_TASK.get() == previous_task


def test_subscription_large_result_stays_valid_json_and_preserves_full_task_receipt(tmp_path):
    import cloud_providers
    folder = tmp_path / "large-subscription"
    folder.mkdir()
    project = {"id": "large-result", "folder": str(folder), "brief": "Keep the full response for later checks"}
    actual = {"provider": "claude", "status": "finished", "exit_code": 0,
              "output": {"result": "Quoted \"text\"\n" * 5000}, "usage": None, "verification": "agent_report_only"}
    with patch.object(long_tasks, "projects", return_value=[project]), patch.object(cloud_providers, "run_subscription_task", create=True, return_value=actual):
        text = native_agent.execute_tool(folder, "delegate_subscription_task", {"provider": "claude", "prompt": "Review one function"})
    result = json.loads(text)
    assert len(text) <= native_agent.MAX_TOOL_OUTPUT and result["output_truncated"]
    assert result["verification"] == "agent_report_only" and result["usage"] is None
    assert json.loads((folder / result["full_result_path"]).read_text(encoding="utf-8")) == actual


def test_subscription_handoff_honors_existing_no_delegation_and_passes_current_rules(tmp_path):
    import cloud_providers
    folder = tmp_path / "scoped-subscription"
    folder.mkdir()
    project = {"id": "scoped-result", "folder": str(folder), "brief": "Build a game"}
    rules = folder / "AGENTS.md"
    rules.write_text("Do not configure new paid AI, invoke Codex, or delegate.", encoding="utf-8")
    with patch.object(long_tasks, "projects", return_value=[project]), patch.object(cloud_providers, "run_subscription_task") as run:
        result = native_agent.execute_tool(folder, "delegate_subscription_task", {"provider": "grok", "prompt": "Add sprites"})
        assert "forbids delegation" in result
        run.assert_not_called()
    rules.write_text("Keep all work offline-compatible and preserve tests.", encoding="utf-8")
    notes = folder / ".local-ai"
    notes.mkdir()
    (notes / "USER-REQUESTS.md").write_text("The player must use arrow keys.", encoding="utf-8")
    with patch.object(long_tasks, "projects", return_value=[project]), patch.object(cloud_providers, "run_subscription_task", return_value={"status": "finished"}) as run:
        native_agent.execute_tool(folder, "delegate_subscription_task", {"provider": "grok", "prompt": "Add sprites"})
        actual = run.call_args.args[2]
        assert "offline-compatible" in actual and "arrow keys" in actual and "Build a game" in actual


@pytest.mark.parametrize("restriction", ["original", "followup"])
def test_subscription_handoff_rejects_explicit_original_or_later_no_delegation(tmp_path, restriction):
    import cloud_providers
    folder = tmp_path / restriction
    folder.mkdir()
    project = {"id": "no-delegation", "folder": str(folder), "brief": "Build an app. Do not delegate." if restriction == "original" else "Build an app."}
    if restriction == "followup":
        (folder / ".local-ai").mkdir()
        (folder / ".local-ai/USER-REQUESTS.md").write_text("Never delegate this task.", encoding="utf-8")
    with patch.object(long_tasks, "projects", return_value=[project]), patch.object(cloud_providers, "run_subscription_task") as run:
        result = native_agent.execute_tool(folder, "delegate_subscription_task", {"provider": "grok", "prompt": "Build the UI"})
        assert "forbids delegation" in result
        run.assert_not_called()


@pytest.mark.parametrize("ownership", ["live", "missing", "deleted", "changed"])
def test_repeated_failure_guidance_requires_live_registry_before_next_action(tmp_path, monkeypatch, ownership):
    item = project(tmp_path / "registry-guidance")
    folder = Path(item["folder"])
    owners = [] if ownership == "missing" else [item.copy()]
    monkeypatch.setattr(long_tasks, "projects", lambda: list(owners))
    monkeypatch.setattr(native_agent, "MAX_TURNS_PER_SLICE", 3)
    seen = []

    def complete(payload):
        seen.append(copy.deepcopy(payload))
        turn = len(seen)
        if turn == 2 and ownership == "deleted":
            owners.clear()
        if turn == 2 and ownership == "changed":
            owners[0]["brief"] = "A newer user request supersedes this worker"
        name = "read_file" if turn <= 2 else "write_file"
        arguments = ({"path": "missing.txt"} if turn <= 2 else
                     {"path": "next-step.txt", "content": "Real next action"})
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": f"registry-{turn}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]}}]}

    state = native_agent.run_slice(item, complete=complete)
    episode = task_context.recovery_episode(folder, native_agent.capability_goal_hash(item), None)
    assert state["status"] == "continue"
    assert episode and episode["trigger"] == "repeated_failure"
    assert "read_file missing.txt: Failed" in task_context.snapshot(folder)
    if ownership == "live":
        assert len(seen) == 3 and episode["turns"] == 1
        assert "Do not retry it unchanged" in str(seen[2]["messages"])
        assert (folder / "next-step.txt").read_text() == "Real next action"
    else:
        assert len(seen) == 2 and episode["turns"] == 0
        assert "ownership or instructions changed" in state["current"]
        assert not (folder / "next-step.txt").exists()


@pytest.mark.parametrize("alias", ["local.import_game_example", "import_game_example", "local.install_game_libraries", "install_game_libraries", "design.import_game_example"])
def test_game_mutators_cannot_target_another_registered_task(tmp_path, monkeypatch, alias):
    import contextlib, toolbox_mcp, design_tools
    current=tmp_path/'current';other=tmp_path/'other';current.mkdir();other.mkdir()
    root=tmp_path/'app';(root/'LongTasks').mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(root/'LongTasks/projects.db')) as conn:
        conn.execute('CREATE TABLE projects(folder TEXT)')
        conn.executemany('INSERT INTO projects VALUES(?)',[(str(current),),(str(other),)]);conn.commit()
    monkeypatch.setattr(toolbox_mcp,'ROOT',root)
    called=[]
    def imported(game_id,target):
        called.append(target);Path(target).mkdir();raise AssertionError('Foreign source import executed')
    monkeypatch.setattr(design_tools,'import_game_example',imported)
    monkeypatch.setattr(toolbox_mcp,'_game_library_download',lambda *_:pytest.fail('Foreign libraries downloaded'))
    arguments=({'game_id':'TEMP','project_path':str(other/'game')} if alias.startswith('design.') else
               {'project_folder':str(other), **({'libraries':['phaser']} if 'install' in alias else {'game_id':'TEMP','path':'game'})})
    with patch('native_specialists.runtime') as remote:
        result=native_agent.execute_tool(current,'call_local_tool',{'tool':alias,'arguments':arguments})
        assert result.startswith('Tool error:') and 'task' in result.lower(),result
        remote.assert_not_called()
    assert called==[] and not (other/'game').exists() and not (other/'vendor').exists()


@pytest.mark.parametrize('alias', ['local.import_game_example','import_game_example','design.import_game_example'])
def test_game_import_current_task_handoff_preserves_caller_arguments(tmp_path, monkeypatch, alias):
    import contextlib, toolbox_mcp, design_tools
    folder=tmp_path/'task';folder.mkdir();root=tmp_path/'app';(root/'LongTasks').mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(root/'LongTasks/projects.db')) as conn:
        conn.execute('CREATE TABLE projects(folder TEXT)');conn.execute('INSERT INTO projects VALUES(?)',(str(folder),));conn.commit()
    monkeypatch.setattr(toolbox_mcp,'ROOT',root)
    def imported(game_id,target):
        path=Path(target);path.mkdir();(path/'index.html').write_text('<script>/* TEMP */</script>');(path/'CALE-IMPORT.json').write_text('{}')
        return {'folder':str(path),'entrypoint':'index.html','receipt_path':str(path/'CALE-IMPORT.json'),'game':{'id':game_id,'title':'TEMP'}}
    monkeypatch.setattr(design_tools,'import_game_example',imported)
    args={'game_id':'TEMP',**({'project_path':str(folder/'game')} if alias.startswith('design.') else {'path':'game'})}
    before=copy.deepcopy(args)
    result=native_agent.execute_tool(folder,'call_local_tool',{'tool':alias,'arguments':args})
    assert not result.startswith('Tool error:'),result
    assert (folder/'game/index.html').is_file() and args==before


def test_game_mutator_changed_request_during_discovery_cannot_publish(tmp_path,monkeypatch):
    import toolbox_mcp, design_tools
    folder=tmp_path/'task';(folder/'.local-ai').mkdir(parents=True)
    async def changed():
        (folder/'.local-ai/USER-REQUESTS.md').write_text('New instruction: do not import')
        return []
    monkeypatch.setattr(toolbox_mcp.server,'list_tools',changed)
    imported=Mock();monkeypatch.setattr(design_tools,'import_game_example',imported)
    result=native_agent.execute_tool(folder,'call_local_tool',{'tool':'local.import_game_example','arguments':{'game_id':'TEMP','path':'game'}})
    assert result.startswith('Tool error:') and 'changed' in result.lower(),result
    imported.assert_not_called();assert not (folder/'game').exists()


@pytest.mark.parametrize('foreign',[False,True])
def test_game_task_folder_junction_alias_is_canonicalized_before_import(tmp_path,monkeypatch,foreign):
    import contextlib, toolbox_mcp, design_tools
    folder=tmp_path/'current';other=tmp_path/'other';folder.mkdir();other.mkdir()
    alias=tmp_path/'alias'
    if os.name=='nt':
        made=subprocess.run(['cmd','/c','mklink','/J',str(alias),str(other if foreign else folder)],capture_output=True)
        assert made.returncode==0
    else:alias.symlink_to(other if foreign else folder,target_is_directory=True)
    root=tmp_path/'app';(root/'LongTasks').mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(root/'LongTasks/projects.db')) as conn:
        conn.execute('CREATE TABLE projects(folder TEXT)');conn.executemany('INSERT INTO projects VALUES(?)',[(str(folder),),(str(other),)]);conn.commit()
    monkeypatch.setattr(toolbox_mcp,'ROOT',root)
    def imported(game_id,target):
        path=Path(target);path.mkdir();(path/'index.html').write_text('<script>/* TEMP */</script>');(path/'CALE-IMPORT.json').write_text('{}')
        return {'folder':str(path),'entrypoint':'index.html','receipt_path':str(path/'CALE-IMPORT.json'),'game':{'id':game_id,'title':'TEMP'}}
    importer=Mock(side_effect=imported);monkeypatch.setattr(design_tools,'import_game_example',importer)
    arguments={'project_folder':str(alias),'game_id':'TEMP','path':'game'};original=copy.deepcopy(arguments)
    try:
        result=native_agent.execute_tool(folder,'call_local_tool',{'tool':'local.import_game_example','arguments':arguments})
        assert arguments==original
        if foreign:
            assert result.startswith('Tool error:');importer.assert_not_called()
            assert not (other/'game').exists()
        else:
            assert not result.startswith('Tool error:'),result
            assert (folder/'game/index.html').is_file()
    finally:
        if os.name=='nt':alias.rmdir()
        else:alias.unlink()


@pytest.mark.parametrize('alias',['local.install_game_libraries','install_game_libraries'])
def test_game_libraries_omitted_task_folder_is_bound_to_current_project(tmp_path,monkeypatch,alias):
    import contextlib, toolbox_mcp
    folder=tmp_path/'current';folder.mkdir();root=tmp_path/'app';(root/'LongTasks').mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(root/'LongTasks/projects.db')) as conn:
        conn.execute('CREATE TABLE projects(folder TEXT)');conn.execute('INSERT INTO projects VALUES(?)',(str(folder),));conn.commit()
    monkeypatch.setattr(toolbox_mcp,'ROOT',root)
    payload=b'/* public TEMP library fixture */';digest=hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(toolbox_mcp,'GAME_LIBRARIES',{'phaser':{'version':'test','repo':'test/repo','commit':'a'*40,
        'files':[{'source_path':'phaser.min.js','url':'https://invalid.test/not-contacted','bytes':len(payload),'sha256':digest}]}})
    monkeypatch.setattr(toolbox_mcp,'_game_library_download',lambda *_:payload)
    arguments={'libraries':['phaser']};before=copy.deepcopy(arguments)
    result=native_agent.execute_tool(folder,'call_local_tool',{'tool':alias,'arguments':arguments})
    assert not result.startswith('Tool error:'),result
    assert (folder/'vendor/cale-game/phaser/phaser.min.js').read_bytes()==payload
    assert arguments==before
# Scoped host evidence port: original accepted85552a5 regressions.

def persistence_bookmark_fixture(tmp_path, *, persistent=False):
    """Real browser fixture; original requirements never come from app code."""
    item=project(tmp_path/'bookmarks');folder=Path(item['folder'])
    item['brief']='Build an offline bookmark app in index.html with a title input and Add button. Save bookmarks across page reloads.'
    (folder/'.local-ai/USER-REQUESTS.md').write_text(item['brief'])
    source="""<!doctype html><meta name=viewport content='width=device-width'>
    <input id=title><button id=add>Add bookmark</button><ul id=bookmarks></ul><span id=count>0</span>
    <script src='app.js'></script>"""
    (folder/'index.html').write_text(source)
    code="""let items=LOAD;
    function render(){document.getElementById('bookmarks').textContent=items.join(' | ');document.getElementById('count').textContent=items.length;}
    document.getElementById('add').onclick=()=>{items.push(document.getElementById('title').value);SAVE;render();};render();"""
    code=code.replace('LOAD',"JSON.parse(localStorage.getItem('bookmarks') || '[]')" if persistent else '[]')
    code=code.replace('SAVE',"localStorage.setItem('bookmarks',JSON.stringify(items))" if persistent else '')
    (folder/'app.js').write_text(code)
    spec={'path':'index.html','cases':[
        {'set':{'title':'Alpha'},'click':['add'],'expect':{'bookmarks':'Alpha','count':'1'}},
        {'set':{'title':'Beta'},'click':['add'],'expect':{'bookmarks':'Beta','count':'2'}}]}
    active=task_context.plan_steps(folder,[{'phase':'Persistence','title':'Bookmarks persist',
        'check_tool':'check_webapp','check_arguments':spec}])['active']
    runtime=tmp_path/'runtime';runtime.mkdir()
    return item,folder,spec,active,runtime



def test_explicit_reload_requirement_rejects_real_weak_passed_suite(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path)
    from contextlib import nullcontext
    context=getattr(project_memory,'verification_context',lambda project:nullcontext())
    with context(item),patch.object(long_tasks,'STATE',runtime),patch.object(long_tasks,'projects',return_value=[item]):
        actual=json.loads(native_agent.execute_tool(folder,'check_webapp',spec))
        assert actual['passed'],actual
        completed=json.loads(native_agent.execute_tool(folder,'task_steps',{'action':'complete','step_id':active['id']}))
        assert completed['passed'] and completed['done']==1
        verification=native_agent.execute_tool(folder,'run_verifier',{})
        assert verification.startswith('Exit code: 0\n'),verification
        state={**native_agent.browser_check_evidence(folder,spec,json.dumps(actual)),'verify_passed':True}
        assert state['browser_verified']
        issue=native_agent.completion_issue(folder,item,state)
    assert 'persistence after reload' in issue
    assert 'same' in issue and task_context.step_status(folder)['done']==1



def test_real_storage_reload_witness_satisfies_only_explicit_persistence_obligation(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    # Retain both original cases; the new outcome is actually observed after
    # two real mutations and then through a reload with no replayed input.
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    with project_memory.verification_context(item),patch.object(long_tasks,'STATE',runtime),patch.object(long_tasks,'projects',return_value=[item]):
        actual=json.loads(native_agent.execute_tool(folder,'check_webapp',spec))
        assert actual['passed'],actual
        original_done=json.loads(native_agent.execute_tool(folder,'task_steps',{'action':'complete','step_id':active['id']}))
        assert original_done['passed'] and original_done['done']==1
        verification=native_agent.execute_tool(folder,'run_verifier',{})
        assert verification.startswith('Exit code: 0\n'),verification
        replay=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert replay['passed'] and replay['persistence_witnesses']
        assert not native_agent.persistence_completion_issue(folder,item)
        state={**native_agent.browser_check_evidence(folder,spec,json.dumps(actual)),'verify_passed':True}
        assert not native_agent.completion_issue(folder,item,state)
        receipts=project_memory.current_check_receipts(folder,item,checker_file=webapp_check.__file__)
        measured=[row for row in receipts if row['checker']=='check_webapp_saved']
        assert measured and measured[0]['persistence_witnesses']
        assert measured[0]['source_before']['scope']=='full_project_source'
        # External JavaScript is part of the source closure, even with unchanged HTML.
        script=folder/'app.js';original=script.read_bytes();script.write_bytes(original+b'\n// changed')
        assert native_agent.persistence_completion_issue(folder,item)
        script.write_bytes(original)
        old=item['brief'];item['brief']+=' Also retain a second field.'
        assert native_agent.persistence_completion_issue(folder,item)
        item['brief']=old
        assert not native_agent.persistence_completion_issue(folder,item)
        # Hash-corrupted/loose cache metadata cannot replace a missing witness.
        with project_memory.connection(folder) as conn:
            conn.execute("UPDATE verification_receipts SET payload=replace(payload,'reload_case','forged_reload_case')")
        assert native_agent.persistence_completion_issue(folder,item)



@pytest.mark.parametrize('brief',[
    'Build an offline counter app.',
    'Build a bookmark app. Do not save bookmarks across page reloads.',
    'Build a bookmark app. Perhaps save bookmarks across page reloads.',
    'Explain how bookmarks might persist after reload?',
    'Build a bookmark app. Save bookmarks if requested later.',
])
def test_persistence_obligation_does_not_invent_negated_or_ambiguous_scope(tmp_path,brief):
    item=project(tmp_path/'ordinary');item['brief']=brief
    assert task_context.explicit_persistence_obligations(item['folder'],item)==[]



@pytest.mark.parametrize('mode',['static','reload_with_new_input','no_storage','separate_contexts'])
def test_real_browser_persistence_witness_rejects_nonproof_sequences(tmp_path,mode):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=mode!='no_storage')
    if mode=='static':
        page=folder/'index.html';page.write_text(page.read_text()+'<h1 id=heading>Bookmarks</h1>')
        spec['cases']=[{'set':{'title':'Alpha'},'click':['add'],'expect':{'heading':'Bookmarks'}},
                       {'reload':True,'expect':{'heading':'Bookmarks'}}]
    elif mode=='reload_with_new_input':
        spec['cases']=[spec['cases'][0],{**spec['cases'][1],'reload':True}]
    elif mode=='no_storage':
        spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    elif mode=='separate_contexts':
        first=webapp_check.check(folder,{'path':'index.html','cases':[spec['cases'][0]]})
        assert first['passed'] and first['interactions'] and not first['persistence_witnesses']
        spec={'path':'index.html','cases':[{'reload':True,'expect':{'bookmarks':'','count':'0'}}]}
    result=webapp_check.check(folder,spec)
    if mode=='no_storage':assert not result['passed']
    else:assert result['passed']
    assert not result['persistence_witnesses']



def test_persistence_obligations_preserve_exact_source_span_and_basis(tmp_path):
    item=project(tmp_path/'span');item['brief']='Build a bookmark page. Save bookmarks across page reloads.'
    obligations=task_context.explicit_persistence_obligations(item['folder'],item)
    assert len(obligations)==1
    value=obligations[0];span=item['brief'][slice(*value['span'])]
    assert value['source']=='brief' and 'Save bookmarks' in span
    assert value['span_sha256']==hashlib.sha256(span.encode()).hexdigest()
    assert value['planning_basis']==native_agent.planning_basis(item)
    for brief in ['Bookmarks should not persist after reload.','Bookmarks must not save across reloads.',
                  'The old app saved bookmarks across reloads.','Could save bookmarks after reload.']:
        item['brief']=brief
        assert task_context.explicit_persistence_obligations(item['folder'],item)==[]



def test_browser_hidden_dependency_remains_limited_even_if_project_manifest_skips_git_metadata(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    hidden=folder/'.git';hidden.mkdir()
    (folder/'app.js').replace(hidden/'app.js')
    page=folder/'index.html';page.write_text(page.read_text().replace("src='app.js'","src='.git/app.js'"))
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    assert project_memory.verification_source_state(folder)['scope']=='full_project_source'
    with project_memory.verification_context(item):
        measured=webapp_check.check(folder,spec)
        assert measured['passed'] and measured['persistence_witnesses']
        assert measured['dependency_scope']=='limited_dependency_manifest'
        webapp_check.save_verifier(folder,spec)
        replay=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert replay['passed'] and replay['dependency_scope']=='limited_dependency_manifest'
        assert native_agent.persistence_completion_issue(folder,item)



def test_command_unit_receipt_binds_original_check_and_does_not_promote_process_success(tmp_path):
    item=project(tmp_path/'commands');folder=Path(item['folder'])
    active=task_context.plan_steps(folder,[{'title':'Actual command','phase':'Run',
        'check_command':'Write-Output "checked"','shell':'powershell'}])['active']
    with project_memory.verification_context(item):
        result=json.loads(native_agent.execute_tool(folder,'task_steps',{'action':'complete','step_id':active['id']}))
    assert result['passed']
    with project_memory.connection(folder) as conn:
        rows=[json.loads(row['payload']) for row in conn.execute('SELECT payload FROM verification_receipts')]
    matching=[row for row in rows if row['checker']=='task_steps']
    assert matching and matching[0]['bound_step_check_sha256']==task_context.step_check_identity(active)
    assert matching[0]['verification_level']=='process_exit_zero'
    assert matching[0]['training_eligible'] is False



@pytest.mark.parametrize('brief',[
    'Build a bookmark app. Save a screenshot after reloading the page.',
    'Build a bookmark app. Remember to save a report after reloading.',
    'Build a bookmark app. Keep the Add button visible after reload.',
])
def test_nonpersistent_after_reload_procedures_do_not_invent_data_retention(tmp_path,brief):
    item=project(tmp_path/'procedure');item['brief']=brief
    assert task_context.explicit_persistence_obligations(item['folder'],item)==[]



def test_reload_preserve_original_dual_clause_cannot_be_satisfied_by_real_add_only_witness(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    item['brief']='Build an offline bookmark app in index.html with input and buttons. Save records on this computer with localStorage. Reloading must preserve added records and deletions.'
    (folder/'.local-ai/USER-REQUESTS.md').write_text(item['brief'])
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        actual=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert actual['passed'] and actual['persistence_witnesses']
        issue=native_agent.persistence_completion_issue(folder,item)
    assert 'removal' in issue or 'deletion' in issue
    obligations=task_context.explicit_persistence_obligations(folder,item)
    assert obligations and any(row['required_effects']==['creation','removal'] for row in obligations)
    assert task_context.step_status(folder)['done']==0



def deletion_reload_fixture(tmp_path, *, store=True, store_deletion=True):
    item,folder,original,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    item['brief']='Build an offline bookmark app in index.html with input and buttons. Save records on this computer with localStorage. Reloading must preserve added records and deletions.'
    (folder/'.local-ai/USER-REQUESTS.md').write_text(item['brief'])
    active=task_context.plan_steps(folder,[{'phase':'Persistence','title':'Bookmark data',
        'check_tool':'check_webapp','check_arguments':original}])['active']
    page=folder/'index.html';page.write_text(page.read_text().replace('<ul id=bookmarks>','<button id=remove>Remove last</button><ul id=bookmarks>'))
    code="""let items=LOAD;
    function render(){const list=document.getElementById('bookmarks');list.textContent='';items.forEach(name=>{const li=document.createElement('li');li.textContent=name;list.appendChild(li);});document.getElementById('count').textContent=items.length;}
    function save(){SAVE;}
    document.getElementById('add').onclick=()=>{items.push(document.getElementById('title').value);save();render();};
    document.getElementById('remove').onclick=()=>{items.pop();DELETE_SAVE;render();};render();"""
    code=code.replace('LOAD',"JSON.parse(localStorage.getItem('bookmarks') || '[]')" if store else '[]')
    code=code.replace('DELETE_SAVE','save()' if store_deletion else '')
    code=code.replace('SAVE',"localStorage.setItem('bookmarks',JSON.stringify(items))" if store else '')
    (folder/'app.js').write_text(code)
    spec=copy.deepcopy(original)
    spec['cases'] += [
        {'reload':True,'expect':{'bookmarks':'Beta','count':'2'}},
        {'click':['remove'],'expect':{'bookmarks':'Alpha','count':'1'}},
        {'reload':True,'expect':{'bookmarks':'Alpha','count':'1'}}]
    return item,folder,spec,active



def test_actual_add_and_delete_reload_have_distinct_persisted_baseline_witnesses(tmp_path):
    import webapp_check
    item,folder,spec,active=deletion_reload_fixture(tmp_path)
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        report=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert report['passed'],report
        effects={row['effect'] for row in report['persistence_witnesses']}
        assert {'creation','removal'}<=effects
        removal=next(row for row in report['persistence_witnesses'] if row['effect']=='removal')
        assert removal['baseline_reload_case']==3 and removal['action_case']==4 and removal['reload_case']==5
        assert removal['count_before']==2 and removal['count_after']==1
        assert removal['baseline_observed_sha256']==removal['before_observed_sha256']
        assert not native_agent.persistence_completion_issue(folder,item)
    assert spec['cases'][:2]==task_context.step_status(folder)['active']['check_arguments']['cases']
    assert task_context.step_status(folder)['done']==0



def test_never_stored_add_delete_reload_empty_is_not_deletion_persistence(tmp_path):
    import webapp_check
    item,folder,spec,active=deletion_reload_fixture(tmp_path,store=False)
    spec['cases']=[spec['cases'][0],{'click':['remove'],'expect':{'bookmarks':'','count':'0'}},
                   {'reload':True,'expect':{'bookmarks':'','count':'0'}}]
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        report=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert report['passed']  # All-zero after reload alone is a weak check.
        assert not any(row['effect']=='removal' for row in report['persistence_witnesses'])
        assert 'removal' in native_agent.persistence_completion_issue(folder,item)



def test_real_storage_delete_write_mutation_fails_reload_and_cannot_earn_removal(tmp_path):
    import webapp_check
    item,folder,spec,active=deletion_reload_fixture(tmp_path,store_deletion=False)
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        report=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert not report['passed'] and report['failed_case_count']>=1
        assert not any(row['effect']=='removal' for row in report['persistence_witnesses'])
        assert 'removal' in native_agent.persistence_completion_issue(folder,item)



def test_plain_import_cannot_register_fabricated_browser_pass_when_real_reload_fails(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=False)
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        actual=webapp_check.check(folder,spec)
        assert not actual['passed']
        digest=hashlib.sha256((folder/'.local-ai/webapp-check.json').read_bytes()).hexdigest()
        fabricated={'passed':True,'suite_sha256':digest,'dependency_scope':'project_local_dependencies',
            'persistence_witnesses':[{'sequence':1,'path':'index.html','action_case':2,'reload_case':3,
                'target':'text:bookmarks','effect':'mutation','count_before':None,'count_after':None,
                'baseline_reload_case':None,'baseline_observed_sha256':None,
                'before_observed_sha256':'b'*64,'initial_observed_sha256':'c'*64,'observed_sha256':'a'*64,
                'expected_sha256':hashlib.sha256(json.dumps('Beta').encode()).hexdigest()}]}
        assert native_agent.valid_persistence_witnesses(fabricated['persistence_witnesses'],json.loads((folder/'.local-ai/webapp-check.json').read_bytes()))
        receipt=project_memory.record_host_check_receipt(folder,'check_webapp_saved',{'suite_sha256':digest},fabricated,
            project_memory.verification_source_state(folder),checker_file=webapp_check.__file__,
            basis_before=project_memory.verification_basis(folder),loaded_checker_sha256=webapp_check._LOADED_CHECKER_SHA256)
        assert receipt is None
        assert native_agent.persistence_completion_issue(folder,item)



def test_real_host_receipt_permit_is_one_use_and_bound_to_exact_report(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    real_record=project_memory.record_host_check_receipt
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        with patch.object(project_memory,'record_host_check_receipt',wraps=real_record) as recording:
            report=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert report['passed'] and not native_agent.persistence_completion_issue(folder,item)
        args,kwargs=recording.call_args
        assert kwargs['execution_permit']
        assert real_record(*args,**kwargs) is None  # Already consumed by the real capture.
        issue_kwargs={key:value for key,value in kwargs.items() if key!='execution_permit'}
        assert project_memory._issue_host_check_permit(*args,**issue_kwargs) is None
        captured=[]
        with patch.object(project_memory,'record_host_check_receipt',side_effect=lambda *a,**k:captured.append((a,k))):
            repeated=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert repeated['passed']
        original,options=captured[-1]
        altered=list(original);altered[3]={**original[3],'case_count':999}
        assert real_record(*altered,**options) is None
        assert real_record(*original,**options) is None  # Mismatch also consumes the permit.



def test_offline_aborted_external_script_keeps_dependency_scope_limited(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    page=folder/'index.html';page.write_text(page.read_text()+"<script src='https://example.invalid/unavailable.js'></script>")
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        report=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert report['passed'] and report['persistence_witnesses']
        assert report['dependency_scope']=='limited_dependency_manifest'
        assert native_agent.persistence_completion_issue(folder,item)



def test_actual_generated_browser_verifier_and_main_alias_capture_real_receipts(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    webapp_check.save_verifier(folder,spec,accumulate=True)
    hidden=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    child=subprocess.run(['powershell','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(folder/'.local-ai/verify.ps1')],
        capture_output=True,text=True,timeout=40,creationflags=hidden)
    assert child.returncode==0,child.stderr+child.stdout
    with project_memory.connection(folder) as conn:
        cli_rows=[json.loads(row['payload']) for row in conn.execute('SELECT payload FROM verification_receipts')]
    assert any(row['checker']=='check_webapp_saved' for row in cli_rows)
    # A separate real runpy __main__ entrypoint has an authoritative TEMP task
    # registry. No fake browser verdict or canonical-module main() shortcut.
    with patch.object(long_tasks,'STATE',runtime),long_tasks.database() as conn:
        conn.execute('INSERT INTO projects(id,folder,brief,created,updated) VALUES(?,?,?,?,?)',
            (item['id'],str(folder.resolve()),item['brief'],time.time(),time.time()))
    bootstrap=tmp_path/'browser_cli_bootstrap.py'
    bootstrap.write_text('import sys,runpy\nfrom pathlib import Path\nsys.path.insert(0,'+repr(str(Path(native_agent.__file__).parent))+')\nimport long_tasks\nlong_tasks.STATE=Path('+repr(str(runtime))+')\nsys.argv='+repr([webapp_check.__file__,'--folder',str(folder),'--spec',str(folder/'.local-ai/webapp-check.json')])+'\nrunpy.run_path('+repr(webapp_check.__file__)+',run_name="__main__")\n')
    aliased=subprocess.run([sys.executable,'-B',str(bootstrap)],capture_output=True,text=True,timeout=40,creationflags=hidden)
    assert aliased.returncode==0,aliased.stderr+aliased.stdout
    assert project_memory.current_check_receipts(folder,item,checker_file=webapp_check.__file__)
    assert not native_agent.persistence_completion_issue(folder,item)



def test_actual_native_function_checker_cli_main_captures_real_pass(tmp_path):
    folder=tmp_path/'native-cli';folder.mkdir();(folder/'.local-ai').mkdir()
    (folder/'logic.py').write_text('def twice(value): return value*2')
    spec={'request_hash':task_context.request_identity(folder),'checks':[
        {'path':'logic.py','function':'twice','cases':[{'args':[3],'expected':6}]}]}
    path=folder/'.local-ai/cale-python-checks.json';path.write_text(json.dumps(spec))
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    child=subprocess.run([sys.executable,'-B',native_agent.__file__,'check-python',str(folder),str(path),sha],
        capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert child.returncode==0,child.stderr+child.stdout
    assert json.loads(child.stdout)['passed']
    with project_memory.connection(folder) as conn:
        rows=[json.loads(row['payload']) for row in conn.execute('SELECT payload FROM verification_receipts')]
    assert any(row['checker']=='check_python_function' and row['verification_level']=='acceptance_passed_unbound' for row in rows)



def test_never_stored_return_to_initial_zero_cannot_satisfy_even_generic_reload_request(tmp_path):
    import webapp_check
    item,folder,spec,active=deletion_reload_fixture(tmp_path,store=False)
    item['brief']='Build an offline bookmark app in index.html with input and buttons. Save bookmarks across page reloads.'
    (folder/'.local-ai/USER-REQUESTS.md').write_text(item['brief'])
    spec['cases']=[spec['cases'][0],{'click':['remove'],'expect':{'bookmarks':'','count':'0'}},
                   {'reload':True,'expect':{'bookmarks':'','count':'0'}}]
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,spec,accumulate=True)
        report=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert report['passed']
        assert native_agent.persistence_completion_issue(folder,item)



def test_collection_witness_observation_never_invokes_app_owned_children_getter(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    script=folder/'app.js'
    script.write_text(script.read_text()+"\nwindow.witness={getterReads:0}; const nativeChildren=Object.getOwnPropertyDescriptor(Element.prototype,'children').get; Object.defineProperty(document.getElementById('bookmarks'),'children',{get(){window.witness.getterReads++;return nativeChildren.call(this);}});")
    for case in spec['cases']:case['state']={'witness.getterReads':0}
    measured=webapp_check.check(folder,spec)
    assert measured['passed'],measured



def test_collection_witness_observation_never_invokes_app_prototype_children_getter(tmp_path):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    script=folder/'app.js'
    script.write_text(script.read_text()+"\nwindow.probe={reads:0}; const pristineChildren=Object.getOwnPropertyDescriptor(Element.prototype,'children').get; Object.defineProperty(Element.prototype,'children',{configurable:true,get(){window.probe.reads++;return pristineChildren.call(this);}});")
    control={'path':'index.html','cases':[{'click':['add'],'state':{'probe.reads':0}}]}
    assert webapp_check.check(folder,control)['passed']
    for case in spec['cases']:case['state']={'probe.reads':0}
    measured=webapp_check.check(folder,spec)
    assert measured['passed'],measured



@pytest.mark.parametrize('kind', ['children','collection_length','collection_item','matches'])
def test_pristine_list_witness_ignores_all_later_prototype_replacements(tmp_path,kind):
    import webapp_check
    item,folder,spec,active=deletion_reload_fixture(tmp_path)
    replacements={
        'children': "const d=Object.getOwnPropertyDescriptor(Element.prototype,'children');Object.defineProperty(Element.prototype,'children',{configurable:true,get(){probe.reads++;return d.get.call(this);}});",
        'collection_length': "const d=Object.getOwnPropertyDescriptor(HTMLCollection.prototype,'length');Object.defineProperty(HTMLCollection.prototype,'length',{configurable:true,get(){probe.reads++;return d.get.call(this);}});",
        'collection_item': "const method=HTMLCollection.prototype.item;HTMLCollection.prototype.item=function(...args){probe.reads++;return Reflect.apply(method,this,args);};",
        'matches': "const method=Element.prototype.matches;Element.prototype.matches=function(...args){probe.reads++;return Reflect.apply(method,this,args);};",
    }
    script=folder/'app.js';script.write_text(script.read_text()+'\nwindow.probe={reads:0};'+replacements[kind])
    control={'path':'index.html','cases':[{'set':{'title':'Control'},'click':['add'],'state':{'probe.reads':0}}]}
    assert webapp_check.check(folder,control)['passed']
    for case in spec['cases']:case['state']={'probe.reads':0}
    measured=webapp_check.check(folder,spec)
    assert measured['passed'],measured
    assert {'creation','removal'}<={row['effect'] for row in measured['persistence_witnesses']}



@pytest.mark.parametrize('kind', ['table_rows','section_rows','collection_length','collection_item','node_length','node_item','query','escape','tag'])
def test_pristine_row_observer_ignores_later_dom_prototype_and_method_replacements(tmp_path,kind):
    import webapp_check
    item=project(tmp_path/'tables');folder=Path(item['folder'])
    replacements={
        'table_rows': "const d=Object.getOwnPropertyDescriptor(HTMLTableElement.prototype,'rows');Object.defineProperty(HTMLTableElement.prototype,'rows',{configurable:true,get(){probe.reads++;return d.get.call(this);}});",
        'section_rows': "const d=Object.getOwnPropertyDescriptor(HTMLTableSectionElement.prototype,'rows');Object.defineProperty(HTMLTableSectionElement.prototype,'rows',{configurable:true,get(){probe.reads++;return d.get.call(this);}});",
        'collection_length': "const d=Object.getOwnPropertyDescriptor(HTMLCollection.prototype,'length');Object.defineProperty(HTMLCollection.prototype,'length',{configurable:true,get(){probe.reads++;return d.get.call(this);}});",
        'collection_item': "const method=HTMLCollection.prototype.item;HTMLCollection.prototype.item=function(...args){probe.reads++;return Reflect.apply(method,this,args);};",
        'node_length': "const d=Object.getOwnPropertyDescriptor(NodeList.prototype,'length');Object.defineProperty(NodeList.prototype,'length',{configurable:true,get(){probe.reads++;return d.get.call(this);}});",
        'node_item': "const method=NodeList.prototype.item;NodeList.prototype.item=function(...args){probe.reads++;return Reflect.apply(method,this,args);};",
        'query': "const method=Document.prototype.querySelectorAll;Document.prototype.querySelectorAll=function(...args){probe.reads++;return Reflect.apply(method,this,args);};",
        'escape': "const method=CSS.escape;CSS.escape=function(...args){probe.reads++;return Reflect.apply(method,this,args);};",
        'tag': "const d=Object.getOwnPropertyDescriptor(Element.prototype,'localName');Object.defineProperty(Element.prototype,'localName',{configurable:true,get(){probe.reads++;return d.get.call(this);}});",
    }
    source="""<button id=add>Add</button><table id=records><tbody id=body></tbody></table>
    <script>window.probe={reads:0};document.getElementById('add').onclick=()=>{const row=document.createElement('tr');const cell=document.createElement('td');cell.textContent='Entry';row.appendChild(cell);document.getElementById('body').appendChild(row);};REPLACEMENT</script>"""
    (folder/'index.html').write_text(source.replace('REPLACEMENT',replacements[kind]))
    control={'path':'index.html','cases':[{'click':['add'],'state':{'probe.reads':0}}]}
    assert webapp_check.check(folder,control)['passed']
    observed=copy.deepcopy(control);observed['cases'][0]['row_counts']={'records':1,'body':1}
    result=webapp_check.check(folder,observed)
    assert result['passed'],result



def test_global_witness_budget_retains_later_complete_group_without_changing_any_verdict(tmp_path):
    import webapp_check
    item,folder,strong,active=deletion_reload_fixture(tmp_path)
    page=folder/'index.html'
    spans=''.join('<span id="noise'+str(index)+'">0</span>' for index in range(32))
    page.write_text(page.read_text().replace("<script src='app.js'></script>",'<input id=seed><button id=fill>Fill</button>'+spans+"<script src='app.js'></script>"))
    script=folder/'app.js'
    script.write_text(script.read_text()+"\nfunction noise(value){for(let i=0;i<32;i++)document.getElementById('noise'+i).textContent=value;}noise(localStorage.getItem('noise')||'0');document.getElementById('fill').onclick=()=>{const value=document.getElementById('seed').value;localStorage.setItem('noise',value);noise(value);};")
    first={'noise'+str(index):'1' for index in range(16)}
    second={'noise'+str(index):'2' for index in range(16,32)}
    noise={'path':'index.html','cases':[{'set':{'seed':'1'},'click':['fill'],'expect':first},
        {'reload':True,'expect':first},{'set':{'seed':'2'},'click':['fill'],'expect':second},
        {'reload':True,'expect':second}]}
    with project_memory.verification_context(item):
        webapp_check.save_verifier(folder,noise,accumulate=True)
        webapp_check.save_verifier(folder,strong,accumulate=True)
        path=folder/'.local-ai/webapp-check.json';before=path.read_bytes()
        report=webapp_check.run_saved_checks(folder,path)
        assert report['passed'] and report['case_count']==9 and report['failed_case_count']==0
        assert len(report['checks'])==2 and all(check['passed'] for check in report['checks'])
        assert path.read_bytes()==before
        assert len(report['persistence_witnesses'])<=32
        later=[row for row in report['persistence_witnesses'] if row['sequence']==2 and row['target']=='text:bookmarks']
        assert {'creation','removal'}<={row['effect'] for row in later}
        assert not native_agent.persistence_completion_issue(folder,item)
        # A later failure is still authoritative; witness priority never edits
        # cases, original numbering, success flags or the global verifier.
        webapp_check.save_verifier(folder,{'path':'index.html','cases':[{'expect':{'missing':'required'}}]},accumulate=True)
        failed_bytes=path.read_bytes()
        failed=webapp_check.run_saved_checks(folder,path)
        assert not failed['passed'] and failed['case_count']==10 and failed['failed_case_count']==1
        assert [check['passed'] for check in failed['checks']]==[True,True,False]
        assert path.read_bytes()==failed_bytes
        assert native_agent.persistence_completion_issue(folder,item)


@pytest.mark.parametrize('served_runtime',[False,True])
def test_application_source_requires_complete_actual_dependency_observation(tmp_path,served_runtime):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    unused=folder/'node_modules/unused';unused.mkdir(parents=True)
    asset=unused/'style.css';asset.write_text('body{color:black}')
    if served_runtime:
        page=folder/'index.html';page.write_text(page.read_text()+"<link rel=stylesheet href='node_modules/unused/style.css'>")
    spec['cases'].append({'reload':True,'expect':{'bookmarks':'Beta','count':'2'}})
    with project_memory.verification_context(item),patch.object(long_tasks,'STATE',runtime),patch.object(long_tasks,'projects',return_value=[item]):
        actual=json.loads(native_agent.execute_tool(folder,'check_webapp',spec))
        assert actual['passed'],actual
        replay=webapp_check.run_saved_checks(folder,folder/'.local-ai/webapp-check.json')
        assert replay['passed'] and replay['persistence_witnesses'],replay
        issue=native_agent.persistence_completion_issue(folder,item)
        if served_runtime:
            assert replay['dependency_scope']=='limited_dependency_manifest' and issue
        else:
            assert replay['dependency_scope']=='project_local_dependencies' and not issue
        assert project_memory.verification_source_state(folder)['scope']=='application_source'
        assert task_context.step_status(folder)['done']==0
        script=folder/'app.js';before=script.stat();content=script.read_bytes()
        script.write_bytes(content.replace(b'let items',b'var items'));os.utime(script,ns=(before.st_atime_ns,before.st_mtime_ns))
        assert native_agent.persistence_completion_issue(folder,item)


def test_persistence_scope_diagnostic_reports_bounded_reason_without_another_capture(tmp_path,monkeypatch):
    import webapp_check
    item,folder,spec,active,runtime=persistence_bookmark_fixture(tmp_path,persistent=True)
    monkeypatch.setattr(project_memory,'_VERIFICATION_MAX_BYTES',1)
    original=project_memory.verification_source_state;captures=[]
    def observed(path):
        captures.append(path);return original(path)
    monkeypatch.setattr(project_memory,'verification_source_state',observed)
    with patch.object(long_tasks,'STATE',runtime),patch.object(long_tasks,'projects',return_value=[item]):
        issue=native_agent.persistence_completion_issue(folder,item)
    # Establish the real retained suite first; absent-suite guidance is separate.
    spec_path=folder/'.local-ai/webapp-check.json'
    webapp_check.save_verifier(folder,spec)
    captures.clear()
    with patch.object(long_tasks,'STATE',runtime),patch.object(long_tasks,'projects',return_value=[item]):
        issue=native_agent.persistence_completion_issue(folder,item)
    assert 'byte_budget' in issue and 'limited_dependency_manifest' in issue
    assert str(folder) not in issue and len(captures)==1


def helper_workflow_fixture(folder,monkeypatch):
    folder.mkdir();project={'id':'b'*12,'folder':str(folder),
        'brief':'Calculate invoice totals with a verified pure helper, then implement the display function.'}
    monkeypatch.setattr(long_tasks,'STATE',folder.parent/'helper-supervisor')
    with long_tasks.database() as conn:
        conn.execute('INSERT INTO projects(id,folder,brief,created,updated) VALUES(?,?,?,?,?)',
                     (project['id'],project['folder'],project['brief'],time.time(),time.time()))
    with project_memory.verification_context(project):
        helper=project_memory.register_expression_helper(folder,'invoice_total',{'amounts':'numbers','tax':'number'},
            'round(sum(amounts)*(1+tax),2)',[{'inputs':{'amounts':[10,5],'tax':.06},'expected':15.9}])
        step={'phase':'Compute','title':'Reuse invoice totals','check_tool':'check_expression_helper',
              'check_arguments':{'helper_id':helper['id'],'cases':[
                  {'inputs':{'amounts':[100,25],'tax':.1},'expected':137.5},
                  {'inputs':{'amounts':[],'tax':0},'expected':0}]}}
        next_step={'phase':'Display','title':'Implement display','check_tool':'check_python_function',
                   'check_arguments':{'path':'display.py','function':'display_total','cases':[{'args':[137.5],'expected':'$137.50'}]}}
        task_context.plan_steps(folder,[step,next_step])
    native_agent.save_state(project,{'status':'continue','turns':7,'assertion_facade_budget':{'unsuccessful':5},
        'messages':[{'role':'system','content':"You are Cale's native project worker."},{'role':'user','content':'Continue saved work.'}]})
    return project,helper,step,next_step


def test_saved_helper_unit_resumes_real_worker_zero_inference_process_or_network(tmp_path,monkeypatch):
    import socket,urllib.request
    project,helper,original,next_step=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    before=task_context.step_status(project['folder']);state_before=native_agent.read_state(project)
    monkeypatch.setattr(long_tasks,'STATE',tmp_path/'supervisor')
    with long_tasks.database() as conn:
        conn.execute('INSERT INTO projects(id,folder,brief,created,updated) VALUES(?,?,?,?,?)',
                     (project['id'],project['folder'],project['brief'],time.time(),time.time()))
    assert long_tasks.projects()[0]['brief']==project['brief']
    with (patch.object(cloud_ai.Router,'complete',side_effect=AssertionError('No LLM inference')) as inference,
          patch('subprocess.run',side_effect=AssertionError('No external process')) as process,
          patch('subprocess.Popen',side_effect=AssertionError('No external process')) as popen,
          patch.object(socket,'create_connection',side_effect=AssertionError('No network')) as network,
          patch.object(urllib.request,'urlopen',side_effect=AssertionError('No network'))):
        state=native_agent.run_slice(project)
    after=task_context.step_status(project['folder'])
    assert after['done']==1 and after['total']==2 and after['active']['title']==next_step['title']
    assert after['active']['check_arguments']==next_step['check_arguments']
    assert state['status']=='continue' and state.get('helper_reuse_checkpoints')==1
    assert state['turns']==state_before['turns']==7
    assert state['assertion_facade_budget']=={'unsuccessful':5}
    assert not state.get('verify_passed')
    inference.assert_not_called();process.assert_not_called();popen.assert_not_called();network.assert_not_called()
    assert not (Path(project['folder'])/'display.py').exists()
    saved_message=state['messages'][-1]['content']
    assert '"helper_results"' in saved_message and '"actual": 137.5' in saved_message



def test_helper_wrong_saved_outcome_does_not_commit_sql_step(tmp_path,monkeypatch):
    project,helper,step,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    monkeypatch.setattr(long_tasks,'projects',lambda:[project])
    step['check_arguments']['cases'][0]['expected']=999
    task_context.revise_step(project['folder'],task_context.step_status(project['folder'])['active']['id'],[step])
    with project_memory.verification_context(project):
        result=json.loads(native_agent.execute_tool(Path(project['folder']),'task_steps',{'action':'complete',
            'step_id':task_context.step_status(project['folder'])['active']['id']}))
    assert result['passed'] is False and task_context.step_status(project['folder'])['done']==0


@pytest.mark.parametrize('tool',['register_expression_helper','find_expression_helpers','run_expression_helper',
    'local.register_expression_helper','local.find_expression_helpers','local.run_expression_helper'])
def test_native_helper_dispatch_rejects_another_registered_task_before_discovery(tmp_path,monkeypatch,tool):
    import toolbox_mcp
    one=tmp_path/'one';two=tmp_path/'two';one.mkdir();two.mkdir()
    monkeypatch.setattr(toolbox_mcp.server,'list_tools',Mock(side_effect=AssertionError('Scope rejected first')))
    result=native_agent.execute_tool(one,'call_local_tool',{'tool':tool,'arguments':{'project_folder':str(two)}})
    assert result.startswith('Tool error:') and 'current task folder' in result
    toolbox_mcp.server.list_tools.assert_not_called()


def test_helper_tools_auto_join_existing_capability_catalog_without_private_values():
    import toolbox_mcp
    results=native_agent.capability_local_search('expression helper')
    names={row['name'] for row in results}
    assert {'local.register_expression_helper','local.find_expression_helpers','local.run_expression_helper'}<=names
    assert all('PRIVATE_EXPRESSION' not in json.dumps(row) for row in results)


def test_saved_helper_pass_cannot_advance_after_pause_during_computation(tmp_path,monkeypatch):
    import quick_calculator
    project,helper,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    monkeypatch.setattr(long_tasks,'projects',lambda:[project])
    actual=quick_calculator.run_helper
    def paused(*args):
        value=actual(*args)
        state=native_agent.read_state(project);state['status']='paused';native_agent.save_state(project,state)
        return value
    with patch.object(quick_calculator,'run_helper',side_effect=paused),patch.object(cloud_ai.Router,'complete',side_effect=AssertionError('No model')) as inference:
        state=native_agent.run_slice(project)
    assert state['status']=='paused' and task_context.step_status(project['folder'])['done']==0
    assert not state.get('helper_reuse_checkpoints')
    inference.assert_not_called()


@pytest.mark.parametrize('mutation',['source','acceptance'])
def test_helper_output_does_not_override_source_or_check_commit_guards(tmp_path,monkeypatch,mutation):
    import quick_calculator
    project,helper,step,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    monkeypatch.setattr(long_tasks,'projects',lambda:[project])
    active=task_context.step_status(project['folder'])['active'];actual=quick_calculator.run_helper;changed=[]
    def raced(*args):
        value=actual(*args)
        if not changed:
            changed.append(True)
            if mutation=='source':(Path(project['folder'])/'new-source.py').write_text('value=2')
            else:
                replacement=copy.deepcopy(step);replacement['check_arguments']['cases'][0]['expected']=999
                task_context.revise_step(project['folder'],active['id'],[replacement])
        return value
    with project_memory.verification_context(project),patch.object(quick_calculator,'run_helper',side_effect=raced):
        raw=native_agent.execute_tool(Path(project['folder']),'task_steps',{'action':'complete','step_id':active['id']})
    if mutation=='acceptance':
        assert raw.startswith('Tool error:') and 'acceptance check changed' in raw
    else:
        assert json.loads(raw)['passed'] is False
    assert task_context.step_status(project['folder'])['done']==0


def test_saved_helper_worker_reuse_survives_fresh_process_restart(tmp_path,monkeypatch):
    project,helper,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    state=tmp_path/'supervisor';monkeypatch.setattr(long_tasks,'STATE',state)
    with long_tasks.database() as conn:
        conn.execute('INSERT INTO projects(id,folder,brief,created,updated) VALUES(?,?,?,?,?)',
                     (project['id'],project['folder'],project['brief'],time.time(),time.time()))
    script="""from pathlib import Path
import sys,json,socket,urllib.request
from unittest.mock import patch
import long_tasks,native_agent,cloud_ai,task_context
long_tasks.STATE=Path(sys.argv[1]);project=long_tasks.projects()[0]
with patch.object(cloud_ai.Router,'complete',side_effect=AssertionError('No model')),patch('subprocess.run',side_effect=AssertionError('No child process')),patch('subprocess.Popen',side_effect=AssertionError('No child process')),patch.object(socket,'create_connection',side_effect=AssertionError('No network')),patch.object(urllib.request,'urlopen',side_effect=AssertionError('No network')):
 state=native_agent.run_slice(project)
print(json.dumps({'status':state['status'],'done':task_context.step_status(project['folder'])['done'],'helper_reuse':state.get('helper_reuse_checkpoints'),'turns':state['turns']}))
"""
    result=subprocess.run([sys.executable,'-B','-c',script,str(state)],cwd=Path(native_agent.__file__).parent,
                          capture_output=True,text=True,timeout=20,check=True)
    assert json.loads(result.stdout)=={'status':'continue','done':1,'helper_reuse':1,'turns':7}
    assert task_context.step_status(project['folder'])['active']['title']=='Implement display'


def test_independent_helper_original_brief_change_during_computation_blocks_step(tmp_path,monkeypatch):
    import quick_calculator
    project,helper,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    monkeypatch.setattr(long_tasks,'STATE',tmp_path/'supervisor')
    with long_tasks.database() as conn:
        conn.execute('INSERT INTO projects(id,folder,brief,created,updated) VALUES(?,?,?,?,?)',
                     (project['id'],project['folder'],project['brief'],time.time(),time.time()))
    actual=quick_calculator.run_helper
    def changed(*args):
        result=actual(*args)
        with long_tasks.database() as conn:
            conn.execute('UPDATE projects SET brief=? WHERE id=?',('Changed original required calculation',project['id']))
        return result
    with patch.object(quick_calculator,'run_helper',side_effect=changed),patch.object(cloud_ai.Router,'complete',side_effect=AssertionError('No model')):
        native_agent.run_slice(project)
    assert task_context.step_status(project['folder'])['done']==0


def test_independent_helper_stop_at_sql_publication_blocks_step(tmp_path,monkeypatch):
    project,helper,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    monkeypatch.setattr(long_tasks,'projects',lambda:[project])
    original=task_context.record_step_check
    def stopped(*args,**kwargs):
        state=native_agent.read_state(project);state['status']='stopped';native_agent.save_state(project,state)
        return original(*args,**kwargs)
    with patch.object(task_context,'record_step_check',side_effect=stopped),patch.object(cloud_ai.Router,'complete',side_effect=AssertionError('No model')):
        state=native_agent.run_slice(project)
    assert state['status']=='stopped'
    assert task_context.step_status(project['folder'])['done']==0


@pytest.mark.parametrize('control',['stopped','paused'])
def test_independent_helper_checkpoint_retains_control_after_sql_commit(tmp_path,monkeypatch,control):
    project,helper,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    monkeypatch.setattr(long_tasks,'STATE',tmp_path/'supervisor')
    with long_tasks.database() as conn:
        conn.execute('INSERT INTO projects(id,folder,brief,created,updated) VALUES(?,?,?,?,?)',
                     (project['id'],project['folder'],project['brief'],time.time(),time.time()))
    original=task_context.record_action
    injected=[]
    def controlled(*args,**kwargs):
        result=original(*args,**kwargs)
        if args[1]=='task_steps' and task_context.step_status(project['folder'])['done']==1:
            state=native_agent.read_state(project);state['status']=control;native_agent.save_state(project,state)
            injected.append(True)
        return result
    with patch.object(task_context,'record_action',side_effect=controlled),patch.object(cloud_ai.Router,'complete',side_effect=AssertionError('No model')):
        state=native_agent.run_slice(project)
    assert injected==[True]
    assert task_context.step_status(project['folder'])['done']==1
    assert state['status']==control
    assert native_agent.read_state(project)['status']==control



def test_helper_offer_schema_does_not_mutate_global_catalog(tmp_path,monkeypatch):
    from jsonschema import validate
    project,helper,step,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    before=copy.deepcopy(native_agent.TOOLS)
    assert 'check_expression_helper' not in {t['function']['name'] for t in before}
    ordinary=next(t['function']['parameters'] for t in before if t['function']['name']=='task_steps')
    assert not any(b['properties'].get('check_tool',{}).get('enum')==['check_expression_helper']
                   for b in ordinary['properties']['steps']['items']['anyOf'])
    offered=native_agent.expression_helper_tools(project,native_agent.TOOLS)
    assert offered is not native_agent.TOOLS and native_agent.TOOLS==before
    assert 'check_expression_helper' in {t['function']['name'] for t in offered}
    schema=next(t['function']['parameters'] for t in offered if t['function']['name']=='task_steps')
    validate({'action':'plan','steps':[step]},schema)
    validate(step['check_arguments'],native_agent.checker_tool_schema('check_expression_helper'))
    compact=native_agent.compact_messages([
        {'role':'system','content':'A real task brief'},
        {'role':'user','content':'Old material '*20000},
        {'role':'user','content':'LATEST_REQUIREMENT keep current task scope'}],Path(project['folder']),6000,tools=offered)
    assert cloud_ai.estimated_input_tokens({'messages':compact,'tools':offered})<=4500
    assert 'LATEST_REQUIREMENT' in json.dumps(compact)
    assert native_agent.TOOLS==before


def test_helper_worker_compacts_actual_copied_offer(tmp_path,monkeypatch):
    project,helper,step,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    step['check_arguments']['cases'][0]['expected']=999
    task_context.revise_step(project['folder'],task_context.step_status(project['folder'])['active']['id'],[step])
    monkeypatch.setattr(native_agent,'MAX_TURNS_PER_SLICE',1)
    seen=[];compactions=[];actual=native_agent.compact_messages
    def compact(*args,**kwargs):
        compactions.append(copy.deepcopy(kwargs.get('tools')))
        return actual(*args,**kwargs)
    def reply(payload):
        seen.append(copy.deepcopy(payload))
        return {'choices':[{'message':{'role':'assistant','content':'Need to inspect the incorrect saved helper expectation.'}}]}
    with patch.object(native_agent,'compact_messages',side_effect=compact):
        state=native_agent.run_slice(project,complete=reply)
    assert len(seen)==1 and any('check_expression_helper'==t['function']['name'] for t in seen[0]['tools'])
    assert compactions[-1]==seen[0]['tools']
    assert state['status']!='done' and task_context.step_status(project['folder'])['done']==0


@pytest.mark.parametrize('control',['stopped','paused'])
def test_helper_sql_callback_rechecks_after_update_and_rolls_back(tmp_path,monkeypatch,control):
    project,helper,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    active=task_context.step_status(project['folder'])['active'];calls=[]
    def allowed():
        calls.append(True)
        if len(calls)==2:
            state=native_agent.read_state(project);state['status']=control;native_agent.save_state(project,state)
            return False
        return True
    with pytest.raises(ValueError,match='before publication'):
        task_context.record_step_check(project['folder'],active['id'],task_context.request_identity(project['folder']),
            'Exit code: 0\nActual helper checked',unchanged=True,
            expected_check=task_context.step_check_identity(active),before_commit=allowed)
    queue=task_context.step_status(project['folder'])
    assert queue['done']==0 and queue['active']['attempts']==0
    assert native_agent.read_state(project)['status']==control and len(calls)==2


@pytest.mark.parametrize('control',['stopped','paused','done'])
def test_explicit_helper_completion_retains_control_without_external_callback(tmp_path,monkeypatch,control):
    import quick_calculator
    project,_,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    active=task_context.step_status(project['folder'])['active'];original=quick_calculator.run_helper
    def controlled(*args):
        value=original(*args)
        state=native_agent.read_state(project);state['status']=control;native_agent.save_state(project,state)
        return value
    with project_memory.verification_context(project),patch.object(quick_calculator,'run_helper',side_effect=controlled):
        result=native_agent.execute_tool(Path(project['folder']),'task_steps',{'action':'complete','step_id':active['id']})
    assert result.startswith('Tool error:') and 'no step advanced' in result
    assert task_context.step_status(project['folder'])['done']==0
    assert native_agent.read_state(project)['status']==control


def test_helper_record_tampered_after_real_check_cannot_publish_sql_pass(tmp_path,monkeypatch):
    project,_,_,_=helper_workflow_fixture(tmp_path/'project',monkeypatch)
    folder=Path(project['folder']);active=task_context.step_status(folder)['active']
    original=task_context.record_step_check
    def raced(*args,**kwargs):
        path=next((folder/'.local-ai/expression-helpers/v1/records').iterdir())
        path.write_bytes(b'changed after actual checker return')
        return original(*args,**kwargs)
    with project_memory.verification_context(project),patch.object(task_context,'record_step_check',side_effect=raced):
        result=native_agent.execute_tool(folder,'task_steps',{'action':'complete','step_id':active['id']})
    assert result.startswith('Tool error:')
    assert task_context.step_status(folder)['done']==0

def context_projection_fixture(tmp_path, notes=True):
    folder = tmp_path / "project"
    folder.mkdir()
    state = folder / ".local-ai"
    state.mkdir()
    (state / "USER-REQUESTS.md").write_text("", encoding="utf-8")
    if notes:
        (state / "PLAN.md").write_text(long_tasks.INITIAL_PLAN, encoding="utf-8")
        (state / "PROGRESS.md").write_text(long_tasks.INITIAL_PROGRESS, encoding="utf-8")
    task_context.plan_steps(folder, [queue_step("Unit "+str(i), "exit 0", "Components") for i in range(1,9)])
    # Saved host-status fixture, NOT evidence that any real app is complete.
    for _ in range(5):
        active = task_context.step_status(folder)["active"]
        task_context.record_step_check(folder, active["id"], task_context.request_identity(folder), "Exit code: 0\nfixture component result", unchanged=True)
    return folder


def context_projection_rows(folder):
    with project_memory.connection(folder) as connection:
        return [tuple(row) for row in connection.execute("SELECT id,request_hash,status,attempts,evidence FROM task_steps ORDER BY id")]


@pytest.mark.parametrize("whitespace", [False, True])
def test_context_projection_labels_saved_components_and_suppresses_only_exact_startup_templates(tmp_path, whitespace):
    folder = context_projection_fixture(tmp_path)
    if whitespace:
        for name, template in (("PLAN.md", long_tasks.INITIAL_PLAN), ("PROGRESS.md", long_tasks.INITIAL_PROGRESS)):
            (folder/".local-ai"/name).write_text("\ufeff\n\t"+template.replace(" ", "  ").replace("\n", "\r\n")+"\n", encoding="utf-8")
    files = {name:(folder/".local-ai"/name).read_bytes() for name in ("PLAN.md", "PROGRESS.md")}
    rows = context_projection_rows(folder)
    checkpoint = task_context.snapshot(folder)
    assert "5/8 component checks recorded passed" in checkpoint
    assert "not current source verification or whole-task completion" in checkpoint
    assert "SQLite step 6: Unit 6" in checkpoint
    assert "No work has been completed yet" not in checkpoint
    assert "The worker has not written milestones yet" not in checkpoint
    assert "startup placeholder" in checkpoint and "not been authored" in checkpoint
    assert context_projection_rows(folder) == rows
    assert files == {name:(folder/".local-ai"/name).read_bytes() for name in files}


def test_context_projection_preserves_custom_notes_even_with_similar_startup_words(tmp_path):
    folder = context_projection_fixture(tmp_path)
    progress = "# Progress\n\nNo work has been completed yet on the optional export. Earlier component checks exist.\nNext: Preserve the pending feature.\n"
    plan = "# User roadmap\n- [ ] Preserve a real custom requirement\n"
    (folder/".local-ai/PROGRESS.md").write_text(progress, encoding="utf-8")
    root_plan = folder/"PLAN.md"
    root_plan.write_text(plan, encoding="utf-8")
    stamp = time.time_ns()+2_000_000_000
    os.utime(root_plan, ns=(stamp, stamp))
    checkpoint = task_context.snapshot(folder)
    assert progress in checkpoint.replace("\r\n", "\n") and plan in checkpoint.replace("\r\n", "\n")
    assert "[Saved at project root: PLAN.md]" in checkpoint
    assert "5/8 component checks recorded passed" in checkpoint
    assert "startup placeholder" not in checkpoint


def test_context_projection_missing_notes_do_not_mean_no_work_or_author_completion(tmp_path):
    folder = context_projection_fixture(tmp_path, notes=False)
    checkpoint = task_context.snapshot(folder)
    assert "5/8 component checks recorded passed" in checkpoint
    assert "PLAN.md:\n[Missing or empty note" in checkpoint
    assert "PROGRESS.md:\n[Missing or empty note" in checkpoint
    assert "No work has been completed yet" not in checkpoint
    assert not (folder/".local-ai/PLAN.md").exists()
    assert not (folder/".local-ai/PROGRESS.md").exists()


def test_context_projection_followup_scopes_out_old_component_records(tmp_path):
    folder = context_projection_fixture(tmp_path)
    rows = context_projection_rows(folder)
    (folder/".local-ai/USER-REQUESTS.md").write_text("Now implement a new requested feature.", encoding="utf-8")
    checkpoint = task_context.snapshot(folder)
    assert "5/8 component checks recorded passed" not in checkpoint
    assert "no current-request component-check records" in checkpoint
    assert "does not mean no work has been performed" in checkpoint
    assert context_projection_rows(folder) == rows


def test_context_projection_real_fresh_and_no_checkpoint_compaction_reuse_same_queue(tmp_path):
    folder = context_projection_fixture(tmp_path)
    state = {"verify_passed": False, "turns": 261, "step_focus_turns": 4}
    before = copy.deepcopy(state)
    messages = [{"role": "system", "content": "Original request must remain intact."},
                {"role": "user", "content": "Repeated old discussion. "*6000}]
    fresh = native_agent.fresh_messages(messages, folder, state)
    fallback = native_agent.compact_messages(messages, folder, context_limit=20000)
    for content in (fresh[1]["content"], fallback[1]["content"]):
        assert "5/8 component checks recorded passed" in content
        assert "SQLite step 6: Unit 6" in content
        assert "No work has been completed yet" not in content
        assert "not current source verification or whole-task completion" in content
    assert fresh[0] == fallback[0] == messages[0]
    assert state == before


def test_context_projection_does_not_author_notes_or_promote_completion(tmp_path):
    folder = context_projection_fixture(tmp_path)
    source = folder/"index.html"
    source.write_text("<!doctype html><title>Unfinished project</title>", encoding="utf-8")
    paths = [source,folder/".local-ai/PLAN.md",folder/".local-ai/PROGRESS.md"]
    before = {str(path):path.read_bytes() for path in paths}
    rows = context_projection_rows(folder)
    state = {"verify_passed":False}
    task_context.snapshot(folder)
    native_agent.compact_messages(recovery_projection_messages(),folder,20000)
    native_agent.fresh_messages([{"role":"system","content":"Maintain accurate project notes."}],folder,state)
    assert before == {str(path):path.read_bytes() for path in paths}
    assert context_projection_rows(folder) == rows
    assert task_context.step_status(folder)["done"] == 5
    assert state["verify_passed"] is False


def test_context_projection_fingerprints_match_real_builtin_constants_and_preserve_quotes():
    for name, template in (("PLAN.md", long_tasks.INITIAL_PLAN), ("PROGRESS.md", long_tasks.INITIAL_PROGRESS)):
        assert "startup placeholder" in task_context.project_note_excerpt(name, template, 3500)
        custom = "# Actual project note\nThe initial note was quoted below:\n" + template
        assert task_context.project_note_excerpt(name, custom, 3500) == custom


def test_context_projection_saved_unit_counts_do_not_attest_new_source_bytes(tmp_path):
    folder = context_projection_fixture(tmp_path)
    (folder/"index.html").write_text("<p>New unverified source</p>", encoding="utf-8")
    messages = [{"role": "system", "content": "Build the requested app."}]
    state = {"verify_passed": False}
    content = native_agent.fresh_messages(messages, folder, state)[1]["content"]
    assert "5/8 component checks recorded passed" in content
    assert "not current source verification or whole-task completion" in content
    assert state["verify_passed"] is False


def recovery_projection_messages():
    return [{"role": "system", "content": "Keep the complete original requirements."},
            {"role": "user", "content": "Old discussion repeated. "*10000}]


@pytest.mark.parametrize("database", ["missing", "corrupt", "oldschema"])
def test_recovery_projection_unavailable_database_retains_custom_notes_without_initialization(tmp_path, database):
    from contextlib import closing
    folder = tmp_path/"project"
    (folder/".local-ai").mkdir(parents=True)
    (folder/"PLAN.md").write_text("# Actual plan\n- [ ] Preserve custom acceptance.\n", encoding="utf-8")
    path = folder/".local-ai/memory.sqlite3"
    if database == "corrupt": path.write_bytes(b"owned corrupt SQLite fixture")
    if database == "oldschema":
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE existing_note (body TEXT)")
            connection.execute("INSERT INTO existing_note VALUES('preserve')")
    before = path.read_bytes() if path.exists() else None
    result = native_agent.compact_messages(recovery_projection_messages(), folder, 20000)
    assert "Preserve custom acceptance" in result[1]["content"]
    assert "SQLite progress is unavailable" in result[1]["content"]
    assert "Do not infer zero progress" in result[1]["content"]
    assert (path.read_bytes() if path.exists() else None) == before
    if database == "oldschema":
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("existing_note",)]


def test_recovery_projection_real_wal_writer_is_nonblocking_and_shows_committed_current_queue(tmp_path):
    import threading
    from contextlib import closing
    folder = context_projection_fixture(tmp_path)
    held = threading.Event()
    errors = []
    def writer():
        try:
            with closing(sqlite3.connect(folder/".local-ai/memory.sqlite3")) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE task_steps SET status='done'")
                held.set()
                time.sleep(.55)
                connection.rollback()
        except Exception as error:
            errors.append(type(error).__name__)
            held.set()
    worker = threading.Thread(target=writer)
    worker.start()
    assert held.wait(2)
    try:
        started = time.monotonic()
        result = native_agent.compact_messages(recovery_projection_messages(), folder, 20000)
        assert time.monotonic()-started < .2
        assert "5/8 component checks recorded passed" in result[1]["content"]
        assert "SQLite step 6: Unit 6" in result[1]["content"]
        assert "8/8 component" not in result[1]["content"]
    finally: worker.join(2)
    assert not errors


def test_recovery_projection_changed_followup_during_read_cannot_publish_old_queue(tmp_path):
    folder = context_projection_fixture(tmp_path)
    actual = task_context._step_status
    observed = []
    def changed(connection, request):
        result = actual(connection, request)
        observed.append(True)
        (folder/".local-ai/USER-REQUESTS.md").write_text("New instructions arrived during the read.", encoding="utf-8")
        return result
    with patch.object(task_context, "_step_status", side_effect=changed):
        result = native_agent.compact_messages(recovery_projection_messages(), folder, 20000)
    assert observed
    assert "SQLite progress is unavailable" in result[1]["content"]
    assert "5/8 component checks recorded passed" not in result[1]["content"]


def test_recovery_projection_exception_closes_reader_and_never_calls_writer_helpers(tmp_path):
    folder = context_projection_fixture(tmp_path)
    actual_connect = sqlite3.connect
    closed = []
    statements = []
    class Reader:
        def __init__(self, connection): self.connection = connection
        @property
        def row_factory(self): return self.connection.row_factory
        @row_factory.setter
        def row_factory(self, value): self.connection.row_factory = value
        def set_progress_handler(self, *args): self.connection.set_progress_handler(*args)
        def execute(self, sql, *args):
            statements.append(sql)
            return self.connection.execute(sql, *args)
        def close(self):
            self.connection.close()
            closed.append(True)
    with patch.object(sqlite3, "connect", side_effect=lambda *a, **kw: Reader(actual_connect(*a, **kw))), \
         patch.object(task_context, "_step_status", side_effect=sqlite3.OperationalError("owned TEMP failure")), \
         patch.object(project_memory, "connection", side_effect=AssertionError("no writer helper")), \
         patch.object(project_memory, "database_path", side_effect=AssertionError("no database initialization")):
        result = native_agent.compact_messages(recovery_projection_messages(), folder, 20000)
    assert closed == [True]
    assert "SQLite progress is unavailable" in result[1]["content"]
    assert not any(re.match(r"(?i)CREATE|INSERT|UPDATE|DELETE|ALTER", sql) for sql in statements)


def test_recovery_projection_exclusive_rollback_writer_returns_unknown_without_waiting(tmp_path):
    import threading
    from contextlib import closing
    folder = context_projection_fixture(tmp_path)
    database = folder/".local-ai/memory.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    (folder/".local-ai/PROGRESS.md").write_text("# Progress\nNext: Preserve the custom note.\n", encoding="utf-8")
    held = threading.Event()
    def writer():
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            held.set()
            time.sleep(.55)
            connection.rollback()
    worker = threading.Thread(target=writer)
    worker.start()
    assert held.wait(2)
    try:
        started = time.monotonic()
        result = native_agent.compact_messages(recovery_projection_messages(), folder, 20000)
        assert time.monotonic()-started < .2
        assert "SQLite progress is unavailable" in result[1]["content"]
        assert "Preserve the custom note" in result[1]["content"]
    finally: worker.join(2)


def test_recovery_projection_sqlite_vm_budget_returns_unknown_not_partial_totals(tmp_path):
    folder = context_projection_fixture(tmp_path)
    with project_memory.connection(folder) as connection:
        first = connection.execute("SELECT request_hash FROM task_steps LIMIT 1").fetchone()[0]
        connection.executemany("INSERT INTO task_steps(request_hash,position,phase,title,check_command,shell,status,updated) VALUES(?,?,'Fixture','Fixture','exit 0','default','done',0)", [(first, i+100) for i in range(10000)])
    calls = []
    def clock():
        calls.append(True)
        return 0 if len(calls) == 1 else 1
    with patch.object(task_context.time, "monotonic", side_effect=clock):
        result = task_context.recovery_snapshot(folder)
    assert len(calls) >= 2
    assert "SQLite progress is unavailable" in result
    assert "component checks recorded passed" not in result


@pytest.mark.parametrize("column", ["phase", "title", "evidence"])
def test_recovery_projection_blob_queue_cell_returns_unknown_and_preserves_notes(tmp_path, column):
    from contextlib import closing
    folder = context_projection_fixture(tmp_path)
    (folder/"PLAN.md").write_text("# Actual plan\nPreserve custom acceptance.\n", encoding="utf-8")
    database = folder/".local-ai/memory.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f"UPDATE task_steps SET {column}=? WHERE id=6", (sqlite3.Binary(b"bounded invalid text cell"),))
    before = database.read_bytes()
    result = native_agent.compact_messages(recovery_projection_messages(), folder, 20000)
    content = result[1]["content"]
    assert "SQLite progress is unavailable" in content
    assert "Preserve custom acceptance" in content
    assert "5/8 component checks recorded passed" not in content
    assert "SQLite step 6" not in content
    assert database.read_bytes() == before


@pytest.mark.parametrize('with_accessibility', [False, True])
def test_submission_repair_cannot_add_or_weaken_accessibility_assertions(tmp_path, with_accessibility):
    from jsonschema import validate, ValidationError
    item, folder, source, bad, future, active, revision, runtime = retained_input_revision_native_fixture(
        tmp_path, 'submission-a11y', accessibility=with_accessibility)
    state = {'step_focus_turns': 1}
    notice = native_agent.active_step_repair_notice(item, state)
    assert notice
    request = native_agent.focused_step_repair_request(item, state, native_agent.TOOLS, notice, 65536)
    assert request
    schema = request['tools'][0]['function']['parameters']
    case = schema['properties']['cases']['items']
    if with_accessibility:
        original = bad['cases'][0]['accessibility']
        assert case['properties']['accessibility'] == {'const': original}
        assert 'accessibility' in case['required']
        revision['cases'][0]['accessibility'] = original
        validate(revision, schema)
        changed = copy.deepcopy(revision)
        changed['cases'][0]['accessibility']['add']['name'] = 'Wrong'
        with pytest.raises(ValidationError):
            validate(changed, schema)
    else:
        assert 'accessibility' not in case['properties']
        validate(revision, schema)
    assert (folder/'index.html').read_bytes() == source



def public_exhausted_context_fixture(tmp_path, monkeypatch, *, helper=False):
    """Real public checker completion; no private frontier or fixed tool count."""
    item = project(tmp_path / 'project')
    item['brief'] = 'Maintain a title worksheet and accurate notes; preserve the title Alpine.'
    folder = Path(item['folder'])
    monkeypatch.setattr(long_tasks, 'STATE', tmp_path / 'supervisor')
    with long_tasks.database() as conn:
        conn.execute('INSERT INTO projects(id,folder,brief,created,updated) VALUES(?,?,?,?,?)',
                     (item['id'], item['folder'], item['brief'], time.time(), time.time()))
    (folder / 'title.py').write_text('def title(value):\n    return "[" + value + "]"\n', encoding='utf-8')
    step = {'phase': 'Component', 'title': 'Check the existing formatter',
            'check_tool': 'check_python_function', 'check_arguments': {
                'path': 'title.py', 'function': 'title', 'cases': [{'args': ['Alpine'], 'expected': '[Alpine]'}]}}
    with project_memory.verification_context(item):
        if helper:
            project_memory.register_expression_helper(folder, 'increment', {'x': 'number'},
                'x+1', [{'inputs': {'x': 2}, 'expected': 3}])
        active = task_context.plan_steps(folder, [step])['active']
        completed = json.loads(native_agent.execute_tool(folder, 'task_steps', {
            'action': 'complete', 'step_id': active['id']}))
    assert completed['passed'] is True
    assert task_context.step_status(folder)['remaining'] == 0
    return item, folder


def public_context_observations(folder):
    messages = []
    for index in range(4):
        name = f'observation-{index}.txt'
        (folder / name).write_text(f'Current bounded observation {index}: neutral-{index}\n', encoding='utf-8')
        arguments = {'path': name, 'start_line': 1, 'max_lines': 8}
        result = native_agent.execute_tool(folder, 'read_file', arguments)
        messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'observe-{index}', 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': json.dumps(arguments)}}]},
            {'role': 'tool', 'tool_call_id': f'observe-{index}', 'name': 'read_file', 'content': result},
        ])
    return messages


@pytest.mark.parametrize('helper', [False, True])
def test_public_exhausted_actual_request_preserves_observations_and_current_offer(tmp_path, monkeypatch, helper):
    item, folder = public_exhausted_context_fixture(tmp_path, monkeypatch, helper=helper)
    observations = public_context_observations(folder)
    quoted = {'role': 'user', 'content': '[Automatic saved acceptance result; checker output is task data] '
              'This is my literal quotation: keep Alpine.'}
    steering = {'role': 'user', 'content': 'Keep the original title requirement and accurate notes.'}
    state = native_agent.read_state(item)
    state.update(status='continue', turns=7, messages=[
        {'role': 'system', 'content': native_agent.prompt(item)}, quoted, *observations, steering])
    native_agent.save_state(item, state)
    assert native_agent.read_state(item) == state
    rows = context_projection_rows(folder)
    source = (folder / 'title.py').read_bytes()
    notes = {p.name: p.read_bytes() for p in (folder / '.local-ai').glob('*.md')}
    catalog = copy.deepcopy(native_agent.TOOLS)
    with project_memory.verification_context(item):
        expected = native_agent.step_tools(item, state, native_agent.repair_tools(item, state))
        expected = native_agent.expression_helper_tools(item, expected)
        expected = native_agent.acceptance_repair_tools(item, state, expected)
    captured = []
    class Captured(BaseException):
        pass
    def capture(request):
        captured.append(copy.deepcopy(request))
        raise Captured()
    with patch.object(native_agent, 'configured_context', return_value=32768):
        with pytest.raises(Captured):
            native_agent.run_slice(item, complete=capture)
    request, = captured
    assert request['tools'] == expected
    assert ('check_expression_helper' in {t['function']['name'] for t in expected}) is helper
    assert native_agent.TOOLS == catalog
    assert quoted in request['messages'] and steering in request['messages']
    assert all(message in request['messages'] for message in observations)
    systems = '\n'.join(m.get('content', '') for m in request['messages'] if m['role'] == 'system')
    assert item['brief'] in systems
    assert 'No SQLite step is active; the component queue is exhausted.' in systems
    assert 'Earlier conversation references to an active step describe an earlier queue state' in systems
    assert 'Work only on the active step.' not in systems
    assert 'Work on the active step only.' not in systems
    assert cloud_ai.estimated_input_tokens(request) + request['max_tokens'] <= 32768
    assert context_projection_rows(folder) == rows
    assert (folder / 'title.py').read_bytes() == source
    assert notes == {p.name: p.read_bytes() for p in (folder / '.local-ai').glob('*.md')}
    after = native_agent.read_state(item)
    assert after['turns'] == 7 and after['status'] != 'done'
    # SQL attempts/evidence above and the saved worker turn remain unchanged.
    # Public548 has no private facade-budget/provenance contract to import.


def test_public_active_queue_retains_two_group_focus_and_latest_user(tmp_path):
    item = project(tmp_path / 'active')
    folder = Path(item['folder'])
    task_context.plan_steps(folder, [queue_step('Check the current unit', 'exit 0')])
    observations = public_context_observations(folder)
    steering = {'role': 'user', 'content': 'Keep my actual Alpine requirement.'}
    messages = [{'role': 'system', 'content': native_agent.prompt(item)}, *observations, steering]
    rows = context_projection_rows(folder)
    result = native_agent.fresh_messages(messages, folder)
    assert steering in result
    assert observations[-2] in result and observations[-1] in result
    assert all(message not in result for message in observations[:-2])
    assert 'Work only on the active step.' in result[1]['content']
    assert 'No SQLite step is active' not in result[1]['content']
    assert context_projection_rows(folder) == rows


def test_public_prompt_active_condition_is_pure_and_preserves_requirements(tmp_path):
    item = project(tmp_path / 'prompt')
    item['brief'] = 'Keep every literal requirement: Alpine, zero, and the quoted text "active step".'
    with patch.object(task_context, 'step_status', side_effect=AssertionError('prompt must not query SQLite')):
        text = native_agent.prompt(item)
    assert item['brief'] in text
    assert 'Work only on an active SQLite step;' in text
    assert 'With no active step, finish that request with normal tools' in text
    assert 'checked components do not prove whole-task completion' in text
    assert not (Path(item['folder']) / '.local-ai' / 'memory.sqlite3').exists()


def test_large_prompt_keeps_budget_with_installed_paths_and_skill_catalog(tmp_path):
    """Install paths/catalog text must not consume the original-request budget."""
    item = project(tmp_path / ("installed-project-" + "x" * 45))
    item["brief"] = "Start: " + "many requirements " * 7000 + "Finish: verify everything."
    skills = [{"id": "skill:local-guidance", "description": "Helpful local skill. " * 42}]
    interpreter = "C:/Users/example/AppData/Local/CaleMedia/AIWorkbench/Toolbox/python/Scripts/python.exe"
    with patch("capability_index.search", return_value=skills), patch.object(native_agent.sys, "executable", interpreter):
        text = native_agent.prompt(item)
    assert len(text) < 10000
    assert "Start:" in text and "Finish: verify everything." in text
    assert ".local-ai/ORIGINAL-REQUEST-sample.md" in text
    assert skills[0]["description"] in text
    assert interpreter in text
    assert str(item["folder"]) in text
    assert "Match explicitly requested output paths exactly." in text
    assert "do not invoke another LLM server" in text




@pytest.mark.parametrize('text,effects', [
    ('Added and removed records must persist after reload.', {'creation', 'removal'}),
    ('Save the selected theme across refreshes.', {'mutation'}),
    ('Do not persist the draft after reload.', set()),
    ('Explain how data could persist after reload.', set()),
    ('Keep the heading visible after reload.', set()),
])
def test_public548_pure_persistence_parser_does_not_write_obligation_authority(text, effects):
    basis = {'brief': 'a' * 64, 'rules': None, 'followups': None}
    with patch.object(project_memory, 'connection', side_effect=AssertionError('Pure parsing must not open SQLite')):
        rows = task_context.parse_persistence_obligations([('brief', text)], basis)
    assert {effect for row in rows for effect in row['required_effects']} == effects
    for row in rows:
        assert row['planning_basis'] == basis
        assert row['span_sha256'] == hashlib.sha256(text[row['span'][0]:row['span'][1]].encode()).hexdigest()
        assert row['source'] == 'brief'


@pytest.mark.parametrize('effect,cases', [
    ('creation', [
        {'row_counts': {'items': 0}},
        {'set': {'name': 'Violet'}, 'click': ['add'], 'row_counts': {'items': 1}},
        {'reload': True, 'row_counts': {'items': 1}},
    ]),
    ('removal', [
        {'reload': True, 'row_counts': {'items': 1}},
        {'click': ['remove'], 'row_counts': {'items': 0}},
        {'reload': True, 'row_counts': {'items': 0}},
    ]),
])
def test_public548_planned_persistence_is_typed_advisory_not_a_measured_pass(effect, cases):
    specification = {'path': 'index.html', 'cases': cases}
    with patch.object(project_memory, 'connection', side_effect=AssertionError('Plan projection must not open SQLite')):
        result = task_context.planned_persistence_effects([specification])
    assert set(result) == {'mutation', effect}
    broken = copy.deepcopy(specification)
    broken['cases'][-1]['row_counts']['items'] = 99
    assert task_context.planned_persistence_effects([broken]) == []


def test_public548_planned_persistence_does_not_union_unrelated_collections():
    creation = {'path': 'index.html', 'cases': [
        {'row_counts': {'alpha': 0}}, {'click': ['add'], 'row_counts': {'alpha': 1}},
        {'reload': True, 'row_counts': {'alpha': 1}}]}
    removal = {'path': 'index.html', 'cases': [
        {'reload': True, 'row_counts': {'beta': 1}}, {'click': ['remove'], 'row_counts': {'beta': 0}},
        {'reload': True, 'row_counts': {'beta': 0}}]}
    result = task_context.planned_persistence_effects([creation, removal])
    assert 'mutation' in result
    assert not {'creation', 'removal'} <= set(result)



def test_tiny_context_preserves_goal_unfinished_plan_and_recent_steering(tmp_path):
    item = project(tmp_path / "page")
    folder = Path(item["folder"])
    plan = "- [x] Build widget\n- [ ] Verify violet widget\n" + "\n".join(
        f"- [x] Earlier fixture checkpoint {number}: widget baseline retained for inspection."
        for number in range(40))
    progress = "\n".join(
        f"Earlier fixture detail {number}: violet widget baseline retained for later inspection."
        for number in range(40))
    assert len(plan) <= task_context.LIMITS["PLAN.md"]
    assert len(progress) <= task_context.LIMITS["PROGRESS.md"]
    (folder / ".local-ai/PLAN.md").write_text(plan)
    (folder / ".local-ai/PROGRESS.md").write_text(progress)
    goal = {"role": "system", "content": "Build and verify the violet widget."}
    steering = {"role": "user", "content": "Keep the violet label."}
    messages = [goal] + [{"role": "user", "content": "old note " * 100} for _ in range(40)] + [steering]
    original_tools = copy.deepcopy(native_agent.TOOLS)
    checkpoint = {"role": "system", "content": task_context.recovery_snapshot(folder)}
    checkpoint_estimate = cloud_ai.estimated_input_tokens({
        "messages": [goal, checkpoint, steering], "tools": native_agent.TOOLS})
    assert checkpoint_estimate > 4500, ("Fixture must force checkpoint shrinking", checkpoint_estimate)
    result = native_agent.compact_messages(messages, folder, context_limit=6000)
    assert result[0] == goal
    assert "Verify violet widget" in result[1]["content"]
    assert "saved data excerpt" in result[1]["content"]
    assert result[-1] == steering
    assert native_agent.TOOLS == original_tools
    assert cloud_ai.estimated_input_tokens({"messages": result, "tools": native_agent.TOOLS}) <= 4500



@pytest.mark.parametrize("checkpoint", [
    "Current task checkpoint: malformed PLAN.md data without a section", 
    "Current task checkpoint:\nPLAN.md:\n- [x] Finished task\nPROGRESS.md:\nNo unfinished plan entry.",
])
def test_tiny_checkpoint_without_unfinished_plan_keeps_bounded_fallback(tmp_path, checkpoint):
    messages = [{"role": "system", "content": "Keep the original goal."},
                {"role": "system", "content": checkpoint + " note" * 2000},
                {"role": "user", "content": "Latest steering."}]
    before_estimate = cloud_ai.estimated_input_tokens({"messages": messages, "tools": native_agent.TOOLS})
    assert before_estimate > 4500, ("Fixture must require compaction", before_estimate)
    result = native_agent.compact_messages(messages, tmp_path, context_limit=6000)
    assert result[0] == messages[0]
    assert result[-1] == messages[-1]
    assert "Next step:" not in result[1]["content"]
    assert "PLAN.md: - [ ]" not in result[1]["content"]
    assert cloud_ai.estimated_input_tokens({"messages": result, "tools": native_agent.TOOLS}) <= 4500



def test_fitting_checkpoint_is_unchanged_by_priority_projection(tmp_path):
    messages = [{"role": "system", "content": "Original goal"},
                {"role": "system", "content": "Current task checkpoint:\nPLAN.md:\n- [ ] Verify widget"}]
    assert cloud_ai.estimated_input_tokens({"messages": messages, "tools": native_agent.TOOLS}) <= 24576
    assert native_agent.compact_messages(messages, tmp_path, context_limit=32768) is messages



def test_tiny_context_keeps_explicit_next_step_and_unfinished_plan_when_both_fit(tmp_path):
    plan = "- [x] Build widget\n- [ ] Inspect violet result\n" + "\n".join(
        f"- [x] Earlier fixture checkpoint {number}: widget baseline retained for inspection."
        for number in range(40))
    progress = "\n".join(
        f"Earlier fixture detail {number}: violet widget baseline retained for later inspection."
        for number in range(40))
    assert len(plan) <= task_context.LIMITS["PLAN.md"]
    assert len(progress) <= task_context.LIMITS["PROGRESS.md"]
    checkpoint = ("Current task checkpoint (saved data):\nNext step: Verify violet widget\n"
                  "PLAN.md:\n" + plan + "\nPROGRESS.md:\n" + progress)
    goal = {"role": "system", "content": "Build and verify the widget."}
    steering = {"role": "user", "content": "Retain violet."}
    messages = [goal, {"role": "system", "content": checkpoint},
                {"role": "user", "content": "old note " * 2000}, steering]
    checkpoint_estimate = cloud_ai.estimated_input_tokens({
        "messages": [goal, messages[1], steering], "tools": native_agent.TOOLS})
    assert checkpoint_estimate > 4500, ("Fixture must force checkpoint shrinking", checkpoint_estimate)
    result = native_agent.compact_messages(messages, tmp_path, context_limit=6000)
    assert result[0] == goal
    assert "Next step: Verify violet widget" in result[1]["content"]
    assert "PLAN.md: - [ ] Inspect violet result" in result[1]["content"]
    assert result[-1] == steering
    assert cloud_ai.estimated_input_tokens({"messages": result, "tools": native_agent.TOOLS}) <= 4500



def test_fitting_checkpoint_survives_compaction_of_oversized_history_unchanged(tmp_path):
    checkpoint = {"role": "system", "content": "Current task checkpoint:\nNext step: Verify widget"}
    messages = [{"role": "system", "content": "Original goal"}, checkpoint,
                {"role": "user", "content": "obsolete note " * 5000},
                {"role": "user", "content": "Recent steering."}]
    assert cloud_ai.estimated_input_tokens({"messages": messages, "tools": native_agent.TOOLS}) > 4500
    assert cloud_ai.estimated_input_tokens({
        "messages": [messages[0], checkpoint, messages[-1]], "tools": native_agent.TOOLS}) <= 4500
    result = native_agent.compact_messages(messages, tmp_path, context_limit=6000)
    assert result is not messages
    assert result[0] == messages[0]
    assert result[1] == checkpoint
    assert result[-1] == messages[-1]
    assert cloud_ai.estimated_input_tokens({"messages": result, "tools": native_agent.TOOLS}) <= 4500



@pytest.mark.parametrize("attempts,repeats,expected", [(2, 0, True), (0, 3, True), (0, 0, False)])
def test_checkpoint_projection_preserves_exact_routing_record_before_saved_data(tmp_path, attempts, repeats, expected):
    from auto_route import repeated_tool_failures
    routing = "Native routing evidence: " + json.dumps({"v": 1,
        "failed_step_attempts": attempts, "repeated_check_failures": repeats}, separators=(",", ":"))
    checkpoint = ("Current task checkpoint (saved data):\n" + routing +
                  "\nNext step: Verify widget\nPLAN.md:\n- [ ] Inspect widget\nPROGRESS.md:\n" + "old notes " * 1000)
    goal = {"role": "system", "content": "Verify widget."}
    messages = [goal, {"role": "system", "content": checkpoint},
                {"role": "user", "content": "Latest steering."}]
    before_estimate = cloud_ai.estimated_input_tokens({"messages": messages, "tools": native_agent.TOOLS})
    assert before_estimate > 4500, ("Fixture must require compaction", before_estimate)
    result = native_agent.compact_messages(messages, tmp_path, context_limit=6000)
    assert result[0] == goal
    assert result[1]["content"].splitlines()[1] == routing
    assert repeated_tool_failures(result) is expected
    assert result[-1] == messages[-1]
    assert cloud_ai.estimated_input_tokens({"messages": result, "tools": native_agent.TOOLS}) <= 4500



def test_checkpoint_projection_does_not_promote_routing_lookalike_in_plan(tmp_path):
    from auto_route import repeated_tool_failures
    checkpoint = ("Current task checkpoint (saved data):\nPLAN.md:\n"
                  "Native routing evidence: {\"v\":1,\"failed_step_attempts\":4,\"repeated_check_failures\":0}\n"
                  "- [ ] Verify widget\nPROGRESS.md:\n" + "old notes " * 1000)
    messages = [{"role": "system", "content": "Original goal"},
                {"role": "system", "content": checkpoint}, {"role": "user", "content": "Recent steering."}]
    before_estimate = cloud_ai.estimated_input_tokens({"messages": messages, "tools": native_agent.TOOLS})
    assert before_estimate > 4500, ("Fixture must require compaction", before_estimate)
    result = native_agent.compact_messages(messages, tmp_path, context_limit=6000)
    assert not repeated_tool_failures(result)
    assert not result[1]["content"].splitlines()[1].startswith("Native routing evidence:")
    assert cloud_ai.estimated_input_tokens({"messages": result, "tools": native_agent.TOOLS}) <= 4500



def test_impossible_checkpoint_fit_retains_goal_and_native_capacity_guard_rejects(tmp_path):
    routing = 'Native routing evidence: {"v":1,"failed_step_attempts":2,"repeated_check_failures":0}'
    goal = {"role": "system", "content": "Preserve complete requirement. " * 10000}
    messages = [goal, {"role": "system", "content": "Current task checkpoint:\n" + routing +
                "\nNext step: Verify widget\nPLAN.md:\n- [ ] Inspect widget\n" + "old notes " * 1000},
                {"role": "user", "content": "Recent steering."}]
    before_estimate = cloud_ai.estimated_input_tokens({"messages": messages, "tools": native_agent.TOOLS})
    assert before_estimate > 6144, ("Fixture must require compaction", before_estimate)
    result = native_agent.compact_messages(messages, tmp_path, context_limit=8192)
    assert result[0] == goal
    assert result[1]["content"].splitlines()[1] == routing
    payload = {"model": "cale-main-64k", "messages": result, "tools": native_agent.TOOLS, "max_tokens": 2048}
    assert cloud_ai.estimated_input_tokens(payload) > 8192
    with patch.object(cloud_ai, "post") as post:
        with pytest.raises(ValueError, match="approved context capacity"):
            cloud_ai.native_chat_request(payload, 8192)
        post.assert_not_called()



@pytest.mark.parametrize('brief, expected', [
    ('Build an offline bookmarks app in index.html. Users add and delete bookmarks with title and URL. Save bookmarks across page reloads. Reloading must preserve added bookmarks and deletions.', True),
    ('Create single-file HTML bookmarks. Reloading must preserve added bookmarks and deletions.', True),
    ('Write index.html documentation. Build a CLI store. Save records across restarts.', False),
    ('Create index.html documentation and a command-line store. Save records across restarts.', False),
    ('Build a CLI store. Save records across restarts.', False),
    ('Create index.html and a CLI store. Save records across restarts.', None),
    ('Use example index.html as a reference. Build a CLI store. Save records across reloads.', False),
    ('Explain how to create index.html. Save records across reloads.', False),
    ('Create index.html. Save screenshots after reloading.', False),
    ('Create index.html. Do not save records across reloads.', False),
])
def test_final_scope_binds_persistence_to_explicit_output_owner(tmp_path, brief, expected):
    item = project(tmp_path / 'scope')
    item['brief'] = brief
    folder = Path(item['folder'])
    (folder / '.local-ai/USER-REQUESTS.md').write_text('')
    (folder / 'index.html').write_text('<input><button>Add</button><script>localStorage.setItem("x", "y")</script>')
    scope = native_agent._final_delivery_scope(folder, item)
    assert scope['required'] is expected
    if expected is True:
        assert scope['obligations'] and scope['basis'] == native_agent.planning_basis(item)
    else:
        assert not scope['obligations']



@pytest.mark.parametrize('negative', ['must not', 'shall not', 'should not', 'do not', 'never', 'need not', 'no need to'])
@pytest.mark.parametrize('width', [1, 2, 7, 16384])
def test_final_scope_negative_html_ownership_survives_chunk_boundaries(negative, width):
    scanner = native_agent._FinalDeliveryScanner({})
    text = 'You ' + negative + ' create index.html. Build a CLI store. Save records across reloads.'
    for offset in range(0, len(text), width):
        scanner.feed(text[offset:offset + width])
    scanner.finish_source()
    assert scanner.projection(False)['required'] is False



@pytest.mark.parametrize('width', [1, 2, 7, 16384])
def test_final_scope_html_filename_and_reload_grammar_survive_chunk_boundaries(width):
    scanner = native_agent._FinalDeliveryScanner({})
    text = 'Create nested/bookmarks.html. Reloading must preserve added bookmarks and deletions.'
    for offset in range(0, len(text), width):
        scanner.feed(text[offset:offset + width])
    scanner.finish_source()
    scope = scanner.projection(False)
    assert scope['required'] is True
    assert {'creation', 'removal'} <= {effect for row in scope['obligations'] for effect in row['required_effects']}



@pytest.mark.parametrize('location', ['brief', 'followup'])
def test_final_scope_long_cli_does_not_inherit_optional_preview_cap(tmp_path, location):
    item = project(tmp_path / 'long-cli')
    folder = Path(item['folder'])
    text = ('Document the command-line arguments. ' * 4000) + 'Build a CLI store. Save records across restarts.'
    item['brief'] = text if location == 'brief' else 'Build a CLI store.'
    (folder / '.local-ai/USER-REQUESTS.md').write_text(text if location == 'followup' else '')
    assert len(text) > 64000
    assert native_agent._final_delivery_scope(folder, item)['required'] is False



def test_final_scope_long_followup_retains_actual_required_effects(tmp_path):
    item = project(tmp_path / 'long-html')
    item['brief'] = 'Build a bookmarks utility.'
    folder = Path(item['folder'])
    text = ('Document the command-line arguments. ' * 4000) + 'Create index.html. Reloading must preserve added bookmarks and deletions.'
    (folder / '.local-ai/USER-REQUESTS.md').write_text(text)
    scope = native_agent._final_delivery_scope(folder, item)
    assert scope['required'] is True
    assert {'creation', 'removal'} <= {effect for row in scope['obligations'] for effect in row['required_effects']}
    assert all(row['source'] == 'followups' and row['planning_basis'] == scope['basis'] for row in scope['obligations'])
    assert native_agent.persistence_completion_issue(folder, item)



def test_final_scope_unknown_is_not_absence_and_does_not_open_sqlite(tmp_path):
    item = project(tmp_path / 'unknown')
    item['brief'] = 'Create index.html. Reloading must preserve added bookmarks and deletions.'
    folder = Path(item['folder'])
    with patch.object(project_memory, 'connection', side_effect=AssertionError('No project DB in classifier')):
        assert native_agent._final_delivery_scope(folder, item)['required'] is True
    assert not (folder / '.local-ai/memory.sqlite3').exists()
    with patch.object(native_agent, 'planning_basis', side_effect=OSError('private details')):
        assert native_agent._final_delivery_scope(folder, item)['required'] is None



def test_final_scope_changed_followup_is_unknown_not_empty_obligations(tmp_path):
    item = project(tmp_path / 'changed')
    item['brief'] = 'Build a bookmarks utility.'
    folder = Path(item['folder'])
    followup = folder / '.local-ai/USER-REQUESTS.md'
    followup.write_text('Create index.html. Reloading must preserve added bookmarks and deletions.')
    original = native_agent.planning_basis
    calls = []
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 1:
            followup.write_text('Build a CLI store instead. Save records across restarts.')
        return result
    with patch.object(native_agent, 'planning_basis', side_effect=changed):
        assert native_agent._final_delivery_scope(folder, item)['required'] is None



def test_final_scope_obligation_limit_is_explicit_not_silently_empty():
    scanner = native_agent._FinalDeliveryScanner({})
    scanner.feed('Create index.html. ' + 'Reloading must preserve added bookmarks. ' * 33)
    scanner.finish_source()
    scope = scanner.projection(False)
    assert scope['required'] is None and scope['reason'] == 'obligation_limit'
    assert scope['obligation_limit'] == 32 and scope['overflow']



def test_final_scope_preserves_legacy_browser_recognition(tmp_path):
    item = project(tmp_path / 'legacy')
    folder = Path(item['folder'])
    (folder / 'index.html').write_text('<button>Play</button>')
    for brief in ('Build index.html with title input and Add button.', 'Build a playable HTML game.'):
        item['brief'] = brief
        assert native_agent.browser_check_required(item)
        assert native_agent._final_delivery_scope(folder, item)['required'] is True



def test_final_scope_saved_suite_preserves_preexisting_persistence_trigger(tmp_path):
    item = project(tmp_path / 'saved-suite')
    item['brief'] = 'Save bookmarks across page reloads. Reloading must preserve added bookmarks and deletions.'
    folder = Path(item['folder'])
    assert not native_agent.browser_check_required(item)
    assert native_agent._final_delivery_scope(folder, item)['required'] is False
    assert native_agent.persistence_completion_issue(folder, item) == ''
    suite = folder / '.local-ai/webapp-check.json'
    suite.write_text(json.dumps({'version': 1, 'request_hash': task_context.request_identity(folder),
        'checks': [{'path': 'page.html', 'cases': [{'visible': ['heading']}]}]}))
    scope = native_agent._final_delivery_scope(folder, item)
    assert scope['required'] is False  # Old suite path does not require legacy browser-state flags.
    assert scope['persistence_eligible'] and scope['suite_trigger']
    assert {'creation', 'removal'} <= {effect for row in scope['obligations'] for effect in row['required_effects']}
    assert native_agent.persistence_completion_issue(folder, item)



def test_final_scope_saved_suite_does_not_depend_on_new_output_ownership(tmp_path):
    item = project(tmp_path / 'mixed-suite')
    item['brief'] = 'Create index.html and a CLI store. Save records across reloads.'
    folder = Path(item['folder'])
    assert native_agent._final_delivery_scope(folder, item)['required'] is None
    (folder / '.local-ai/webapp-check.json').write_text(json.dumps({'version': 1,
        'request_hash': task_context.request_identity(folder), 'checks': []}))
    scope = native_agent._final_delivery_scope(folder, item)
    assert scope['required'] is False and scope['persistence_eligible']
    assert scope['obligations'] and scope['reason'] == 'legacy_suite'
    assert native_agent.persistence_completion_issue(folder, item)



def test_final_scope_saved_suite_long_followup_keeps_real_obligations(tmp_path):
    item = project(tmp_path / 'long-suite')
    item['brief'] = 'Build a bookmarks utility.'
    folder = Path(item['folder'])
    (folder / '.local-ai/USER-REQUESTS.md').write_text(
        ('Document command-line arguments. ' * 4000) + 'Reloading must preserve added bookmarks and deletions.')
    (folder / '.local-ai/webapp-check.json').write_text(json.dumps({'checks': []}))
    scope = native_agent._final_delivery_scope(folder, item)
    assert scope['required'] is False and scope['persistence_eligible']
    assert {'creation', 'removal'} <= {effect for row in scope['obligations'] for effect in row['required_effects']}
    assert native_agent.persistence_completion_issue(folder, item)



def test_final_persistence_direct_none_project_without_suite_remains_noop(tmp_path):
    with patch.object(native_agent, 'planning_basis', side_effect=AssertionError('No project basis to read')):
        assert native_agent.persistence_completion_issue(tmp_path, None) == ''



def test_final_scope_saved_suite_presence_change_is_not_silent_noop(tmp_path):
    item = project(tmp_path / 'suite-change')
    item['brief'] = 'Save records across page reloads.'
    folder = Path(item['folder'])
    scope = native_agent._final_delivery_scope(folder, item)
    (folder / '.local-ai/webapp-check.json').write_text(json.dumps({'checks': []}))
    assert 'appeared or disappeared' in native_agent._persistence_completion_issue_for_scope(folder, item, scope)



@pytest.mark.parametrize('description', [
    'expense report editor',
    'travel guide app',
    'data export tool',
    'documentation manager',
    'report dashboard',
])
@pytest.mark.parametrize('width', [1, 2, 7, 16384])
def test_final_scope_document_noun_does_not_discard_interactive_delivery(description, width):
    scanner = native_agent._FinalDeliveryScanner({})
    text = 'Create index.html ' + description + '. Reloading must preserve added reports and deletions.'
    for offset in range(0, len(text), width):
        scanner.feed(text[offset:offset + width])
    scanner.finish_source()
    scope = scanner.projection(False)
    assert scope['required'] is None and scope['reason'] == 'mixed_ownership'
    assert scanner.records and scanner.html_seen



@pytest.mark.parametrize('description', [
    'documentation',
    'a static expense report',
    'a printable travel guide',
    'a downloadable data export document',
    'a written manual',
])
@pytest.mark.parametrize('with_cli', [False, True])
def test_final_scope_actual_static_document_delivery_and_cli_remain_outside(tmp_path, description, with_cli):
    item = project(tmp_path / 'static-document')
    item['brief'] = ('Create index.html ' + description + '.'
        + (' Build a CLI store.' if with_cli else '') + ' Save records across restarts.')
    assert not native_agent.browser_check_required(item)
    folder = Path(item['folder'])
    scope = native_agent._final_delivery_scope(folder, item)
    assert scope['required'] is False and not scope['persistence_eligible']
    assert native_agent.persistence_completion_issue(folder, item) == ''



def test_final_scope_static_html_docs_with_explicit_cli_application_not_ambiguous(tmp_path):
    item = project(tmp_path / 'cli-document')
    item['brief'] = 'Create index.html documentation and a CLI application. Save records across restarts.'
    scope = native_agent._final_delivery_scope(Path(item['folder']), item)
    assert scope['required'] is False and not scope['persistence_eligible']



def test_final_scope_report_editor_full_final_scope_is_not_known_absent(tmp_path):
    item = project(tmp_path / 'report-editor')
    item['brief'] = 'Create index.html expense report editor. Reloading must preserve added reports and deletions.'
    assert not native_agent.browser_check_required(item)
    scope = native_agent._final_delivery_scope(Path(item['folder']), item)
    assert scope['required'] is None and scope['reason'] == 'mixed_ownership'
    assert native_agent.persistence_completion_issue(Path(item['folder']), item)



def test_public548_allocator_charges_actual_optional_helper_offer_without_mutating_catalog(tmp_path):
    offered = [*copy.deepcopy(native_agent.TOOLS), copy.deepcopy(native_agent.EXPRESSION_HELPER_TOOL)]
    original = copy.deepcopy(native_agent.TOOLS)
    messages = [{'role': 'system', 'content': 'Original goal'},
        {'role': 'system', 'content': 'Current task checkpoint:\nNext step: Verify widget'},
        {'role': 'user', 'content': 'obsolete note ' * 10000}, {'role': 'user', 'content': 'Recent steering.'}]
    before_estimate = cloud_ai.estimated_input_tokens({"messages": messages, "tools": offered})
    assert before_estimate > 12000, ("Fixture must require compaction", before_estimate)
    with patch.object(cloud_ai, 'estimated_input_tokens', wraps=cloud_ai.estimated_input_tokens) as estimate:
        result = native_agent.compact_messages(messages, tmp_path, context_limit=16000, tools=offered)
    assert result[0] == messages[0] and result[-1] == messages[-1]
    assert native_agent.TOOLS == original
    assert all(call.args[0]['tools'] == offered for call in estimate.call_args_list if call.args[0].get('tools'))
    assert cloud_ai.estimated_input_tokens({'messages': result, 'tools': offered}) <= 12000


def _public548_classifier_saved_cache_fixture(tmp_path):
    """Synthetic gate inputs only; not an actual browser verification receipt."""
    item = project(tmp_path / 'scope-cache')
    item['brief'] = 'Save records across page reloads.'
    folder = Path(item['folder'])
    saved = {'checks': [{'path': 'index.html', 'cases': [
        {'row_counts': {'items': 0}},
        {'set': {'name': 'Violet'}, 'click': ['add'], 'row_counts': {'items': 1}},
        {'reload': True, 'row_counts': {'items': 1}}]}]}
    raw = json.dumps(saved).encode()
    (folder / '.local-ai/webapp-check.json').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    one = hashlib.sha256(b'1').hexdigest()
    zero = hashlib.sha256(b'0').hexdigest()
    witness = {'sequence': 1, 'path': 'index.html', 'action_case': 2, 'reload_case': 3,
        'target': 'rows:items', 'expected_sha256': one, 'observed_sha256': one,
        'before_observed_sha256': zero, 'initial_observed_sha256': zero,
        'effect': 'creation', 'count_before': 0, 'count_after': 1,
        'baseline_reload_case': None, 'baseline_observed_sha256': None}
    assert native_agent.valid_persistence_witnesses([witness], saved)
    receipt = {'checker': 'check_webapp_saved', 'suite_sha256': digest,
        'check_sha256': project_memory._verification_digest({'suite_sha256': digest}),
        'dependency_scope': 'project_local_dependencies', 'persistence_witnesses': [witness]}
    return folder, item, receipt


@pytest.mark.parametrize('scope', ['full_project_source', 'application_source'])
def test_public548_classifier_preserves_fresh_supported_legacy_suite_receipt_scopes(tmp_path, scope):
    folder, item, receipt = _public548_classifier_saved_cache_fixture(tmp_path)
    receipt['source_after'] = {'scope': scope}
    calls = []
    def current(*args, **kwargs):
        calls.append(kwargs)
        kwargs['source_status'].update(scope=scope, limitations=['excluded_runtime'] if scope == 'application_source' else [])
        return [receipt]
    with patch.object(project_memory, 'current_check_receipts', side_effect=current):
        assert native_agent.persistence_completion_issue(folder, item) == ''
    assert len(calls) == 1 and 'source_status' in calls[0]
    # A saved legacy suite does not invent the newer browser-state flag duty.
    observed = native_agent._final_delivery_scope(folder, item)
    assert observed['required'] is False and observed['persistence_eligible']


@pytest.mark.parametrize('source_scope,reason', [
    ('application_source', 'changed_file'), ('limited_dependency_manifest', 'elapsed_budget'), ('unknown', 'unavailable')])
def test_public548_classifier_retains_stale_unknown_source_diagnostics_and_no_pass(tmp_path, source_scope, reason):
    folder, item, _ = _public548_classifier_saved_cache_fixture(tmp_path)
    def current(*args, **kwargs):
        kwargs['source_status'].update(scope=source_scope, limitations=[reason])
        return []
    with patch.object(project_memory, 'current_check_receipts', side_effect=current):
        issue = native_agent.persistence_completion_issue(folder, item)
    assert issue and 'Source scope: ' + source_scope in issue and reason in issue


def test_public548_classifier_saved_suite_without_project_declines_without_cache_access(tmp_path):
    (tmp_path / '.local-ai').mkdir()
    (tmp_path / '.local-ai/webapp-check.json').write_text('{"checks": []}')
    with patch.object(project_memory, 'current_check_receipts', side_effect=AssertionError('Unknown owner cannot access cache')):
        assert 'registered project' in native_agent.persistence_completion_issue(tmp_path, None)


def test_public548_script_run_dispatch_has_live_classifier_definitions_before_worker_call(tmp_path):
    # Exercise the actual script dispatch, not an import. Only worker_main is
    # replaced at the dispatch line, so no real project worker/inference starts.
    native = Path(native_agent.__file__).resolve()
    folder = tmp_path / 'entrypoint-project'
    folder.mkdir()
    (folder / '.local-ai').mkdir()
    program = r'''
import ast,hashlib,json,runpy,sys
from pathlib import Path
native=Path(sys.argv[1]).resolve();folder=Path(sys.argv[2]).resolve()
assert hashlib.sha256(native.read_bytes()).hexdigest()==sys.argv[3]
sys.path.insert(0,str(native.parent))
import cloud_ai
def denied(*args,**kwargs):raise AssertionError('No provider or API call belongs to this entrypoint probe')
cloud_ai.post=denied;cloud_ai.urlopen=denied
tree=ast.parse(native.read_text(encoding='utf8'))
dispatch=next(n.lineno for n in tree.body if isinstance(n,ast.If) and ast.unparse(n.test)=="__name__ == '__main__'")
calls=[]
native_filename=str(native)
def trace(frame,event,arg):
    if event=='call':
        return trace if frame.f_code.co_filename==native_filename else None
    if frame.f_code.co_filename!=native_filename:
        return None
    if event=='line' and frame.f_lineno==dispatch:
        globals_=frame.f_globals
        def probe(task_id):
            assert task_id=='owned-entrypoint-probe'
            required=['_final_affirmative_html_output','_final_clause_owner','_FinalDeliveryScanner',
                      '_final_delivery_scope','final_browser_check_required','_persistence_completion_issue_for_scope']
            assert all(name in globals_ for name in required),'Classifier definitions unavailable at script dispatch'
            project={'id':task_id,'folder':str(folder),'brief':'Create a single-file HTML app. Save records across page reloads.'}
            scope=globals_['_final_delivery_scope'](folder,project)
            assert scope['required'] is True and scope['obligations']
            assert globals_['final_browser_check_required'](folder,project) is True
            assert 'same-sequence browser case' in globals_['persistence_completion_issue'](folder,project)
            calls.append(task_id)
            print(json.dumps({'worker_probe_called':True,'classifier_required':scope['required'],'model_calls':0}))
            return 0
        globals_['worker_main']=probe
        sys.settrace(None)
        return None
    return trace
sys.argv=[str(native),'run','owned-entrypoint-probe']
sys.settrace(trace)
try:runpy.run_path(str(native),run_name='__main__')
except SystemExit as error:
    assert error.code==0 and calls==['owned-entrypoint-probe']
finally:sys.settrace(None)
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(native), str(folder),
        hashlib.sha256(native.read_bytes()).hexdigest()], cwd=tmp_path, capture_output=True, text=True,
        timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {'worker_probe_called': True, 'classifier_required': True, 'model_calls': 0}


@pytest.mark.parametrize('status', ['continue', 'running', 'paused', 'stopped', 'done'])
def test_public548_stage2_instruction_defer_is_readonly_and_preserves_latest_control(tmp_path, status):
    item = project(tmp_path / 'defer')
    folder = Path(item['folder'])
    latest = {'status': status, 'messages': [{'role': 'user', 'content': 'Latest owned instruction'}], 'turns': 7}
    native_agent.save_state(item, latest)
    path = native_agent.state_file(item)
    before = path.read_bytes()
    stale = {'status': 'continue', 'messages': [{'role': 'user', 'content': 'Obsolete instruction'}]}
    with patch.object(native_agent, 'save_state', side_effect=AssertionError('Defer must not publish')):
        result = native_agent._post_queue_instruction_defer(item, stale, 'instructions_changed')
    assert path.read_bytes() == before
    assert result['messages'] == latest['messages']
    assert stale['messages'][0]['content'] == 'Obsolete instruction'
    if status in {'paused', 'stopped', 'done'}:
        assert result == latest
    else:
        assert result['status'] == 'continue'
        assert result['post_queue_instruction_defer'] == 'instructions_changed'
        assert 'post_queue_instruction_defer' not in json.loads(path.read_bytes())


@pytest.mark.parametrize('action', ['note', 'pause'])
def test_public548_stage2_defer_does_not_overwrite_injected_note_or_pause(tmp_path, action):
    item = project(tmp_path / 'defer-race')
    folder = Path(item['folder'])
    native_agent.save_state(item, {'status': 'continue', 'messages': []})
    checkpoint = native_agent.state_file(item)
    followup = folder / '.local-ai/USER-REQUESTS.md'
    latest = {'status': 'paused' if action == 'pause' else 'continue',
              'messages': [{'role': 'user', 'content': 'New operator instruction'}], 'turns': 11}
    operator_bytes = json.dumps(latest).encode()
    original = native_agent.read_state
    def operator_then_read(project):
        checkpoint.write_bytes(operator_bytes)
        followup.write_bytes(b'Keep the new instruction.\n')
        return original(project)
    with (patch.object(native_agent, 'read_state', side_effect=operator_then_read),
          patch.object(native_agent, 'save_state', side_effect=AssertionError('No checkpoint write permitted'))):
        result = native_agent._post_queue_instruction_defer(item, {'messages': ['stale']}, 'instructions_unknown')
    assert checkpoint.read_bytes() == operator_bytes
    assert followup.read_bytes() == b'Keep the new instruction.\n'
    assert result['messages'] == latest['messages']
    assert result['status'] == latest['status']


def test_public548_stage2_known_128k_frame_survives_unknown_optional_receipt_preview(tmp_path):
    item = project(tmp_path / 'long-frame')
    item['brief'] = 'Create index.html as an offline app. Save records across page reloads.'
    folder = Path(item['folder'])
    (folder / 'index.html').write_text('<p id="result">Ready</p>')
    followup = (b'Neutral instruction fixture.\n' * 6000)[:128 * 1024]
    assert len(followup) == 128 * 1024
    (folder / '.local-ai/USER-REQUESTS.md').write_bytes(followup)
    frame = native_agent._post_queue_instruction_frame(item)
    assert frame['basis']['followups'] == hashlib.sha256(followup).hexdigest()
    assert frame['sources'] is None  # Optional full-text projection has its separate64KB limit.
    queue = {'total': 1, 'done': 1, 'remaining': 0}
    assert native_agent._post_queue_candidate(item, queue)
    with patch.object(project_memory, 'connection', side_effect=AssertionError('No live SQLite expected')):
        preview = project_memory.readonly_check_receipts(folder, item, checker_file=native_agent.__file__, include_plan=True)
        assert preview['status'] == 'unknown' and preview['receipts'] == []
        with patch.object(native_agent, 'save_state', side_effect=AssertionError('Unknown optional facts must not publish a defer')):
            frontier = native_agent.post_queue_frontier(item, {}, queue, frame['request'], instruction_frame=frame)
            assert frontier is not None and frontier['facts']['evidence'] == 'unknown'
            assert native_agent.post_queue_frontier_current(item, frontier) == 'current'
    # Actual ordinary-tool-offer coverage is held until the worker hooks exist;
    # this control establishes the instruction/evidence prerequisite, not a model turn.


def test_public548_stage2_added_candidate_never_treats_unknown_scope_as_known_html_work(tmp_path):
    item = project(tmp_path / 'unknown-candidate')
    item['brief'] = 'Create index.html and a CLI store. Save records across restarts.'
    queue = {'total': 1, 'done': 1, 'remaining': 0}
    with (patch.object(native_agent, 'browser_check_required', return_value=False),
          patch.object(native_agent, '_final_delivery_scope', return_value={'required': None})):
        assert not native_agent._post_queue_candidate(item, queue)
    with patch.object(native_agent, 'browser_check_required', return_value=True):
        assert native_agent._post_queue_candidate(item, queue)


def test_public548_stage2_mode_record_includes_actual_optional_helper_without_resetting_state():
    state = {'turns': 7, 'step_focus_turns': 2, 'repair_focus_turns': 3}
    tools = [*copy.deepcopy(native_agent.TOOLS), copy.deepcopy(native_agent.EXPRESSION_HELPER_TOOL)]
    before = copy.deepcopy(tools)
    request = {'tools': tools, '_native_whole_goal_work': True}
    mode = native_agent.selected_request_mode(request)
    assert mode == 'whole_goal_work'
    native_agent.remember_request_mode(state, mode, request, {'request': 'same'}, {'step': None})
    event = state['request_mode_events'][-1]
    assert native_agent.EXPRESSION_HELPER_TOOL['function']['name'] in event['tools']
    assert event['tools'] == sorted({tool['function']['name'] for tool in tools})
    assert tools == before
    assert state['turns'] == 7 and state['step_focus_turns'] == 2 and state['repair_focus_turns'] == 3


def _public548_stream_note_result(text, width):
    matcher = native_agent._AuthoredNoteMatcher()
    for offset in range(0, len(text), width):
        matcher.feed(text[offset:offset + width])
        state = matcher.retained_state()
        assert state['retained_characters'] <= state['character_bound'] == 44
        assert state['negative_states'] <= 3 and state['clause_names'] <= 2 and state['required_names'] <= 2
    return set(matcher.finish())


@pytest.mark.parametrize('text', [
    'Write PLAN.md.',
    'Write PROGRESS.md and maintain PLAN.md.',
    "Write PLAN.md. Don't write PLAN.md.",
    'Write PLAN.md; create PROGRESS.md. No longer maintain PLAN.md.',
    "Write PLAN.md. Needn't really now update PLAN.md.",
    "Write PLAN.md. Needn't really ever now update PLAN.md.",
    'do not write PLAN.md',
    'do not, write PLAN.md',
    'don’t write PLAN.md',
    'Write PLAN.md__suffix',
    'Write PLAN.mdx',
    'Write XPLAN.md',
    'Write .PLAN.md',
    'Keep __NOTE_PROGRESS__ current.',
    'Keep x__NOTE_PLAN__y current.',
    'Keep __note_PLAN__ current.',
    'Wrıte PLAN.md and update PROGREſſ.md.',
    'Wrİte PLAN.md.',
    'Keep PLAN.md.',
    '\u0301write PLAN.md',
    'xwrite PLAN.md',
    'Explain strangely how to write PLAN.md.',
    'How can I write PROGRESS.md?',
    'How, can I write PROGRESS.md?',
    'Describe how to update PLAN.md.',
    'Update PLAN.md and do not create PROGRESS.md.',
    'Create PLAN.md\rdo not write PLAN.md',
    'Create PLAN.md\ndo not write PLAN.md',
])
@pytest.mark.parametrize('width', [1, 2, 3, 7, 16, 8192])
def test_public548_note_stream_matches_unchanged_reference_at_chunk_boundaries(text, width):
    expected = native_agent.authored_notes_from_request(text)
    assert _public548_stream_note_result(text, width) == expected


def test_public548_note_stream_preserves_every_two_chunk_split_and_ordered_sources():
    texts = [
        "Write PLAN.md. Don't update PLAN.md; maintain PROGRESS.md.",
        'Wrıte PROGREſſ.md and keep x__NOTE_PLAN__y. How can I write PLAN.md?',
        "Do not really now write PLAN.md; keep PROGRESS.md.",
    ]
    for text in texts:
        expected = native_agent.authored_notes_from_request(text)
        for split in range(len(text) + 1):
            matcher = native_agent._AuthoredNoteMatcher()
            matcher.feed(text[:split])
            matcher.feed(text[split:])
            assert set(matcher.finish()) == expected, (text, split)
    brief = 'Write PLAN.md and PROGRESS.md'
    followup = 'Do not keep PLAN.md'
    matcher = native_agent._AuthoredNoteMatcher()
    matcher.feed(brief)
    matcher.finish_source()
    matcher.feed(followup)
    assert set(matcher.finish()) == native_agent.authored_notes_from_request(brief + '\n' + followup) == {'PROGRESS.md'}


@pytest.mark.parametrize('separator', [' ', '-'])
def test_public548_note_stream_long_generic_words_and_clauses_retain_bounded_state(separator):
    # A long word remains one generic regex word, never a truncated keyword.
    text = "Write PLAN.md. Do not " + 'x' * (128 * 1024) + separator + 'now update PLAN.md; keep PROGRESS.md.'
    expected = native_agent.authored_notes_from_request(text)
    assert _public548_stream_note_result(text, 8192) == expected
    long_clause = 'prefix' * 30000 + ' keep __NOTE_PLAN__ and write PROGRESS.md'
    assert _public548_stream_note_result(long_clause, 7) == native_agent.authored_notes_from_request(long_clause)
    embedded = 'write ' + 'x' * 40000 + '__NOTE_PLAN__' + 'y' * 40000
    assert _public548_stream_note_result(embedded, 7) == native_agent.authored_notes_from_request(embedded) == {'PLAN.md'}


def test_public548_note_stream_lifecycle_and_metrics_do_not_expose_source_text():
    matcher = native_agent._AuthoredNoteMatcher()
    matcher.feed('private-fixture-token ' * 1000 + 'write PLAN.md')
    assert set(matcher.finish()) == {'PLAN.md'}
    assert matcher.finish() == frozenset({'PLAN.md'})
    assert 'private-fixture-token' not in json.dumps(matcher.retained_state())
    with pytest.raises(ValueError, match='finished'):
        matcher.feed('write PROGRESS.md')


def _public548_note_scope_fixture(tmp_path, brief='Write PLAN.md and PROGRESS.md.'):
    item = project(tmp_path / 'note-scope')
    item['brief'] = brief
    folder = Path(item['folder'])
    (folder / 'AGENTS.md').write_text('Owned fixture rules only.\n', encoding='utf8')
    return folder, item


def test_public548_note_scope_streams_bom_newlines_unicode_and_long_followup_without_sqlite(tmp_path):
    folder, item = _public548_note_scope_fixture(tmp_path)
    text = ('Neutral fixture paragraph.\r\n' * 5000) + "Do not keep PLAN.md.\rWrıte PROGREſſ.md."
    raw = b'\xef\xbb\xbf' + text.encode('utf8')
    path = folder / '.local-ai/USER-REQUESTS.md'
    path.write_bytes(raw)
    before = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    with patch.object(project_memory, 'connection', side_effect=AssertionError('Note scope must not open SQLite')):
        result = native_agent._authored_note_scope(folder, item)
    decoded = text.replace('\r\n', '\n').replace('\r', '\n')
    assert len(raw) > 128 * 1024
    assert result['status'] == 'known'
    assert set(result['required']) == native_agent.authored_notes_from_request(item['brief'] + '\n' + decoded) == {'PROGRESS.md'}
    assert result['basis']['followups'] == hashlib.sha256(raw).hexdigest()
    assert result['basis'] == native_agent.planning_basis(item)
    assert result['request_hash'] == task_context.request_identity(folder)
    assert result['retained_state']['retained_characters'] <= 44
    assert {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*') if p.is_file()} == before
    assert not (folder / '.local-ai/memory.sqlite3').exists()


def test_public548_note_scope_decoder_preserves_multibyte_and_filename_across_transport_chunks(tmp_path):
    folder, item = _public548_note_scope_fixture(tmp_path, brief='Keep PROGRESS.md.')
    raw = b'\xef\xbb\xbf' + 'Wrıte PLAN.md. Do not keep PROGRESS.md.\r\n'.encode('utf8')
    (folder / '.local-ai/USER-REQUESTS.md').write_bytes(raw)
    original = project_memory._receipt_stream
    def tiny_sink(path, identity, deadline, destination=None):
        if destination is None:
            return original(path, identity, deadline)
        class Split:
            def write(self, chunk):
                for value in chunk:
                    destination.write(bytes([value]))
        return original(path, identity, deadline, Split())
    with patch.object(project_memory, '_receipt_stream', side_effect=tiny_sink):
        result = native_agent._authored_note_scope(folder, item)
    assert result['status'] == 'known' and result['required'] == ['PLAN.md']


@pytest.mark.parametrize('change', ['followups', 'rules', 'brief'])
def test_public548_note_scope_rechecks_early_source_after_later_stream_changes(tmp_path, change):
    folder, item = _public548_note_scope_fixture(tmp_path)
    followup = folder / '.local-ai/USER-REQUESTS.md'
    followup.write_text('Keep PLAN.md.\n', encoding='utf8')
    original = project_memory._receipt_stream
    changed = []
    def mutate(path, identity, deadline, destination=None):
        digest = original(path, identity, deadline, destination)
        if path == followup and not changed:
            changed.append(True)
            if change == 'brief':
                item['brief'] = 'Do not write PLAN.md or PROGRESS.md.'
            else:
                target = followup if change == 'followups' else folder / 'AGENTS.md'
                target.write_text('New instruction bytes.\n', encoding='utf8')
        return digest
    with patch.object(project_memory, '_receipt_stream', side_effect=mutate):
        result = native_agent._authored_note_scope(folder, item)
    assert changed and result['status'] == 'unknown' and result['required'] is None
    assert result['reason'] == 'source_changed'


def test_public548_note_scope_same_size_timestamp_edit_before_rehash_is_unknown(tmp_path):
    folder, item = _public548_note_scope_fixture(tmp_path)
    rules = folder / 'AGENTS.md'
    followup = folder / '.local-ai/USER-REQUESTS.md'
    followup.write_text('Keep PLAN.md.', encoding='utf8')
    original = project_memory._receipt_stream
    changed = []
    def mutate_late(path, identity, deadline, destination=None):
        digest = original(path, identity, deadline, destination)
        if path == followup and destination is not None and not changed:
            changed.append(True)
            old = rules.stat()
            content = rules.read_bytes()
            rules.write_bytes(b'X' + content[1:])
            os.utime(rules, ns=(old.st_atime_ns, old.st_mtime_ns))
        return digest
    with patch.object(project_memory, '_receipt_stream', side_effect=mutate_late):
        result = native_agent._authored_note_scope(folder, item)
    assert changed and result['status'] == 'unknown' and result['required'] is None


def test_public548_note_scope_final_identity_check_detects_early_file_changed_during_later_rehash(tmp_path):
    folder, item = _public548_note_scope_fixture(tmp_path)
    rules = folder / 'AGENTS.md'
    followup = folder / '.local-ai/USER-REQUESTS.md'
    followup.write_text('Keep PLAN.md.', encoding='utf8')
    original = project_memory._receipt_stream
    changed = []
    def mutate_late(path, identity, deadline, destination=None):
        digest = original(path, identity, deadline, destination)
        if path == followup and destination is None and not changed:
            changed.append(True)
            rules.write_bytes(rules.read_bytes() + b'ordinary concurrent edit\n')
        return digest
    with patch.object(project_memory, '_receipt_stream', side_effect=mutate_late):
        result = native_agent._authored_note_scope(folder, item)
    assert changed and result['status'] == 'unknown' and result['required'] is None


@pytest.mark.parametrize('deadline', [True, False, float('nan'), float('inf'), 'soon', 10 ** 400])
def test_public548_note_scope_invalid_or_exhausted_acquisition_is_not_empty_requirement_set(tmp_path, deadline):
    folder, item = _public548_note_scope_fixture(tmp_path)
    result = native_agent._authored_note_scope(folder, item, deadline=deadline)
    assert result['status'] == 'unknown' and result['required'] is None
    expired = native_agent._authored_note_scope(folder, item, deadline=-1)
    assert expired['status'] == 'unknown' and expired['required'] is None


def test_public548_note_scope_unavailable_source_or_changed_project_never_authorizes_empty_scope(tmp_path):
    folder, item = _public548_note_scope_fixture(tmp_path)
    path = folder / '.local-ai/USER-REQUESTS.md'
    path.mkdir()
    result = native_agent._authored_note_scope(folder, item)
    assert result['status'] == 'unknown' and result['required'] is None
    result = native_agent._authored_note_scope(folder, {**item, 'folder': str(tmp_path / 'other')})
    assert result['status'] == 'unknown' and result['required'] is None


def public549_worker_frontier_fixture(tmp_path, monkeypatch, *, long_followup=False, cli_only=False, followup_html=False):
    """Owned registered component success, not browser/whole-goal proof."""
    item = project(tmp_path / 'records')
    folder = Path(item['folder'])
    item['brief'] = ('Create index.html as an offline records app. Save added records across page reloads. '
                     'Maintain PLAN.md and PROGRESS.md with actual checks and remaining work.')
    if cli_only or followup_html:
        item['brief'] = 'Build a local utility in component.py. Maintain PLAN.md and PROGRESS.md with actual checks.'
    runtime = tmp_path / 'owned-registry'; runtime.mkdir()
    monkeypatch.setattr(long_tasks, 'STATE', runtime)
    with long_tasks.database() as db:
        db.execute('INSERT INTO projects(id,title,folder,brief,status,created,updated) VALUES(?,?,?,?,?,?,?)',
                   (item['id'], 'Neutral records', item['folder'], item['brief'], 'working', time.time(), time.time()))
    (folder / 'index.html').write_text('<!doctype html><p id="status">Records</p>', encoding='utf-8')
    (folder / 'component.py').write_text('def normalize(value):\n    return value.strip()\n', encoding='utf-8')
    (folder / '.local-ai/PLAN.md').write_text(long_tasks.INITIAL_PLAN, encoding='utf-8')
    (folder / '.local-ai/PROGRESS.md').write_text(long_tasks.INITIAL_PROGRESS, encoding='utf-8')
    followup = 'Keep the record label readable. Do not remove my reload requirement.\n'
    if cli_only:
        followup = 'Keep the local utility readable.\n'
    elif followup_html:
        followup = 'Create index.html and save added records across page reloads.\n'
    if long_followup:
        followup += 'Neutral background detail.\n' * 6000
        assert len(followup.encode('utf-8')) > 128 * 1024
    (folder / '.local-ai/USER-REQUESTS.md').write_text(followup, encoding='utf-8')
    task_context.plan_steps(folder, [{'title': 'Normalize a label', 'phase': 'Component',
        'check_tool': 'check_python_function', 'check_arguments': {'path': 'component.py',
            'function': 'normalize', 'cases': [{'args': ['  Cedar  '], 'expected': 'Cedar'}]}}])
    with project_memory.verification_context(item):
        queue = task_context.step_status(folder)
        result = json.loads(native_agent.execute_tool(folder, 'task_steps', {'action': 'complete', 'step_id': queue['active']['id']}))
        assert result['passed']
    assert task_context.step_status(folder)['remaining'] == 0
    steering = {'role': 'user', 'content': 'Retain my exact current steering: keep the record label readable.'}
    state = native_agent.read_state(item)
    state.update(status='continue', turns=7, verify_passed=False,
                 messages=[{'role': 'system', 'content': native_agent.prompt(item)}, steering])
    native_agent.save_state(item, state)
    return item, folder, steering


@pytest.mark.parametrize('long_followup', [False, True])
@pytest.mark.parametrize('automatic_router', [False, True])
def test_public549_actual_whole_goal_request_preserves_tools_context_and_host_metadata_boundary(
        tmp_path, monkeypatch, long_followup, automatic_router):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, long_followup=long_followup)
    original_tools = copy.deepcopy(native_agent.TOOLS)
    before_queue = task_context.step_status(folder)
    before_html = (folder / 'index.html').read_bytes()
    before_budget = copy.deepcopy(native_agent.read_state(item).get('capability_recovery_budget'))
    captured, modes, compactions, offered = [], [], [], []
    class Captured(BaseException): pass
    def capture(request, *args, **kwargs):
        captured.append(copy.deepcopy(request)); raise Captured()
    original_mode = native_agent.remember_request_mode
    def mode(*args, **kwargs):
        original_mode(*args, **kwargs)
        modes.append(copy.deepcopy(args[0]['request_mode_events'][-1]))
    original_compact = native_agent.compact_messages
    def compact(messages, *args, **kwargs):
        compactions.append(copy.deepcopy(kwargs['tools']))
        return original_compact(messages, *args, **kwargs)
    original_offer = native_agent.expression_helper_tools
    def offer(project, tools):
        result = original_offer(project, tools)
        offered.append(copy.deepcopy(result)); return result
    monkeypatch.setattr(native_agent, 'remember_request_mode', mode)
    monkeypatch.setattr(native_agent, 'compact_messages', compact)
    monkeypatch.setattr(native_agent, 'expression_helper_tools', offer)
    monkeypatch.setattr(native_agent, 'configured_context', lambda *args: 32768)
    if automatic_router:
        # Actual automatic_call runs, including metadata stripping; its Router
        # is an injected endpoint that cannot perform model discovery or RPC.
        monkeypatch.setattr(cloud_ai, 'Router', lambda: SimpleNamespace(complete=capture))
    with pytest.raises(Captured):
        native_agent.run_slice(item, complete=None if automatic_router else capture)
    assert len(captured) == len(modes) == 1 and modes[0]['mode'] == 'whole_goal_work'
    request = captured[0]
    assert request['tool_choice'] == 'auto' and request.get('response_format') is None
    assert {t['function']['name'] for t in original_tools} <= {t['function']['name'] for t in request['tools']}
    assert offered[-1] == compactions[-1] == request['tools']
    assert native_agent.TOOLS == original_tools and steering in request['messages']
    assert item['brief'] in request['messages'][0]['content']
    assert cloud_ai.estimated_input_tokens(request) + request['max_tokens'] <= 32768
    if automatic_router:
        assert not any(key.startswith('_native_') for key in request)
    else:
        assert request['_native_whole_goal_work'] is True
    if long_followup:
        assert native_agent._post_queue_instruction_frame(item)['sources'] is None
        assert '[Whole-goal work]' in '\n'.join(str(m.get('content', '')) for m in request['messages'])
    assert task_context.step_status(folder) == before_queue
    assert (folder / 'index.html').read_bytes() == before_html
    after = native_agent.read_state(item)
    assert after['turns'] == 7 and after['status'] != 'done'
    assert after.get('capability_recovery_budget') == before_budget


@pytest.mark.parametrize('unknown', [False, True])
def test_public549_authored_notes_shared_completion_prerequisite_blocks_recovered_handoff(tmp_path, monkeypatch, unknown):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, cli_only=True)
    (folder / '.local-ai/PLAN.md').write_text('# Plan\n- [x] Component checked\n', encoding='utf-8')
    state = native_agent.read_state(item); state['verify_passed'] = True
    if unknown:
        monkeypatch.setattr(native_agent, '_authored_note_scope', lambda *args, **kwargs:
            {'status': 'unknown', 'required': None, 'reason': 'source_unavailable'})
    before = native_agent.state_file(item).read_bytes()
    assert native_agent.authored_notes_issue(folder, item)
    assert native_agent.completion_issue(folder, item, state)
    assert native_agent.automatic_handoff_ready(folder, item, state) is False
    assert native_agent.state_file(item).read_bytes() == before
    assert (folder / '.local-ai/PROGRESS.md').read_text(encoding='utf-8') == long_tasks.INITIAL_PROGRESS


def test_public549_authored_notes_gate_still_applies_after_an_ordinary_tool(tmp_path, monkeypatch):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, cli_only=True)
    (folder / '.local-ai/PLAN.md').write_text('# Plan\n- [x] Component checked\n', encoding='utf-8')
    state = native_agent.read_state(item); state['verify_passed'] = True
    native_agent.save_state(item, state)
    observed, calls = [], []
    original = native_agent.authored_notes_issue
    def notes(*args, **kwargs):
        issue = original(*args, **kwargs); observed.append(issue); return issue
    def provider(request):
        calls.append(copy.deepcopy(request))
        return {'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'owned-read', 'type': 'function', 'function': {'name': 'read_file',
                'arguments': json.dumps({'path': 'component.py', 'max_lines': 8})}}]}}]}
    monkeypatch.setattr(native_agent, 'authored_notes_issue', notes)
    monkeypatch.setattr(native_agent, 'MAX_TURNS_PER_SLICE', 1)
    result = native_agent.run_slice(item, complete=provider)
    assert len(calls) == 1 and result['status'] != 'done'
    assert len(observed) >= 2 and all(observed)
    assert (folder / '.local-ai/PROGRESS.md').read_text(encoding='utf-8') == long_tasks.INITIAL_PROGRESS


@pytest.mark.parametrize('control', ['continue', 'paused', 'stopped', 'done'])
def test_public549_instruction_change_after_response_preserves_latest_checkpoint_without_writer(tmp_path, monkeypatch, control):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch)
    original_save = native_agent.save_state
    changed = {'value': False}; latest_bytes = []
    def guarded_save(*args, **kwargs):
        assert not changed['value'], 'No stale checkpoint writer may run on the instruction-defer path'
        return original_save(*args, **kwargs)
    def provider(request):
        (folder / '.local-ai/USER-REQUESTS.md').write_text('New real user steering after planning.\n', encoding='utf-8')
        latest = native_agent.read_state(item)
        latest.update(status=control, messages=[{'role': 'user', 'content': 'Newest owned instruction'}])
        original_save(item, latest)
        latest_bytes.append(native_agent.state_file(item).read_bytes()); changed['value'] = True
        return {'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'obsolete', 'type': 'function', 'function': {'name': 'write_file',
                'arguments': json.dumps({'path': 'must-not-exist.txt', 'content': 'obsolete response'})}}]}}]}
    monkeypatch.setattr(native_agent, 'save_state', guarded_save)
    result = native_agent.run_slice(item, complete=provider)
    assert result['status'] == control
    assert result['messages'] == [{'role': 'user', 'content': 'Newest owned instruction'}]
    assert native_agent.state_file(item).read_bytes() == latest_bytes[0]
    assert not (folder / 'must-not-exist.txt').exists()


@pytest.mark.parametrize('cut', ['later_note_read', 'second_instruction_scope', 'later_note_rehash', 'instructions_during_note_rehash'])
def test_public549_authored_note_confirmation_rejects_early_note_changes(tmp_path, monkeypatch, cut):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, cli_only=True)
    plan = folder / '.local-ai/PLAN.md'; progress = folder / '.local-ai/PROGRESS.md'
    plan.write_text('# Plan\n- [x] Neutral component checked\n', encoding='utf-8')
    progress.write_text('# Progress\nCPU component check passed; integration work remains.\n', encoding='utf-8')
    original_read = native_agent._post_queue_read
    original_scope = native_agent._authored_note_scope
    original_stream = project_memory._receipt_stream
    changed = []; scopes = []
    def change():
        if not changed:
            plan.write_text(long_tasks.INITIAL_PLAN, encoding='utf-8'); changed.append(True)
    def read(root, path, *args, **kwargs):
        result = original_read(root, path, *args, **kwargs)
        if cut == 'later_note_read' and path.name == 'PROGRESS.md': change()
        return result
    def scope(*args, **kwargs):
        result = original_scope(*args, **kwargs); scopes.append(result)
        if cut == 'second_instruction_scope' and len(scopes) == 2: change()
        return result
    def stream(path, identity, deadline, sink=None):
        result = original_stream(path, identity, deadline, sink)
        if path.name == 'PROGRESS.md' and sink is None:
            if cut == 'later_note_rehash': change()
            elif cut == 'instructions_during_note_rehash' and not changed:
                (folder / '.local-ai/USER-REQUESTS.md').write_text('New owned instructions.\n', encoding='utf-8')
                changed.append(True)
        return result
    monkeypatch.setattr(native_agent, '_post_queue_read', read)
    monkeypatch.setattr(native_agent, '_authored_note_scope', scope)
    monkeypatch.setattr(project_memory, '_receipt_stream', stream)
    issue = native_agent.authored_notes_issue(folder, item)
    assert changed and issue
    if cut != 'instructions_during_note_rehash':
        assert plan.read_text(encoding='utf-8') == long_tasks.INITIAL_PLAN


def test_public549_authored_notes_heading_only_is_not_an_authored_body(tmp_path, monkeypatch):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, cli_only=True)
    (folder / '.local-ai/PLAN.md').write_text('# Plan\n', encoding='utf-8')
    (folder / '.local-ai/PROGRESS.md').write_text('# Progress\nActual component checks remain.\n', encoding='utf-8')
    assert native_agent.authored_notes_issue(folder, item)


@pytest.mark.parametrize('wrapper', ['', '# Progress\n\n', '  # Progress\n\n\t'])
def test_public549_host_placeholder_label_written_back_is_not_authored(tmp_path, monkeypatch, wrapper):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, cli_only=True)
    label = task_context.project_note_excerpt('PROGRESS.md', long_tasks.INITIAL_PROGRESS, 1)
    assert label == ('[Untouched startup placeholder; this note has not been authored. '
                     'It is not a factual progress report. Requested note-writing remains unfinished.]')
    (folder / '.local-ai/PLAN.md').write_text('# Plan\n- [x] Component checked\n', encoding='utf-8')
    (folder / '.local-ai/PROGRESS.md').write_text(wrapper + label + '\n  ', encoding='utf-8')
    assert native_agent.authored_notes_issue(folder, item)


def test_public549_substantive_authored_note_may_quote_the_host_placeholder(tmp_path, monkeypatch):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, cli_only=True)
    label = task_context.project_note_excerpt('PROGRESS.md', long_tasks.INITIAL_PROGRESS, 1)
    (folder / '.local-ai/PLAN.md').write_text('# Plan\n- [x] CPU component checked\n', encoding='utf-8')
    (folder / '.local-ai/PROGRESS.md').write_text('# Progress\n' + label +
        '\nThe prior note above was a placeholder. The normalize component passed its actual CPU check; integration remains unfinished.\n', encoding='utf-8')
    assert native_agent.authored_notes_issue(folder, item) == ''


def test_public549_whole_goal_router_reuses_validated_identity_without_late_reader(tmp_path, monkeypatch):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch)
    original_mode = native_agent.remember_request_mode
    original_avoid = native_agent.repair_avoid_models
    expected = hashlib.sha256((folder / '.local-ai/USER-REQUESTS.md').read_bytes()).hexdigest()
    calls, identities = [], []
    class Captured(BaseException): pass
    def deny(*args, **kwargs):
        raise AssertionError('No late uncapped request_identity acquisition is allowed')
    def mode(*args, **kwargs):
        original_mode(*args, **kwargs)
        # Arm only after the actual worker's final frame validation. Deliberate
        # owned growth must not be acquired through a new uncapped late reader.
        with (folder / '.local-ai/USER-REQUESTS.md').open('ab') as stream:
            stream.write(b'Later owned data.\n' * 10000)
        monkeypatch.setattr(task_context, 'request_identity', deny)
    def avoid(state, request):
        identities.append(request)
        return original_avoid(state, request)
    def provider(payload, *args, **kwargs):
        calls.append(copy.deepcopy(payload)); raise Captured()
    monkeypatch.setattr(native_agent, 'remember_request_mode', mode)
    monkeypatch.setattr(native_agent, 'repair_avoid_models', avoid)
    monkeypatch.setattr(cloud_ai, 'Router', lambda: SimpleNamespace(complete=provider))
    with pytest.raises(Captured): native_agent.run_slice(item, complete=None)
    assert identities == [expected] and len(calls) == 1
    assert not any(key.startswith('_native_') for key in calls[0])
    assert steering in calls[0]['messages']
    assert native_agent.read_state(item)['status'] != 'done'


def test_public549_followup_only_candidate_disappearing_defers_before_notice_or_provider(tmp_path, monkeypatch):
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch, followup_html=True)
    assert not native_agent.browser_check_required(item)
    assert native_agent._final_delivery_scope(folder, item)['required'] is True
    original_fresh = native_agent.fresh_messages
    original_save = native_agent.save_state
    mutated = []; latest = []
    def fresh(*args, **kwargs):
        result = original_fresh(*args, **kwargs)
        (folder / '.local-ai/USER-REQUESTS.md').write_text('Explain the existing local CLI only.\n', encoding='utf-8')
        state = native_agent.read_state(item)
        state.update(status='continue', messages=[{'role': 'user', 'content': 'Newest owned clarification'}])
        original_save(item, state); latest.append(native_agent.state_file(item).read_bytes()); mutated.append(True)
        return result
    def save(*args, **kwargs):
        assert not mutated, 'No stale checkpoint publication on a changed candidate'
        return original_save(*args, **kwargs)
    def provider(*args, **kwargs): raise AssertionError('No provider may see a vanished frontier')
    monkeypatch.setattr(native_agent, 'fresh_messages', fresh)
    monkeypatch.setattr(native_agent, 'save_state', save)
    result = native_agent.run_slice(item, complete=provider)
    assert mutated and result['status'] == 'continue'
    assert result['messages'] == [{'role': 'user', 'content': 'Newest owned clarification'}]
    assert native_agent.state_file(item).read_bytes() == latest[0]


def test_public549_ordinary_cli_unreadable_rules_do_not_break_observational_mode_record(tmp_path, monkeypatch):
    item = project(tmp_path / 'ordinary-cli'); folder = Path(item['folder'])
    item['brief'] = 'Explain the existing local command-line utility.'
    rules = folder / 'AGENTS.md'; rules.write_text('Owned unreadable rules fixture.', encoding='utf-8')
    runtime = tmp_path / 'ordinary-registry'; runtime.mkdir(); monkeypatch.setattr(long_tasks, 'STATE', runtime)
    state = native_agent.read_state(item)
    state.update(status='continue', turns=7, messages=[{'role': 'system', 'content': native_agent.prompt(item)},
                                                    {'role': 'user', 'content': 'Keep my CLI question.'}])
    native_agent.save_state(item, state)
    original_open = Path.open
    def open_file(path, *args, **kwargs):
        if path == rules: raise PermissionError('Owned unreadable file')
        return original_open(path, *args, **kwargs)
    observed, modes = [], []
    original_mode = native_agent.remember_request_mode
    class Captured(BaseException): pass
    def mode(*args, **kwargs):
        original_mode(*args, **kwargs); modes.append(copy.deepcopy(args[0]['request_mode_events'][-1]))
    def provider(request): observed.append(request); raise Captured()
    original_recovery = native_agent.capability_recovery_request
    armed = []
    def recovery(*args, **kwargs):
        result = original_recovery(*args, **kwargs)
        # The existing recovery reader is not the new observational mode read.
        # Arm the actual file fault only after that unchanged earlier path.
        monkeypatch.setattr(Path, 'open', open_file)
        armed.append(True)
        return result
    monkeypatch.setattr(native_agent, 'capability_recovery_request', recovery)
    monkeypatch.setattr(native_agent, 'remember_request_mode', mode)
    with pytest.raises(Captured): native_agent.run_slice(item, complete=provider)
    assert len(observed) == len(modes) == 1
    assert modes[0]['mode'] == 'ordinary_implementation' and modes[0]['basis_status'] == 'unavailable'
    assert not observed[0].get('_native_whole_goal_work')
    assert armed == [True]


# Host-only saved-suite replay controls. These tests use only owned temporary
# projects and injected model endpoints; no real provider/TTS/device is involved.
def saved_browser_feedback_fixture(tmp_path, monkeypatch):
    import webapp_check
    item, folder, steering = public549_worker_frontier_fixture(tmp_path, monkeypatch)
    (folder / 'index.html').write_text("""<!doctype html><input id=name><button id=add>Add</button>
<table><tbody id=items></tbody></table><script>
add.onclick=()=>{let r=items.insertRow();r.insertCell().textContent=name.value};
</script>""", encoding='utf-8')
    spec = {'path': 'index.html', 'cases': [
        {'row_counts': {'items': 0}},
        {'set': {'name': 'Cedar'}, 'click': ['add'], 'row_counts': {'items': 1}},
        {'reload': True, 'row_counts': {'items': 1}}]}
    saved = webapp_check.saved_suite(folder, spec)
    (folder / '.local-ai/webapp-check.json').write_text(json.dumps(saved), encoding='utf-8')
    return item, folder, steering


def test_saved_browser_replay_actual_failed_reload_reaches_ordinary_request_without_sql_credit(tmp_path, monkeypatch):
    item, folder, steering = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    before_queue = task_context.step_status(folder)
    html = (folder / 'index.html').read_bytes()
    suite = (folder / '.local-ai/webapp-check.json').read_bytes()
    before_state = native_agent.read_state(item)
    captured = []
    # Wrap the real transport; never replace its result with a fixture verdict.
    import win32job
    real_verify = long_tasks.verify_project
    real_popen = long_tasks.subprocess.Popen
    real_query = win32job.QueryInformationJobObject
    executing = [False]
    children, terminal_results, member_queries = [], [], []
    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        if executing[0]:
            children.append(process)
        return process
    def query(job, kind):
        result = real_query(job, kind)
        if executing[0] and kind == win32job.JobObjectBasicProcessIdList:
            member_queries.append(result)
        return result
    def verify(*args, **kwargs):
        executing[0] = True
        try:
            result = real_verify(*args, **kwargs)
            terminal_results.append(result)
            return result
        finally:
            executing[0] = False
    monkeypatch.setattr(long_tasks, 'verify_project', verify)
    monkeypatch.setattr(long_tasks.subprocess, 'Popen', launch)
    monkeypatch.setattr(win32job, 'QueryInformationJobObject', query)
    class Captured(BaseException): pass
    def capture(request):
        captured.append(copy.deepcopy(request)); raise Captured()
    monkeypatch.setattr(native_agent, 'configured_context', lambda *a: 32768)
    with pytest.raises(Captured):
        native_agent.run_slice(item, complete=capture)
    assert len(captured) == 1
    request = captured[0]
    feedback = [m['content'] for m in request['messages'] if 'Automatic saved browser integration result;' in m.get('content', '')]
    assert len(feedback) == 1 and 'Checker did not pass.' in feedback[0]
    assert 'row' in feedback[0].lower() and 'SQL' in feedback[0]
    # The older substring assertion above is retained, but is not proof:
    # 'browser' also contains 'row'. Require the actual fixed child's report.
    assert len(children) == 1 and children[0].poll() == 1
    assert children[0].args[1:4] == ['-I', '-B', '-c']
    assert children[0].args[4] == long_tasks._SAVED_BROWSER_BOOTSTRAP
    assert member_queries and member_queries[-1] == ()
    assert len(terminal_results) == 1 and terminal_results[0][0] is False
    actual_output = terminal_results[0][1]
    reports = [json.loads(line) for line in actual_output.splitlines() if line.lstrip().startswith('{')]
    assert len(reports) == 1, actual_output
    report = reports[0]
    assert report['passed'] is False and report['case_count'] == 3
    assert report['failed_case_count'] == 1
    assert report['suite_sha256'] == hashlib.sha256(suite).hexdigest()
    assert report['checks'][0]['page_sha256'] == hashlib.sha256(html).hexdigest()
    failures = report['checks'][0]['cases']
    assert len(failures) == 1 and failures[0]['case'] == 3
    assert failures[0]['inputs']['reload'] is True
    assert failures[0]['row_counts']['items']['actual'] == 0
    assert failures[0]['row_counts']['items']['expected'] == 1
    assert any(reason.startswith('Row count #items was 0; expected 1') for reason in failures[0]['failures'])
    assert report['checks'][0]['javascript_errors'] == []
    assert steering in request['messages'] and request['tool_choice'] == 'auto'
    assert {x['function']['name'] for x in native_agent.TOOLS} <= {x['function']['name'] for x in request['tools']}
    assert task_context.step_status(folder) == before_queue
    assert (folder / 'index.html').read_bytes() == html
    assert (folder / '.local-ai/webapp-check.json').read_bytes() == suite
    assert native_agent.read_state(item).get('capability_recovery_budget') == before_state.get('capability_recovery_budget')
    assert not native_agent.read_state(item).get('verify_passed')


def test_saved_browser_replay_cache_claim_restart_and_changed_basis(tmp_path, monkeypatch):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    queue = task_context.step_status(folder); state = native_agent.read_state(item)
    calls = []
    def verify(*args, **kwargs):
        proof = kwargs['_host_admit']('resume')
        assert set(proof) == {'key', 'source'} and proof['key'] == kwargs['_saved_browser']['snapshot_key']
        assert kwargs['_host_admit']('final') == proof
        calls.append(kwargs['_saved_browser'].copy())
        return False, 'Owned checker says reload row count differs.'
    monkeypatch.setattr(long_tasks, 'verify_project', verify)
    request = task_context.request_identity(folder)
    assert native_agent.automatic_saved_browser_feedback(item, state, queue, request)
    assert native_agent.automatic_saved_browser_feedback(item, state, queue, request) is None
    fresh = native_agent.read_state(item)
    assert 'stored task data' in native_agent.automatic_saved_browser_feedback(item, fresh, queue, request)['content']
    assert len(calls) == 1
    (folder / 'index.html').write_text('<p>Changed source</p>', encoding='utf-8')
    assert native_agent.automatic_saved_browser_feedback(item, fresh, queue, request)
    assert len(calls) == 2
    (folder / '.local-ai/verify.ps1').write_text('# Changed saved verifier, never executed here', encoding='utf-8')
    assert native_agent.automatic_saved_browser_feedback(item, fresh, queue, request)
    assert len(calls) == 3
    assert all(not x.get('verify_passed') for x in (state, fresh))


@pytest.mark.parametrize('policy', ['Read-only inspection.', 'Inspect-only; do not execute commands.',
    'Do not run tests.', 'Never launch browser checks.', 'No execution.'])
def test_saved_browser_replay_deny_only_policy_never_launches(tmp_path, monkeypatch, policy):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    (folder / 'AGENTS.md').write_text(policy, encoding='utf-8')
    monkeypatch.setattr(long_tasks, 'verify_project', lambda *a, **k: pytest.fail('Policy denied automatic execution'))
    queue = task_context.step_status(folder)
    assert native_agent.automatic_saved_browser_feedback(item, native_agent.read_state(item), queue,
        task_context.request_identity(folder)) is None
    assert not (folder / '.local-ai/saved-browser-feedback.json').exists()


@pytest.mark.parametrize('action', ['pause', 'stop', 'delete', 'note', 'resume'])
def test_saved_browser_replay_pending_control_never_launches_or_overwrites_state(tmp_path, monkeypatch, action):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    queue = task_context.step_status(folder); state = native_agent.read_state(item)
    # Real registered request row; leave application processing to its normal owner.
    with long_tasks.database() as conn:
        conn.execute("INSERT INTO requests(project,action,note,status) VALUES(?,?,?,?)",
            (item['id'], action, 'Owned control', 'pending'))
    before = native_agent.state_file(item).read_bytes()
    monkeypatch.setattr(long_tasks, 'verify_project', lambda *a, **k: pytest.fail('Pending control must deny launch'))
    assert native_agent.automatic_saved_browser_feedback(item, state, queue,
        task_context.request_identity(folder)) is None
    assert native_agent.state_file(item).read_bytes() == before


def test_saved_browser_replay_optional_unknown_and_cli_leave_normal_tools_unchanged(tmp_path, monkeypatch):
    item, folder, _ = public549_worker_frontier_fixture(tmp_path, monkeypatch, cli_only=True)
    queue = task_context.step_status(folder); state = native_agent.read_state(item)
    original_tools = copy.deepcopy(native_agent.TOOLS)
    monkeypatch.setattr(long_tasks, 'verify_project', lambda *a, **k: pytest.fail('No saved suite must not launch'))
    assert native_agent.automatic_saved_browser_feedback(item, state, queue, task_context.request_identity(folder)) is None
    assert native_agent.TOOLS == original_tools
    assert not (folder / '.local-ai/saved-browser-feedback.json').exists()


@pytest.mark.parametrize('action', ['note', 'resume', 'pause', 'stop', 'delete'])
def test_saved_browser_replay_real_late_control_preserves_new_checkpoint(tmp_path, monkeypatch, action):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    queue = task_context.step_status(folder); state = native_agent.read_state(item)
    checkpoint_after_control = []
    def verify(*args, **kwargs):
        assert not native_agent.process_alive(native_agent.read_state(item)), 'Owned fixture must have no worker PID'
        native_agent.apply_request(item, action, 'Keep the exact later operator instruction.' if action in {'note', 'resume'} else '')
        checkpoint_after_control.append(native_agent.state_file(item).read_bytes())
        return True, 'Checker finished before operator action.'
    monkeypatch.setattr(long_tasks, 'verify_project', verify)
    assert native_agent.automatic_saved_browser_feedback(item, state, queue, task_context.request_identity(folder)) is False
    assert native_agent.state_file(item).read_bytes() == checkpoint_after_control[0]
    assert not native_agent.read_state(item).get('verify_passed')


@pytest.mark.parametrize('polls', [0, 20])
def test_saved_browser_actual_host_callback_full_captures_are_phase_bound(tmp_path, monkeypatch, polls):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    queue = task_context.step_status(folder); state = native_agent.read_state(item)
    original = native_agent._saved_browser_replay_basis
    captures = []
    def basis(*args, **kwargs):
        captures.append('full')
        return original(*args, **kwargs)
    monkeypatch.setattr(native_agent, '_saved_browser_replay_basis', basis)
    original_source = project_memory.verification_source_state
    source_captures = []
    def source(*args, **kwargs):
        source_captures.append('content')
        return original_source(*args, **kwargs)
    monkeypatch.setattr(project_memory, 'verification_source_state', source)
    def verify(*args, **kwargs):
        admit = kwargs['_host_admit']
        assert admit('launch') is True
        before = admit('resume')
        assert before['key'] == kwargs['_saved_browser']['snapshot_key']
        for _ in range(polls):
            assert admit('monitor') is True
        assert admit('result') is True
        assert admit('final') == before
        return False, 'Owned injected terminal result, no execution.'
    monkeypatch.setattr(long_tasks, 'verify_project', verify)
    assert native_agent.automatic_saved_browser_feedback(item, state, queue, task_context.request_identity(folder))
    assert len(captures) == 3  # cache-key + Resume + final, regardless of poll count
    assert len(source_captures) == 6  # existing stable-byte preview scans twice per capture


def test_saved_browser_monitor_pause_denies_without_a_full_capture(tmp_path, monkeypatch):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    queue = task_context.step_status(folder)
    basis = native_agent._saved_browser_replay_basis(item, queue)
    assert basis is not None
    native_agent.apply_request(item, 'pause', '')
    monkeypatch.setattr(project_memory, 'verification_source_state', lambda *a, **k: pytest.fail('Monitor must not scan source'))
    monkeypatch.setattr(project_memory, 'readonly_check_receipts', lambda *a, **k: pytest.fail('Monitor must not copy SQLite'))
    assert native_agent._saved_browser_replay_monitor(item, basis) is False



def test_saved_browser_actual_passing_replay_records_bound_receipt_without_supervisor_import_bootstrap(tmp_path, monkeypatch):
    import ast
    import webapp_check
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    (folder / 'index.html').write_text("""<!doctype html><input id=name><button id=add>Add</button>
<table><tbody id=items></tbody></table><script>
let rows=JSON.parse(localStorage.getItem('records')||'[]');
function render(){let body=document.getElementById('items');body.replaceChildren();
for(let value of rows){body.insertRow().insertCell().textContent=value}}
document.getElementById('add').onclick=()=>{rows.push(document.getElementById('name').value);
localStorage.setItem('records',JSON.stringify(rows));render()};render();
</script>""", encoding='utf-8')
    queue = task_context.step_status(folder); state = native_agent.read_state(item)
    import win32job
    real_verify = long_tasks.verify_project
    real_query = win32job.QueryInformationJobObject
    real_popen = long_tasks.subprocess.Popen
    terminal, memberships, children = [], [], []
    def verify(*args, **kwargs):
        result = real_verify(*args, **kwargs); terminal.append(result); return result
    def query(job, kind):
        result = real_query(job, kind)
        if kind == win32job.JobObjectBasicProcessIdList:
            memberships.append(result)
        return result
    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs); children.append(process); return process
    monkeypatch.setattr(long_tasks, 'verify_project', verify)
    monkeypatch.setattr(win32job, 'QueryInformationJobObject', query)
    monkeypatch.setattr(long_tasks.subprocess, 'Popen', launch)
    expected_suite_sha = hashlib.sha256((folder/'.local-ai/webapp-check.json').read_bytes()).hexdigest()
    expected_page_sha = hashlib.sha256((folder/'index.html').read_bytes()).hexdigest()
    result = native_agent.automatic_saved_browser_feedback(item, state, queue, task_context.request_identity(folder))
    assert result is not None and 'Checker returned success.' in result['content']
    assert len(children) == 1 and children[0].poll() == 0
    assert memberships and memberships[-1] == ()
    assert len(terminal) == 1 and terminal[0][0] is True
    reports = [json.loads(line) for line in terminal[0][1].splitlines() if line.startswith('{')]
    assert len(reports) == 1, terminal[0][1]
    assert reports[0]['passed'] is True and reports[0]['case_count'] == 3 and reports[0]['failed_case_count'] == 0
    assert reports[0]['suite_sha256'] == expected_suite_sha
    assert reports[0]['checks'][0]['page_sha256'] == expected_page_sha
    assert reports[0]['persistence_witnesses']
    assert not state.get('verify_passed') and state.get('status') != 'done'
    preview = project_memory.readonly_check_receipts(folder, item, checker_file=webapp_check.__file__)
    assert preview['status'] == 'available'
    assert any(row['checker'] == 'check_webapp_saved' and row['passed'] is True
        and row['planning_basis'] == project_memory.verification_basis(folder, item)
        for row in preview['receipts'])
    assert task_context.step_status(folder) == queue
    imports = [alias.name for node in ast.walk(ast.parse(long_tasks._SAVED_BROWSER_BOOTSTRAP))
        if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names]
    assert 'long_tasks' not in imports



def test_saved_browser_read_tuple_contract_and_monitor_stat_identities(tmp_path, monkeypatch):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    verifier = folder/'.local-ai/verify.ps1'
    verifier.write_text('throw "Never execute the saved script in typed replay"', encoding='utf-8')
    path = folder/'.local-ai/webapp-check.json'
    value = native_agent._post_queue_read(folder, path, 64000, time.monotonic()+2)
    assert value[0] == '.local-ai/webapp-check.json' and isinstance(value[0], str)
    assert value[1] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert value[2] == path.read_bytes()
    basis = native_agent._saved_browser_replay_basis(item, task_context.step_status(folder))
    assert basis is not None
    for name, target in [('suite', path), ('verifier', verifier)]:
        assert type(basis['monitor'][name]) is tuple and len(basis['monitor'][name]) == 5
        assert basis['monitor'][name] == project_memory._receipt_file_identity(target)
    assert native_agent._saved_browser_replay_monitor(item, basis) is True



def test_saved_browser_unknown_cleanup_defers_current_real_slice_without_rpc(tmp_path, monkeypatch):
    item, folder, _ = saved_browser_feedback_fixture(tmp_path, monkeypatch)
    at_checker = []
    queue = task_context.step_status(folder)
    def unknown(*args, **kwargs):
        at_checker.append((native_agent.state_file(item).read_bytes(), native_agent.read_state(item)))
        raise InterruptedError('Owned browser membership unknown')
    monkeypatch.setattr(long_tasks, 'verify_project', unknown)
    result = native_agent.run_slice(item, complete=lambda *a, **k: pytest.fail('Unknown cleanup must defer before RPC'))
    assert len(at_checker) == 1 and result == at_checker[0][1]
    assert result['status'] in {'running', 'continue'}
    assert task_context.step_status(folder) == queue
    assert not result.get('verify_passed')
    # The ordinary slice initialization may publish running before the hook.
    # Unknown cleanup must not publish another stale helper checkpoint.
    assert native_agent.state_file(item).read_bytes() == at_checker[0][0]


def test_unreadable_rules_before_recovery_defers_without_provider_or_checkpoint_loss(tmp_path, monkeypatch):
    folder = tmp_path/'owned-task'
    (folder/'.local-ai').mkdir(parents=True)
    item = {'id':'instruction-read-neutral','folder':str(folder),
            'brief':'Explain the existing local command-line utility.'}
    rules = folder/'AGENTS.md'
    rules.write_text('Keep the existing implementation unchanged.',encoding='utf-8')
    registry = tmp_path/'owned-supervisor';registry.mkdir()
    monkeypatch.setattr(long_tasks,'STATE',registry)
    state = native_agent.read_state(item)
    state.update(status='continue',turns=7,messages=[
        {'role':'system','content':native_agent.prompt(item)},
        {'role':'user','content':'Preserve this existing question and checkpoint.'}])
    native_agent.save_state(item,state)
    original_messages = state['messages'].copy()
    original_open = Path.open
    def open_file(path,*args,**kwargs):
        if path == rules:raise PermissionError('Owned neutral unreadable-rules fixture')
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_file)
    calls = []
    def provider(request):
        calls.append(request)
        raise AssertionError('Unreadable authoritative rules must not reach inference')
    result = native_agent.run_slice(item,complete=provider)
    assert result['status'] == 'continue'
    assert not calls
    retained = native_agent.read_state(item)
    assert retained['turns'] == 7 and retained['messages'] == original_messages
    assert int(retained.get('failures',0)) == 0


def instruction_retry_fixture(tmp_path):
    folder = tmp_path/'retry-task'
    (folder/'.local-ai').mkdir(parents=True)
    (folder/'AGENTS.md').write_text('Preserve local data.',encoding='utf-8')
    item = {'id':'retry-neutral','folder':str(folder),
            'brief':'Explain the existing local command-line utility.', 'status':'working','detail':''}
    state = native_agent.read_state(item)
    state.update(status='continue',turns=7,messages=[
        {'role':'system','content':native_agent.prompt(item)},
        {'role':'user','content':'Keep this prior question.'}])
    native_agent.save_state(item,state)
    return item,folder


def test_instruction_wait_polling_does_not_initialize_a_database(tmp_path):
    item,folder = instruction_retry_fixture(tmp_path)
    database = folder/'.local-ai/memory.sqlite3'
    assert not database.exists()
    assert task_context.instruction_wait(item) is False
    task_context.clear_instruction_wait(item)
    assert not database.exists()
    project_memory.initialize(folder)
    with patch.object(project_memory,'connection',side_effect=AssertionError('No polling initialization')):
        assert task_context.instruction_wait(item) is False
        task_context.clear_instruction_wait(item)
    with project_memory.connection(folder) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='native_instruction_wait'").fetchone()


@pytest.mark.parametrize('change',['brief','rules','followup','clock_forward','clock_backward'])
def test_instruction_wait_is_bounded_and_invalidated_without_granting_authority(tmp_path,monkeypatch,change):
    item,folder = instruction_retry_fixture(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(task_context.time,'time',lambda:clock[0])
    checkpoint = native_agent.state_file(item).read_bytes()
    assert [task_context.defer_instruction_read(item) for _ in range(8)] == [2,4,8,16,30,30,30,30]
    assert task_context.instruction_wait(item)
    if change == 'brief':item['brief'] += ' Preserve every output.'
    elif change == 'rules':(folder/'AGENTS.md').write_text('Changed task rules with a different size.',encoding='utf-8')
    elif change == 'followup':(folder/'.local-ai/USER-REQUESTS.md').write_text('New user instruction.',encoding='utf-8')
    elif change == 'clock_forward':clock[0] += 31
    else:clock[0] -= 100
    assert task_context.instruction_wait(item) is False
    assert native_agent.state_file(item).read_bytes() == checkpoint


def test_instruction_wait_clear_is_request_bound_and_resets_backoff(tmp_path,monkeypatch):
    item,folder = instruction_retry_fixture(tmp_path)
    monkeypatch.setattr(task_context.time,'time',lambda:1000.0)
    assert task_context.defer_instruction_read(item) == 2
    assert task_context.defer_instruction_read(item) == 4
    other = {**item,'brief':'A different request.'}
    task_context.clear_instruction_wait(other)
    assert task_context.instruction_wait(item)
    task_context.clear_instruction_wait(item)
    assert task_context.instruction_wait(item) is False
    assert task_context.defer_instruction_read(item) == 2


def test_instruction_retry_actual_worker_keeps_checkpoint_and_supervisor_waits(tmp_path,monkeypatch):
    item,folder = instruction_retry_fixture(tmp_path)
    registry = tmp_path/'retry-registry';registry.mkdir()
    monkeypatch.setattr(long_tasks,'STATE',registry)
    monkeypatch.setattr(long_tasks,'projects',lambda:[item])
    original_slice = native_agent.run_slice
    requests = []
    def provider(payload):
        requests.append(payload)
        raise AssertionError('Unreadable rules must not reach inference')
    monkeypatch.setattr(native_agent,'run_slice',lambda project:original_slice(project,complete=provider))
    original_open = Path.open
    def open_file(path,*args,**kwargs):
        if path == folder/'AGENTS.md':raise PermissionError('Owned test instruction lock')
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_file)
    clock = [1000.0]
    monkeypatch.setattr(task_context.time,'time',lambda:clock[0])
    assert native_agent.worker_main(item['id']) == 0
    state = native_agent.read_state(item)
    assert state['turns'] == 7 and not state.get('failures') and state['status'] != 'error'
    assert state['messages'][-1]['content'] == 'Keep this prior question.'
    assert not requests and task_context.instruction_wait(item)
    starts = []
    monkeypatch.setattr(native_agent,'start_worker',lambda project:starts.append(project))
    status,detail = native_agent.advance(item)
    assert status == 'working' and 'retrying automatically' in detail and not starts
    clock[0] += 3
    native_agent.advance(item)
    assert starts == [item]
    monkeypatch.setattr(Path,'open',original_open)
    class InferenceReached(BaseException):pass
    def available(payload):raise InferenceReached()
    with pytest.raises(InferenceReached):original_slice(item,complete=available)
    monkeypatch.setattr(native_agent,'run_slice',lambda project:{'status':'continue'})
    assert native_agent.worker_main(item['id']) == 0
    assert task_context.instruction_wait(item) is False


@pytest.mark.parametrize('control',['paused','stopped','done'])
def test_instruction_read_defer_preserves_newer_control_checkpoint(tmp_path,monkeypatch,control):
    item,folder = instruction_retry_fixture(tmp_path)
    original_open = Path.open
    latest = []
    def open_file(path,*args,**kwargs):
        if path == folder/'AGENTS.md':
            state = native_agent.read_state(item)
            state.update(status=control,messages=[{'role':'user','content':'Newer control checkpoint'}])
            native_agent.save_state(item,state)
            latest.append(native_agent.state_file(item).read_bytes())
            raise PermissionError('Owned lock after control')
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',open_file)
    def provider(payload):raise AssertionError('No provider after instruction failure')
    result = native_agent.run_slice(item,complete=provider)
    assert result['status'] == control and not result.get('instruction_read_unavailable')
    assert native_agent.state_file(item).read_bytes() == latest[-1]


@pytest.mark.parametrize('control',['paused','stopped'])
def test_instruction_retry_never_restarts_a_paused_or_stopped_checkpoint(tmp_path,monkeypatch,control):
    item,folder = instruction_retry_fixture(tmp_path)
    task_context.defer_instruction_read(item)
    state = native_agent.read_state(item);state['status'] = control;native_agent.save_state(item,state)
    with patch.object(native_agent,'start_worker',side_effect=AssertionError('No control override')):
        assert native_agent.advance(item) is None


def test_instruction_read_handler_does_not_swallow_unrelated_io_errors(tmp_path):
    item,folder = instruction_retry_fixture(tmp_path)
    def provider(payload):raise OSError('Owned unrelated provider failure')
    with pytest.raises(OSError,match='unrelated provider failure'):
        native_agent.run_slice(item,complete=provider)


@pytest.mark.parametrize('code',[sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED])
def test_instruction_retry_optional_lock_does_not_break_supervisor_polling(tmp_path,code):
    item,folder = instruction_retry_fixture(tmp_path)
    project_memory.initialize(folder)
    error = sqlite3.OperationalError('Owned transient hint lock')
    error.sqlite_errorcode = code
    with patch.object(sqlite3,'connect',side_effect=error):
        assert task_context.instruction_wait(item) is False
        task_context.clear_instruction_wait(item)


def test_instruction_retry_does_not_hide_database_corruption(tmp_path):
    item,folder = instruction_retry_fixture(tmp_path)
    project_memory.initialize(folder)
    error = sqlite3.OperationalError('Owned corrupted database fixture')
    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    with patch.object(sqlite3,'connect',side_effect=error), pytest.raises(sqlite3.OperationalError):
        task_context.instruction_wait(item)
