"""Local-only repair fixture: a correct app with one overstrict saved assertion."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import native_worker_smoke as worker

import model_library
import native_browser
import task_context
import webapp_check


def observed_router(stats: dict):
    """Observe production routing without replacing native feedback or eligibility."""
    import cloud_ai

    class ObservedRouter(cloud_ai.Router):
        def complete(self, payload, task_id="", **kwargs):
            started = time.monotonic()
            result = super().complete(payload, task_id, **kwargs)
            stats["model_calls"] += 1
            calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
            stats["tool_calls"] += len(calls)
            stats["tools_used"].extend(str(item.get("function", {}).get("name", "")) for item in calls)
            for name in ("prompt_tokens", "completion_tokens"):
                stats[name] += int((result.get("usage") or {}).get(name) or 0)
            stats["routes"].append({"model": self.last_local_model,
                "weights": self.last_local_weights, "repair_avoid": kwargs.get("repair_avoid", []),
                "seconds": round(time.monotonic() - started, 2)})
            return result

    return ObservedRouter()

PAGE = '''<label for="bill">Bill</label><input id="bill" type="number" value="100">
<p id="answer"></p><p id="error" hidden>Invalid</p><script>
const bill=document.getElementById('bill'),out=document.getElementById('answer'),err=document.getElementById('error');
function update(){const n=Number(bill.value),valid=n>0;out.hidden=!valid;err.hidden=valid;
if(valid)out.textContent='$'+(n*1.2).toFixed(2);}bill.addEventListener('input',update);update();</script>'''
BRIEF = (
    "Repair the failing saved browser verification for index.html. Preserve the HTML: "
    "its required behavior is already correct. With Bill input 100, answer is $120.00; "
    "with 80 it is $96.00. Bill zero is invalid: error is visible and answer is hidden. "
    "The request requires hiding an old answer, not clearing its DOM text. Keep the "
    "other saved checks and input actions. Use the recorded failure evidence to correct "
    "the mistaken assertion and run the repeatable verifier. Plan at least two tiny SQLite steps."
)
BAD = {"path": "index.html", "cases": [
    {"set": {"bill": "80"}, "expect": {"answer": "96.00"}},
    {"set": {"bill": "0"}, "expect": {"answer": "", "error": "Invalid"}}]}


def grade(folder: Path, source_sha256: str) -> dict:
    source = folder / "index.html"
    fixed = source.read_bytes()
    actual = webapp_check.check(folder, {"path": "index.html", "cases": [
        {"set": {"bill": "80"}, "expect": {"answer": "96.00"}},
        {"set": {"bill": "0"}, "visible": ["error"], "hidden": ["answer"]}]})
    result = {"source_preserved": hashlib.sha256(fixed).hexdigest() == source_sha256,
              "app_behavior": actual["passed"], "verifier_passes_correct": False,
              "verifier_rejects_invalid_state_regression": False}
    script = folder / ".local-ai/verify.ps1"
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)]
    try:
        checked = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=45, check=False)
        result["verifier_passes_correct"] = checked.returncode == 0
        source.write_text(PAGE.replace("valid=n>0", "valid=true"), encoding="utf-8")
        rejected = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=45, check=False)
        result["verifier_rejects_invalid_state_regression"] = rejected.returncode != 0
    finally:
        source.write_bytes(fixed)
    result["passed"] = all(result.values())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="cale-fast")
    parser.add_argument("--auto-route", action="store_true",
                        help="Use the real local-only router, including persisted tool-failure feedback")
    parser.add_argument("--runtime-root", type=Path,
                        help="Read candidate tool-probe approvals from an installed runtime")
    parser.add_argument("--slices", type=int, default=2)
    parser.add_argument("--keep-folder")
    args = parser.parse_args()
    if args.runtime_root and not (args.runtime_root / "LongTasks/model-approvals.json").is_file():
        parser.error("Runtime root must contain actual candidate model approvals")
    if not args.auto_route:
        info = model_library.post("/api/show", {"model": args.model})
        if info.get("remote_host") or info.get("remote_model"):
            parser.error("Only physically installed local weights may run this fixture")
    context = nullcontext(Path(args.keep_folder).resolve()) if args.keep_folder else tempfile.TemporaryDirectory()
    with context as temporary:
        folder = Path(temporary)
        if args.keep_folder:
            folder.mkdir(parents=True, exist_ok=False)
        notes = folder / ".local-ai"
        notes.mkdir()
        (folder / "index.html").write_text(PAGE, encoding="utf-8", newline="")
        source_sha256 = hashlib.sha256((folder / "index.html").read_bytes()).hexdigest()
        (folder / "AGENTS.md").write_text("Preserve the correct HTML. Fix only the erroneous saved browser assertion.")
        (notes / "USER-REQUESTS.md").write_text("")
        (notes / "PLAN.md").write_text(worker.long_tasks.INITIAL_PLAN)
        (notes / "PROGRESS.md").write_text(worker.long_tasks.INITIAL_PROGRESS)
        worker.long_tasks.save_original_request(folder, "benchmark", BRIEF)
        project = {"id": "benchmark", "folder": str(folder), "brief": BRIEF}
        stats = {"model_calls": 0, "tool_calls": 0, "tools_used": [], "prompt_tokens": 0, "completion_tokens": 0,
                 "assertion_revision_attempts": 0, "successful_assertion_revisions": 0,
                 "step_tool_errors": [], "revision_errors": []}
        stats["routes"] = []
        # Keep the native worker's automatic call path intact. A callback would
        # bypass its feedback recorder and fail to exercise this feature.
        router = observed_router(stats) if args.auto_route else None
        complete = None if args.auto_route else worker.ollama_completion(args.model, 120, stats)
        router_context = patch("cloud_ai.Router", return_value=router) if args.auto_route else nullcontext()
        worker.native_agent.PROJECT_COMMAND_TIMEOUT = 15
        started = time.monotonic()
        execute = worker.native_agent.execute_tool
        def observed_execute(folder, name, arguments):
            result = execute(folder, name, arguments)
            if name == "task_steps" and task_context.action_outcome(name, result).startswith("Failed"):
                stats["step_tool_errors"] = (stats["step_tool_errors"] + [result[:400]])[-8:]
            if name == "check_webapp" and "saved_sequence" in arguments:
                stats["assertion_revision_attempts"] += 1
                try:
                    verdict = json.loads(result)
                    if verdict.get("passed") is True and verdict.get("verifier_check_passed") is True:
                        stats["successful_assertion_revisions"] += 1
                except ValueError:
                    stats["revision_errors"] = (stats["revision_errors"] + [result[:400]])[-8:]
            return result
        with ExitStack() as stack, router_context, patch("routing_mode.load_scope", return_value="local_only"), \
             patch.object(worker.long_tasks, "projects", return_value=[project]), \
             patch.object(worker.native_agent, "execute_tool", side_effect=observed_execute):
            if args.runtime_root:
                # Reuse actual tool-probe approvals, never invent an approval to
                # force a desired model into the eligible candidates.
                stack.enter_context(patch.object(model_library, "ROOT", args.runtime_root.resolve()))
            webapp_check.save_verifier(folder, BAD, accumulate=True)
            seed_args = {"path": "index.html", "cases": [{"set": {"bill": "50"}, "expect": {"answer": "60.00"}}]}
            seeded = worker.native_agent.execute_tool(folder, "check_webapp", seed_args)
            if json.loads(seeded)["passed"]:
                raise RuntimeError("The overstrict fixture did not fail")
            task_context.record_action(folder, "check_webapp", seeded, seed_args)
            for _ in range(args.slices):
                state = worker.native_agent.run_slice(project, complete=complete)
                if state.get("status") != "continue":
                    break
        native_browser.close()
        elapsed = time.monotonic() - started
        result = grade(folder, source_sha256)
        queue = task_context.step_status(folder)
        result["steps_completed"] = queue["total"] >= 2 and queue["remaining"] == 0
        result["passed"] = result["passed"] and result["steps_completed"]
        report = {"scenario": "saved_browser_assertion_repair", "model": "automatic_local" if args.auto_route else args.model,
                  "seconds_including_seed_check": round(elapsed, 2), **stats,
                  "status": state["status"], "grade": result,
                  "steps": {key: queue[key] for key in ("total", "done", "remaining")}}
        if args.keep_folder:
            (folder / "benchmark-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return 0 if state["status"] == "done" and result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
