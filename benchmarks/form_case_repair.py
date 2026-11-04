"""Measure isolated saved-form-case repair through the real local native worker.

This is an opt-in inference benchmark, not an app-building benchmark. Its tiny
application is already correct; the seeded acceptance case forgot to submit.
No model answer, repair call, approval, or successful verdict is supplied.
"""

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from browser_assertion_repair import observed_router, require_idle_media
import cloud_ai
import long_tasks
import model_library
import native_agent
import native_browser
import task_context
import webapp_check


PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Units calculator</title>
</head><body><form id="calculator"><label for="units">Units</label>
<input id="units" type="number" step="any"><button id="compute" type="submit">Compute</button>
</form><output id="total">0</output><script>
document.getElementById('calculator').addEventListener('submit',event=>{
 event.preventDefault();
 document.getElementById('total').textContent=String(Number(document.getElementById('units').value)*5);
});</script></body></html>'''

BRIEF = (
    "Preserve the existing correct index.html source bytes. The calculator must multiply the "
    "entered number by 5 when the user presses Compute and show the result in total. "
    "Repair the mistaken saved acceptance case using the original behavior and actual control IDs, "
    "then run its real acceptance and the retained verifier. Do not modify the working application "
    "just to satisfy an erroneous test."
)


def run(runtime: Path, max_seconds: float) -> dict:
    """Use installed approvals and routing; isolate every project/checkpoint write."""
    if not (runtime / "LongTasks/model-approvals.json").is_file():
        raise ValueError("Runtime root must contain actual installed candidate model approvals")
    require_idle_media(runtime)
    stats = {"model_calls": 0, "tool_calls": 0, "prompt_tokens": 0,
             "completion_tokens": 0, "tools_used": [], "routes": [], "operations": []}
    with tempfile.TemporaryDirectory(prefix="cale-form-case-repair-") as temporary:
        folder = Path(temporary) / "project"
        folder.mkdir()
        page = folder / "index.html"
        page.write_text(PAGE, encoding="utf-8", newline="")
        original = page.read_bytes()
        (folder / "AGENTS.md").write_text(
            "Preserve the correct application source. Correct only mistaken checker definitions; "
            "real browser behavior and the original request remain binding.", encoding="utf-8")
        supervisor = Path(temporary) / "supervisor"
        supervisor.mkdir()
        project = {"id": "form-case-repair", "folder": str(folder), "brief": BRIEF}
        long_tasks.save_original_request(folder, project["id"], BRIEF)
        bad = {"path": "index.html", "cases": [{"set": {"units": "7.2"}, "expect": {"total": "1 row"}}]}
        active = task_context.plan_steps(folder, [{"phase": "Calculation", "title": "Verify submitted units times five",
            "check_tool": "check_webapp", "check_arguments": bad}])["active"]
        seed_args = {"action": "complete", "step_id": active["id"]}
        seed = native_agent.execute_tool(folder, "task_steps", seed_args)
        task_context.record_action(folder, "task_steps", seed, seed_args)
        seeded = json.loads(seed)
        if seeded["passed"] or task_context.step_status(folder)["active"]["attempts"] != 1:
            raise RuntimeError("The deliberately incorrect no-submit acceptance did not fail exactly once")
        native_agent.save_state(project, {"status": "continue", "messages": [], "turns": 1,
            "failures": 0, "step_focus_turns": 1})
        started = time.monotonic()
        failure = None
        execute = native_agent.execute_tool

        def observed_execute(task_folder, name, arguments, **kwargs):
            result = execute(task_folder, name, arguments, **kwargs)
            if name in {"check_webapp", "task_steps"}:
                try:
                    value = json.loads(result)
                except (ValueError, TypeError):
                    value = {}
                stats["operations"].append({"tool": name, "action": arguments.get("action"),
                    "revise_inputs": arguments.get("revise_inputs") is True,
                    "passed": value.get("passed") is True,
                    "active_step_check_aligned": value.get("active_step_check_aligned") is True,
                    "tool_error": result.startswith("Tool error:")})
            return result

        with (patch.object(model_library, "ROOT", runtime),
              patch("routing_mode.load_scope", return_value="local_only"),
              patch.object(long_tasks, "projects", return_value=[project]),
              patch.object(long_tasks, "STATE", supervisor)):
            router = observed_router(stats, deadline=started + max_seconds)
            with (patch.object(cloud_ai, "Router", return_value=router),
                  patch.object(native_agent, "execute_tool", side_effect=observed_execute),
                  # Yield to the benchmark controller after each inference so
                  # it can observe this component's actual completion before
                  # the worker starts unrelated whole-project follow-up work.
                  patch.object(native_agent, "MAX_TURNS_PER_SLICE", 1)):
                try:
                    for _ in range(8):
                        if time.monotonic() >= started + max_seconds:
                            stats["time_budget_exhausted"] = True
                            break
                        state = native_agent.run_slice(project)
                        if state.get("status") != "continue" or task_context.step_status(folder)["remaining"] == 0:
                            break
                except Exception as error:
                    failure = f"{type(error).__name__}: {str(error)[:500]}"
            state = native_agent.read_state(project)
            queue = task_context.step_status(folder)
            # Independent grading supplies no additional information to the model.
            actual = webapp_check.check(folder, {"path": "index.html", "cases": [
                {"set": {"units": "7.2"}, "click": ["compute"], "expect": {"total": "36"}}]})
            verifier = execute(folder, "run_verifier", {})
        native_browser.close()
        saved = json.loads((folder / ".local-ai/webapp-check.json").read_text(encoding="utf-8"))
        cases = [case for check in saved.get("checks", []) for case in check.get("cases", [])]
        submitted = any(case.get("set") == {"units": "7.2"}
            and any(isinstance(target, str) and target.casefold() == "compute" for target in case.get("click", []))
            and case.get("expect", {}).get("total") == "36" for case in cases)
        aligned = any(item["active_step_check_aligned"] for item in stats["operations"])
        unchanged = page.read_bytes() == original
        grade = {"source_unchanged": unchanged, "saved_case_submits_and_expects_36": submitted,
            "sql_definition_aligned": aligned, "sql_unit_completed": queue["done"] == 1 and queue["remaining"] == 0,
            "real_output_36": actual.get("passed") is True,
            "global_verifier_passed": verifier.startswith("Exit code: 0\n")}
        return {"scenario": "isolated_form_case_repair", "seconds": round(time.monotonic() - started, 2),
            **stats, "inference_error": failure, "checkpoint_status": state.get("status"),
            "initial_failed_attempts": 1, "source_sha256": hashlib.sha256(original).hexdigest(),
            "worker_turns_per_slice": 1,
            "checkpoint_current": state.get("current"),
            "completion_issue": native_agent.completion_issue(folder, project, state),
            "steps": {key: queue[key] for key in ("total", "done", "remaining")}, "grade": grade,
            "passed": failure is None and all(grade.values()),
            "limitation": "Seeded mistaken no-submit checker on an existing correct tiny app; not an app build."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=60)
    args = parser.parse_args()
    if not 0 < args.max_seconds <= 300:
        parser.error("Use an inference budget above zero and at most 300 seconds")
    report = run(args.runtime_root.resolve(strict=True), args.max_seconds)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    raise SystemExit(0 if report["passed"] else 1)
