"""Connect each Windows user to their own GitHub account through GitHub CLI.

GitHub CLI owns the browser authorization and credentials. This module never
reads a token, and the public workbench package contains no account secret.
"""

from __future__ import annotations

import os
import re
import threading
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


def connect(on_code=None):
    """Keep GitHub's official browser flow; show device codes in Cale, not CMD."""
    gh = executable()
    if not gh:
        raise RuntimeError("GitHub tools are still installing. Try again when setup finishes.")
    process = subprocess.Popen(
        [gh, "auth", "login", "--web", "--clipboard", "--git-protocol", "https", "--hostname", "github.com"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=HIDE,
    )
    # All account/protocol choices are explicit. A newline answers the CLI's
    # optional 'Press Enter to open the browser' prompt without a terminal.
    try:
        process.stdin.write("\n")
        process.stdin.close()
    except (OSError, BrokenPipeError):
        pass

    def read_login_code():
        try:
            while True:
                line = process.stdout.readline(2048)
                if not isinstance(line, str) or not line:
                    break
                line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
                match = re.search(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})\b", line, re.I)
                if match and on_code:
                    on_code(match[1].upper())
        except (OSError, ValueError):
            pass
        finally:
            process.stdout.close()
    threading.Thread(target=read_login_code, daemon=True).start()
    return process
