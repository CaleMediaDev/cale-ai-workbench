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
                     browser_checked_hash=hashlib.sha256(page.read_bytes()).hexdigest())
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
            folder, arguments, '{"passed":true}')
        assert evidence["browser_verified"] is True
        assert evidence["browser_checked_hash"] == hashlib.sha256(page.read_bytes()).hexdigest()
        assert native_agent.browser_check_evidence(folder, arguments, '{"passed":false}') == {}


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
