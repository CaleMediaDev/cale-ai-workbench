"""Real local native-worker recovery of an invalid saved acceptance definition.

This seeds a correct identity function and a legacy bad checker. It measures
recovery, not from-scratch app generation. No user project or cloud is used.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from browser_assertion_repair import observed_router

import long_tasks
import model_library
import native_agent
import task_context


def run(runtime: Path, folder: Path, resume: bool = False, max_seconds: float = 90) -> dict:
    if not resume:
        folder.mkdir(parents=True, exist_ok=False)
    notes = folder / ".local-ai"
    if not resume:
        notes.mkdir()
        (notes / "USER-REQUESTS.md").write_text("")
        (notes / "PLAN.md").write_text(long_tasks.INITIAL_PLAN)
        (notes / "PROGRESS.md").write_text(long_tasks.INITIAL_PROGRESS)
    source = b"def identity(value):\n    return value\n"
    if not resume:
        (folder / "logic.py").write_bytes(source)
    brief = ("Repair the invalid saved acceptance check for logic.py and finish this task. "
        "The existing implementation is correct and must not be edited. identity(value) returns value. "
        "Verify actual outputs for inputs 2 and -3 using a repeatable read-only check; preserve source. "
        "The active SQLite row incorrectly uses the Cale tool name write_file as a shell checker. "
        "Revise that saved step to an appropriate checker, run it, and keep PLAN.md and PROGRESS.md accurate.")
    project = {"id": "legacy-recovery", "folder": str(folder), "brief": brief}
    if resume:
        assert long_tasks.original_request_path(folder, project["id"]).read_text() == brief
    else:
        long_tasks.save_original_request(folder, project["id"], brief)
        step = task_context.plan_steps(folder, [{"phase": "Check", "title": "Verify identity inputs",
            "check_command": "Write-Output pending"}])["active"]
        # Simulate a row written before the checker-name guard existed.
        import project_memory
        with project_memory.connection(folder) as conn:
            conn.execute("UPDATE task_steps SET check_command='write_file' WHERE id=?", (step["id"],))
        native_agent.save_state(project, {"status": "continue", "turns": 1, "messages": [
            {"role": "system", "content": native_agent.prompt(project)},
            {"role": "user", "content": "Continue from the saved SQLite active step."}]})
    stats = {"model_calls": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "tools_used": [], "routes": []}
    started = time.monotonic()
    router = observed_router(stats, deadline=started + max_seconds)
    error = None
    with (patch("routing_mode.load_scope", return_value="local_only"),
          patch("cloud_ai.Router", return_value=router), patch.object(model_library, "ROOT", runtime),
          patch.object(long_tasks, "projects", return_value=[project])):
        try:
            state = native_agent.run_slice(project)
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:400]}"
            state = native_agent.read_state(project)
    seconds = round(time.monotonic() - started, 2)
    queue = task_context.step_status(folder)
    result = {"scenario": "legacy checker recovery with a seeded correct function",
        "seconds": seconds, **stats, "resumed": resume, "inference_budget_seconds": max_seconds,
        "inference_error": error, "status": state.get("status"),
        "source_preserved": (folder / "logic.py").read_bytes() == source,
        "units_done": queue["done"], "units_remaining": queue["remaining"],
        "verifier_passed": False, "mutation_rejected": False,
        "negative_input_mutation_rejected": False, "restored_verifier_passed": False}
    script = notes / "verify.ps1"
    if script.exists() and result["source_preserved"]:
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)]
        result["verifier_passed"] = subprocess.run(command, cwd=folder, capture_output=True, timeout=30).returncode == 0
        if result["verifier_passed"]:
            try:
                (folder / "logic.py").write_bytes(source.replace(b"return value", b"return value + 1"))
                result["mutation_rejected"] = subprocess.run(command, cwd=folder, capture_output=True, timeout=30).returncode != 0
                (folder / "logic.py").write_bytes(source.replace(b"return value", b"return 0 if value == -3 else value"))
                result["negative_input_mutation_rejected"] = subprocess.run(command, cwd=folder, capture_output=True, timeout=30).returncode != 0
            finally:
                (folder / "logic.py").write_bytes(source)
            result["restored_verifier_passed"] = subprocess.run(command, cwd=folder, capture_output=True, timeout=30).returncode == 0
    result["passed"] = (not error and state.get("status") == "done" and queue["remaining"] == 0
        and queue["done"] == 1 and all(result[k] for k in
            ("source_preserved", "verifier_passed", "mutation_rejected",
             "negative_input_mutation_rejected", "restored_verifier_passed")))
    result["source_sha256"] = hashlib.sha256(source).hexdigest()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=90)
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 300:
        parser.error("Use an inference budget above zero and at most 300 seconds")
    result = run(args.runtime_root.resolve(strict=True), args.folder.resolve(), args.resume, args.max_seconds)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
