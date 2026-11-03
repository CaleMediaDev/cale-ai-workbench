"""Build a synthetic game through the real local-only native worker and grade it.

No original user game is opened or changed. This is an opt-in, bounded developer
fixture, not a general model ranking or a visual quality benchmark.
"""

import argparse
import json
import re
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
import native_browser
import task_context
import webapp_check

BRIEF = model_fitness.GAME_BUILD_BRIEF.replace(
    'Return JSON {"html":"complete offline space shooter HTML"}.',
    'Create index.html, a complete offline HTML space shooter, using your project tools.'
) + (
    " Save actual files, not JSON in chat. Use task_steps for at least two tiny "
    "SQLite units, finish their real checks and create/run a repeatable verifier. "
    "Show a visible HUD with score, lives and wave. Test real keyboard movement, "
    "shooting and restart, not just text presence. Expose the requested window.game "
    "object as the actual live game state, not a separate test implementation. "
    "Keep all JavaScript inline in index.html for this single-file game."
)


def grade_browser(folder: Path) -> dict:
    """Check actual keyboard input and layout on the new fixture, offline."""
    from playwright.sync_api import sync_playwright

    result = {}
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(offline=True, viewport={"width": 375, "height": 812})
            context.route("**/*", webapp_check.project_only_route(folder))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)[:300]))
            page.goto((folder / "index.html").as_uri(), wait_until="load", timeout=15_000)
            page.wait_for_timeout(150)
            result["mobile_fit"] = page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
            result["canvas_visible"] = page.locator("canvas").count() == 1 and page.locator("canvas").is_visible()
            page.screenshot(path=str(folder / "independent-mobile.png"))
            page.evaluate("window.game.reset()")
            before = page.evaluate("window.game.player.x")
            page.keyboard.down("ArrowRight")
            page.wait_for_timeout(200)
            page.keyboard.up("ArrowRight")
            after = page.evaluate("window.game.player.x")
            result["keyboard_moves_actual_player"] = after > before + 5
            page.keyboard.down("Space")
            page.wait_for_timeout(80)
            result["keyboard_shoots"] = page.evaluate("window.game.bullets.length>0")
            page.keyboard.up("Space")
            page.evaluate("window.game.lives=0;window.game.playing=false")
            page.keyboard.press("Enter")
            result["keyboard_restarts"] = page.evaluate("window.game.playing&&window.game.lives===3&&window.game.score===0")
            page.screenshot(path=str(folder / "independent-mobile.png"))
        except Exception as error:
            errors.append(f"{type(error).__name__}: {str(error)[:400]}")
        finally:
            browser.close()
    return {"checks": result, "errors": errors,
            "passed": len(result) == 5 and all(value is True for value in result.values()) and not errors}


def grade(folder: Path, runtime: Path) -> dict:
    path = folder / "index.html"
    if not path.is_file():
        return {"passed": False, "errors": ["index.html was not created"]}
    fixed = path.read_bytes()
    logic = model_fitness.grade_game_html(fixed.decode("utf-8-sig"), runtime)
    browser = grade_browser(folder)
    result = {"logic": logic, "browser": browser, "verifier_passes_correct": False,
              "verifier_rejects_missing_javascript": False}
    script = folder / ".local-ai/verify.ps1"
    if script.is_file():
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)]
        try:
            checked = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=45, check=False)
            result["verifier_passes_correct"] = checked.returncode == 0
            path.write_text(re.sub(r"<script\b[^>]*>.*?</script>", "", fixed.decode("utf-8-sig"),
                                   flags=re.I | re.S), encoding="utf-8")
            rejected = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                      capture_output=True, timeout=45, check=False)
            result["verifier_rejects_missing_javascript"] = rejected.returncode != 0
        except subprocess.SubprocessError as error:
            result["verifier_error"] = str(error)[:300]
        finally:
            path.write_bytes(fixed)
    queue = task_context.step_status(folder)
    result["steps"] = {key: queue[key] for key in ("total", "done", "remaining")}
    result["passed"] = (len(logic) == 13 and all(value is True for value in logic.values())
                        and browser["passed"] and result["verifier_passes_correct"]
                        and result["verifier_rejects_missing_javascript"]
                        and queue["total"] >= 2 and queue["remaining"] == 0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--keep-folder", type=Path, required=True)
    parser.add_argument("--slices", type=int, default=2)
    parser.add_argument("--resume", action="store_true",
                        help="Resume this fixture's saved worker and queue; retain its original report")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve(strict=True)
    if not (runtime / "LongTasks/model-approvals.json").is_file():
        parser.error("Use an installed runtime with actual model tool-probe approvals")
    folder = args.keep_folder.resolve()
    notes = folder / ".local-ai"
    if args.resume:
        original = json.loads((folder / "benchmark-report.json").read_text())
        if original.get("scenario") != "native_game_build" or original.get("brief") != BRIEF:
            parser.error("Resume only this synthetic fixture with the same complete brief")
        if (folder / "benchmark-resume-report.json").exists():
            parser.error("A resume report already exists; preserve it before another run")
    else:
        folder.mkdir(parents=True, exist_ok=False)
        notes.mkdir()
        (folder / "AGENTS.md").write_text("Build and verify the whole requested game using real tool calls.")
        (notes / "USER-REQUESTS.md").write_text("")
        (notes / "PLAN.md").write_text(long_tasks.INITIAL_PLAN)
        (notes / "PROGRESS.md").write_text(long_tasks.INITIAL_PROGRESS)
        long_tasks.save_original_request(folder, "benchmark", BRIEF)
    project = {"id": "benchmark", "folder": str(folder), "brief": BRIEF}
    initial_queue = task_context.step_status(folder)
    initial_turns = native_agent.read_state(project).get("turns", 0)
    stats = {"model_calls": 0, "tool_calls": 0, "prompt_tokens": 0,
             "completion_tokens": 0, "tools_used": [], "routes": []}
    router = observed_router(stats)
    state = {}
    failure = None
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
            failure = f"{type(error).__name__}: {str(error)[:500]}"
        finally:
            native_browser.close()
        elapsed = time.monotonic() - started
        try:
            graded = grade(folder, runtime)
        except Exception as error:
            graded = {"passed": False, "error": f"{type(error).__name__}: {str(error)[:500]}"}
    report = {"scenario": "native_game_build", "brief": BRIEF, "seconds": round(elapsed, 2),
              "automatic_step_checks": state.get("automatic_step_checks", 0),
              "status": state.get("status"), "error": failure, **stats, "grade": graded,
              "resumed": args.resume, "initial_turns": initial_turns,
              "initial_steps": {key: initial_queue[key] for key in ("total", "done", "remaining")}}
    report_path = folder / ("benchmark-resume-report.json" if args.resume else "benchmark-report.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if state.get("status") == "done" and graded["passed"] and not failure else 1


if __name__ == "__main__":
    raise SystemExit(main())
