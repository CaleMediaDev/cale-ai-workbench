"""Local Hermes Connector setup for user-selected signed-in Chrome tabs."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / "data/plugins/hermes-connector"
STORE_URL = "https://chromewebstore.google.com/detail/hermes-connector-%E2%80%94-by-cor/cdhaldcgafmkcnpanlmmpaebabnlledm"
EXPECTED_VERSION = "0.2.6"


def installed_version() -> str:
    """A matching local companion is required by the Chrome extension."""
    manifest = PLUGIN / "plugin.yaml"
    try:
        if not manifest.is_file():
            return ""
        contents = manifest.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    match = re.search(r"^version[ \t]*:[ \t]*(['\"]?)([\w.\-]+)\1[ \t]*(?:#[^\r\n]*)?\r?$", contents, re.M)
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
