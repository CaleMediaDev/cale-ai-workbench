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
    # Imported or incomplete records can contain null/non-text fields. Keep
    # valid text useful without interpreting arbitrary JSON as project intent.
    brief = project.get("brief")
    title = project.get("title")
    brief = brief if isinstance(brief, str) else ""
    title = title if isinstance(title, str) else ""
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
            or not re.fullmatch(
                r"(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]+",
                parsed.netloc,
                re.I,
            )
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
    try:
        folder = Path(folder).resolve()
    except (OSError, RuntimeError, ValueError, TypeError):
        # An inaccessible, invalid, or looping root leaves no usable preview.
        return ""
    declaration = folder / ".local-ai/preview.json"
    try:
        declaration = declaration.resolve()
        if declaration.is_relative_to(folder) and declaration.is_file():
            record = json.loads(declaration.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("Preview declaration must be an object")
            url = safe_loopback_url(record.get("url"))
            if url:
                return url
            # Imported games may live below the task root. This remains a
            # contained local HTML file, never an arbitrary file or URL.
            relative = record.get("file")
            if isinstance(relative, str) and relative and not Path(relative).is_absolute():
                page = (folder / relative).resolve()
                if page.is_relative_to(folder) and page.is_file() and page.suffix.lower() in (".html", ".htm"):
                    return page.as_uri()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        pass
    for name in HTML_CANDIDATES:
        try:
            html = (folder / name).resolve()
            if (
                html.is_relative_to(folder)
                and html.is_file()
                and html.suffix.lower() in (".html", ".htm")
            ):
                return html.as_uri()
        except (OSError, RuntimeError):
            # An unreadable path or symlink loop must not block later candidates.
            continue
    return ""


def screenshot_path(folder):
    """Keep generated previews in the project's own hidden progress folder."""
    return Path(folder).resolve() / ".local-ai/preview.png"


def browser_screenshot_path(folder):
    """The Chrome DevTools bridge writes its latest page here."""
    return Path(folder).resolve() / ".local-ai/browser.png"
