"""Build a new offline app, then grade real input, deletion, and persistence.

No implementation is seeded. This synthetic fixture is not a model ranking or
proof of arbitrary app-building ability. User files and browser sessions are not used.
"""

import argparse
import hashlib
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
import model_library
import native_agent
import native_browser
import task_context
import webapp_check

BRIEF = """Create index.html, a complete single-file offline expense log for a small business.
Use no accounts, external libraries, fonts, or network requests. Make it readable on a phone.
The form has accessible labels Description, Amount, and Category. Category choices are
Supplies, Travel, Meals, and Other. Its button says Add expense. Show an expense table
with Description, Amount, Category, and a Delete button on each expense row.
Show Total: $0.00 initially; display dollars with two decimal places and keep the total
correct after adding and deleting entries. Reject a blank description, zero or negative
amount, and nonnumeric amounts, showing an accessible inline error with role=alert.
Descriptions are plain text even when they contain HTML characters. Save entries on
this computer with localStorage. Reloading must preserve added entries and deletions.
Build actual working files using small SQLite steps for form validation, the expense
table/totals, and persistence. Save and run a repeatable verifier that exercises real
form input and checks persistence across page reload. Keep PLAN.md and PROGRESS.md accurate.
"""


def grade_browser(folder: Path, *, capture: bool = True) -> dict:
    from playwright.sync_api import sync_playwright

    checks, errors, remote = {}, [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(offline=True, viewport={"width": 375, "height": 812})
            context.route("**/*", webapp_check.project_only_route(folder))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)[:300]))
            page.on("request", lambda request: remote.append(request.url[:200]) if request.url.startswith(("http:", "https:")) else None)
            page.goto((folder / "index.html").as_uri(), wait_until="load", timeout=15_000)
            description = page.get_by_label(re.compile(r"^Description\s*:?\s*$", re.I))
            amount = page.get_by_label(re.compile(r"^Amount(?:\s*\(\$\))?\s*:?\s*$", re.I))
            category = page.get_by_label(re.compile(r"^Category\s*:?\s*$", re.I))
            add = page.get_by_role("button", name=re.compile(r"^Add\s+expense$", re.I))

            def rows():
                return page.get_by_role("row").filter(has=page.get_by_role("button", name=re.compile(r"^Delete\b", re.I)))

            def total():
                match = re.search(r"Total\s*:\s*\$\s*(\d+\.\d{2})", page.locator("body").inner_text(), re.I)
                return match[1] if match else None

            def enter(text, value, kind="Other"):
                description.fill(text)
                amount.fill(value)
                options = category.evaluate("el => Array.from(el.options, option => ({value:option.value,label:option.label}))")
                chosen = next(option for option in options if option["label"].strip().casefold() == kind.casefold())
                category.select_option(value=chosen["value"])
                add.click()

            labels = category.evaluate("el => Array.from(el.options, option => option.label.trim().toLowerCase())")
            checks["requested_category_choices"] = {"supplies", "travel", "meals", "other"} <= set(labels)
            def error_visible():
                return any(alert.is_visible() for alert in page.get_by_role("alert").all())
            checks["initial_empty"] = rows().count() == 0 and total() == "0.00"
            enter("", "5.00")
            checks["reject_blank"] = rows().count() == 0 and error_visible()
            enter("Invalid", "-2.00")
            checks["reject_negative"] = rows().count() == 0 and total() == "0.00" and error_visible()
            enter("Invalid", "0")
            checks["reject_zero"] = rows().count() == 0 and total() == "0.00" and error_visible()
            # Native number inputs reject nonnumeric text before submit; text
            # inputs must reject it through the app's validation instead.
            if amount.get_attribute("type") == "number":
                amount.evaluate("el => el.value = 'nonsense'")
                add.click()
            else:
                enter("Invalid", "nonsense")
            checks["reject_nonnumeric"] = rows().count() == 0 and total() == "0.00" and error_visible()
            enter("Wash supplies", "12.50", "Supplies")
            enter("Fuel", "7.25", "Travel")
            checks["adds_two_and_totals"] = rows().count() == 2 and total() == "19.75"
            checks["category_visible"] = rows().filter(has_text="Wash supplies").get_by_role("cell", name=re.compile(r"^Supplies$", re.I)).count() == 1
            page.reload(wait_until="load")
            checks["reload_keeps_entries"] = rows().count() == 2 and total() == "19.75"
            rows().filter(has_text="Wash supplies").get_by_role("button", name=re.compile(r"^Delete\b", re.I)).click()
            checks["delete_updates_total"] = rows().count() == 1 and total() == "7.25"
            page.reload(wait_until="load")
            checks["reload_keeps_deletion"] = rows().count() == 1 and total() == "7.25" and rows().filter(has_text="Wash supplies").count() == 0
            enter("<b>Small item</b>", "0.10")
            enter("Second small item", "0.20")
            checks["description_plain_text"] = page.get_by_role("cell", name="<b>Small item</b>", exact=True).count() == 1
            rows().filter(has_text="Fuel").get_by_role("button", name=re.compile(r"^Delete\b", re.I)).click()
            checks["decimal_totals"] = rows().count() == 2 and total() == "0.30"
            checks["mobile_fit"] = page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            checks["no_remote_requests"] = not remote
            if capture:
                page.screenshot(path=str(folder / "independent-mobile.png"), full_page=True)
        except Exception as error:
            errors.append(f"{type(error).__name__}: {str(error)[:500]}")
        finally:
            browser.close()
    return {"checks": checks, "errors": errors, "remote_requests": remote, "passed": len(checks) == 15 and all(checks.values()) and not errors}


def grade(folder: Path) -> dict:
    page = folder / "index.html"
    if not page.is_file():
        return {"passed": False, "error": "index.html was not created"}
    result = {"browser": grade_browser(folder), "verifier_passes_correct": False, "verifier_rejects_missing_javascript": False}
    source = page.read_bytes()
    verifier = folder / ".local-ai/verify.ps1"
    if verifier.is_file():
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(verifier)]
        try:
            result["verifier_passes_correct"] = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode == 0
        except subprocess.TimeoutExpired:
            result["baseline_verifier_timeout"] = True
        if not result["verifier_passes_correct"]:
            # An already failing verifier rejecting a mutation proves nothing.
            # Keep the original source and browser evidence; skip expensive
            # mutation runs without relaxing the all-required pass criterion.
            result["mutation_status"] = "not_run_baseline_verifier_failed"
            queue = task_context.step_status(folder)
            result["steps"] = {key: queue[key] for key in ("total", "done", "remaining")}
            result["passed"] = False
            return result
        try:
            page.write_text(re.sub(r"<script\b[^>]*>.*?</script>", "", source.decode("utf-8-sig"), flags=re.I | re.S), encoding="utf-8")
            result["verifier_rejects_missing_javascript"] = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode != 0
            mutations = {
                "storage_writes": ("Storage.prototype.setItem=function(){};", "reload_keeps_entries"),
                "deletion_persistence": ("""(()=>{const set=Storage.prototype.setItem;
                    Storage.prototype.setItem=function(key,value){const old=this.getItem(key);
                    if(old!==null&&String(value).length<old.length)return;return set.call(this,key,value)};
                    Storage.prototype.removeItem=function(){};})();""", "reload_keeps_deletion"),
            }
            result["persistence_mutations"] = {}
            original = source.decode("utf-8-sig")
            for name, (hook, target) in mutations.items():
                mutated = re.sub(r"<script\b", lambda match: "<script>" + hook + "</script>" + match[0], original, count=1, flags=re.I)
                page.write_text(mutated, encoding="utf-8")
                observed = grade_browser(folder, capture=False)
                meaningful = (result["browser"].get("checks", {}).get(target) is True
                              and observed.get("checks", {}).get(target) is False)
                rejected = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode != 0
                result["persistence_mutations"][name] = {"valid_behavior_mutation": meaningful, "verifier_rejected": rejected}
        finally:
            page.write_bytes(source)
            result["verifier_passes_restored"] = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode == 0
    queue = task_context.step_status(folder)
    result["steps"] = {key: queue[key] for key in ("total", "done", "remaining")}
    result["passed"] = result["browser"]["passed"] and result["verifier_passes_correct"] and result["verifier_rejects_missing_javascript"] and result.get("verifier_passes_restored") is True and len(result.get("persistence_mutations", {})) == 2 and all(item["valid_behavior_mutation"] and item["verifier_rejected"] for item in result["persistence_mutations"].values()) and queue["total"] >= 3 and queue["remaining"] == 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--keep-folder", type=Path, required=True)
    parser.add_argument("--slices", type=int, default=2)
    parser.add_argument("--resume", action="store_true", help="Continue only this synthetic fixture; retain prior evidence")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve(strict=True)
    if not (runtime / "LongTasks/model-approvals.json").is_file():
        parser.error("Use an installed runtime's actual model approvals")
    folder = args.keep_folder.resolve()
    notes = folder / ".local-ai"
    if args.resume:
        original = long_tasks.original_request_path(folder, "benchmark")
        if (not (notes / "native-worker.json").is_file() or not original.is_file()
                or original.read_text(encoding="utf-8") != BRIEF):
            parser.error("Resume only this synthetic fixture with its unchanged original brief")
    else:
        folder.mkdir(parents=True, exist_ok=False)
        notes.mkdir()
        (notes / "USER-REQUESTS.md").write_text("", encoding="utf-8")
        (notes / "PLAN.md").write_text(long_tasks.INITIAL_PLAN, encoding="utf-8")
        (notes / "PROGRESS.md").write_text(long_tasks.INITIAL_PROGRESS, encoding="utf-8")
        long_tasks.save_original_request(folder, "benchmark", BRIEF)
    project = {"id": "benchmark", "folder": str(folder), "brief": BRIEF}
    initial_turns = native_agent.read_state(project).get("turns", 0)
    initial_source = hashlib.sha256((folder / "index.html").read_bytes()).hexdigest() if (folder / "index.html").is_file() else None
    stats = {"model_calls": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "tools_used": [], "routes": []}
    router = observed_router(stats)
    state, failure = {}, None
    started = time.monotonic()
    with (patch("routing_mode.load_scope", return_value="local_only"), patch("cloud_ai.Router", return_value=router),
          patch.object(model_library, "ROOT", runtime), patch.object(long_tasks, "projects", return_value=[project])):
        try:
            for _ in range(args.slices):
                state = native_agent.run_slice(project)
                if state.get("status") != "continue":
                    break
        except Exception as error:
            failure = f"{type(error).__name__}: {str(error)[:500]}"
            state = native_agent.read_state(project)
        finally:
            native_browser.close()
        elapsed = time.monotonic() - started
        grading_started = time.monotonic()
        try:
            graded = grade(folder)
        except Exception as error:
            graded = {"passed": False, "error": f"{type(error).__name__}: {str(error)[:500]}"}
    passed = state.get("status") == "done" and graded["passed"] and not failure
    deferred = bool(failure and failure.startswith("RuntimeError: Benchmark deferred:"))
    report = {"scenario": "offline_expense_app", "brief": BRIEF, "seconds": round(elapsed, 2), **stats,
              "grade_seconds": round(time.monotonic() - grading_started, 2),
              "total_seconds": round(time.monotonic() - started, 2),
              "resumed": args.resume, "initial_turns": initial_turns, "initial_source_sha256": initial_source,
              "checkpoint_status": state.get("status"), "inference_error": failure, "grade": graded,
              "run_outcome": "passed" if passed else "deferred" if deferred else "failed"}
    # A resumed developer-assisted repair is not an unassisted fresh build.
    # Carry the intervention receipt into the report, even on a later success.
    assistance = notes / "benchmark-validation-observation.json"
    if assistance.is_file():
        report["diagnostic_assistance"] = json.loads(assistance.read_text(encoding="utf-8"))
    report["independent_check_diagnostics"] = [json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(notes.glob("benchmark-row*-observation.json"))]
    report_path = folder / ("benchmark-resume-report.json" if args.resume else "benchmark-report.json")
    if report_path.exists():
        report_path = report_path.with_name(report_path.stem + "-" + str(time.time_ns()) + ".json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if passed else 2 if deferred else 1


if __name__ == "__main__":
    raise SystemExit(main())
