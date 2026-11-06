"""Bounded bridge to Hermes' existing skill registry and install scanner.

Remote search results and skill instructions are untrusted data. Installation
requires a recent search and inspection in this process, and never uses --force.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = frozenset({"skills-sh", "official", "github"})
IDENTIFIER = re.compile(r"^(?:skills-sh|official|github)/(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+(?:/(?!\.{1,2}(?:/|$))[A-Za-z0-9_.-]+){1,5}$")
SEEN: dict[str, float] = {}
INSPECTED: dict[str, float] = {}
MAX_AGE = 30 * 60


def _run(*arguments: str, timeout: int = 75) -> str:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(ROOT / "data")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PATH"] = os.pathsep.join((
        str(ROOT / "data/bin"), str(ROOT / "Toolbox/python/Scripts"),
        env.get("PATH", ""),
    ))
    result = subprocess.run(
        [str(ROOT / "data/bin/hermes.exe"), "skills", *arguments],
        cwd=ROOT, env=env, text=True, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.returncode:
        raise RuntimeError((result.stdout + "\n" + result.stderr)[-3000:].strip())
    return result.stdout


def search(query: str, source: str = "skills-sh", limit: int = 5) -> dict:
    """Search one registry without downloading or installing a skill."""
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in query):
        raise ValueError("Use a short skill topic without control characters.")
    query = query.strip()
    if not query or len(query) > 100:
        raise ValueError("Use a short skill topic without control characters.")
    if source not in SOURCES:
        raise ValueError("Choose skills-sh, official, or github.")
    limit = max(1, min(int(limit), 5))
    rows = json.loads(_run("search", query, "--source", source, "--limit", str(limit), "--json"))
    if not isinstance(rows, list):
        raise RuntimeError("The skill registry returned an invalid result.")
    safe = []
    terms = []
    term = ""
    for char in unicodedata.normalize("NFC", query.casefold()):
        if char.isalnum() or (term and unicodedata.category(char).startswith("M")):
            term += char
        else:
            if term:
                terms.append(term)
                term = ""
    if term:
        terms.append(term)
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("identifier", ""))
        name = row.get("name", "")
        description = row.get("description", "")
        name = name if isinstance(name, str) else ""
        description = description if isinstance(description, str) else ""
        haystack = unicodedata.normalize("NFC", (name + " " + description).casefold())
        name = name[:100]
        description = description[:500]
        if (
            IDENTIFIER.fullmatch(identifier)
            and identifier.split("/", 1)[0] == source
            and any(term in haystack for term in terms)
        ):
            SEEN[identifier] = time.monotonic()
            safe.append({
                "identifier": identifier,
                "name": name,
                "description": description,
                "source": str(row.get("source", ""))[:40],
                "trust_level": str(row.get("trust_level", ""))[:40],
            })
    return {"matches": safe, "source": source}


def inspect(identifier: str) -> dict:
    """Preview one recently discovered skill before considering installation."""
    if (
        not IDENTIFIER.fullmatch(identifier)
        or identifier not in SEEN
        or time.monotonic() - SEEN[identifier] > MAX_AGE
    ):
        raise ValueError("Search for this exact skill first.")
    INSPECTED.pop(identifier, None)
    content = _run("inspect", identifier)
    if not content.strip():
        raise ValueError("The skill registry returned an empty preview.")
    if len(content) > 30000:
        raise ValueError("This skill preview is too long for automatic installation.")
    INSPECTED[identifier] = time.monotonic()
    return {"identifier": identifier, "preview": content, "untrusted": True}


def install(identifier: str) -> dict:
    """Install a recently inspected skill only if Hermes' scanner allows it."""
    now = time.monotonic()
    if (
        not IDENTIFIER.fullmatch(identifier)
        or identifier not in SEEN
        or identifier not in INSPECTED
        or now - SEEN[identifier] > MAX_AGE
        or now - INSPECTED[identifier] > MAX_AGE
    ):
        raise ValueError("Search and inspect this exact skill first.")
    INSPECTED.pop(identifier, None)
    result = _run("install", identifier, "--yes", timeout=120)
    return {"identifier": identifier, "result": result[-3000:].strip(),
            "scanner": "Hermes built-in scan; no force override"}
