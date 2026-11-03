"""Run one real local-model task and grade its output independently.

This is an opt-in benchmark: it calls the local Ollama API and can consume
several minutes of CPU/GPU time. It never uses a cloud account or changes an
existing Cale task. Run it with the installed Toolbox Python on Windows.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import long_tasks  # noqa: E402
import native_agent  # noqa: E402

TEMPERATURE_BRIEF = (
    "Fix the Fahrenheit-to-Celsius conversion in temperature.py. Preserve "
    "the function name and make a repeatable PowerShell verification script. "
    "The answer must handle freezing, boiling, and negative temperatures."
)

TEMPERATURE_STARTER = '''"""Small temperature conversion utility."""

def to_celsius(fahrenheit):
    return fahrenheit * 5 / 9
'''

CONTACTS_BRIEF = (
    "Create cleaned_contacts.csv from contacts.csv. Keep the first row for each "
    "10-digit phone number, in the original order. Trim and title-case names "
    "and cities. Normalize valid US phone numbers to digits only; discard rows "
    "with missing or invalid phones. Preserve the header name,phone,city. "
    "Create a meaningful repeatable PowerShell verifier and run it."
)
CONTACTS_STARTER = '''name,phone,city
"  avery LEE  ","(517) 555-0101"," lansing "
"JORDAN kim","517.555.0102","DETROIT"
"Avery Lee","5175550101","Lansing"
"sam patel","517 555 0103","ann arbor"
"Morgan Fox","bad","Flint"
'''
EXPECTED_CONTACTS = [
    {"name": "Avery Lee", "phone": "5175550101", "city": "Lansing"},
    {"name": "Jordan Kim", "phone": "5175550102", "city": "Detroit"},
    {"name": "Sam Patel", "phone": "5175550103", "city": "Ann Arbor"},
]


def ollama_completion(model: str, timeout: int, stats: dict):
    """Use the same OpenAI-compatible Ollama endpoint as Cale's router."""

    def complete(payload: dict) -> dict:
        request = Request(
            "http://127.0.0.1:11434/v1/chat/completions",
            data=json.dumps({
                "model": model,
                "messages": payload["messages"],
                "tools": payload["tools"],
                "tool_choice": payload.get("tool_choice", "auto"),
                "stream": False,
                "temperature": payload.get("temperature", 0.2),
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Ollama HTTP {exc.code}: {exc.read(1000).decode('utf-8', 'replace')}") from exc
        stats["model_calls"] += 1
        calls = data.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
        stats["tool_calls"] += len(calls)
        stats["tools_used"].extend(
            str(item.get("function", {}).get("name", "")) for item in calls)
        usage = data.get("usage") or {}
        for name in ("prompt_tokens", "completion_tokens"):
            stats[name] += int(usage.get(name) or 0)
        return data

    return complete


def grade_temperature(folder: Path) -> dict:
    """Check the code and whether its verifier detects the old bug."""
    source = folder / "temperature.py"
    script = folder / ".local-ai" / "verify.ps1"
    result = {"source_present": source.is_file(), "verifier_present": script.is_file()}
    if not source.is_file():
        result["passed"] = False
        return result
    scope = {}
    try:
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), scope)
        convert = scope["to_celsius"]
        expected = [(32, 0), (212, 100), (-40, -40), (98.6, 37)]
        result["cases"] = {str(value): abs(convert(value) - target) < 1e-8
                           for value, target in expected}
    except (Exception, SystemExit) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["cases"] = {}
    # A verifier that only returns exit 0 should not count. Restore the
    # original broken function temporarily and demand a failing verification.
    result["verifier_catches_regression"] = False
    if script.is_file() and all(result["cases"].values()):
        fixed = source.read_bytes()
        try:
            source.write_text(TEMPERATURE_STARTER, encoding="utf-8")
            checked = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)],
                cwd=folder, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30, check=False,
            )
            result["verifier_catches_regression"] = checked.returncode != 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["verifier_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            source.write_bytes(fixed)
    result["passed"] = (result["verifier_present"] and all(result["cases"].values())
                        and result["verifier_catches_regression"])
    return result


def grade_contacts(folder: Path) -> dict:
    """Check the CSV answer and reject a verifier that misses invalid data."""
    output = folder / "cleaned_contacts.csv"
    script = folder / ".local-ai" / "verify.ps1"
    result = {"output_present": output.is_file(), "verifier_present": script.is_file()}
    if not output.is_file():
        result["passed"] = False
        return result
    try:
        with output.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            result["header_correct"] = reader.fieldnames == ["name", "phone", "city"]
            result["rows_correct"] = list(reader) == EXPECTED_CONTACTS
    except (OSError, UnicodeError, csv.Error) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["header_correct"] = result["rows_correct"] = False
    result["verifier_passes_correct"] = False
    result["verifier_rejects_invalid"] = False
    if script.is_file() and result["header_correct"] and result["rows_correct"]:
        command = "powershell.exe -NoProfile -NonInteractive -File .local-ai/verify.ps1"
        result["verifier_passes_correct"] = native_agent.execute_tool(
            folder, "run_command", {"command": command}).startswith("Exit code: 0\n")
        correct = output.read_bytes()
        try:
            output.write_bytes(correct.replace(b"5175550101", b"invalid", 1))
            rejected = native_agent.execute_tool(
                folder, "run_command", {"command": command})
            result["verifier_rejects_invalid"] = (
                rejected.startswith("Exit code: ")
                and not rejected.startswith("Exit code: 0\n"))
        finally:
            output.write_bytes(correct)
    result["passed"] = (result["header_correct"] and result["rows_correct"]
                        and result["verifier_passes_correct"]
                        and result["verifier_rejects_invalid"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("temperature", "contacts"),
                        default="temperature")
    parser.add_argument("--model", default="cale-fast")
    parser.add_argument("--call-timeout", type=int, default=120)
    parser.add_argument("--command-timeout", type=int, default=90,
                        help="Limit each project command; lower this when diagnosing hangs")
    parser.add_argument("--slices", type=int, default=2)
    parser.add_argument("--keep-folder", help="Preserve task files here for debugging (must not exist)")
    args = parser.parse_args()
    native_agent.PROJECT_COMMAND_TIMEOUT = args.command_timeout
    started = time.monotonic()
    context = (nullcontext(Path(args.keep_folder).resolve()) if args.keep_folder
               else tempfile.TemporaryDirectory(prefix="cale-native-benchmark-"))
    with context as temporary:
        folder = Path(temporary)
        if args.keep_folder:
            folder.mkdir(parents=True, exist_ok=False)
        (folder / ".local-ai").mkdir()
        brief = TEMPERATURE_BRIEF if args.scenario == "temperature" else CONTACTS_BRIEF
        if args.scenario == "temperature":
            (folder / "temperature.py").write_text(
                TEMPERATURE_STARTER, encoding="utf-8")
        else:
            (folder / "contacts.csv").write_text(CONTACTS_STARTER, encoding="utf-8")
        project_instruction = (
            "Fix the conversion and verify it.\n"
            if args.scenario == "temperature"
            else "Clean the contacts and verify the saved CSV.\n"
        )
        (folder / "AGENTS.md").write_text(project_instruction, encoding="utf-8")
        (folder / ".local-ai" / "USER-REQUESTS.md").write_text("", encoding="utf-8")
        (folder / ".local-ai" / "PLAN.md").write_text(
            long_tasks.INITIAL_PLAN, encoding="utf-8")
        (folder / ".local-ai" / "PROGRESS.md").write_text(
            long_tasks.INITIAL_PROGRESS, encoding="utf-8")
        long_tasks.save_original_request(folder, "benchmark", brief)
        project = {"id": "benchmark", "folder": str(folder), "brief": brief}
        stats = {"model_calls": 0, "tool_calls": 0,
                 "prompt_tokens": 0, "completion_tokens": 0, "tools_used": []}
        complete = ollama_completion(args.model, args.call_timeout, stats)
        error = None
        state = {}
        # The isolated benchmark folder is intentionally absent from the real
        # task database. Let its verifier use the same contained supervisor
        # path as a registered task without creating a visible app task.
        with patch.object(long_tasks, "projects", return_value=[project]):
            for _ in range(args.slices):
                try:
                    state = native_agent.run_slice(project, complete=complete)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    break
                if state.get("status") != "continue":
                    break
        report = {
            "model": args.model,
            "scenario": args.scenario,
            "command_timeout": args.command_timeout,
            "seconds": round(time.monotonic() - started, 2),
            "worker_status": state.get("status", "error"),
            "turns": state.get("turns", 0),
            **stats,
            "worker_verified": bool(state.get("verify_passed")),
            "current": state.get("current", ""),
            "completion_issue": native_agent.completion_issue(folder, project, state),
            "grade": (grade_temperature(folder) if args.scenario == "temperature"
                      else grade_contacts(folder)),
            "error": error,
        }
        if args.keep_folder:
            (folder / "benchmark-report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0 if report["grade"]["passed"] and state.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
