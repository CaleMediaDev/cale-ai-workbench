"""Resume a failed first-run installer without making the user find Retry setup."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "LongTasks"
RETRY_DELAYS = (20, 120, 600)
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def recover() -> bool:
    """Retry a stopped base installer at most three times, preserving task files."""
    progress = _read(STATE / "install-progress.json")
    record_path = STATE / "base-install-repair.json"
    if progress.get("status") == "complete":
        record_path.unlink(missing_ok=True)
        return False
    if (progress.get("status") != "error" or not str(progress.get("current", "")).startswith(
        "Setup needs attention:"
    )):
        return False
    script = ROOT / "Install-Backend.ps1"
    if not script.is_file():
        return False
    record = _read(record_path)
    attempts = max(0, int(record.get("attempts", 0)))
    if attempts >= len(RETRY_DELAYS):
        return False
    if time.time() - max(float(progress.get("updated", 0)), float(record.get("started", 0))) < RETRY_DELAYS[attempts]:
        return False
    import msvcrt

    lock_path = STATE / "base-install-repair.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"1")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        try:
            # Another monitor or a manual retry may have changed setup state.
            progress = _read(STATE / "install-progress.json")
            record = _read(record_path)
            attempts = max(0, int(record.get("attempts", 0)))
            if (progress.get("status") != "error" or attempts >= len(RETRY_DELAYS)
                    or time.time() - max(float(progress.get("updated", 0)),
                                         float(record.get("started", 0))) < RETRY_DELAYS[attempts]):
                return False
            now = time.time()
            _write(record_path, {"attempts": attempts + 1, "started": now})
            log = STATE / "base-install-repair.log"
            with log.open("ab") as output:
                subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(script), "-Destination", str(ROOT),
                     "-Profile", "auto", "-NoLaunch"],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, creationflags=HIDE,
                )
            return True
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
