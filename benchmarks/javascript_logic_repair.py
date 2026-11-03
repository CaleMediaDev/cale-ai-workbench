"""Repair real JS game helpers through local tools, then independently grade them.

This is a pure-module fixture, not a complete playable game or visual benchmark.
No cloud account, original user project, model installation or app task is used.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from browser_assertion_repair import observed_router

import long_tasks
import model_fitness
import model_library
import native_agent
import task_context

STARTER = """function movePlayer(x,direction,seconds,width,speed){return x+direction*speed}
function overlaps(a,b){return a.x<=b.x+b.w&&a.x+a.w>=b.x&&a.y<=b.y+b.h&&a.y+a.h>=b.y}
function advanceWave(wave,remaining,spawned){return remaining===0?wave+1:wave}
function tickCooldown(remaining,seconds){return remaining-seconds}
"""
BRIEF = """Repair logic.js, the actual plain-script module loaded by a browser game.
Preserve its four top-level function APIs and plain function-declaration format:
movePlayer(x,direction,seconds,width,speed) moves by direction*speed*seconds and
clamps the player's center to [20,width-20]. Inputs have direction -1..1, width>=40,
finite numbers and nonnegative seconds. Verify left/right bounds and frame-independent timing.
overlaps(a,b) takes top-left x,y,w,h rectangles. Only positive-area overlap is true;
touching edges and separated rectangles are false.
advanceWave(wave,remaining,spawned) adds one only when remaining===0 AND spawned is true.
tickCooldown(remaining,seconds) subtracts seconds and clamps at zero.
Use one small SQLite unit per function, four completed units total. Save meaningful
check_javascript_function input/expected acceptance cases in each unit, including
the explicit boundaries above. Run the accumulated read-only verifier. No PowerShell
test code is needed. Do not create a test-only copy or change the public APIs.
Keep PLAN.md and PROGRESS.md accurate. This request is for the helper module only.
"""
MUTANTS = {
    "frame_time": "globalThis.movePlayer=(x,d,dt,w,s)=>Math.max(20,Math.min(w-20,x+d*s));",
    "touching_edges": "globalThis.overlaps=(a,b)=>a.x<=b.x+b.w&&a.x+a.w>=b.x&&a.y<=b.y+b.h&&a.y+a.h>=b.y;",
    "unspawned_wave": "globalThis.advanceWave=(w,n,spawned)=>n===0?w+1:w;",
    "negative_cooldown": "globalThis.tickCooldown=(r,dt)=>r-dt;",
}


def grade(folder: Path, runtime: Path) -> dict:
    source = folder / "logic.js"
    if not source.is_file():
        return {"passed": False, "error": "The requested logic.js was not created"}
    fixed = source.read_bytes()
    checks = model_fitness.grade_game_code(fixed.decode("utf-8-sig"), runtime)
    result = {"checks": checks, "verifier_passes_correct": False, "mutations": {}}
    verifier = folder / ".local-ai/verify.ps1"
    if verifier.is_file():
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(verifier)]
        try:
            result["verifier_passes_correct"] = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                capture_output=True, timeout=45, check=False).returncode == 0
            for name, replacement in MUTANTS.items():
                mutant = fixed.decode("utf-8-sig") + "\n" + replacement
                observed = model_fitness.grade_game_code(mutant, runtime)
                meaningful = len(observed) == 13 and any(value is False for value in observed.values())
                source.write_text(mutant, encoding="utf-8")
                rejected = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                    capture_output=True, timeout=45, check=False).returncode != 0
                result["mutations"][name] = {"valid_behavior_mutation": meaningful, "verifier_rejected": rejected}
        finally:
            source.write_bytes(fixed)
    queue = task_context.step_status(folder)
    result["steps"] = {key: queue[key] for key in ("total", "done", "remaining")}
    result["passed"] = (len(checks) == 13 and all(value is True for value in checks.values())
        and result["verifier_passes_correct"] and len(result["mutations"]) == 4
        and all(m["valid_behavior_mutation"] and m["verifier_rejected"] for m in result["mutations"].values())
        and queue["total"] >= 4 and queue["remaining"] == 0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--keep-folder", type=Path, required=True)
    parser.add_argument("--slices", type=int, default=2)
    parser.add_argument("--resume", action="store_true", help="Continue this fixture's existing checkpoint without resetting its code or steps")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve(strict=True)
    if not (runtime / "LongTasks/model-approvals.json").is_file():
        parser.error("Use an installed runtime's actual tool-probe approvals")
    folder = args.keep_folder.resolve()
    if args.resume:
        if not (folder / ".local-ai/native-worker.json").is_file():
            parser.error("--resume requires this fixture's existing native worker checkpoint")
    else:
        folder.mkdir(parents=True, exist_ok=False)
    notes = folder / ".local-ai"
    if not args.resume:
        notes.mkdir()
        (folder / "logic.js").write_text(STARTER)
        (folder / "AGENTS.md").write_text("Repair and check the real module. Keep its APIs and retain earlier cases.")
        (notes / "USER-REQUESTS.md").write_text("")
        (notes / "PLAN.md").write_text(long_tasks.INITIAL_PLAN)
        (notes / "PROGRESS.md").write_text(long_tasks.INITIAL_PROGRESS)
        long_tasks.save_original_request(folder, "benchmark", BRIEF)
    project = {"id": "benchmark", "folder": str(folder), "brief": BRIEF}
    stats = {"model_calls": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "tools_used": [], "routes": []}
    router = observed_router(stats)
    failure = None
    state = {}
    started = time.monotonic()
    with (patch("routing_mode.load_scope", return_value="local_only"),
          patch("cloud_ai.Router", return_value=router), patch.object(model_library, "ROOT", runtime),
          patch.object(long_tasks, "projects", return_value=[project])):
        try:
            for _ in range(args.slices):
                state = native_agent.run_slice(project)
                if state.get("status") != "continue":
                    break
        except Exception as error:
            failure = f"{type(error).__name__}: {str(error)[:600]}"
            state = native_agent.read_state(project)
        elapsed = time.monotonic() - started
        try:
            graded = grade(folder, runtime)
        except Exception as error:
            graded = {"passed": False, "error": f"{type(error).__name__}: {str(error)[:500]}"}
    report = {"scenario": "javascript_logic_repair", "scope": __doc__, "brief": BRIEF, "resumed": args.resume,
              "seconds": round(elapsed, 2), **stats, "checkpoint_status": state.get("status"),
              "inference_error": failure, "grade": graded}
    report["run_outcome"] = "passed" if state.get("status") == "done" and graded["passed"] and not failure else "failed"
    (folder / "benchmark-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["run_outcome"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
