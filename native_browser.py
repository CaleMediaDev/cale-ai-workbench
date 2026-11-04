"""Private, task-scoped browser for Cale's native worker.

Each task gets its own persistent Chromium profile. The browser starts only when
needed, and screenshots feed the existing side preview without sending page data
to a cloud service. Page content is always untrusted tool output.
"""

from __future__ import annotations

import atexit
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import project_memory

_PLAYWRIGHT = None
_CONTEXTS = {}
_ACTIVE_LOCAL = {}
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def _control(page, ref: str):
    """Resolve only a control numbered by the last browser snapshot."""
    if not re.fullmatch(r"[0-9]{1,4}", ref):
        raise ValueError("Use a numbered ref from browser_snapshot")
    return page.locator(f'[data-cale-ref="{ref}"]')


def _download_target(folder: Path, filename: str) -> Path:
    """Keep an untrusted server filename inside this task's download folder."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
    name = name[:120] or "download.bin"
    destination = folder / "Downloads"
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    stem, suffix = target.stem, target.suffix
    number = 2
    while target.exists():
        target = destination / f"{stem}-{number}{suffix}"
        number += 1
    return target


def _page(folder: Path, local: bool | None = None):
    global _PLAYWRIGHT
    root = str(folder.resolve())
    local = _ACTIVE_LOCAL.get(root, False) if local is None else local
    key = (root, local)
    if key not in _CONTEXTS:
        from playwright.sync_api import sync_playwright

        if _PLAYWRIGHT is None:
            _PLAYWRIGHT = sync_playwright().start()
        profile = folder / ".local-ai" / ("project-browser-profile" if local else "browser-profile")
        profile.mkdir(parents=True, exist_ok=True)
        context = _PLAYWRIGHT.chromium.launch_persistent_context(
            str(profile), headless=True, accept_downloads=True, offline=local,
        )
        if local:
            from webapp_check import project_only_route

            context.route("**/*", project_only_route(folder))
        _CONTEXTS[key] = context
    _ACTIVE_LOCAL[root] = local
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
    _ACTIVE_LOCAL.clear()
    if _PLAYWRIGHT is not None:
        try:
            _PLAYWRIGHT.stop()
        except Exception:
            pass
        _PLAYWRIGHT = None


atexit.register(close)


def execute(folder: Path, action: str, arguments: dict) -> str:
    """Operate only this task's browser; refs are assigned by the last snapshot."""
    if action == "open":
        url = str(arguments["url"])
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Use a full http:// or https:// URL. For a local HTML app, "
                             "use browser_open_project with a relative HTML path; no server is needed.")
    elif action == "open_project":
        from webapp_check import _project_file

        # Validate before launching Chromium or changing the active context.
        url = _project_file(folder, arguments.get("path")).as_uri()
    page = _page(folder, local=action == "open_project") if action in {"open", "open_project"} else _page(folder)
    if action == "open":
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    elif action == "open_project":
        page.goto(url, wait_until="load", timeout=15000)
    elif action == "click":
        _control(page, str(arguments["ref"])).click(timeout=10000)
    elif action == "type":
        text = str(arguments["text"])
        if len(text) > 10000:
            raise ValueError("Text is too long")
        target = _control(page, str(arguments["ref"]))
        target.fill(text, timeout=10000)
        if arguments.get("submit"):
            target.press("Enter")
    elif action == "select":
        value = str(arguments["value"])
        if len(value) > 200:
            raise ValueError("Dropdown value is too long")
        selected = _control(page, str(arguments["ref"])).select_option(value=value, timeout=10000)
        _preview(folder, page)
        return json.dumps({"url": page.url, "selected": selected}, ensure_ascii=False)
    elif action == "press":
        key = str(arguments["key"])
        if key not in {"Enter", "Tab", "Escape", "Space", "Backspace",
                       "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"}:
            raise ValueError("Use a supported browser key")
        _control(page, str(arguments["ref"])).press(key, timeout=10000)
    elif action == "back":
        page.go_back(wait_until="domcontentloaded", timeout=15000)
    elif action == "download":
        with page.expect_download(timeout=30000) as event:
            _control(page, str(arguments["ref"])).click(timeout=10000)
        download = event.value
        source = Path(download.path())
        size = source.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            download.delete()
            raise ValueError("Browser download exceeds 512 MB; use a project command for large files")
        target = _download_target(folder, download.suggested_filename)
        download.save_as(str(target))
        try:
            project_memory.index_file(folder, target)
        except (OSError, ValueError, sqlite3.Error):
            # Images, archives, and large files still download normally.
            pass
        return json.dumps({"saved": target.relative_to(folder).as_posix(),
                           "bytes": size, "url": download.url}, ensure_ascii=False)
    elif action == "scroll":
        page.mouse.wheel(0, max(-5000, min(5000, int(arguments.get("pixels", 600)))))
    elif action == "snapshot":
        controls = page.evaluate("""() => {
          document.querySelectorAll('[data-cale-ref]').forEach(el => el.removeAttribute('data-cale-ref'));
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
