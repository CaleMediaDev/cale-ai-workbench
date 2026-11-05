"""Run the shipped regression suite after updates without blocking the desktop.

The check uses the installed Python and local source only. A concise status is
kept beside the full log so a failed upgrade has reproducible evidence.
"""

from __future__ import annotations

import json
import hashlib
import uuid
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
# The growing shipped suite needs margin beyond the observed 20-minute cap.
# Active user work still yields immediately; unknown SQLite activity gets 15s.
MAX_SECONDS = 30 * 60
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOW_PRIORITY = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
HISTORY_LIMIT = 3
HISTORY_READ_BYTES = 32000
_ACTIVITY_REASONS = {"active_projects", "project_db_unavailable", "activity_unknown"}
_SQLITE_ERROR_NAMES = {name for name in dir(sqlite3) if re.fullmatch(r"SQLITE_[A-Z0-9_]{1,64}", name)}
_SQLITE_ERRORS = {"Error", "DatabaseError", "DataError", "IntegrityError", "InterfaceError",
                  "InternalError", "NotSupportedError", "OperationalError", "ProgrammingError"}


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _write(path: Path, data: dict) -> None:
    """Retry only atomic metadata publication on a transient Windows reader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + .3
        while True:
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                if getattr(error, "winerror", None) not in {5,32,33} or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
    finally:
        temporary.unlink(missing_ok=True)


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
    if (not isinstance(release, str) or len(release) > 64 or not re.fullmatch(
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
    for key in ("sqlite_errorcode", "sqlite_errorname"):
        value = report.get(key)
        if key == "sqlite_errorcode" and type(value) is int and 0 <= value <= 2**31-1:
            item[key] = value
        elif key == "sqlite_errorname" and isinstance(value, str) and value in _SQLITE_ERROR_NAMES:
            item[key] = value
    progress = report.get("progress")
    if isinstance(progress, dict):
        item["progress"] = {key: value for key, value in progress.items()
                            if key in {"collected", "completed", "passed", "failed", "skipped"}
                            and type(value) is int and 0 <= value <= 20000}
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
            # Bound the actual read, not a prior stat: another writer can
            # replace or grow optional history between inspection and open.
            with target.open("rb") as stream:
                raw = stream.read(HISTORY_READ_BYTES + 1)
            previous = json.loads(raw.decode("utf-8")) if len(raw) <= HISTORY_READ_BYTES else {}
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError, RecursionError, OverflowError):
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
                code, name = getattr(error, "sqlite_errorcode", None), getattr(error, "sqlite_errorname", None)
                if type(code) is int and 0 <= code <= 2**31-1:
                    details["sqlite_errorcode"] = code
                if isinstance(name, str) and name in _SQLITE_ERROR_NAMES:
                    details["sqlite_errorname"] = name
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
    if report.get("version") == current and report.get("status") in ("passed", "failed", "deferred", "interrupted"):
        retry_after = (
            FAILURE_RETRY
            if report["status"] in {"deferred", "interrupted"}
            or report["status"] == "failed" and int(report.get("failures", 1)) < MAX_FAILURES
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


UNKNOWN_GRACE = 15.0
_LIVE_ENV = "CALE_OWNED_SELFCHECK"
_LIVE_PLUGIN = None
_LIVE_MAX_NODES = 20000
_PYTEST_ARGUMENTS = ["-B", "-m", "pytest", "-q", "-p", "self_diagnostics", "Validation"]


def _check_python(root):
    return Path(root) / "Toolbox/python/Scripts/python.exe"


def _owns_live_pytest(process, progress):
    """Windows venv launchers may own the actual pytest Python descendant."""
    import psutil
    try:
        pid, started = progress.get("child_pid"), progress.get("child_started")
        if type(pid) is not int or type(started) not in (int, float):
            return False
        child = psutil.Process(pid)
        launcher = psutil.Process(process.pid)
        return (child.is_running() and abs(child.create_time()-started) < .02
                and child.cmdline()[1:] == _PYTEST_ARGUMENTS
                and (child.pid == launcher.pid or any(parent.pid == launcher.pid
                     and abs(parent.create_time()-launcher.create_time()) < .02 for parent in child.parents())))
    except (OSError, ValueError, TypeError, psutil.Error):
        return False


def _transient_database_busy(details):
    code = details.get("sqlite_errorcode")
    return (details.get("reason") == "project_db_unavailable" and type(code) is int
            and 0 <= code <= 2**31-1 and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})


def _live_basis(root, python):
    """Source/runtime correlation for ONE live session, never cached pass reuse."""
    import importlib.metadata
    root = Path(root).resolve()
    paths = (set(root.glob("*.py")) | set((root / "Validation").rglob("*.py"))
             | set((root / "benchmarks").rglob("*.py")))
    manifest = root / "install-files.json"
    if manifest.is_file():
        with manifest.open("rb") as stream:
            raw = stream.read(64001)
        if len(raw) > 64000:
            raise ValueError("Self-check install manifest is unavailable")
        names = json.loads(raw)
        if not isinstance(names, list) or not names or len(names) > 250:
            raise ValueError("Self-check install manifest is unavailable")
        for name in names:
            if (not isinstance(name, str) or len(name) > 512 or "\\" in name or ":" in name
                    or Path(name).is_absolute() or ".." in Path(name).parts):
                raise ValueError("Self-check install manifest is unavailable")
            path = root / name
            if not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError("A declared self-check runtime file is unavailable")
            paths.add(path)
    paths.update(root / name for name in ("pytest.ini", "pyproject.toml", "setup.cfg", ".cale-ai-install", "install-files.json"))
    if len(paths) > 3000:
        raise ValueError("Self-check source inventory is unavailable")
    digest = hashlib.sha256()
    for path in sorted(paths):
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(root) or path.is_symlink() or path.stat().st_size > 20*1024*1024:
            raise ValueError("Self-check source inventory is unavailable")
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    for path in (Path(python), Path(python).parent.parent / "pyvenv.cfg", Path(__file__)):
        if path.is_file():
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
    runtime = {"root": str(root), "python": str(Path(python).resolve()), "version": sys.version,
        "base": str(Path(sys.base_prefix).resolve()), "platform": sys.platform,
        "packages": sorted((str(dist.metadata.get("Name", "")), dist.version)
                           for dist in importlib.metadata.distributions()),
        "environment": {name: hashlib.sha256(os.environ.get(name, "").encode()).hexdigest()
                        for name in ("PYTHONHOME", "PYTHONPATH", "CUDA_VISIBLE_DEVICES", "OLLAMA_HOST")}}
    digest.update(json.dumps(runtime, sort_keys=True).encode())
    return digest.hexdigest()


def _read_control(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(8001)
    if len(raw) > 8000:
        raise ValueError("Self-check control is unavailable")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Self-check control is unavailable")
    return value


def _write_control(state, context, phase, activity=None):
    value = {"nonce": context["nonce"], "basis": context["basis"], "phase": phase,
             "updated": time.time(), "owner": context["owner"]}
    if activity:
        value["activity"] = activity
    _write(Path(state) / "self-check-control.json", value)


@contextmanager
def _live_progress_db(state):
    connection = sqlite3.connect(Path(state) / "self-check-progress.sqlite3", timeout=1)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS live_session (slot INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS live_results (nodeid TEXT PRIMARY KEY, outcome TEXT NOT NULL)")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _save_live_progress(context, data, result=None, *, initialize=False):
    with _live_progress_db(context["state"]) as connection:
        if initialize:
            connection.execute("DELETE FROM live_results")
        else:
            row = connection.execute("SELECT substr(value,1,8001),length(value) FROM live_session WHERE slot=1").fetchone()
            previous = json.loads(row[0]) if row and row[1] <= 8000 else None
            if not isinstance(previous, dict) or previous.get("nonce") != context["nonce"]:
                raise ValueError("Self-check session was superseded")
        connection.execute("INSERT OR REPLACE INTO live_session VALUES (1,?)", (json.dumps(data, sort_keys=True),))
        if result:
            connection.execute("INSERT OR REPLACE INTO live_results VALUES (?,?)", result)


def _read_live_progress(context):
    path = Path(context["state"]) / "self-check-progress.sqlite3"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as connection:
            connection.execute("BEGIN")  # Header/results must describe ONE writer commit.
            row = connection.execute("SELECT substr(value,1,8001),length(value) FROM live_session WHERE slot=1").fetchone()
            if not row or row[1] > 8000:
                return {"complete": False}
            value = json.loads(row[0])
            if not isinstance(value, dict) or any(value.get(key) != context[key] for key in ("nonce", "basis")):
                return {"complete": False}
            rows = connection.execute("SELECT substr(nodeid,1,2049),length(nodeid),substr(outcome,1,16),length(outcome) FROM live_results LIMIT ?", (_LIVE_MAX_NODES+1,)).fetchall()
            if (len(rows) > _LIVE_MAX_NODES or any(node_length > 2048 or length > 7 for _,node_length,_,length in rows)
                    or sum(node_length for _,node_length,_,_ in rows) > 8_000_000):
                return {"complete": False}
            counts = {}
            for _nodeid, _node_length, outcome, _length in rows:
                counts[outcome] = counts.get(outcome, 0) + 1
        fields = ("collected", "completed", "passed", "failed", "skipped")
        if (any(type(value.get(key)) is not int or not 0 <= value[key] <= _LIVE_MAX_NODES for key in fields)
                or set(counts) - {"passed", "failed", "skipped"}
                or any(counts.get(key, 0) != value[key] for key in ("passed", "failed", "skipped"))
                or sum(counts.values()) != value["completed"] or value["completed"] > value["collected"]
                or value.get("complete") is True and (value.get("finished") is not True
                    or value.get("all_nodes_finalized") is not True or value.get("binding_unchanged") is not True
                    or value["collected"] == 0 or value["completed"] != value["collected"]
                    or hashlib.sha256(json.dumps(sorted(row[0] for row in rows)).encode()).hexdigest() != value.get("node_set_sha256"))):
            return {"complete": False}
        if value.get("complete") is True and value.get("unconfigured") is not True:
            value["complete"] = False  # Session-finish alone is not the final process lifecycle.
        return value
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {"complete": False}


def _begin_live_session(root, state, python):
    import psutil
    context = {"root": str(Path(root).resolve()), "state": str(Path(state).resolve()), "nonce": uuid.uuid4().hex,
        "basis": _live_basis(root, python),
        "owner": {"pid": os.getpid(), "started": psutil.Process(os.getpid()).create_time()}}
    _save_live_progress(context, {"nonce": context["nonce"], "basis": context["basis"], "complete": False,
        "finished": False, "collected": 0, "completed": 0, "passed": 0, "failed": 0, "skipped": 0}, initialize=True)
    _write_control(state, context, "run")
    return context


class _LiveSelfCheck:
    """One process owns all fixture memory and finalizers. Counters are diagnostic."""
    def __init__(self, context):
        self.context = context
        self.nodes = set()
        self.finished_nodes = set()
        self.phases = {}
        self.invalid = False
        import psutil
        self.data = {"owner": context["owner"], "child_started": psutil.Process(os.getpid()).create_time(),
            "nonce": context["nonce"], "basis": context["basis"], "finished": False,
            "complete": False, "collected": 0, "completed": 0, "passed": 0, "failed": 0, "skipped": 0,
            "plugin_active": True, "child_pid": os.getpid()}

    def publish(self):
        _save_live_progress(self.context, self.data)

    def defer(self, details):
        import pytest
        self.data.update(deferred_reason=details.get("reason", "activity_unknown"), waiting_at_boundary=True)
        for key in ("error_type", "sqlite_errorcode", "sqlite_errorname"):
            if key in details:
                self.data[key] = details[key]
        self.publish()
        pytest.exit("Owned self-check yielded before starting another test", returncode=2)

    def boundary(self):
        import psutil
        since = None
        while True:
            try:
                control = _read_control(Path(self.context["state"]) / "self-check-control.json")
                owner = psutil.Process(self.context["owner"]["pid"])
                if (not owner.is_running() or abs(owner.create_time()-self.context["owner"]["started"]) > .02
                        or any(control.get(key) != self.context[key] for key in ("nonce", "basis", "owner"))
                        or time.time()-control.get("updated", 0) > 10 or control.get("phase") not in {"run", "pause"}):
                    self.defer({"reason": "activity_unknown"})
                details = {}
                busy = active_projects(Path(self.context["state"]), details=details)
                paused = control["phase"] == "pause"
                if not busy and not paused:
                    if self.data.get("waiting_at_boundary"):
                        if _live_basis(Path(self.context["root"]), Path(sys.executable)) != self.context["basis"]:
                            import pytest
                            self.data["invalidated"] = "source_runtime_changed"
                            self.publish()
                            pytest.exit("Owned self-check source/runtime changed during pause", returncode=0)
                        self.data["waiting_at_boundary"] = False
                        self.publish()
                    return
                if busy and not _transient_database_busy(details):
                    self.defer(details)
                if since is None:
                    since = time.monotonic()
                if not self.data.get("waiting_at_boundary"):
                    self.data["boundary_waits"] = min(_LIVE_MAX_NODES, self.data.get("boundary_waits", 0) + 1)
                    self.data["boundary_pause_ack_at"] = time.time()
                self.data["waiting_at_boundary"] = True
                self.publish()
                if time.monotonic()-since >= UNKNOWN_GRACE:
                    self.defer(details if busy else {"reason": "activity_unknown"})
                time.sleep(.1)
            except (OSError, ValueError, TypeError, sqlite3.Error, psutil.Error):
                self.defer({"reason": "activity_unknown"})


def pytest_configure(config):
    global _LIVE_PLUGIN
    _LIVE_PLUGIN = None
    raw = os.environ.get(_LIVE_ENV, "")
    if not raw or len(raw) > 8000:
        return
    try:
        context = json.loads(raw)  # Captured ONCE; tests cannot change it through env monkeypatches.
        root, state = Path(context["root"]).resolve(), Path(context["state"]).resolve()
        if (Path(config.invocation_params.dir).resolve() == root and config.args == ["Validation"]
                and state == root / "LongTasks" and _live_basis(root, Path(sys.executable)) == context["basis"]
                and _read_live_progress(context).get("nonce") == context["nonce"]):
            _LIVE_PLUGIN = _LiveSelfCheck(context)
            _LIVE_PLUGIN.publish()
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return


def pytest_collectreport(report):
    if _LIVE_PLUGIN and report.failed:
        _LIVE_PLUGIN.data["collection_failed"] = min(_LIVE_MAX_NODES,
            _LIVE_PLUGIN.data.get("collection_failed", 0) + 1)
        _LIVE_PLUGIN.publish()  # A real collection error, never inferred from log text.


def pytest_collection_finish(session):
    if _LIVE_PLUGIN:
        nodes = [item.nodeid for item in session.items]
        if (not nodes or len(nodes) > _LIVE_MAX_NODES or len(set(nodes)) != len(nodes)
                or any(len(node.encode()) > 2048 for node in nodes)):
            _LIVE_PLUGIN.invalid = True
            return
        _LIVE_PLUGIN.nodes = set(nodes)
        _LIVE_PLUGIN.data.update(collected=len(nodes), catalog_sha256=hashlib.sha256(json.dumps(nodes).encode()).hexdigest(),
            node_set_sha256=hashlib.sha256(json.dumps(sorted(nodes)).encode()).hexdigest())
        _LIVE_PLUGIN.publish()


def pytest_runtest_logstart(nodeid, location):
    if _LIVE_PLUGIN:
        _LIVE_PLUGIN.boundary()  # BEFORE setup/call; never skip/deselect or synthesize a pass.


pytest_runtest_logstart.pytest_impl = {"tryfirst": True}


def pytest_runtest_logreport(report):
    plugin = _LIVE_PLUGIN
    if not plugin or report.nodeid not in plugin.nodes:
        return
    phases = plugin.phases.setdefault(report.nodeid, {})
    if phases.get(report.when) != "failed":
        phases[report.when] = report.outcome
    if report.when != "teardown" or "setup" not in phases or phases["setup"] == "passed" and "call" not in phases:
        return
    if report.nodeid in plugin.finished_nodes:
        plugin.invalid = True
        return
    outcome = "failed" if "failed" in phases.values() else "skipped" if "skipped" in phases.values() else "passed"
    plugin.finished_nodes.add(report.nodeid)
    plugin.data["completed"] += 1
    plugin.data[outcome] += 1
    _save_live_progress(plugin.context, plugin.data, (report.nodeid, outcome))


def pytest_sessionfinish(session, exitstatus):
    yield
    plugin = _LIVE_PLUGIN
    if plugin:
        try:
            stable = _live_basis(Path(plugin.context["root"]), Path(sys.executable)) == plugin.context["basis"]
            finalized = not plugin.invalid and bool(plugin.nodes) and plugin.finished_nodes == plugin.nodes
            plugin.data.update(finished=True, complete=finalized and stable,
                all_nodes_finalized=finalized, binding_unchanged=stable,
                session_exitstatus=int(exitstatus), waiting_at_boundary=False)
            plugin.publish()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            pass  # Missing completion evidence is a failure, never partial green.


pytest_sessionfinish.pytest_impl = {"hookwrapper": True, "tryfirst": True}


def pytest_unconfigure(config):
    yield
    if _LIVE_PLUGIN:
        try:
            _LIVE_PLUGIN.data["unconfigured"] = True
            _LIVE_PLUGIN.publish()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            pass


pytest_unconfigure.pytest_impl = {"hookwrapper": True, "tryfirst": True}


def run(root: Path = ROOT, state: Path = STATE) -> dict:
    """Execute real packaged tests; persist their output even on failure."""
    started = time.time()
    started_monotonic = time.monotonic()
    started_version = version(root)
    python = _check_python(root)
    state.mkdir(parents=True, exist_ok=True)
    log = state / "self-check.log"
    activity = {}
    deferred = active_projects(state, details=activity)
    timed_out = False
    returncode = None
    progress = {"complete": False}
    process = None
    stop_requested = False
    try:
        with log.open("wb") as stream:
            if not deferred:
                context = _begin_live_session(root, state, python)
                environment = {**os.environ, _LIVE_ENV: json.dumps(context), "PYTHONDONTWRITEBYTECODE": "1",
                               "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""}
                process = subprocess.Popen(
                    [str(python), *_PYTEST_ARGUMENTS],
                    env=environment, cwd=root, stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.STDOUT, creationflags=HIDE | LOW_PRIORITY,
                )
                unknown_since = None
                while (returncode := process.poll()) is None:
                    busy = active_projects(state, details=activity)
                    if busy:
                        live = _read_live_progress(context)
                        if (_transient_database_busy(activity) and live.get("plugin_active") is True
                                and _owns_live_pytest(process, live)):
                            if unknown_since is None:
                                unknown_since = time.monotonic()
                            _write_control(state, context, "pause", activity)
                            if time.monotonic()-unknown_since < UNKNOWN_GRACE:
                                time.sleep(.1)
                                continue  # Same child, no new tests while unknown.
                        _write_control(state, context, "stop", activity)
                        stop_requested = True
                        _stop_check(process)
                        deferred = True
                        break
                    unknown_since = None
                    _write_control(state, context, "run")
                    if time.monotonic() - started_monotonic > MAX_SECONDS:
                        stop_requested = True
                        _stop_check(process)
                        timed_out = True
                        break
                    time.sleep(2)
        output = log.read_text(encoding="utf-8", errors="replace")
        if deferred:
            output += "\nSelf-check yielded to an active task.\n"
        if timed_out:
            output += "\nSelf-check exceeded its time limit.\n"
        progress = _read_live_progress(context) if "context" in locals() else {"complete": False}
        if progress.get("deferred_reason") in _ACTIVITY_REASONS:
            deferred = True
            activity = {"reason": progress["deferred_reason"],
                        **{key: progress[key] for key in ("error_type", "sqlite_errorcode", "sqlite_errorname") if key in progress}}
        passed = (returncode == 0 and not deferred and not timed_out and progress.get("complete") is True
                  and progress.get("session_exitstatus") == 0 and not progress.get("failed")
                  and not progress.get("collection_failed")
                  and progress.get("owner") == context["owner"])
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        if process is not None and not stop_requested:
            stop_requested = True
            try:
                _stop_check(process)
            except (OSError, TypeError, subprocess.TimeoutExpired):
                pass
        try:
            output = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        output += f"\nSelf-check could not finish: {type(exc).__name__}: {exc}\n"
        passed = False
    with _report_lock(state, blocking=True):
        if version(root) != started_version:
            return {"status": "superseded", "version": started_version}
        if "context" in locals():
            try:
                stable = _live_basis(root, python) == context["basis"]
            except (OSError, ValueError, TypeError):
                stable = False
            if not stable:
                passed = False
                progress["complete"] = False
                progress["binding_unchanged"] = False
                progress["invalidated"] = "source_runtime_changed"
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
                report["activity_error_type"] = activity.get("error_type", "Error")
                for key in ("sqlite_errorcode", "sqlite_errorname"):
                    if key in activity:
                        report[key] = activity[key]
            if "progress" in locals():
                report["progress"] = progress
            _write(state / "self-check.json", report)
            return report
        failure_proved = (returncode not in (None, 0) and progress.get("finished") is True
            and progress.get("unconfigured") is True and progress.get("binding_unchanged") is True
            and not progress.get("invalidated")
            and "context" in locals() and progress.get("owner") == context["owner"]
            and (progress.get("failed", 0) > 0 or progress.get("collection_failed", 0) > 0))
        interrupted = not passed and (timed_out or not failure_proved)
        failures = 0
        if interrupted:
            failures = int(previous.get("failures", 0)) if previous.get("version") == started_version else 0
            output += "\nSelf-check interrupted before complete session evidence; no full-suite result was accepted.\n"
            log.write_text(output, encoding="utf-8")
        elif not passed:
            failures = (int(previous.get("failures", 0)) + 1
                        if previous.get("version") == started_version else 1)
        report = {
            "version": started_version,
            "status": "passed" if passed else "interrupted" if interrupted else "failed",
            "failures": failures,
            "checked_at": time.time(),
            "duration_seconds": round(time.time() - started, 1),
            "summary": "\n".join(output.strip().splitlines()[-8:]),
            "log": str(log),
        }
        if "progress" in locals():
            report["progress"] = progress
        _write(state / "self-check.json", report)
        return report


if __name__ == "__main__" and sys.argv[1:] == ["--run"]:
    raise SystemExit(0 if run()["status"] == "passed" else 1)
