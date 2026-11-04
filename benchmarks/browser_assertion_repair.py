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


def require_idle_media(runtime: Path) -> None:
    """Never let a development inference contend with a live user media worker."""
    import psutil

    for kind in ("music", "image"):
        for marker in (runtime / "LongTasks").glob(f"{kind}-gpu-active-*.json"):
            try:
                pid = int(marker.stem.rsplit("-", 1)[-1])
                process = psutil.Process(pid)
                if process.is_running() and process.create_time() <= marker.stat().st_mtime + 2:
                    raise RuntimeError(f"Benchmark deferred: active {kind} worker {pid} needs the GPU")
            except (OSError, ValueError, psutil.Error):
                continue


def observed_router(stats: dict):
    """Observe production routing without replacing native feedback or eligibility."""
    import cloud_ai

    class ObservedRouter(cloud_ai.Router):
        def complete(self, payload, task_id="", **kwargs):
            require_idle_media(model_library.ROOT)
            started = time.monotonic()
            stats["router_call_attempts"] = stats.get("router_call_attempts", 0) + 1
            post = cloud_ai.post
            def observed_post(url, body, *args, **options):
                inference = url.endswith("/v1/chat/completions")
                if inference:
                    require_idle_media(model_library.ROOT)  # Include internal schema/empty-output retries.
                    stats["upstream_local_calls"] = stats.get("upstream_local_calls", 0) + 1
                response = post(url, body, *args, **options)
                if inference:
                    for name in ("prompt_tokens", "completion_tokens"):
                        stats[name] += int((response.get("usage") or {}).get(name) or 0)
                    # Retain transport shapes before schema validation rejects
                    # a response, without logging prompts or argument values.
                    for choice in response.get("choices", []) or []:
                        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                            continue
                        for call in choice["message"].get("tool_calls", []) or []:
                            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                                continue
                            function = call.get("function") or {}
                            if function.get("name") != "task_steps":
                                continue
                            try:
                                arguments = json.loads(function.get("arguments", "{}"))
                            except (ValueError, TypeError):
                                continue
                            if not isinstance(arguments, dict) or "steps" not in arguments:
                                continue
                            value, layers = arguments["steps"], []
                            for _ in range(3):
                                entry = {"type": type(value).__name__}
                                if isinstance(value, str):
                                    entry.update(length=len(value), first=value.lstrip()[:1])
                                layers.append(entry)
                                if not isinstance(value, str) or len(value) > 64000:
                                    break
                                try:
                                    value = json.loads(value)
                                except ValueError as error:
                                    entry["decode_error"] = error.msg
                                    entry["decode_position"] = error.pos
                                    break
                            stats.setdefault("step_transport_shapes", []).append(layers)
                return response
            with patch.object(cloud_ai, "post", side_effect=observed_post):
                result = super().complete(payload, task_id, **kwargs)
            stats["model_calls"] += 1
            calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
            stats["tool_calls"] += len(calls)
            stats["tools_used"].extend(str(item.get("function", {}).get("name", "")) for item in calls)
            stats["routes"].append({"model": self.last_local_model,
                "weights": self.last_local_weights, "repair_avoid": kwargs.get("repair_avoid", []),
                "size_hint": kwargs.get("size_hint"),
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


def grade(folder: Path, source_sha256: str, *, calculation_mutation: bool = False) -> dict:
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
        if calculation_mutation:
            path = folder / "index.html"
            path.write_bytes(fixed.replace(b'n*1.2', b'n*1.1'))
            rejected = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                      capture_output=True, timeout=45, check=False)
            result["verifier_rejects_wrong_calculation"] = rejected.returncode != 0
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
    parser.add_argument("--input-step", action="store_true",
                        help="Repair a prepared input acceptance unit that only observes an unchanged error label")
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
        brief = (
            "The existing calculator HTML is correct. Preserve its source bytes. Its current SQLite "
            "input step is weak: it sets Bill to 80 but only checks that error stays hidden. That "
            "does not establish the required calculated answer $96.00. Correct the acceptance cases "
            "or add a positive control while preserving valid invariant checks. Use real browser input "
            "and the actual saved outputs. Bill 100 must produce $120.00, Bill 80 must produce $96.00; "
            "zero is invalid, showing error and hiding answer. Two SQLite steps are already prepared; "
            "repair and complete both and run the retained verifier. Do not edit the working HTML."
            if args.input_step else BRIEF)
        (folder / "index.html").write_text(PAGE, encoding="utf-8", newline="")
        source_sha256 = hashlib.sha256((folder / "index.html").read_bytes()).hexdigest()
        (folder / "AGENTS.md").write_text("Preserve the correct HTML. Fix only the erroneous saved browser assertion.")
        (notes / "USER-REQUESTS.md").write_text("")
        (notes / "PLAN.md").write_text(worker.long_tasks.INITIAL_PLAN)
        (notes / "PROGRESS.md").write_text(worker.long_tasks.INITIAL_PROGRESS)
        worker.long_tasks.save_original_request(folder, "benchmark", brief)
        project = {"id": "benchmark", "folder": str(folder), "brief": brief}
        stats = {"model_calls": 0, "tool_calls": 0, "tools_used": [], "prompt_tokens": 0, "completion_tokens": 0,
                 "assertion_revision_attempts": 0, "successful_assertion_revisions": 0,
                 "step_tool_errors": [], "revision_errors": [], "step_revisions": 0}
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
            if name == "task_steps" and arguments.get("action") == "revise":
                stats["step_revisions"] += 1
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
            if args.input_step:
                weak = {"path": "index.html", "cases": [{"set": {"bill": "80"}, "hidden": ["error"]}]}
                queue = task_context.plan_steps(folder, [
                    {"phase": "Calculation", "title": "Verify changed Bill total", "check_tool": "check_webapp", "check_arguments": weak},
                    {"phase": "Boundary", "title": "Verify invalid zero", "check_tool": "check_webapp", "check_arguments": {
                        "path": "index.html", "cases": [{"set": {"bill": "0"}, "visible": ["error"], "hidden": ["answer"]}]}}])
                seed_args = {"action": "complete", "step_id": queue["active"]["id"]}
                seeded = worker.native_agent.execute_tool(folder, "task_steps", seed_args)
                task_context.record_action(folder, "task_steps", seeded, seed_args)
            else:
                webapp_check.save_verifier(folder, BAD, accumulate=True)
                seed_args = {"path": "index.html", "cases": [{"set": {"bill": "50"}, "expect": {"answer": "60.00"}}]}
                seeded = worker.native_agent.execute_tool(folder, "check_webapp", seed_args)
                task_context.record_action(folder, "check_webapp", seeded, seed_args)
            if json.loads(seeded)["passed"]:
                raise RuntimeError("The incorrect acceptance fixture did not fail")
            failure = None
            try:
                for _ in range(args.slices):
                    state = worker.native_agent.run_slice(project, complete=complete)
                    if state.get("status") != "continue":
                        break
            except Exception as error:
                failure = f"{type(error).__name__}: {str(error)[:600]}"
                state = worker.native_agent.read_state(project)
        native_browser.close()
        elapsed = time.monotonic() - started
        result = grade(folder, source_sha256, calculation_mutation=args.input_step)
        queue = task_context.step_status(folder)
        result["steps_completed"] = queue["total"] >= 2 and queue["remaining"] == 0
        result["passed"] = result["passed"] and result["steps_completed"]
        report = {"scenario": "weak_input_step_repair" if args.input_step else "saved_browser_assertion_repair",
                  "prepared_steps": args.input_step, "model": "automatic_local" if args.auto_route else args.model,
                  "seconds_including_seed_check": round(elapsed, 2), **stats,
                  "status": state["status"], "checkpoint_status": state["status"],
                  "run_outcome": "passed" if state["status"] == "done" and result["passed"] and not failure else "failed",
                  "inference_error": failure, "grade": result,
                  "steps": {key: queue[key] for key in ("total", "done", "remaining")}}
        if args.keep_folder:
            (folder / "benchmark-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return 0 if state["status"] == "done" and result["passed"] and not failure else 1


if __name__ == "__main__":
    raise SystemExit(main())
