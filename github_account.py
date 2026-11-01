"""Connect each Windows user to their own GitHub account through GitHub CLI.

GitHub CLI owns the browser authorization and credentials. This module never
reads a token, and the public workbench package contains no account secret.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def executable():
    bundled = ROOT / "Toolbox/gh_2.103.3_windows_amd64/bin/gh.exe"
    if bundled.is_file():
        return str(bundled)
    import shutil

    return shutil.which("gh")


def account():
    """Return the account name only; never expose authentication material."""
    gh = executable()
    if not gh:
        return ""
    try:
        result = subprocess.run(
            [gh, "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=HIDE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    name = result.stdout.strip()
    return (
        name
        if result.returncode == 0 and name.isascii() and name.replace("-", "").isalnum()
        else ""
    )


def connect():
    """Open the interactive GitHub browser approval flow in a visible console."""
    gh = executable()
    if not gh:
        raise RuntimeError(
            "GitHub tools are still installing. Try again when setup finishes."
        )
    return subprocess.Popen(
        [gh, "auth", "login", "--web", "--clipboard", "--git-protocol", "https"],
        cwd=ROOT,
        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0,
    )
