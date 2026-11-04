"""Run the shipped regression suite after updates without blocking the desktop.

The check uses the installed Python and local source only. A concise status is
kept beside the full log so a failed upgrade has reproducible evidence.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
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
LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
HISTORY_LIMIT = 3
_ACTIVITY_REASONS = {"active_projects", "project_db_unavailable", "activity_unknown"}
_SQLITE_ERRORS = {"Error", "DatabaseError", "DataError", "IntegrityError", "InterfaceError",
                  "InternalError", "NotSupportedError", "OperationalError", "ProgrammingError"}


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


def _safe_log_lines(lines) -> list[str]:
    """Retain only pytest progress/counts, never failures, paths or task text."""
    result = []
    for value in lines:
        line = value.strip() if isinstance(value, str) else ""
        if not line or len(line) > 160:
            continue
        progress = re.fullmatch(r"[.sFxEX!?]{10,}(?:\s+\[\s*\d{1,3}%\])?", line)
        counts = re.fullmatch(r"\d+ (?:passed|failed|skipped|deselected|errors?|subtests passed)"
            r"(?:, \d+ (?:passed|failed|skipped|deselected|errors?|subtests passed))*"
            r" in \d+(?:\.\d+)?s(?: \([0-9:]+\))?", line)
        if progress or counts:
            result.append(line)
    return result[-4:]


def _safe_attempt(report: dict) -> dict | None:
    if not isinstance(report, dict) or not isinstance(report.get("status"), str) or report["status"] not in {
            "passed", "failed", "deferred", "running", "interrupted"}:
        return None
    release = report.get("version")
    if (not isinstance(release, str) or not re.fullmatch(
            r"Cale AI Workbench \d+\.\d+\.\d+|development", release)):
        release = "unknown"
    item = {"version": release, "status": "interrupted" if report["status"] == "running" else report["status"]}
    for field in ("started_at", "checked_at", "duration_seconds"):
        value = report.get(field)
        if type(value) in (int, float):
            try:
                if math.isfinite(value):
                    item[field] = value
            except OverflowError:
                pass
    if isinstance(report.get("deferred_reason"), str) and report["deferred_reason"] in _ACTIVITY_REASONS:
        item["deferred_reason"] = report["deferred_reason"]
    if isinstance(report.get("activity_error_type"), str) and report["activity_error_type"] in _SQLITE_ERRORS:
        item["activity_error_type"] = report["activity_error_type"]
    item["log_tail"] = _safe_log_lines(report.get("log_tail", [])[-4:]) if isinstance(
        report.get("log_tail"), list) else []
    return item


def _retain_previous_attempt(state: Path, report: dict) -> None:
    """Keep a few safe diagnostics before the scheduler's next log truncation.

    Called under the existing launch lock. Optional history errors must never
    prevent or delay validation; this adds no activity or scheduling policy.
    """
    try:
        item = _safe_attempt(report)
        if item is None:
            return
        log = state / "self-check.log"
        if log.is_file():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8192))
                item["log_tail"] = _safe_log_lines(stream.read(8192).decode("utf-8", "replace").splitlines())
        target = state / "self-check-history.json"
        try:
            previous = _read(target) if target.is_file() and target.stat().st_size <= 32000 else {}
        except (RecursionError, OverflowError):
            previous = {}  # Damaged optional history must not hide this attempt.
        rows = previous.get("attempts", [])
        rows = rows[-HISTORY_LIMIT:] if isinstance(rows, list) else []
        rows = [safe for row in rows if (safe := _safe_attempt(row)) is not None]
        identity = lambda row: tuple(row.get(key) for key in ("version", "status", "started_at", "checked_at"))
        rows = [row for row in rows if identity(row) != identity(item)]
        _write(target, {"attempts": [*rows, item][-HISTORY_LIMIT:]})
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        pass  # A diagnostics-only file must not become a validation dependency.


@contextmanager
def _report_lock(state: Path, *, blocking: bool):
    """Serialize launches and final reports across GUI and supervisor processes."""
    if os.name != "nt":
        import fcntl

        state.mkdir(parents=True, exist_ok=True)
        with (state / "self-check.lock").open("a+b") as handle:
            acquired = False
            try:
                try:
                    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), flags)
                    acquired = True
                except BlockingIOError:
                    if blocking:
                        raise
                yield acquired
            finally:
                if acquired:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
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


def active_projects(state: Path, *, details: dict | None = None) -> bool:
    """Defer the CPU-heavy suite while any kind of user task is running."""
    def observed(busy, reason, error=None):
        if isinstance(details, dict):
            details.clear()
            details["reason"] = reason
            if error is not None:
                kind = type(error).__name__
                details["error_type"] = kind if kind in _SQLITE_ERRORS else "Error"
        return busy
    database = state / "projects.db"
    if not database.is_file():
        return observed(False, "no_project_database")
    try:
        with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True,
                                     timeout=1)) as connection:
            busy = connection.execute(
                "SELECT 1 FROM projects WHERE status IN ('queued','working','checking') LIMIT 1"
            ).fetchone() is not None
            return observed(busy, "active_projects" if busy else "idle")
    except sqlite3.Error as error:
        return observed(True, "project_db_unavailable", error)  # Same fail-closed policy.


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


def _stop_check(process: subprocess.Popen) -> None:
    """Stop only the self-check process tree when user work needs the PC."""
    import psutil

    if process.poll() is not None:
        return
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # An exited or inaccessible child must not prevent sibling cleanup.
                pass
        try:
            process.terminate()
        except (ProcessLookupError, PermissionError):
            # An exited or inaccessible parent must not prevent child cleanup.
            pass
        _, alive = psutil.wait_procs(children, timeout=5)
        for child in alive:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # An exited or inaccessible child must not prevent sibling cleanup.
                pass
        process.wait(timeout=5)
    except (OSError, psutil.Error, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


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
    if active_projects(state):
        return False
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
    _retain_previous_attempt(state, report)
    try:
        process = subprocess.Popen(
            [str(python), str(root / "self_diagnostics.py"), "--run"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=HIDE,
        )
    except OSError:
        # No supervisor started; preserve the report and allow a later retry.
        return False
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
    started_monotonic = time.monotonic()
    started_version = version(root)
    python = root / "Toolbox/python/Scripts/python.exe"
    state.mkdir(parents=True, exist_ok=True)
    log = state / "self-check.log"
    activity = {}
    deferred = active_projects(state, details=activity)
    timed_out = False
    returncode = None
    try:
        with log.open("wb") as stream:
            if not deferred:
                process = subprocess.Popen(
                    [str(python), "-m", "pytest", "-q", "Validation"],
                    cwd=root, stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.STDOUT, creationflags=HIDE | LOW_PRIORITY,
                )
                while (returncode := process.poll()) is None:
                    if active_projects(state, details=activity):
                        _stop_check(process)
                        deferred = True
                        break
                    if time.monotonic() - started_monotonic > MAX_SECONDS:
                        _stop_check(process)
                        timed_out = True
                        break
                    time.sleep(2)
        output = log.read_text(encoding="utf-8", errors="replace")
        if deferred:
            output += "\nSelf-check yielded to an active task.\n"
        if timed_out:
            output += "\nSelf-check exceeded its time limit.\n"
        passed = returncode == 0 and not deferred and not timed_out
    except (OSError, subprocess.TimeoutExpired) as exc:
        try:
            output = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        output += f"\nSelf-check could not finish: {type(exc).__name__}: {exc}\n"
        passed = False
    with _report_lock(state, blocking=True):
        if version(root) != started_version:
            return {"status": "superseded", "version": started_version}
        previous = _read(state / "self-check.json")
        log.write_text(output, encoding="utf-8")
        if deferred:
            reason = activity.get("reason", "activity_unknown")
            report = {"version": started_version, "status": "deferred",
                      "failures": (int(previous.get("failures", 0))
                                   if previous.get("version") == started_version else 0),
                      "checked_at": time.time(),
                      "started_at": started,
                      "duration_seconds": round(time.time() - started, 1),
                      "deferred_reason": reason,
                      "summary": "Self-check paused while a task is active"}
            if reason == "project_db_unavailable":
                report["summary"] = "Self-check postponed because project activity is unavailable"
                report["activity_error_type"] = activity["error_type"]
            _write(state / "self-check.json", report)
            return report
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
