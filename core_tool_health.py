"""Detect and quietly repair managed CLI and local browser tools."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHECK_INTERVAL = 300
_last_check = 0.0
_cached_root: Path | None = None
_cached_missing: list[str] = []
_repair_thread: threading.Thread | None = None
_last_repair: float | None = None


def _runnable(path: Path, *args: str) -> bool:
    if not path.is_file():
        return False
    try:
        result = subprocess.run([str(path), *args], capture_output=True, timeout=10,
                                creationflags=HIDE)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _browser_works(root: Path, node: Path) -> bool:
    """Run the packaged probe against installed npm packages and Chromium."""
    probe = root / "browser_probe.js"
    if not probe.is_file() or not node.is_file():
        return False
    try:
        result = subprocess.run(
            [str(node), str(probe), str(root), str(root / "Toolbox")],
            cwd=root, capture_output=True, timeout=30, creationflags=HIDE,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def missing(root: Path = ROOT, *, fresh: bool = False) -> list[str]:
    """Probe actual executables, cached briefly to keep the desktop responsive."""
    global _cached_root, _cached_missing, _last_check
    root = Path(root)
    if not fresh and root == _cached_root and time.monotonic() - _last_check < CHECK_INTERVAL:
        return list(_cached_missing)
    box = root / "Toolbox"
    unavailable = []
    node = box / "node-v26.13.0-win-x64/node.exe"
    node_ok = _runnable(node, "--version")
    if not node_ok:
        unavailable.append("Node.js")
    elif not _browser_works(root, node):
        unavailable.append("Local browser runtime")
    if not _runnable(box / "gh_2.103.3_windows_amd64/bin/gh.exe", "--version"):
        unavailable.append("GitHub CLI")
    if not _runnable(box / "jdk-21.0.13+9/bin/java.exe", "-version"):
        unavailable.append("Java")
    if not (box / "ghidra_12.1.4_PUBLIC/ghidraRun.bat").is_file():
        unavailable.append("Ghidra")
    _cached_root, _cached_missing, _last_check = root, unavailable, time.monotonic()
    return list(unavailable)


def _status(root: Path, state: str, detail: str) -> None:
    path = root / "LongTasks/core-tool-repair.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"status": state, "detail": detail,
                                     "updated": time.time()}), encoding="utf-8")
    os.replace(temporary, path)


def start_background(root: Path = ROOT) -> bool:
    """Repair missing app-owned tools once per cooldown, without blocking tasks."""
    global _repair_thread, _last_repair
    root = Path(root)
    if _repair_thread is not None and _repair_thread.is_alive():
        return False
    if _last_repair is not None and time.monotonic() - _last_repair < CHECK_INTERVAL:
        return False
    marker = root / ".cale-ai-install"
    try:
        if "setup pending" in marker.read_text(encoding="utf-8").lower():
            return False
    except OSError:
        return False
    progress = root / "LongTasks/install-progress.json"
    try:
        if json.loads(progress.read_text(encoding="utf-8")).get("status") == "working":
            return False
    except (OSError, ValueError, TypeError):
        pass  # Older installed versions may have no setup progress record.
    unavailable = missing(root)
    if not unavailable:
        return False

    def repair() -> None:
        _status(root, "working", "Repairing " + ", ".join(unavailable))
        log_path = root / "LongTasks/core-tool-repair.log"
        try:
            with log_path.open("ab") as log:
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(root / "Repair-CoreTools.ps1"), "-Destination", str(root)],
                    cwd=root, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, timeout=900, creationflags=HIDE,
                )
            remaining = missing(root, fresh=True)
            if result.returncode or remaining:
                _status(root, "failed", "Still missing: " + ", ".join(remaining or unavailable))
            else:
                _status(root, "complete", "Managed core tools answered local checks")
        except (OSError, subprocess.TimeoutExpired) as exc:
            _status(root, "failed", f"Repair will retry: {type(exc).__name__}")

    _repair_thread = threading.Thread(target=repair, name="core-tool-repair", daemon=True)
    _last_repair = time.monotonic()
    _repair_thread.start()
    return True
