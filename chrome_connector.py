"""Local Hermes Connector setup for user-selected signed-in Chrome tabs."""

import re
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / "data/plugins/hermes-connector"
STORE_URL = "https://chromewebstore.google.com/detail/hermes-connector-%E2%80%94-by-cor/cdhaldcgafmkcnpanlmmpaebabnlledm"
EXPECTED_VERSION = "0.2.6"


def _strip_tag_directives(contents: str) -> str | None:
    """Recognize a conservative TAG prologue, not an entire YAML document.

    Hermes accepts ASCII URI characters and percent-encoded UTF-8 prefixes.
    A bare comment, invalid escape, or misplaced directive must not disappear
    and make an indented manifest appear to be a matching installation.
    """
    lines = contents.splitlines()
    if not any(line.lstrip().startswith("%TAG") for line in lines):
        return contents
    kept, handles = [], set()
    prologue, document_started, yaml_seen = True, False, False
    for line in lines:
        if line.lstrip().startswith("%TAG"):
            if not prologue or not line.startswith("%TAG"):
                # Indented text in a mapping/block scalar is ordinary content.
                if not line.startswith("%TAG") and not prologue:
                    kept.append(line)
                    continue
                return None
            directive = re.fullmatch(
                r"%TAG +(!|!!|![0-9A-Za-z_-]+!) +"
                r"((?:[0-9A-Za-z;/?:@&=+$,_.!~*'()\[\]-]|%[0-9A-Fa-f]{2})+)"
                r"(?: +#[^\r\n]*)? *", line)
            if not directive or directive[1] in handles:
                return None
            try:
                unquote(directive[2], encoding="utf-8", errors="strict")
            except UnicodeError:
                return None
            handles.add(directive[1])
            continue
        if re.fullmatch(r"---(?: +#[^\r\n]*)? *", line):
            document_started = True
            prologue = False
        elif re.fullmatch(r"%YAML +1\.[12](?: +#[^\r\n]*)? *", line):
            if not prologue or yaml_seen:
                return None
            yaml_seen = True
        elif line.strip() and not line.lstrip().startswith("#"):
            prologue = False
        kept.append(line)
    return "\n".join(kept) if not handles or document_started else None


def installed_version() -> str:
    """A matching local companion is required by the Chrome extension."""
    manifest = PLUGIN / "plugin.yaml"
    try:
        if not manifest.is_file():
            return ""
        contents = manifest.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    contents = _strip_tag_directives(contents)
    if contents is None:
        return ""
    # Do not merge separate YAML documents into one apparent root mapping.
    document_started, document_ended = False, False
    for line in contents.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if document_ended:
            return ""
        if re.fullmatch(r"%YAML[ \t]+1\.[12](?:[ \t]+#[^\r\n]*)?[ \t]*", line):
            continue
        if re.fullmatch(r"---(?:[ \t]+#[^\r\n]*)?[ \t]*", line):
            if document_started:
                return ""
            document_started = True
        elif re.fullmatch(r"\.\.\.(?:[ \t]+#[^\r\n]*)?[ \t]*", line):
            document_ended = True
        else:
            document_started = True
    # YAML comments, directives and document markers may be less indented than the root mapping.
    mapping_lines = "\n".join(
        line for line in contents.splitlines()
        if not line.lstrip().startswith("#")
        and not re.fullmatch(r"%YAML[ \t]+1\.[12](?:[ \t]+#[^\r\n]*)?[ \t]*", line)
        and not re.fullmatch(r"(?:---|\.\.\.)(?:[ \t]+#[^\r\n]*)?[ \t]*", line)
    )
    match = re.search(r"^(?:version|'version'|\"version\")[ \t]*:[ \t]+(['\"]?)([\w.\-]+)\1(?:[ \t]+#[^\r\n]*)?[ \t]*\r?$", dedent(mapping_lines), re.M)
    return match.group(2) if match else ""


def pairing_code() -> str:
    """Fetch the local pairing secret for direct display to the user only."""
    version = installed_version()
    if version != EXPECTED_VERSION:
        raise RuntimeError(
            f"Chrome companion {EXPECTED_VERSION} is needed; installed version is {version or 'missing'}. "
            "Run the installer again to repair it."
        )
    result = subprocess.run(
        [sys.executable, str(PLUGIN / "broker.py"), "--show-code", "--root", str(ROOT / "data")],
        capture_output=True, text=True, timeout=20, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    code = result.stdout.strip()
    if not code or len(code) > 300:
        raise RuntimeError("The local Chrome pairing code was unavailable.")
    return code
