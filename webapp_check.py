"""Run repeatable, offline browser checks for a project's local HTML page."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


def _project_file(folder: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Use a relative HTML path")
    path = (folder / name).resolve()
    if not path.is_relative_to(folder.resolve()) or path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Choose an HTML file in this project")
    if not path.is_file() or path.stat().st_size > 5_000_000:
        raise ValueError("HTML file must exist and be no larger than 5 MB")
    return path


def _element_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("Use a short HTML element ID")
    return value


def normalize_spec(spec: dict) -> dict:
    """Accept a JSON-encoded case list from smaller tool-calling models."""
    if not isinstance(spec, dict):
        raise ValueError("Browser check must be an object")
    result = dict(spec)
    if isinstance(result.get("cases"), str):
        try:
            result["cases"] = json.loads(result["cases"])
        except ValueError as exc:
            raise ValueError("cases must be a JSON array of browser cases") from exc
    return result


def validate_spec(folder: Path, spec: dict) -> Path:
    """Reject broad or ambiguous browser instructions before opening the page."""
    if not isinstance(spec, dict):
        raise ValueError("Browser check must be an object")
    page = _project_file(folder, spec.get("path"))
    cases = spec.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 12:
        raise ValueError("Give 1 to 12 browser cases")
    width = spec.get("viewport_width", 375)
    if not isinstance(width, int) or isinstance(width, bool) or not 320 <= width <= 1920:
        raise ValueError("Viewport width must be 320 to 1920 pixels")
    if "mobile_fit" in spec and not isinstance(spec["mobile_fit"], bool):
        raise ValueError("mobile_fit must be true or false")
    total_wait = 0
    for case in cases:
        if not isinstance(case, dict) or not set(case).issubset({"set", "click", "keys", "wait_ms", "expect", "visible", "hidden", "mobile_fit"}):
            raise ValueError("Use set, click, keys, wait_ms, expect, visible, hidden, and mobile_fit in each case")
        keys = case.get("keys", [])
        if not isinstance(keys, list) or len(keys) > 20:
            raise ValueError("keys must be a list of at most 20 key actions")
        for action in keys:
            if not isinstance(action, dict) or not set(action).issubset({"key", "hold_ms"}):
                raise ValueError("Each key action needs key and optional hold_ms")
            key = action.get("key")
            if not isinstance(key, str) or not (
                    re.fullmatch(r"[A-Za-z0-9]", key) or key in {
                        "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Space",
                        "Enter", "Escape", "Tab", "Backspace", "Home", "End", "PageUp", "PageDown"}):
                raise ValueError("Use a single letter, digit, or supported navigation key")
            duration = action.get("hold_ms", 0)
            if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= 1000:
                raise ValueError("hold_ms must be 0 to 1000 milliseconds")
            total_wait += duration
        delay = case.get("wait_ms", 0)
        if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 2000:
            raise ValueError("wait_ms must be 0 to 2000 milliseconds")
        total_wait += delay
        if total_wait > 10_000:
            raise ValueError("Browser cases may wait at most 10000 milliseconds in total")
        clicks = case.get("click", [])
        if not isinstance(clicks, list) or len(clicks) > 20:
            raise ValueError("click must be a list of at most 20 element IDs")
        for element_id in clicks:
            _element_id(element_id)
        updates, expected = case.get("set", {}), case.get("expect", {})
        if not isinstance(updates, dict) or not isinstance(expected, dict):
            raise ValueError("set and expect must be objects keyed by HTML ID")
        if len(updates) > 20 or len(expected) > 20:
            raise ValueError("Too many elements in one browser case")
        for name, value in [*updates.items(), *expected.items()]:
            _element_id(name)
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError("Browser values and expected text must be short strings")
        for field in ("visible", "hidden"):
            ids = case.get(field, [])
            if not isinstance(ids, list) or len(ids) > 20:
                raise ValueError("visible and hidden must be short lists")
            if any(not isinstance(name, str) or not name or len(name) > 200 for name in ids):
                raise ValueError("visible and hidden entries must be short IDs or text")
        if "mobile_fit" in case and not isinstance(case["mobile_fit"], bool):
            raise ValueError("mobile_fit must be true or false")
        if not expected and not case.get("visible") and not case.get("hidden") and not case.get("mobile_fit"):
            raise ValueError("Each browser case needs an observable assertion")
    return page


def project_only_route(folder: Path):
    """Restrict a local preview/check to project files and inline resources."""
    root = folder.resolve()

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.scheme == "file":
            local = Path(url2pathname(parsed.path)).resolve()
            if local.is_relative_to(root):
                route.continue_()
                return
        elif parsed.scheme in {"data", "blob", "about"}:
            route.continue_()
            return
        route.abort()

    return handle


def check(folder: Path, spec: dict, screenshot: bool = False) -> dict:
    """Exercise a local page offline and report each actual browser assertion."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    spec = normalize_spec(spec)
    page_path = validate_spec(folder, spec)
    page_hash = hashlib.sha256(page_path.read_bytes()).hexdigest()
    results = []
    errors = []
    interactions = []
    normalized_inputs = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(offline=True, viewport={
                "width": spec.get("viewport_width", 375), "height": 812})
            context.route("**/*", project_only_route(folder))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)[:300]))
            page.goto(page_path.as_uri(), wait_until="load", timeout=15_000)
            def find_target(token):
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", token):
                    by_id = page.locator("#" + token)
                    if by_id.count() == 1:
                        return by_id
                return page.get_by_text(token, exact=False).first

            def asserted_state(case):
                # Observe only targets asserted by this case. An animated
                # starfield or the input's own value is not gameplay evidence.
                observed = {}
                for element_id in case.get("expect", {}):
                    target = page.locator("#" + element_id)
                    exists = target.count() == 1
                    observed["text:" + element_id] = (
                        exists, exists and target.is_visible(),
                        target.inner_text()[:200] if exists else "",
                    )
                for field in ("visible", "hidden"):
                    for token in case.get(field, []):
                        target = find_target(token)
                        observed["visibility:" + token] = target.count() > 0 and target.is_visible()
                return observed

            for number, case in enumerate(spec["cases"], 1):
                failures = []
                before = asserted_state(case)
                for element_id, value in case.get("set", {}).items():
                    control = page.locator("#" + element_id)
                    if control.count() != 1:
                        failures.append(f"Missing input #{element_id}")
                    else:
                        try:
                            control.fill(value, timeout=5_000)
                        except PlaywrightError as exc:
                            if (control.get_attribute("type") == "number" and
                                    "Cannot type text into input[type=number]" in str(exc)):
                                # A real browser rejects letters in a number
                                # field. Test the resulting empty value rather
                                # than encouraging the agent to change the
                                # required input type just to satisfy a test.
                                control.fill("", timeout=5_000)
                                normalized_inputs.append(
                                    f"#{element_id}: {value!r} cannot be typed into a number input; tested empty instead"
                                )
                            else:
                                raise
                # Replay actual user input: fill, click, keys, wait, assertions.
                for element_id in case.get("click", []):
                    control = page.locator("#" + element_id)
                    if control.count() != 1:
                        failures.append(f"Missing click target #{element_id}")
                        continue
                    try:
                        control.click(timeout=5_000)
                    except PlaywrightError as exc:
                        failures.append(f"Could not click #{element_id}: {str(exc)[:200]}")
                for action in case.get("keys", []):
                    key = action["key"]
                    try:
                        page.keyboard.down(key)
                        try:
                            if action.get("hold_ms", 0):
                                page.wait_for_timeout(action["hold_ms"])
                        finally:
                            page.keyboard.up(key)
                    except PlaywrightError as exc:
                        failures.append(f"Could not press {key}: {str(exc)[:200]}")
                if case.get("wait_ms", 0):
                    page.wait_for_timeout(case["wait_ms"])
                for element_id, text in case.get("expect", {}).items():
                    target = page.locator("#" + element_id)
                    actual = target.inner_text()[:200] if target.count() == 1 else "<missing>"
                    if target.count() != 1 or not target.is_visible() or text not in actual:
                        failures.append(
                            f"#{element_id} displayed {actual!r}; expected visible text containing {text!r}"
                        )
                for field, wanted in (("visible", True), ("hidden", False)):
                    for token in case.get(field, []):
                        target = find_target(token)
                        actual = target.count() > 0 and target.is_visible()
                        if actual != wanted:
                            failures.append(f"{token!r} visibility was wrong")
                if (case.get("mobile_fit") or spec.get("mobile_fit")) and not page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"):
                    failures.append("Page overflows the viewport")
                results.append({"case": number, "passed": not failures, "failures": failures})
                actions = [kind for kind in ("set", "click", "keys") if case.get(kind)]
                after = asserted_state(case)
                changed = [name for name, value in after.items() if before.get(name) != value]
                if actions and changed and not failures:
                    interactions.append({"case": number, "actions": actions, "targets": changed})
            if screenshot:
                destination = folder / ".local-ai" / "browser.png"
                destination.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(destination), timeout=8_000)
        finally:
            browser.close()
    if hashlib.sha256(page_path.read_bytes()).hexdigest() != page_hash:
        errors.append("HTML source changed while the browser check was running; rerun the check.")
    return {"passed": all(item["passed"] for item in results) and not errors,
            "cases": results, "javascript_errors": errors,
            "page_sha256": page_hash, "interactions": interactions,
            "normalized_inputs": normalized_inputs}


def save_verifier(folder: Path, spec: dict) -> bool:
    """Seed a read-only project verifier, preserving a custom existing check."""
    spec = normalize_spec(spec)
    validate_spec(folder, spec)
    notes = folder / ".local-ai"
    notes.mkdir(parents=True, exist_ok=True)
    verifier = notes / "verify.ps1"
    marker = "# Generated by Cale check_webapp"
    existing = verifier.read_text(encoding="utf-8-sig") if verifier.is_file() else ""
    if existing and not existing.startswith(marker):
        return False
    spec_path = notes / "webapp-check.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
    script = (
        marker + "\n$ErrorActionPreference = 'Stop'\n"
        f"$python = {literal(sys.executable)}\n"
        "if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction Stop).Source }\n"
        f"& $python {literal(str(Path(__file__).resolve()))} --folder "
        "(Split-Path -Parent $PSScriptRoot) --spec "
        "(Join-Path $PSScriptRoot 'webapp-check.json')\n"
        "if ($LASTEXITCODE -ne 0) { exit 1 }\n"
    )
    verifier.write_text(script, encoding="utf-8-sig")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    folder = args.folder.resolve()
    spec_file = args.spec.resolve()
    if not spec_file.is_relative_to(folder / ".local-ai"):
        raise ValueError("Browser check spec must be in this project's .local-ai folder")
    result = check(folder, json.loads(spec_file.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
