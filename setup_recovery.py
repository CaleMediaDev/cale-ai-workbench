"""Resume a failed first-run installer without making the user find Retry setup."""

from __future__ import annotations

import json
import math
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
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def _installer_running(progress: dict) -> bool:
    """Recognize the live installer even when its script is in an extracted ZIP.

    The status PID is written by that installer itself. Validate its process
    creation time as well as the script name so a reused PID cannot suppress
    repair. Older records without a PID require the installed script path.
    """
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
                    if argument.lower() != "-file":
                        continue
                    actual = Path(command[index + 1].strip('"')).resolve()
                    if actual.name.lower() != script.name.lower():
                        continue
                    if not installer_pid and actual != script:
                        continue
                    # Even the installed script can target another install.
                    # Check its destination for both PID and legacy records.
                    destination = next((command[position + 1] for position, value in enumerate(command[:-1])
                                        if value.lower() == "-destination"), None)
                    if destination and Path(destination.strip('"')).resolve() != ROOT.resolve():
                        continue
                    if installer_pid:
                        created = process.create_time()
                        started = float(progress.get("installer_started") or 0)
                        updated = float(progress.get("updated") or 0)
                        # The process can spend minutes initializing PowerShell
                        # and waiting for the install lock before writing its
                        # first progress record. A process created *after* the
                        # recorded start is a reused PID; an earlier process
                        # with this script and destination is still our installer.
                        if started and created > started + 10:
                            continue
                        if not started and (not updated or created > updated + 10):
                            continue
                    return True
            except (OSError, TypeError, ValueError, psutil.Error):
                continue
    except (OSError, TypeError, ValueError, psutil.Error):
        pass
    if installer_pid:
        # A replacement may still be initializing and have not published its
        # PID yet. Also check for the installed script targeting this install.
        legacy_progress = dict(progress)
        legacy_progress.pop("pid", None)
        return _installer_running(legacy_progress)
    return False


def _retry_runner_running() -> bool:
    """Do not race the first-run runner during its delay between attempts.

    The runner may live in an extracted installer folder, so identify it by
    script name and the destination it is actively installing into. A stale
    progress file alone is never evidence that this process still exists.
    """
    import psutil

    try:
        for process in psutil.process_iter(["cmdline"]):
            try:
                command = process.cmdline()
                options = {argument.lower(): index for index, argument in enumerate(command[:-1])
                           if argument.startswith("-")}
                script_index = options.get("-file")
                destination_index = options.get("-destination")
                if script_index is None or destination_index is None:
                    continue
                if Path(command[script_index + 1].strip('"')).name.lower() != "install-retry.ps1":
                    continue
                if Path(command[destination_index + 1].strip('"')).resolve() == ROOT.resolve():
                    return True
            except (OSError, TypeError, ValueError, psutil.Error):
                continue
    except (OSError, TypeError, ValueError, psutil.Error):
        return False
    return False


def _stalled(progress: dict) -> bool:
    """A quiet download may take hours; only a dead installer is stalled."""
    if progress.get("owner") == "runtime_features" or progress.get("status") != "working":
        return False
    try:
        updated = float(progress.get("updated", 0))
    except (TypeError, ValueError):
        # A readable JSON record can still contain an invalid timestamp.
        # Without a usable timestamp, do not infer that setup is stale.
        return False
    return (math.isfinite(updated)
            and time.time() - updated >= STALE_SECONDS
            and not _installer_running(progress)
            and not _retry_runner_running())


def repair_exhausted(progress: dict) -> bool:
    """Tell waiting tasks when automatic repair is finished without success."""
    if progress.get("owner") == "runtime_features":
        return (progress.get("status") == "failed" and
                int(progress.get("attempts", 0)) >= 3)
    if not (progress.get("status") == "error" and
            str(progress.get("current", "")).startswith("Setup needs attention:")):
        return False
    try:
        attempts = int(_read(STATE / "base-install-repair.json").get("attempts", 0))
    except (TypeError, ValueError, OverflowError):
        return False
    return (attempts >= len(RETRY_DELAYS) and
            not _installer_running(progress) and not _retry_runner_running())


def recover() -> bool:
    """Retry a stopped base installer at most three times, preserving task files."""
    progress = _read(STATE / "install-progress.json")
    record_path = STATE / "base-install-repair.json"
    # The optional crawler/Chrome installer owns this shared progress file
    # while it runs. Its own recovery loop verifies its Python process; the
    # base installer must never overwrite that status or start beside it.
    if progress.get("owner") == "runtime_features":
        return False
    if progress.get("status") == "complete":
        record_path.unlink(missing_ok=True)
        return False
    if not (_stalled(progress) or (progress.get("status") == "error" and
            str(progress.get("current", "")).startswith("Setup needs attention:"))):
        return False
    if _retry_runner_running():
        return False
    if _installer_running(progress):
        return False
    script = ROOT / "Install-Backend.ps1"
    if not script.is_file():
        return False
    record = _read(record_path)
    try:
        attempts = max(0, int(record.get("attempts", 0)))
    except (TypeError, ValueError, OverflowError):
        return False
    if attempts < len(RETRY_DELAYS):
        try:
            timestamps = (float(progress.get("updated", 0)),
                          float(record.get("started", 0)))
        except (TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in timestamps):
            return False
        last_update = max(timestamps)
        if time.time() - last_update < RETRY_DELAYS[attempts]:
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
            if progress.get("owner") == "runtime_features":
                return False
            if _retry_runner_running():
                return False
            if _installer_running(progress):
                return False
            record = _read(record_path)
            try:
                attempts = max(0, int(record.get("attempts", 0)))
            except (TypeError, ValueError, OverflowError):
                return False
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
            try:
                timestamps = (float(progress.get("updated", 0)),
                              float(record.get("started", 0)))
            except (TypeError, ValueError):
                return False
            if not all(math.isfinite(value) for value in timestamps):
                return False
            last_update = max(timestamps)
            if time.time() - last_update < RETRY_DELAYS[attempts]:
                return False
            now = time.time()
            _write(record_path, {"attempts": attempts + 1, "started": now})
            log = STATE / "base-install-repair.log"
            with log.open("ab") as output:
                try:
                    subprocess.Popen(
                        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), "-Destination", str(ROOT),
                         "-Profile", "auto", "-NoLaunch"],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=output,
                        stderr=subprocess.STDOUT, creationflags=HIDE,
                    )
                except OSError as error:
                    output.write(f"Unable to start setup repair: {error}\n".encode("utf-8"))
                    return False
            return True
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
