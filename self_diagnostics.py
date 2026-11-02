"""Run the shipped regression suite after updates without blocking the desktop.

The check uses the installed Python and local source only. A concise status is
kept beside the full log so a failed upgrade has reproducible evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "LongTasks"
REPORT = STATE / "self-check.json"
LOG = STATE / "self-check.log"
INTERVAL = 7 * 24 * 60 * 60
FAILURE_RETRY = 60 * 60
MAX_FAILURES = 3
MAX_SECONDS = 20 * 60
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _report_lock(state: Path, *, blocking: bool):
    """Serialize launches and final reports across GUI and supervisor processes."""
    if os.name != "nt":
        yield True
        return
    import msvcrt

    state.mkdir(parents=True, exist_ok=True)
    with (state / "self-check.lock").open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        acquired = False
        try:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                if blocking:
                    raise
            yield acquired
        finally:
            if acquired:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def version(root: Path = ROOT) -> str:
    try:
        return (root / ".cale-ai-install").read_text(encoding="utf-8").strip()
    except OSError:
        return "development"


def persistent_failure(report: dict, current: str) -> bool:
    """Tell the person only after a problem repeats on this installed release."""
    return (report.get("version") == current and report.get("status") == "failed"
            and int(report.get("failures", 0)) >= 2)


def _running(report: dict) -> bool:
    """Reject PID reuse; a stale report must not suppress the next check."""
    import psutil

    try:
        process = psutil.Process(int(report["pid"]))
        return (abs(process.create_time() - float(report["process_started"])) < 2
                and Path(__file__).resolve() in
                [Path(arg).resolve() for arg in process.cmdline()[1:] if arg.endswith("self_diagnostics.py")])
    except (KeyError, TypeError, ValueError, OSError, psutil.Error):
        return False


def schedule(root: Path = ROOT, state: Path = STATE, now: float | None = None) -> bool:
    """Start at most one idle check per release or week; never wait in the UI."""
    with _report_lock(state, blocking=False) as acquired:
        if not acquired:
            return False
        return _schedule_locked(root, state, now)


def _schedule_locked(root: Path, state: Path, now: float | None) -> bool:
    """Read, launch, and record under one cross-process lock."""
    now = time.time() if now is None else now
    report_path = state / "self-check.json"
    report = _read(report_path)
    current = version(root)
    if report.get("status") == "running" and _running(report):
        return False
    if report.get("version") == current and report.get("status") in ("passed", "failed"):
        retry_after = (
            FAILURE_RETRY
            if report["status"] == "failed" and int(report.get("failures", 1)) < MAX_FAILURES
            else INTERVAL
        )
        if now - float(report.get("checked_at", 0)) < retry_after:
            return False
    python = root / "Toolbox/python/Scripts/python.exe"
    if not python.is_file() or not (root / "Validation/test_long_tasks.py").is_file():
        return False
    process = subprocess.Popen(
        [str(python), str(root / "self_diagnostics.py"), "--run"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=HIDE,
    )
    import psutil

    try:
        process_started = psutil.Process(process.pid).create_time()
    except psutil.Error:
        # A fast failure may already have written its final report.
        return True
    _write(report_path, {
        "version": current, "status": "running", "started_at": now,
        "pid": process.pid, "process_started": process_started,
        "failures": int(report.get("failures", 0)) if report.get("version") == current else 0,
    })
    return True


def run(root: Path = ROOT, state: Path = STATE) -> dict:
    """Execute real packaged tests; persist their output even on failure."""
    started = time.time()
    started_version = version(root)
    python = root / "Toolbox/python/Scripts/python.exe"
    try:
        result = subprocess.run(
            [str(python), "-m", "pytest", "-q", "Validation"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=MAX_SECONDS,
            creationflags=HIDE,
        )
        output = result.stdout + result.stderr
        passed = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        output = f"Self-check could not finish: {type(exc).__name__}: {exc}\n"
        passed = False
    with _report_lock(state, blocking=True):
        if version(root) != started_version:
            return {"status": "superseded", "version": started_version}
        previous = _read(state / "self-check.json")
        log = state / "self-check.log"
        log.write_text(output, encoding="utf-8")
        failures = 0
        if not passed:
            failures = (int(previous.get("failures", 0)) + 1
                        if previous.get("version") == started_version else 1)
        report = {
            "version": started_version,
            "status": "passed" if passed else "failed",
            "failures": failures,
            "checked_at": time.time(),
            "duration_seconds": round(time.time() - started, 1),
            "summary": "\n".join(output.strip().splitlines()[-8:]),
            "log": str(log),
        }
        _write(state / "self-check.json", report)
        return report


if __name__ == "__main__" and sys.argv[1:] == ["--run"]:
    raise SystemExit(0 if run()["status"] == "passed" else 1)
