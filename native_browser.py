"""Private, task-scoped browser for Cale's native worker.

Each task gets its own persistent Chromium profile. The browser starts only when
needed, and screenshots feed the existing side preview without sending page data
to a cloud service. Page content is always untrusted tool output.
"""

from __future__ import annotations

import atexit
import json
import re
from pathlib import Path
from urllib.parse import urlparse

_PLAYWRIGHT = None
_CONTEXTS = {}


def _page(folder: Path):
    global _PLAYWRIGHT
    key = str(folder.resolve())
    if key not in _CONTEXTS:
        from playwright.sync_api import sync_playwright

        if _PLAYWRIGHT is None:
            _PLAYWRIGHT = sync_playwright().start()
        profile = folder / ".local-ai" / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        context = _PLAYWRIGHT.chromium.launch_persistent_context(
            str(profile), headless=True, accept_downloads=True,
        )
        _CONTEXTS[key] = context
    context = _CONTEXTS[key]
    return context.pages[-1] if context.pages else context.new_page()


def _preview(folder: Path, page) -> None:
    try:
        image = folder / ".local-ai" / "browser.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(image), timeout=8000)
    except Exception:
        pass  # A failed preview must not turn a successful browser action into a failure.


def close() -> None:
    global _PLAYWRIGHT
    for context in list(_CONTEXTS.values()):
        try:
            context.close()
        except Exception:
            pass
    _CONTEXTS.clear()
    if _PLAYWRIGHT is not None:
        try:
            _PLAYWRIGHT.stop()
        except Exception:
            pass
        _PLAYWRIGHT = None


atexit.register(close)


def execute(folder: Path, action: str, arguments: dict) -> str:
    """Operate only this task's browser; refs are assigned by the last snapshot."""
    page = _page(folder)
    if action == "open":
        url = str(arguments["url"])
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Use a full http:// or https:// URL")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    elif action == "click":
        ref = str(arguments["ref"])
        if not re.fullmatch(r"[0-9]{1,4}", ref):
            raise ValueError("Use a numbered ref from browser_snapshot")
        page.locator(f'[data-cale-ref="{ref}"]').click(timeout=10000)
    elif action == "type":
        ref = str(arguments["ref"])
        if not re.fullmatch(r"[0-9]{1,4}", ref):
            raise ValueError("Use a numbered ref from browser_snapshot")
        text = str(arguments["text"])
        if len(text) > 10000:
            raise ValueError("Text is too long")
        target = page.locator(f'[data-cale-ref="{ref}"]')
        target.fill(text, timeout=10000)
        if arguments.get("submit"):
            target.press("Enter")
    elif action == "scroll":
        page.mouse.wheel(0, max(-5000, min(5000, int(arguments.get("pixels", 600)))))
    elif action == "snapshot":
        controls = page.evaluate("""() => {
          const nodes = [...document.querySelectorAll('a,button,input,textarea,select,[role="button"],[contenteditable="true"]')];
          let n = 0;
          return nodes.filter(el => el.getClientRects().length && !el.closest('[aria-hidden="true"]'))
            .slice(0, 100).map(el => {
              const ref = String(++n);
              el.setAttribute('data-cale-ref', ref);
              return {ref, tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
                label: (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                  el.innerText || el.value || '').trim().slice(0, 120),
                href: (el.getAttribute('href') || '').slice(0, 180)};
            });
        }""")
        _preview(folder, page)
        result = {"url": page.url, "title": page.title(), "controls": controls,
                  "text": page.locator("body").inner_text(timeout=10000)[:6000]}
        while len(json.dumps(result, ensure_ascii=False)) > 15000 and result["controls"]:
            result["controls"].pop()
        return json.dumps(result, ensure_ascii=False)
    elif action == "read":
        return json.dumps({"url": page.url, "title": page.title(),
                           "text": page.locator("body").inner_text(timeout=10000)[:16000]},
                          ensure_ascii=False)
    else:
        raise ValueError("Unknown browser action")
    _preview(folder, page)
    return json.dumps({"url": page.url, "title": page.title()}, ensure_ascii=False)
