"""Locate a project's local preview without trusting arbitrary URLs or paths."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

WEB_WORDS = re.compile(
    r"\b(?:website|web\s*app|app|application|dashboard|game|site|frontend|front.end)\b",
    re.I,
)
HTML_CANDIDATES = (
    "index.html",
    "dist/index.html",
    "build/index.html",
    "public/index.html",
)


def wants_preview(project):
    """Open a preview for building projects, not ordinary question cards."""
    if project.get("status") not in (
        "queued",
        "working",
        "checking",
        "ready_for_review",
    ):
        return False
    brief = project.get("brief") or ""
    title = project.get("title") or ""
    return bool(WEB_WORDS.search(brief + " " + title))


def safe_loopback_url(value):
    """A task may expose a local dev server; never navigate to remote URLs."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is None
            or not (1 <= parsed.port <= 65535)
        ):
            return ""
    except ValueError:
        return ""
    return value


def source(folder):
    """Prefer a declared local server; otherwise preview a project HTML file."""
    folder = Path(folder).resolve()
    declaration = folder / ".local-ai/preview.json"
    if declaration.is_file():
        try:
            url = safe_loopback_url(
                json.loads(declaration.read_text(encoding="utf-8"))["url"]
            )
            if url:
                return url
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for name in HTML_CANDIDATES:
        html = (folder / name).resolve()
        if html.is_relative_to(folder) and html.is_file():
            return html.as_uri()
    return ""


def screenshot_path(folder):
    """Keep generated previews in the project's own hidden progress folder."""
    return Path(folder).resolve() / ".local-ai/preview.png"


def browser_screenshot_path(folder):
    """The Chrome DevTools bridge writes its latest page here."""
    return Path(folder).resolve() / ".local-ai/browser.png"
