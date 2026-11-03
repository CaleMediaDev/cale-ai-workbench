"""Run one real local-model task and grade its output independently.

This is an opt-in benchmark: it calls the local Ollama API and can consume
several minutes of CPU/GPU time. It never uses a cloud account or changes an
existing Cale task. Run it with the installed Toolbox Python on Windows.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
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

WEBAPP_BRIEF = (
    "Build a single-file index.html tip splitter that works offline. Give "
    "the three labeled number inputs the IDs bill, tip, and people, with "
    "defaults 100, 20, and 4. Show the per-person total in an element with "
    "ID per-person and any validation message in an element with ID error. "
    "Update immediately when inputs change: a $100 bill with 20% tip split "
    "four ways is $30.00 each; an $80 bill with the same tip and people is "
    "$24.00 each. People must be a positive whole number: zero and 4.5 are "
    "invalid. An empty bill is invalid too. Invalid inputs must show an error "
    "and must not leave a stale total visible. Fit a 375-pixel mobile viewport without "
    "horizontal scrolling. Create and run a repeatable PowerShell verifier "
    "that checks behavior, not just file existence."
)

DOCUMENT_BRIEF = (
    "Read terms.pdf, including its final terms on page 2. Create summary.md "
    "with these four labeled lines: 'Final due date:', 'Net payable:', "
    "'Support phone:', and 'Draft status:'. Use the exact written date and "
    "phone format from the final page. Compute net payable as the final base "
    "fee minus the setup credit, formatted with a dollar sign and comma. "
    "State that the page 1 draft is superseded. Do not use its older date, "
    "fee, or phone as the current terms. Create and run a meaningful, read-only "
    "PowerShell verifier that checks the summary against the final terms."
)

DOCUMENT_PAGES = [
    ["PAGE 1 - SUPERSEDED DRAFT", "Payment due: October 15, 2026",
     "Base fee: $980", "Setup credit: $0", "Support phone: 517-555-0100",
     "This draft is superseded by the signed final terms on page 2."],
    ["PAGE 2 - SIGNED FINAL TERMS", "Payment due: November 15, 2026",
     "Base fee: $1,280", "Setup credit: $180", "Support phone: 517-555-0142",
     "These final terms replace the page 1 draft."],
]


def write_document_pdf(path: Path) -> None:
    """Build a small two-page text PDF without a benchmark-only dependency."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 7 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
    ]
    for page in DOCUMENT_PAGES:
        lines = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                 for line in page]
        drawing = ("BT /F1 12 Tf 72 720 Td " +
                   " ".join(f"({line}) Tj 0 -22 Td" for line in lines) + " ET").encode("ascii")
        objects.append(b"<< /Length " + str(len(drawing)).encode("ascii") +
                       b" >>\nstream\n" + drawing + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    cross_reference = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
                f"startxref\n{cross_reference}\n%%EOF\n".encode("ascii"))
    path.write_bytes(data)


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


def routed_local_completion(stats: dict):
    """Exercise Cale's real model tier selection with cloud disabled."""
    import cloud_ai

    router = cloud_ai.Router()

    def complete(payload: dict) -> dict:
        result = router.complete(payload, "benchmark")
        stats["model_calls"] += 1
        stats["models_used"].append(result.get("model", ""))
        calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
        stats["tool_calls"] += len(calls)
        stats["tools_used"].extend(
            str(item.get("function", {}).get("name", "")) for item in calls)
        usage = result.get("usage") or {}
        for name in ("prompt_tokens", "completion_tokens"):
            stats[name] += int(usage.get(name) or 0)
        return result

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


def grade_webapp(folder: Path) -> dict:
    """Exercise the saved app in an offline browser and challenge its verifier."""
    html = folder / "index.html"
    verifier = folder / ".local-ai" / "verify.ps1"
    result = {"page_present": html.is_file(), "verifier_present": verifier.is_file(),
              "labels": False, "number_inputs": False, "default_total": False,
              "changed_total": False, "invalid_people": False,
              "invalid_blank_bill": False, "invalid_fractional_people": False,
              "mobile_fits": False,
              "verifier_passes_correct": False, "verifier_rejects_broken_app": False}
    if not html.is_file():
        result["passed"] = False
        return result
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(offline=True, viewport={"width": 375, "height": 812})
                page = context.new_page()
                page.goto(html.as_uri(), wait_until="load", timeout=15_000)
                inputs = [page.locator("#" + name) for name in ("bill", "tip", "people")]
                result["labels"] = all(control.count() == 1 and control.evaluate(
                    "el => !!(el.labels && el.labels.length) || !!el.getAttribute('aria-label')"
                ) for control in inputs)
                result["number_inputs"] = all(
                    control.count() == 1 and control.get_attribute("type") == "number"
                    for control in inputs
                )
                total = page.locator("#per-person")
                result["default_total"] = (total.count() == 1 and total.is_visible()
                                           and "30.00" in total.inner_text())
                inputs[0].fill("80")
                result["changed_total"] = total.is_visible() and "24.00" in total.inner_text()
                inputs[2].fill("0")
                error = page.locator("#error")
                result["invalid_people"] = (error.count() == 1 and error.is_visible()
                                            and bool(error.inner_text().strip())
                                            and (not total.is_visible()
                                                 or "24.00" not in total.inner_text()))
                inputs[2].fill("4")
                inputs[0].fill("")
                result["invalid_blank_bill"] = (
                    error.is_visible() and bool(error.inner_text().strip())
                    and (not total.is_visible() or not total.inner_text().strip())
                )
                inputs[0].fill("100")
                inputs[2].fill("4.5")
                result["invalid_fractional_people"] = (
                    error.is_visible() and bool(error.inner_text().strip())
                    and (not total.is_visible() or not total.inner_text().strip())
                )
                result["mobile_fits"] = page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1")
            finally:
                browser.close()
    except Exception as exc:
        result["browser_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    if verifier.is_file() and all(result[key] for key in (
            "labels", "number_inputs", "default_total", "changed_total",
            "invalid_people", "invalid_blank_bill", "invalid_fractional_people",
            "mobile_fits")):
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(verifier)]
        try:
            checked = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=45, check=False)
            result["verifier_passes_correct"] = checked.returncode == 0
            original = html.read_bytes()
            broken = re.sub(rb"(?is)<script\b[^>]*>.*?</script>", b"<script></script>",
                            original, count=1)
            if broken != original:
                try:
                    html.write_bytes(broken)
                    rejected = subprocess.run(
                        command, cwd=folder, stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=45, check=False)
                    result["verifier_rejects_broken_app"] = rejected.returncode != 0
                finally:
                    html.write_bytes(original)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["verifier_error"] = f"{type(exc).__name__}: {exc}"
    result["passed"] = all(value for key, value in result.items()
                           if key not in {"browser_error", "verifier_error"})
    return result


def grade_document(folder: Path) -> dict:
    """Check final-page extraction, arithmetic, and the saved verifier."""
    output = folder / "summary.md"
    verifier = folder / ".local-ai" / "verify.ps1"
    result = {"summary_present": output.is_file(), "verifier_present": verifier.is_file(),
              "final_due_date": False, "net_payable": False, "support_phone": False,
              "draft_superseded": False, "verifier_passes_correct": False,
              "verifier_rejects_wrong_total": False,
              "verifier_rejects_changed_source": False}
    if not output.is_file():
        result["passed"] = False
        return result
    summary = output.read_text(encoding="utf-8-sig", errors="replace")
    result["final_due_date"] = bool(re.search(
        r"(?im)^\s*Final due date:\s*November 15, 2026\s*$", summary))
    result["net_payable"] = bool(re.search(
        r"(?im)^\s*Net payable:\s*\$1,100(?:\.00)?\s*$", summary))
    result["support_phone"] = bool(re.search(
        r"(?im)^\s*Support phone:\s*517-555-0142\s*$", summary))
    result["draft_superseded"] = bool(re.search(
        r"(?im)^\s*Draft status:\s*.*superseded", summary))
    if verifier.is_file() and all(result[key] for key in (
            "final_due_date", "net_payable", "support_phone", "draft_superseded")):
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(verifier)]
        try:
            checked = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=45, check=False)
            result["verifier_passes_correct"] = checked.returncode == 0
            original = output.read_bytes()
            damaged = original.replace(b"$1,100", b"$9,999", 1)
            if damaged != original:
                try:
                    output.write_bytes(damaged)
                    rejected = subprocess.run(command, cwd=folder, stdin=subprocess.DEVNULL,
                                              capture_output=True, text=True, timeout=45,
                                              check=False)
                    result["verifier_rejects_wrong_total"] = rejected.returncode != 0
                finally:
                    output.write_bytes(original)
            source = folder / "terms.pdf"
            source_bytes = source.read_bytes()
            altered_source = source_bytes.replace(b"Base fee: $1,280", b"Base fee: $1,290", 1)
            if altered_source != source_bytes:
                try:
                    source.write_bytes(altered_source)
                    rejected_source = subprocess.run(
                        command, cwd=folder, stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=45, check=False)
                    result["verifier_rejects_changed_source"] = rejected_source.returncode != 0
                finally:
                    source.write_bytes(source_bytes)
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["verifier_error"] = f"{type(exc).__name__}: {exc}"
    result["passed"] = all(value for key, value in result.items()
                           if key != "verifier_error")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("temperature", "contacts", "webapp", "document"),
                        default="temperature")
    parser.add_argument("--model", default="cale-fast")
    parser.add_argument("--auto-route", action="store_true",
                        help="Use Cale's real local model router; cloud access is disabled")
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
        brief = {"temperature": TEMPERATURE_BRIEF, "contacts": CONTACTS_BRIEF,
                 "webapp": WEBAPP_BRIEF, "document": DOCUMENT_BRIEF}[args.scenario]
        if args.scenario == "temperature":
            (folder / "temperature.py").write_text(
                TEMPERATURE_STARTER, encoding="utf-8")
        elif args.scenario == "contacts":
            (folder / "contacts.csv").write_text(CONTACTS_STARTER, encoding="utf-8")
        elif args.scenario == "document":
            write_document_pdf(folder / "terms.pdf")
        project_instruction = {
            "temperature": "Fix the conversion and verify it.\n",
            "contacts": "Clean the contacts and verify the saved CSV.\n",
            "webapp": "Build the requested offline page and test it.\n",
            "document": "Read the final document terms and verify the summary.\n",
        }[args.scenario]
        (folder / "AGENTS.md").write_text(project_instruction, encoding="utf-8")
        (folder / ".local-ai" / "USER-REQUESTS.md").write_text("", encoding="utf-8")
        (folder / ".local-ai" / "PLAN.md").write_text(
            long_tasks.INITIAL_PLAN, encoding="utf-8")
        (folder / ".local-ai" / "PROGRESS.md").write_text(
            long_tasks.INITIAL_PROGRESS, encoding="utf-8")
        long_tasks.save_original_request(folder, "benchmark", brief)
        project = {"id": "benchmark", "folder": str(folder), "brief": brief}
        stats = {"model_calls": 0, "tool_calls": 0,
                 "prompt_tokens": 0, "completion_tokens": 0,
                 "tools_used": [], "models_used": []}
        complete = (routed_local_completion(stats) if args.auto_route else
                    ollama_completion(args.model, args.call_timeout, stats))
        error = None
        state = {}
        # The isolated benchmark folder is intentionally absent from the real
        # task database. Let its verifier use the same contained supervisor
        # path as a registered task without creating a visible app task.
        # A benchmark must never consume a connected free or paid account.
        # This patch also covers a user who changes the visible mode mid-run.
        local_scope = patch("routing_mode.load_scope", return_value="local_only")
        with local_scope, patch.object(long_tasks, "projects", return_value=[project]):
            for _ in range(args.slices):
                try:
                    state = native_agent.run_slice(project, complete=complete)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    break
                if state.get("status") != "continue":
                    break
        # Native browser tools keep a Sync Playwright driver alive in this
        # process. Close it before the independent grader starts its own one.
        import native_browser

        native_browser.close()
        report = {
            "model": "automatic-local" if args.auto_route else args.model,
            "scenario": args.scenario,
            "command_timeout": args.command_timeout,
            "seconds": round(time.monotonic() - started, 2),
            "worker_status": state.get("status", "error"),
            "turns": state.get("turns", 0),
            **stats,
            "worker_verified": bool(state.get("verify_passed")),
            "current": state.get("current", ""),
            "completion_issue": native_agent.completion_issue(folder, project, state),
            "grade": {"temperature": grade_temperature,
                      "contacts": grade_contacts,
                      "webapp": grade_webapp,
                      "document": grade_document}[args.scenario](folder),
            "error": error,
        }
        if args.keep_folder:
            (folder / "benchmark-report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0 if report["grade"]["passed"] and state.get("status") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
