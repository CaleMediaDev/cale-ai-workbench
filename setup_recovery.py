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
STALE_SECONDS = 90


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


def _installer_running(progress: dict) -> bool:
    """Check the exact installer command, including older setup records without a PID."""
    import psutil

    script = (ROOT / "Install-Backend.ps1").resolve()
    installer_pid = progress.get("pid")
    try:
        processes = ([psutil.Process(int(installer_pid))] if installer_pid
                     else psutil.process_iter(["cmdline"]))
        for process in processes:
            try:
                command = process.cmdline()
                for index, argument in enumerate(command[:-1]):
                    if argument.lower() == "-file" and Path(command[index + 1]).resolve() == script:
                        return True
            except (OSError, ValueError, psutil.Error):
                continue
    except (OSError, ValueError, psutil.Error):
        return False
    return False


def _stalled(progress: dict) -> bool:
    """A quiet download may take hours; only a dead installer is stalled."""
    return (progress.get("status") == "working"
            and time.time() - float(progress.get("updated", 0)) >= STALE_SECONDS
            and not _installer_running(progress))


def repair_exhausted(progress: dict) -> bool:
    """Tell waiting tasks when automatic repair is finished without success."""
    if progress.get("owner") == "runtime_features":
        return (progress.get("status") == "failed" and
                int(progress.get("attempts", 0)) >= 3)
    return (progress.get("status") == "error" and
            str(progress.get("current", "")).startswith("Setup needs attention:") and
            int(_read(STATE / "base-install-repair.json").get("attempts", 0)) >= len(RETRY_DELAYS) and
            not _installer_running(progress))


def recover() -> bool:
    """Retry a stopped base installer at most three times, preserving task files."""
    progress = _read(STATE / "install-progress.json")
    record_path = STATE / "base-install-repair.json"
    if progress.get("status") == "complete":
        record_path.unlink(missing_ok=True)
        return False
    if not (_stalled(progress) or (progress.get("status") == "error" and
            str(progress.get("current", "")).startswith("Setup needs attention:"))):
        return False
    script = ROOT / "Install-Backend.ps1"
    if not script.is_file():
        return False
    record = _read(record_path)
    attempts = max(0, int(record.get("attempts", 0)))
    if (attempts < len(RETRY_DELAYS) and
            time.time() - max(float(progress.get("updated", 0)),
                              float(record.get("started", 0))) < RETRY_DELAYS[attempts]):
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
            stalled = _stalled(progress)
            failed = (progress.get("status") == "error" and
                      str(progress.get("current", "")).startswith("Setup needs attention:"))
            if not (stalled or failed):
                return False
            if stalled:
                progress.update(status="error", percent=0,
                                current="Setup needs attention: installer stopped before finishing",
                                updated=time.time())
                _write(STATE / "install-progress.json", progress)
            if attempts >= len(RETRY_DELAYS):
                return False
            if time.time() - max(float(progress.get("updated", 0)),
                                 float(record.get("started", 0))) < RETRY_DELAYS[attempts]:
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
