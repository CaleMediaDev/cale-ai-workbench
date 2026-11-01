"""Local desktop companion. Hermes owns task claims, workers and crash recovery.

This process owns the human queue and independently runs project verification.
It never calls an LLM API. All board mutations go through the installed Hermes CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "LongTasks"
BOARD = "long-tasks"
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PS = str(
    Path(os.environ.get("SystemRoot", "C:/Windows"))
    / "System32/WindowsPowerShell/v1.0/powershell.exe"
)


def environment():
    """Give Hermes access to this installation's tools and private router token."""
    env = os.environ.copy()
    env.update(
        HERMES_HOME=str(ROOT / "data"),
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        TEMP=str(ROOT / "Temp"),
        TMP=str(ROOT / "Temp"),
        JAVA_HOME=str(ROOT / "Toolbox/jdk-21.0.13+9"),
        GHIDRA_INSTALL_DIR=str(ROOT / "Toolbox/ghidra_12.1.4_PUBLIC"),
    )
    paths = [
        ROOT / "Toolbox/node-v26.13.0-win-x64",
        ROOT / "Toolbox/node_modules/.bin",
        ROOT / "Toolbox/python/Scripts",
        ROOT / "bin",
    ]
    env["PATH"] = os.pathsep.join(map(str, paths)) + os.pathsep + env.get("PATH", "")
    env["NODE_PATH"] = (
        str(ROOT / "Toolbox/node_modules")
        + os.pathsep
        + str(Path(env["APPDATA"]) / "npm/node_modules/@playwright/mcp/node_modules")
    )
    from cloud_ai import credentials

    env["LOCAL_AI_ROUTER_KEY"] = credentials()["router_token"]
    return env


def hermes(*args, parse=False, timeout=100):
    """Run one Hermes CLI operation with a bounded wait and readable errors."""
    result = subprocess.run(
        [str(ROOT / "data/bin/hermes.exe"), *map(str, args)],
        env=environment(),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=HIDE,
    )
    if result.returncode:
        raise RuntimeError((result.stderr + "\n" + result.stdout)[-5000:])
    return json.loads(result.stdout) if parse else result.stdout


def board(*args, **kwargs):
    return hermes("kanban", "--board", BOARD, *args, **kwargs)


@contextlib.contextmanager
def database():
    """Open the local task queue and commit mutations when the caller exits."""
    STATE.mkdir(exist_ok=True)
    conn = sqlite3.connect(STATE / "projects.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY, title TEXT, folder TEXT UNIQUE, brief TEXT,
      card TEXT DEFAULT '', phase TEXT DEFAULT 'build', status TEXT DEFAULT 'queued',
      detail TEXT DEFAULT '', failures INTEGER DEFAULT 0, generation INTEGER DEFAULT 0,
      fingerprint TEXT DEFAULT '', updated REAL, created REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
      id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, action TEXT, note TEXT,
      status TEXT DEFAULT 'pending', result TEXT DEFAULT '')""")
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def update(pid, **values):
    values["updated"] = time.time()
    with database() as conn:
        conn.execute(
            "UPDATE projects SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
            [*values.values(), pid],
        )


def projects():
    with database() as conn:
        return [
            dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY created")
        ]


def task_progress(p):
    """Best-effort milestone progress; reserve the last part for independent review."""
    state = p["status"]
    if state == "accepted":
        return 100, "Finished and accepted"
    if state == "ready_for_review":
        return 95, "Ready for you to check"
    folder = Path(p["folder"]) / ".local-ai"
    plan = folder / "PLAN.md"
    progress = folder / "PROGRESS.md"
    checklist = (
        plan.read_text(encoding="utf-8", errors="replace") if plan.is_file() else ""
    )
    marks = re.findall(r"(?m)^\s*[-*]\s*\[([ xX])\]", checklist)
    if marks:
        done = sum(mark.lower() == "x" for mark in marks)
        percent = min(80, 10 + round(70 * done / len(marks)))
    else:
        percent = {"queued": 0, "checking": 85}.get(
            state, 70 if p["phase"] == "review" else 10
        )
    if p["phase"] == "review" and state not in ("queued", "stopped"):
        percent = max(percent, 80)
    if state == "checking":
        percent = 85
    note = (
        progress.read_text(encoding="utf-8", errors="replace")[-16000:]
        if progress.is_file()
        else ""
    )
    step = ""
    match = re.search(
        r"(?im)^\s*(?:#{1,4}\s*)?(?:[-*]\s*)?(?:next(?: concrete)? step|current task|working on)\s*[:\-]\s*(.*)$",
        note,
    )
    if match:
        step = match.group(1).strip()
        if not step:
            following = note[match.end() :].splitlines()
            step = next((line.strip(" -*\t") for line in following if line.strip()), "")
    if not step:
        step = {
            "queued": "Getting ready",
            "checking": "Checking the work",
            "paused": "Paused until you continue",
            "stopped": "Stopped",
            "needs_input": "Needs your help",
        }.get(
            state,
            "Reviewing the work" if p["phase"] == "review" else "Planning and building",
        )
    return percent, step[:180]


def read_task_status(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def request(pid, action, note=""):
    """Queue a UI action for the monitor instead of blocking the window."""
    with database() as conn:
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
            raise ValueError("This task is no longer in the list.")
        conn.execute(
            "INSERT INTO requests(project,action,note) VALUES(?,?,?)",
            (pid, action, note),
        )


def add_project(title, folder, brief):
    """Create a task without overlapping another task's project folder."""
    title, brief = title.strip(), brief.strip()
    if not brief:
        raise ValueError("Tell the assistant what you would like done.")
    if not title:
        title = brief.splitlines()[0].strip()[:60] or "My task"
    pid = uuid.uuid4().hex[:12]
    folder = Path(folder).resolve() if folder.strip() else ROOT / "Projects" / pid
    if (
        folder == ROOT
        or folder in ROOT.parents
        or folder.is_relative_to(ROOT / "app")
        or folder.is_relative_to(ROOT / "data")
    ):
        raise ValueError(
            "Choose a project folder, not the AI installation or a parent folder."
        )
    for item in projects():
        other = Path(item["folder"])
        if (
            folder == other
            or folder.is_relative_to(other)
            or other.is_relative_to(folder)
        ):
            raise ValueError(
                "That folder overlaps another queued project. Use its Add instructions button."
            )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".local-ai").mkdir(exist_ok=True)
    rules = folder / "AGENTS.md"
    if not rules.exists():
        rules.write_text(
            "# Local project\n\nPreserve existing data and tests. Keep .local-ai/PLAN.md and PROGRESS.md current. "
            "Work toward the user's full request in verified milestones. Run real behavior tests after the last edit. "
            "Use the configured AI route only: local Ollama or user-connected free cloud. Do not configure paid AI or invoke Codex. Do not publish or send external messages without authorization.\n",
            encoding="utf-8",
        )
    with database() as conn:
        conn.execute(
            "INSERT INTO projects(id,title,folder,brief,created,updated) VALUES(?,?,?,?,?,?)",
            (pid, title, str(folder), brief, time.time(), time.time()),
        )
    return pid


def project_prompt(p, extra=""):
    """Build the resumable worker brief and its independent check contract."""
    return f"""USER PROJECT: {p["title"]}
PROJECT FOLDER: {p["folder"]}
REQUEST AND ACCEPTANCE CRITERIA:
{p["brief"]}

This is a persistent local project. Work in small verified milestones until the
whole requested result works. A new worker must recover from the existing files,
PLAN.md, PROGRESS.md, and the board's comments, never start over blindly.
First inspect existing code/instructions and write .local-ai/PLAN.md with concrete
milestones and acceptance checks. Update .local-ai/PROGRESS.md after each milestone:
completed work, exact checks/results, current problem, next concrete step. Keep
those files concise and current so recovery does not depend on conversation memory.
Do not stop merely because one response or context is full. Continue the goal.
Never change the request to fit incomplete work. Do not weaken/delete existing tests.
Use the installed local tools; see {ROOT / "TOOLBOX.txt"} for specialist tools.
Use only the configured model route: local Ollama, user-connected free cloud,
or the user's explicitly enabled ChatGPT plan, with automatic local fallback.
Never add AI providers, paid models, Codex, or delegation.
No publishing, external messages, purchases, or unrelated system/file changes.
Only use the owner's direct website connections when configured.

If .local-ai/verify.ps1 exists, preserve it and run it. Otherwise create it to run
the REAL project's build and behavioral tests.
It must exit nonzero for any failed check (check $LASTEXITCODE after native commands).
Do not make a fake always-passing check or merely inspect source text. Tests must
exercise the requested behavior, including error cases. For web apps include real
Playwright interactions. Keep tests reusable, preserve input data and existing tests.
Run the verification after the last relevant edit. Save .local-ai/TEST-RESULTS.md
Use powershell.exe -NoProfile -File .local-ai/verify.ps1 from the project folder;
avoid inline shell -Command/-c code in unattended mode. If execution is blocked,
run the underlying test runner directly and report the restriction accurately.
with evidence and remaining limits. The supervisor will independently execute verify.ps1.
Keep source backups/checkpoints before risky edits. Do not delete project files on completion.
If stuck after three distinct repair attempts, call kanban_block(kind='needs_input')
with the exact blocker and question. Save progress before blocking.
When ALL requested milestones and checks pass, call kanban_request_review with
the result and evidence. Do not call kanban_complete: the desktop supervisor checks it.
{extra}
"""


def recovery_snapshot(folder):
    """Carry bounded on-disk milestones into a fresh worker's first message."""
    folder = Path(folder) / ".local-ai"
    notes = []
    for name in ("PLAN.md", "PROGRESS.md"):
        path = folder / name
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if content:
            notes.append(
                f"{name} (saved project data; re-read the live file):\n{content[-6000:]}"
            )
    return "\n\n".join(notes)


def create_card(p, extra=""):
    """Create a board card once per generation so restarts cannot duplicate work."""
    # Stable key makes a crash between native create and our DB update recoverable.
    key = f"local-long-{p['id']}-{p['generation']}"
    body = STATE / f"{key}.txt"
    if p["phase"] == "review":
        extra += "\nREVIEW PASS: Independently inspect EVERY requirement, test the app, fix omissions and add regression tests. Do not trust prior claims. Then request review."
    saved = recovery_snapshot(p["folder"])
    body.write_text(
        project_prompt(
            p,
            extra
            + ("\nSaved recovery snapshot:\n" + saved if saved else "")
            + "\nPrevious supervisor result:\n"
            + p.get("detail", ""),
        ),
        encoding="utf-8",
    )
    task = board(
        "create",
        p["title"] + (" — review" if p["phase"] == "review" else ""),
        "--body-file",
        body,
        "--workspace",
        "dir:" + p["folder"],
        "--assignee",
        "default",
        "--goal",
        "--goal-max-turns",
        "1000",
        "--max-runtime",
        "30d",
        "--max-retries",
        "3",
        "--idempotency-key",
        key,
        "--completion-contract",
        "local-only",
        "--json",
        parse=True,
    )
    update(
        p["id"],
        card=task["id"],
        status="working",
        detail="Saved on the persistent Hermes board.",
    )
    return task


SKIP = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".local-ai",
    ".next",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
}


def fingerprint(folder):
    """Fingerprint project files and verification script before acceptance."""
    digest = hashlib.sha256()
    count = 0
    for base, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP and not (Path(base) / d).is_symlink()
        )
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink():
                continue
            stat = path.stat()
            digest.update(str(path.relative_to(folder)).encode())
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
            count += 1
            if count > 100000:
                raise RuntimeError(
                    "Project too large for the completion freshness check."
                )
    # The actual verification entry point is always included.
    verify = Path(folder) / ".local-ai/verify.ps1"
    if verify.exists():
        digest.update(verify.read_bytes())
    return digest.hexdigest()


def verify_project(p, timeout=1200):
    """Run project checks in a kill-on-close Windows Job for safe cancellation."""
    folder = Path(p["folder"])
    script = folder / ".local-ai/verify.ps1"
    if not script.is_file() or not script.read_text(encoding="utf-8-sig").strip():
        return (
            False,
            "Missing .local-ai/verify.ps1. Create and run real acceptance tests.",
        )
    log = STATE / f"{p['id']}-verification.log"
    with log.open("w", encoding="utf-8") as out:
        out.write(f"Independent verification: {time.ctime()}\nProject: {folder}\n")
        out.flush()
        # Start suspended, assign a Windows Job, then resume. If the supervisor
        # crashes, Windows closes the job and kills only this check's descendants.
        import ctypes
        from ctypes import wintypes

        import win32job

        job = win32job.CreateJobObject(None, "Local\\LocalAIVerify-" + uuid.uuid4().hex)
        limits = win32job.QueryInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation
        )
        limits["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        win32job.SetInformationJobObject(
            job, win32job.JobObjectExtendedLimitInformation, limits
        )
        process = subprocess.Popen(
            [
                PS,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            cwd=folder,
            env=environment(),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=HIDE | 0x00000004,
        )  # CREATE_SUSPENDED
        try:
            win32job.AssignProcessToJobObject(job, int(process._handle))
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            resume.restype = wintypes.LONG
            if resume(int(process._handle)) < 0:
                raise RuntimeError("Could not resume verification process")
        except BaseException:
            process.kill()
            process.wait()
            job.Close()
            raise
        deadline = time.monotonic() + timeout
        try:
            while True:
                (STATE / "heartbeat.txt").write_text(time.ctime(), encoding="utf-8")
                with database() as conn:
                    pause = conn.execute(
                        "SELECT 1 FROM requests WHERE project=? AND action IN ('pause','stop','delete') AND status='pending' LIMIT 1",
                        (p["id"],),
                    ).fetchone()
                if pause:
                    win32job.TerminateJobObject(job, 125)
                    process.wait(timeout=15)
                    code = 125
                    break
                try:
                    code = process.wait(
                        timeout=min(10, max(0.01, deadline - time.monotonic()))
                    )
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise
        except subprocess.TimeoutExpired:
            # Only our verification process tree, never the shared model/gateway.
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                creationflags=HIDE,
            )
            process.wait(timeout=15)
            code = 124
        finally:
            job.Close()
        out.write(f"\nVerification exit code: {code}\n")
    return code == 0, log.read_text(encoding="utf-8", errors="replace")[-12000:]


def handle_handoff(p, task):
    """Verify a worker handoff, then start review or ask the user for help."""
    update(
        p["id"],
        status="checking",
        detail="Running the project's verification independently (up to 20 minutes).",
    )
    passed, evidence = verify_project(p)
    with database() as conn:
        pending = conn.execute(
            "SELECT 1 FROM requests WHERE project=? AND status='pending' LIMIT 1",
            (p["id"],),
        ).fetchone()
    if pending:
        process_requests()
        current = next((x for x in projects() if x["id"] == p["id"]), None)
        if not current or current["status"] != "checking":
            return
    if not passed:
        failures = p["failures"] + 1
        update(
            p["id"],
            failures=failures,
            detail=evidence,
            status="needs_input" if failures >= 3 else "working",
        )
        if failures >= 3:
            return
        note = (
            "Independent verification failed. Fix the underlying behavior and rerun tests.\n"
            + evidence
        )
        if task["status"] == "review":
            board("comment", task["id"], note)
            board(
                "reopen-review",
                task["id"],
                "--reason",
                "Independent verification failed",
            )
        else:
            update(p["id"], card="", generation=p["generation"] + 1)
            p = next(x for x in projects() if x["id"] == p["id"])
            create_card(p, note)
        return
    if p["phase"] == "build":
        # Fresh worker/context reviews the finished work against the original request.
        # Keep the implementation card's review state/history. Its worker has
        # handed off; local review dispatch is disabled. Do not ask an LLM judge
        # to certify an implementation while the separate review is still owed.
        update(
            p["id"],
            card="",
            phase="review",
            generation=p["generation"] + 1,
            detail=evidence,
        )
        p = next(x for x in projects() if x["id"] == p["id"])
        create_card(
            p,
            "REVIEW PASS: Independently inspect the implementation against EVERY original requirement. "
            "Read existing tests, exercise the app, repair omissions and add regression tests. "
            "Do not trust the previous worker's claims. Then request review with evidence.",
        )
    else:
        update(
            p["id"],
            status="ready_for_review",
            detail=evidence,
            fingerprint=fingerprint(Path(p["folder"])),
        )


def process_requests():
    """Apply queued UI actions to SQLite and the native Hermes board."""
    with database() as conn:
        pending = [
            dict(x)
            for x in conn.execute(
                "SELECT * FROM requests WHERE status='pending' ORDER BY id"
            )
        ]
    for req in pending:
        try:
            p = next((x for x in projects() if x["id"] == req["project"]), None)
            if not p:
                raise ValueError("This task has already been removed.")
            action, note = req["action"], req["note"]
            snapshot = (
                board("show", p["card"], "--json", parse=True) if p["card"] else {}
            )
            task = snapshot.get("task")
            if action in ("pause", "stop", "delete"):
                if task and task["status"] in ("running", "ready", "scheduled"):
                    board(
                        "block",
                        p["card"],
                        action.title() + " by user",
                        "--kind",
                        "needs_input",
                    )
                elif (
                    task
                    and action in ("stop", "delete")
                    and task["status"] in ("review", "done", "triage")
                ):
                    board("archive", p["card"])
                if action == "delete":
                    with database() as conn:
                        conn.execute("DELETE FROM projects WHERE id=?", (p["id"],))
                        conn.execute(
                            "UPDATE requests SET status='done',result='Task removed' WHERE project=? AND status='pending' AND id!=?",
                            (p["id"], req["id"]),
                        )
                else:
                    state = "stopped" if action == "stop" else "paused"
                    update(
                        p["id"],
                        status=state,
                        detail=f"{state.title()}. A retiring worker may take up to two minutes to exit. Project files are preserved.",
                    )
            elif action in ("resume", "note"):
                ended = max(
                    (r.get("ended_at") or 0 for r in snapshot.get("runs", [])),
                    default=0,
                )
                if p["status"] == "paused" and ended and time.time() - ended < 180:
                    raise ValueError(
                        "The paused worker is retiring. Wait up to three minutes before resuming to avoid overlapping edits."
                    )
                if note:
                    update(p["id"], brief=p["brief"] + "\n\nUSER FOLLOW-UP:\n" + note)
                    if task:
                        board("comment", p["card"], "USER FOLLOW-UP: " + note)
                if task and task["status"] in ("blocked", "scheduled"):
                    board(
                        "unblock",
                        p["card"],
                        "--reason",
                        note or "User requested resume",
                    )
                elif task and task["status"] == "review":
                    board(
                        "reopen-review",
                        p["card"],
                        "--reason",
                        note or "Continue and recheck the project",
                    )
                elif task and task["status"] in ("done", "archived", "triage"):
                    update(p["id"], card="", generation=p["generation"] + 1)
                update(
                    p["id"], status="working", failures=0, fingerprint="", phase="build"
                )
            elif action == "accept":
                if (
                    p["status"] != "ready_for_review"
                    or fingerprint(Path(p["folder"])) != p["fingerprint"]
                ):
                    raise ValueError(
                        "Checks are missing or stale. Resume and verify before accepting."
                    )
                if task and task["status"] in ("review", "done"):
                    board(
                        "comment",
                        p["card"],
                        "User accepted the independently checked project.\n"
                        + p["detail"][-8000:],
                    )
                    board("archive", p["card"])
                update(
                    p["id"],
                    status="accepted",
                    detail="Accepted by user. Project files and history retained.",
                )
            else:
                raise ValueError("Unknown action")
            result, status = "Applied", "done"
        except Exception as exc:
            result, status = str(exc), "error"
        with database() as conn:
            conn.execute(
                "UPDATE requests SET status=?,result=? WHERE id=?",
                (status, result, req["id"]),
            )


def tick():
    """Advance each runnable task by one supervisor step."""
    process_requests()
    for p in projects():
        if p["status"] in ("accepted", "paused", "stopped", "needs_input"):
            continue
        if p["status"] == "ready_for_review":
            if fingerprint(Path(p["folder"])) != p["fingerprint"]:
                update(
                    p["id"],
                    status="needs_input",
                    detail="Files changed after verification. Resume to check them again.",
                )
            continue
        if not p["card"]:
            create_card(p)
            continue
        snapshot = board("show", p["card"], "--json", parse=True)
        task = snapshot["task"]
        if task["status"] in ("review", "done"):
            ended = max(
                (r.get("ended_at") or 0 for r in snapshot.get("runs", [])), default=0
            )
            if ended and time.time() - ended < 150:
                update(
                    p["id"],
                    status="working",
                    detail="Worker handed off. Letting it finish and release files before independent checks.",
                )
                continue
            handle_handoff(p, task)
        elif task["status"] in ("blocked", "triage", "archived", "scheduled"):
            comments = snapshot.get("comments", [])
            update(
                p["id"],
                status="needs_input",
                detail=json.dumps(
                    {"task": task, "recent_comments": comments[-3:]}, indent=2
                )[-12000:],
            )
        else:
            update(
                p["id"],
                status="working",
                detail=f"Hermes: {task['status']}. Card {p['card']}. Progress is saved in the project folder.",
            )


def monitor():
    """Own the single background supervisor loop across app windows."""
    import msvcrt

    STATE.mkdir(exist_ok=True)
    lock = (STATE / "monitor.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"1")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return
    (STATE / "monitor-pid.txt").write_text(str(os.getpid()), encoding="utf-8")
    # Native gateway startup is idempotent and owns its own process lock.
    last_start = 0
    while True:
        try:
            active = any(
                p["status"]
                not in (
                    "accepted",
                    "paused",
                    "stopped",
                    "needs_input",
                    "ready_for_review",
                )
                for p in projects()
            )
            if active and time.monotonic() - last_start > 120:
                facts_file = max(
                    (ROOT / "data/installs").glob("*/facts.json"),
                    key=lambda f: f.stat().st_mtime,
                )
                facts = json.loads(facts_file.read_text())
                py = str(
                    Path(facts["packages"]["venv"]["environment"])
                    / "Scripts/python.exe"
                )
                settings = subprocess.run(
                    [py, str(ROOT / "read-settings.py")],
                    env=environment(),
                    check=True,
                    capture_output=True,
                    text=True,
                    creationflags=HIDE,
                )
                ensure_ollama(json.loads(settings.stdout)["model"])
                from cloud_ai import ensure_router

                ensure_router()
                hermes("gateway", "start", timeout=120)
                last_start = time.monotonic()
            tick()
            (STATE / "heartbeat.txt").write_text(time.ctime(), encoding="utf-8")
            (STATE / "service-status.txt").write_text(
                "Supervisor online. " + time.ctime(), encoding="utf-8"
            )
        except Exception:
            (STATE / "service-status.txt").write_text(
                "Supervisor is retrying after an error. See monitor-errors.log. "
                + time.ctime(),
                encoding="utf-8",
            )
            with (STATE / "monitor-errors.log").open("a", encoding="utf-8") as log:
                log.write(time.ctime() + "\n" + traceback.format_exc() + "\n")
        time.sleep(15)


def ensure_ollama(model):
    """Start local Ollama if needed and reject remote model aliases."""
    from urllib.request import Request, urlopen

    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            json.load(response)
    except OSError:
        exe = Path(os.environ["LOCALAPPDATA"]) / "Programs/Ollama/ollama.exe"
        if not exe.exists():
            exe = ROOT / "Toolbox/ollama/ollama.exe"
        subprocess.Popen(
            [str(exe), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=HIDE,
        )
        for attempt in range(15):
            time.sleep(1)
            try:
                with urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
                    json.load(response)
                break
            except OSError:
                if attempt == 14:
                    raise RuntimeError("Ollama did not start")
    request = Request(
        "http://127.0.0.1:11434/api/show",
        data=json.dumps({"model": model}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        info = json.load(response)
    if info.get("remote_host") or info.get("remote_model"):
        raise RuntimeError(
            "Long tasks require local weights, not a remote Ollama model."
        )


def start_monitor():
    """Launch a background supervisor; its file lock keeps one owner active."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen(
        [str(pythonw), str(Path(__file__).resolve()), "--monitor"],
        cwd=ROOT,
        env=environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=HIDE,
    )


def gui():
    """Show the desktop shell; the monitor and model workers run separately."""
    import queue
    import threading
    import tkinter as tk
    import webbrowser
    from tkinter import filedialog, messagebox, simpledialog, ttk

    from PIL import Image, ImageTk

    import chatgpt_auth
    import voice_assistant
    from cloud_ai import (
        connect_account,
        disconnect,
        ensure_router,
    )
    from cloud_ai import (
        status as cloud_status,
    )
    from model_library import catalog_signature, save_download_status

    start_monitor()
    window = tk.Tk()
    window.title("Cale AI Workbench")
    window.geometry("1120x790")
    window.minsize(850, 600)
    bg, card, field = "#0b1020", "#151d31", "#202d46"
    text, muted, accent = "#edf3ff", "#aab8d3", "#6e82ff"
    window.configure(background=bg)
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("TFrame", background=bg)
    style.configure("Card.TFrame", background=card)
    style.configure("TLabel", background=bg, foreground=text, font=("Segoe UI", 11))
    style.configure("Muted.TLabel", background=bg, foreground=muted)
    style.configure("Card.TLabel", background=card, foreground=text)
    style.configure("CardMuted.TLabel", background=card, foreground=muted)
    style.configure(
        "TButton",
        background="#293955",
        foreground=text,
        borderwidth=0,
        padding=(16, 10),
        font=("Segoe UI", 10, "bold"),
    )
    style.map(
        "TButton",
        background=[("active", "#3a4c6d"), ("disabled", "#1b263a")],
        foreground=[("disabled", "#73819a")],
    )
    style.configure(
        "Accent.TButton", background=accent, foreground="white", padding=(18, 11)
    )
    style.map("Accent.TButton", background=[("active", "#8797ff")])
    style.configure("Danger.TButton", background="#a94358", foreground="white")
    style.map("Danger.TButton", background=[("active", "#c15367")])
    style.configure(
        "Cale.Horizontal.TProgressbar",
        background=accent,
        troughcolor="#33415e",
        borderwidth=0,
        thickness=12,
    )

    outer = ttk.Frame(window, padding=(26, 20))
    outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer)
    header.pack(fill="x", pady=(0, 20))
    logo_path = ROOT / "assets/cale-logo.png"
    if logo_path.is_file():
        with Image.open(logo_path) as logo_source:
            logo_source.thumbnail((66, 66), Image.Resampling.LANCZOS)
            window.cale_logo = ImageTk.PhotoImage(logo_source.copy())
        tk.Label(header, image=window.cale_logo, bg=bg, borderwidth=0).pack(
            side="left", padx=(0, 14)
        )
    else:
        tk.Label(
            header,
            text="◆",
            bg=accent,
            fg="white",
            font=("Segoe UI", 22, "bold"),
            width=3,
            height=1,
        ).pack(side="left", padx=(0, 16))
    brand = ttk.Frame(header)
    brand.pack(side="left")
    ttk.Label(brand, text="CALE  /  AI", font=("Segoe UI", 21, "bold")).pack(anchor="w")
    ttk.Label(
        brand,
        text="A helpful assistant that keeps working and remembers your progress.",
        style="Muted.TLabel",
    ).pack(anchor="w")

    main_page = ttk.Frame(outer)
    super_page = ttk.Frame(outer)

    def show_super():
        main_page.pack_forget()
        rocket.pack_forget()
        super_page.pack(fill="both", expand=True)

    def show_main():
        super_page.pack_forget()
        rocket.pack(side="right")
        main_page.pack(fill="both", expand=True)

    rocket = ttk.Button(
        header, text="🚀  Supercharge my AI", command=show_super, style="Accent.TButton"
    )
    rocket.pack(side="right")
    main_page.pack(fill="both", expand=True)

    ttk.Button(super_page, text="←  Back to my tasks", command=show_main).pack(
        anchor="w", pady=(0, 12)
    )
    ttk.Label(super_page, text="Supercharge my AI", font=("Segoe UI", 28, "bold")).pack(
        anchor="w"
    )
    ttk.Label(
        super_page,
        text="Connect free online AI when you want more help.",
        font=("Segoe UI", 14),
        style="Muted.TLabel",
    ).pack(anchor="w", pady=(5, 12))
    cloud_events = queue.Queue()
    cloud_text = tk.StringVar(value="Your local AI still works without this.")

    def connect():
        connect_button.configure(state="disabled")
        cloud_text.set(
            "Finish signing in and approving the connection in your browser."
        )

        def worker():
            try:
                ensure_router()
                connect_account()
                cloud_events.put("Connected. Free online AI is ready when available.")
            except Exception as exc:
                cloud_events.put("Connection not finished: " + str(exc))

        threading.Thread(target=worker, daemon=True).start()

    connect_button = ttk.Button(
        super_page,
        text="Connect to OpenRouter",
        command=connect,
        style="Accent.TButton",
    )
    connect_button.pack(anchor="w", ipadx=38, ipady=4)
    ttk.Label(
        super_page, textvariable=cloud_text, style="Muted.TLabel", wraplength=680
    ).pack(anchor="w", pady=(8, 2))

    def disconnect_cloud():
        disconnect()
        cloud_text.set("Disconnected. Your local AI still works.")

    disconnect_button = ttk.Button(
        super_page, text="Disconnect account", command=disconnect_cloud
    )
    router_note = ttk.Label(
        super_page,
        text="OpenRouter uses free models only. Your local AI takes over if it is unavailable.",
        style="Muted.TLabel",
        wraplength=680,
    )
    router_note.pack(anchor="w", pady=(4, 15))
    ttk.Label(
        super_page, text="Or use your ChatGPT account", font=("Segoe UI", 18, "bold")
    ).pack(anchor="w")
    ttk.Label(
        super_page,
        text="ChatGPT plan use shares your ChatGPT and Codex allowance. Connecting alone will not use it.",
        style="Muted.TLabel",
        wraplength=720,
    ).pack(anchor="w", pady=(4, 9))
    chat_events = queue.Queue()
    chat_text = tk.StringVar(value="Not connected.")

    def connect_chatgpt():
        chat_button.configure(state="disabled")
        chat_text.set("Finish signing in and approving the connection in your browser.")

        def worker():
            try:
                result = chatgpt_auth.connect()
                message = "Connected as " + (result["email"] or "your ChatGPT account")
                if not result["sharing"]:
                    message += ". ChatGPT plan use was not approved."
                chat_events.put(message)
            except Exception as exc:
                chat_events.put("Connection not finished: " + str(exc))

        threading.Thread(target=worker, daemon=True).start()

    chat_button = ttk.Button(
        super_page,
        text="Continue with ChatGPT",
        command=connect_chatgpt,
        style="Accent.TButton",
    )
    chat_button.pack(anchor="w", ipadx=36, ipady=4)
    ttk.Label(
        super_page, textvariable=chat_text, style="Muted.TLabel", wraplength=720
    ).pack(anchor="w", pady=(8, 4))

    def toggle_chatgpt():
        current = chatgpt_auth.status()
        if not current["enabled"] and not messagebox.askyesno(
            "Use ChatGPT plan",
            "Use your shared ChatGPT/Codex allowance for this app's tasks? OpenRouter and local AI remain available.",
            parent=window,
        ):
            return
        try:
            chatgpt_auth.set_enabled(not current["enabled"])
            refresh_chatgpt()
        except Exception as exc:
            messagebox.showerror("ChatGPT plan", str(exc), parent=window)

    use_chat_button = ttk.Button(
        super_page, text="Use my ChatGPT plan for tasks", command=toggle_chatgpt
    )
    use_chat_button.pack(anchor="w", pady=(4, 0))
    ttk.Button(
        super_page,
        text="Manage ChatGPT usage",
        command=lambda: webbrowser.open("https://chatgpt.com/settings/usage"),
    ).pack(anchor="w", pady=(4, 0))

    def disconnect_chatgpt():
        chat_button.configure(state="disabled")

        def worker():
            try:
                chatgpt_auth.disconnect()
                chat_events.put("ChatGPT disconnected. Your local AI still works.")
            except Exception as exc:
                chat_events.put(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    chat_disconnect_button = ttk.Button(
        super_page, text="Disconnect ChatGPT", command=disconnect_chatgpt
    )

    def refresh_chatgpt():
        state = chatgpt_auth.status()
        use_chat_button.configure(
            state="normal" if state["sharing"] else "disabled",
            text="Stop using my ChatGPT plan"
            if state["enabled"]
            else "Use my ChatGPT plan for tasks",
        )
        if state["connected"] and not chat_disconnect_button.winfo_manager():
            chat_disconnect_button.pack(anchor="w", pady=(4, 0))
        elif not state["connected"] and chat_disconnect_button.winfo_manager():
            chat_disconnect_button.pack_forget()
        if state["connected"] and not chat_text.get().startswith(
            ("Connection not finished", "Finish signing")
        ):
            chat_text.set(
                "Connected as "
                + (state["email"] or "your ChatGPT account")
                + (
                    ". Using your ChatGPT plan for tasks."
                    if state["enabled"]
                    else ". Plan usage is off."
                )
            )

    title_row = ttk.Frame(main_page)
    title_row.pack(fill="x", pady=(0, 8))
    title_copy = ttk.Frame(title_row)
    title_copy.pack(side="left")
    ttk.Label(title_copy, text="My tasks", font=("Segoe UI", 29, "bold")).pack(
        anchor="w"
    )
    ttk.Label(
        title_copy,
        text="See what your assistant is doing and what needs your attention.",
        style="Muted.TLabel",
    ).pack(anchor="w", pady=(3, 0))
    status = tk.StringVar(value="")

    def new(initial_text=""):
        setup = read_task_status(STATE / "install-progress.json")
        if setup and setup.get("status") != "complete":
            messagebox.showinfo(
                "Setting up your AI",
                "Wait for setup to finish before starting a task.",
                parent=window,
            )
            return
        dialog = tk.Toplevel(window)
        dialog.title("New task")
        dialog.geometry("720x520")
        dialog.configure(background=bg)
        dialog.transient(window)
        pane = ttk.Frame(dialog, padding=20)
        pane.pack(fill="both", expand=True)
        folder = tk.StringVar()
        ttk.Label(
            pane, text="What would you like done?", font=("Segoe UI", 17, "bold")
        ).pack(anchor="w")
        ttk.Label(
            pane,
            text="For example: Make a simple website for my bakery, with hours, photos, and a contact form.",
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(7, 14))
        brief = tk.Text(
            pane,
            wrap="word",
            height=14,
            background=field,
            foreground=text,
            insertbackground=text,
            selectbackground="#334a85",
            relief="flat",
            padx=12,
            pady=10,
        )
        brief.pack(fill="both", expand=True)
        if initial_text:
            brief.insert("1.0", initial_text)
        folder_label = tk.StringVar(
            value="A new folder will be made automatically for this task."
        )
        choice = ttk.Frame(pane)
        choice.pack(fill="x", pady=(14, 0))
        ttk.Label(
            choice, textvariable=folder_label, style="Muted.TLabel", wraplength=460
        ).pack(side="left", fill="x", expand=True)

        def browse():
            selected = filedialog.askdirectory(parent=dialog)
            if selected:
                folder.set(selected)
                folder_label.set("Working in: " + selected)

        ttk.Button(choice, text="Use a folder I already have", command=browse).pack(
            side="right"
        )
        ttk.Label(
            pane,
            text="Your computer needs to stay on while it works. You can close this window and check back later.",
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(12, 8))

        def save():
            try:
                add_project("", folder.get(), brief.get("1.0", "end"))
                dialog.destroy()
                status.set("Task saved. Your assistant will start shortly.")
                render_cards()
            except Exception as exc:
                messagebox.showerror("Could not start task", str(exc), parent=dialog)

        ttk.Button(
            pane, text="Start my task", command=save, style="Accent.TButton"
        ).pack(anchor="e")
        brief.focus_set()

    new_button = ttk.Button(
        title_row, text="+  New task", command=new, style="Accent.TButton"
    )
    new_button.pack(side="right", pady=(8, 0))
    voice_events = queue.Queue()
    voice_busy = threading.Event()
    voice_speaking = threading.Event()
    listening = threading.Event()
    spoken_updates = {}
    last_spoken_at = {}
    voice_label = tk.StringVar(value="Voice ready · Ctrl+Alt+Space")

    def speak_update(message):
        """Keep speech off the UI thread and avoid recording the app's voice."""

        def worker():
            voice_speaking.set()
            try:
                voice_assistant.say(message)
            except Exception as exc:
                voice_events.put(("error", str(exc)))
            finally:
                voice_speaking.clear()

        threading.Thread(target=worker, daemon=True).start()

    def record_voice(repeat=False):
        if voice_busy.is_set() or voice_speaking.is_set():
            return
        voice_busy.set()
        voice_events.put(("state", "Listening… speak now."))

        def worker():
            try:
                phrase = voice_assistant.listen(8)
                if phrase:
                    voice_events.put(("heard", phrase))
                elif not repeat:
                    voice_events.put(
                        ("state", "I didn't hear anything. Try Talk again.")
                    )
            except Exception as exc:
                listening.clear()
                voice_events.put(("error", str(exc)))
            finally:
                voice_busy.clear()
                if repeat and listening.is_set():
                    voice_events.put(("again", ""))

        threading.Thread(target=worker, daemon=True).start()

    def toggle_listening():
        if listening.is_set():
            listening.clear()
            listen_button.configure(text="Hands-free: Off")
            voice_label.set("Hands-free listening stopped.")
        else:
            listening.set()
            listen_button.configure(text="Hands-free: On")
            maybe_listen()

    def use_spoken_request(phrase):
        """Send spoken instructions to the current task, or start a new one."""
        active = [
            item
            for item in projects()
            if item["status"]
            in ("queued", "working", "checking", "paused", "needs_input")
        ]
        if active:
            selected = max(active, key=lambda item: item["updated"] or 0)
            request(selected["id"], "note", phrase)
            voice_label.set(
                "Heard: " + phrase[:100] + " · added to " + selected["title"]
            )
        else:
            listening.clear()
            listen_button.configure(text="Hands-free: Off")
            voice_label.set("Heard: " + phrase[:100])
            new(phrase)

    ttk.Button(title_row, text="🎤  Talk", command=record_voice).pack(
        side="right", padx=(0, 8), pady=(8, 0)
    )
    listen_button = ttk.Button(
        title_row, text="Hands-free: Off", command=toggle_listening
    )
    listen_button.pack(side="right", padx=(0, 8), pady=(8, 0))
    window.bind("<Control-Alt-space>", lambda _: record_voice())
    ttk.Label(main_page, textvariable=voice_label, style="Muted.TLabel").pack(
        anchor="w", pady=(0, 5)
    )
    ttk.Label(
        main_page, textvariable=status, style="Muted.TLabel", wraplength=1000
    ).pack(anchor="w", pady=(0, 6))

    list_wrap = ttk.Frame(main_page)
    list_wrap.pack(fill="both", expand=True)
    canvas = tk.Canvas(list_wrap, background=bg, borderwidth=0, highlightthickness=0)
    scrollbar = ttk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    cards = ttk.Frame(canvas)
    cards_window = canvas.create_window((0, 0), window=cards, anchor="nw")
    cards.bind(
        "<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all"))
    )
    canvas.bind(
        "<Configure>",
        lambda event: canvas.itemconfigure(cards_window, width=event.width),
    )
    wheel_remainder = 0.0

    def over_task_list():
        if not main_page.winfo_ismapped():
            return False
        widget = window.winfo_containing(
            window.winfo_pointerx(), window.winfo_pointery()
        )
        while widget is not None:
            if widget is canvas:
                return True
            widget = widget.master
        return False

    def on_mousewheel(event):
        nonlocal wheel_remainder
        if not over_task_list():
            return None
        wheel_remainder -= event.delta / 120 * 3
        lines = int(wheel_remainder)
        wheel_remainder -= lines
        if lines:
            canvas.yview_scroll(lines, "units")
        return "break"

    def on_wheel_button(event):
        if not over_task_list():
            return None
        canvas.yview_scroll(-3 if event.num == 4 else 3, "units")
        return "break"

    window.bind_all("<MouseWheel>", on_mousewheel, add="+")
    window.bind_all("<Button-4>", on_wheel_button, add="+")
    window.bind_all("<Button-5>", on_wheel_button, add="+")

    def task_action(pid, kind):
        item = next((p for p in projects() if p["id"] == pid), None)
        if not item:
            return
        if kind in ("camera_on", "camera_off"):
            consent = ROOT / "Media/camera-consent.json"
            consent.parent.mkdir(exist_ok=True)
            consent.write_text(
                json.dumps({"until": time.time() + 3600 if kind == "camera_on" else 0}),
                encoding="utf-8",
            )
            status.set(
                "Camera allowed for one hour."
                if kind == "camera_on"
                else "Camera turned off."
            )
            render_cards()
            return
        if kind == "open":
            os.startfile(item["folder"])
            return
        if kind == "delete":
            if not messagebox.askyesno(
                "Delete task",
                "Remove this task from the list? Its project files will stay on your PC. A running worker may take up to two minutes to stop.",
                parent=window,
            ):
                return
        if kind == "stop":
            if not messagebox.askyesno(
                "Stop task",
                "Stop this task permanently? Its project files will stay on your PC.",
                parent=window,
            ):
                return
        note = ""
        if kind == "note":
            note = simpledialog.askstring(
                "Task instructions",
                "What should your assistant do next?",
                parent=window,
            )
            if not note:
                return
        try:
            request(pid, kind, note)
            status.set("Request saved. Your assistant will apply it shortly.")
        except Exception as exc:
            messagebox.showerror("Could not update task", str(exc), parent=window)

    def task_details(item):
        dialog = tk.Toplevel(window)
        dialog.title(item["title"])
        dialog.geometry("680x480")
        dialog.configure(background=bg)
        pane = ttk.Frame(dialog, padding=18)
        pane.pack(fill="both", expand=True)
        ttk.Label(
            pane, text=item["title"], font=("Segoe UI", 17, "bold"), wraplength=620
        ).pack(anchor="w")
        detail = tk.Text(
            pane,
            wrap="word",
            background=card,
            foreground=text,
            relief="flat",
            padx=14,
            pady=12,
        )
        detail.pack(fill="both", expand=True, pady=(14, 0))
        content = f"Your request:\n{item['brief']}\n\nSaved progress:\n{item['detail']}"
        progress_file = Path(item["folder"]) / ".local-ai/PROGRESS.md"
        if progress_file.is_file():
            content += (
                "\n\n"
                + progress_file.read_text(encoding="utf-8", errors="replace")[-12000:]
            )
        detail.insert("1.0", content)
        detail.configure(state="disabled")

    def start_auto_models(setup):
        """Resume automatic downloads when the app opens after setup."""
        if setup and setup.get("status") != "complete":
            return
        path = STATE / "auto-model-worker.json"
        worker = read_task_status(path)
        if (
            worker.get("status") in ("complete", "error")
            and worker.get("catalog") == catalog_signature()
        ):
            return
        if (
            worker.get("status") == "working"
            and time.time() - worker.get("updated", 0) < 600
        ):
            return
        save_download_status(path, "working", "Checking this PC for optional models", 0)
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "model_library.py"),
                    "--auto",
                    "--status-dir",
                    str(STATE),
                ],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=HIDE,
            )
        except Exception as exc:
            save_download_status(path, "error", str(exc), 0)

    task_cards = {}
    setup_card = None
    empty_label = None
    # Keep a layout anchor for task cards without showing model installation
    # details on the main screen. The separate model worker owns downloads.
    models_section = ttk.Frame(cards)
    models_section.pack(fill="x")

    def set_text(variable, value):
        if variable.get() != value:
            variable.set(value)

    def create_progress_card(title, before=None):
        panel = ttk.Frame(cards, style="Card.TFrame", padding=(17, 12))
        panel.pack(fill="x", pady=(0, 9), before=before or models_section)
        top = ttk.Frame(panel, style="Card.TFrame")
        top.pack(fill="x")
        title_var = tk.StringVar(value=title)
        name = ttk.Label(
            top,
            textvariable=title_var,
            style="Card.TLabel",
            font=("Segoe UI", 15, "bold"),
            wraplength=610,
        )
        name.pack(side="left")
        percent_var = tk.StringVar(value="0%")
        ttk.Label(
            top,
            textvariable=percent_var,
            style="Card.TLabel",
            font=("Segoe UI", 13, "bold"),
        ).pack(side="right")
        state_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=state_var, style="CardMuted.TLabel").pack(
            side="right", padx=(0, 15)
        )
        bar = ttk.Progressbar(
            panel, value=0, maximum=100, style="Cale.Horizontal.TProgressbar"
        )
        bar.pack(fill="x", pady=(10, 7))
        current_var = tk.StringVar(value="")
        ttk.Label(
            panel, textvariable=current_var, style="CardMuted.TLabel", wraplength=900
        ).pack(anchor="w")
        return {
            "panel": panel,
            "name": name,
            "title": title_var,
            "percent": percent_var,
            "state": state_var,
            "bar": bar,
            "current": current_var,
        }

    def update_progress_card(card_info, percent, current, state=""):
        percent = max(0, min(100, int(percent)))
        set_text(card_info["percent"], f"{percent}%")
        set_text(card_info["current"], current)
        set_text(card_info["state"], state)
        if int(float(card_info["bar"]["value"])) != percent:
            card_info["bar"].configure(value=percent)

    def choices_for(state):
        if state in ("queued", "working", "checking"):
            return (("Stop", "stop"), ("Pause", "pause"), ("Delete", "delete"))
        if state == "paused":
            return (("Continue", "resume"), ("Stop", "stop"), ("Delete", "delete"))
        if state == "needs_input":
            return (
                ("Add instructions", "note"),
                ("Continue", "resume"),
                ("Stop", "stop"),
                ("Delete", "delete"),
            )
        if state == "ready_for_review":
            return (
                ("Looks good", "accept"),
                ("Request changes", "note"),
                ("Delete", "delete"),
            )
        return (("Delete", "delete"),)

    def update_task_controls(card_info, item):
        camera = "camera" in (item["brief"] + " " + item["detail"]).lower()
        consent = read_task_status(ROOT / "Media/camera-consent.json") if camera else {}
        allowed = bool(consent.get("until", 0) > time.time())
        signature = (item["status"], camera, allowed)
        if card_info.get("controls_signature") == signature:
            return
        controls = card_info["controls"]
        for child in controls.winfo_children():
            child.destroy()
        for label, kind in choices_for(item["status"]):
            ttk.Button(
                controls,
                text=label,
                style="Danger.TButton" if kind == "stop" else "TButton",
                command=lambda pid=item["id"], action=kind: task_action(pid, action),
            ).pack(side="left", padx=(0, 6))
        if camera:
            ttk.Button(
                controls,
                text="Turn camera off" if allowed else "Allow camera (1 hour)",
                command=lambda pid=item["id"], action="camera_off" if allowed else "camera_on": (
                    task_action(pid, action)
                ),
            ).pack(side="left", padx=(0, 6))
        ttk.Button(
            controls,
            text="Open files",
            command=lambda pid=item["id"]: task_action(pid, "open"),
        ).pack(side="right")
        card_info["controls_signature"] = signature

    def render_cards():
        """Update existing widgets in place so polling never resets the scroll."""
        nonlocal setup_card, empty_label
        setup = read_task_status(STATE / "install-progress.json")
        if setup:
            if setup_card is None:
                first = next(
                    (entry["panel"] for entry in task_cards.values()), models_section
                )
                setup_card = create_progress_card("Set up your AI", first)
            update_progress_card(
                setup_card,
                setup.get("percent", 0),
                "Current step: " + str(setup.get("current", "Preparing")),
                "Ready" if setup.get("status") == "complete" else "Setting up",
            )
        elif setup_card is not None:
            setup_card["panel"].destroy()
            setup_card = None

        all_tasks = list(reversed(projects()))
        live_ids = {item["id"] for item in all_tasks}
        for pid in list(task_cards):
            if pid not in live_ids:
                task_cards.pop(pid)["panel"].destroy()
        if not all_tasks and not setup:
            if empty_label is None:
                empty_label = ttk.Label(
                    cards,
                    text="No tasks yet. Click New task to get started.",
                    style="Muted.TLabel",
                    font=("Segoe UI", 13),
                )
                empty_label.pack(anchor="w", pady=24, before=models_section)
        elif empty_label is not None:
            empty_label.destroy()
            empty_label = None

        # Create oldest first, then insert each newer task above it. Existing
        # widgets stay in place, which preserves the exact scroll position.
        for item in reversed(all_tasks):
            pid = item["id"]
            if pid not in task_cards:
                first = next(
                    (
                        task_cards[p["id"]]["panel"]
                        for p in all_tasks
                        if p["id"] in task_cards
                    ),
                    models_section,
                )
                card_info = create_progress_card(item["title"], first)
                card_info["name"].configure(cursor="hand2")
                card_info["name"].bind(
                    "<Button-1>",
                    lambda _, task_id=pid: task_details(
                        next(p for p in projects() if p["id"] == task_id)
                    ),
                )
                controls = ttk.Frame(card_info["panel"], style="Card.TFrame")
                controls.pack(fill="x", pady=(9, 0))
                card_info["controls"] = controls
                task_cards[pid] = card_info
            card_info = task_cards[pid]
            set_text(card_info["title"], item["title"])
            percent, current = task_progress(item)
            state = item["status"].replace("_", " ").capitalize()
            update_progress_card(card_info, percent, "Current step: " + current, state)
            update_task_controls(card_info, item)

    def maybe_listen():
        if listening.is_set():
            if voice_speaking.is_set() or voice_busy.is_set():
                window.after(500, maybe_listen)
            else:
                record_voice(repeat=True)

    def voice_poll():
        try:
            while True:
                event, value = voice_events.get_nowait()
                if event == "heard":
                    try:
                        use_spoken_request(value)
                    except Exception as exc:
                        voice_label.set("Could not add spoken request: " + str(exc))
                elif event == "again":
                    maybe_listen()
                elif event == "error":
                    voice_label.set("Voice unavailable: " + value[:140])
                    listen_button.configure(text="Hands-free: Off")
                else:
                    voice_label.set(value)
        except queue.Empty:
            pass
        window.after(500, voice_poll)

    def refresh():
        setup = read_task_status(STATE / "install-progress.json")
        new_button.configure(
            state="disabled"
            if setup and setup.get("status") != "complete"
            else "normal"
        )
        try:
            while True:
                cloud_text.set(cloud_events.get_nowait())
                connect_button.configure(state="normal")
        except queue.Empty:
            pass
        try:
            while True:
                chat_text.set(chat_events.get_nowait())
                chat_button.configure(state="normal")
        except queue.Empty:
            pass
        refresh_chatgpt()
        connected = cloud_status()["connected"]
        if connected:
            if not cloud_text.get().startswith("Connected"):
                cloud_text.set("Connected. Free online AI is ready when available.")
            if not disconnect_button.winfo_manager():
                disconnect_button.pack(anchor="w", before=router_note, pady=(4, 0))
        elif disconnect_button.winfo_manager():
            disconnect_button.pack_forget()
        render_cards()
        for item in projects():
            current = (item["status"], task_progress(item)[1])
            previous = spoken_updates.get(item["id"])
            spoken_updates[item["id"]] = current
            if previous is None or previous == current:
                continue
            important = item["status"] in (
                "needs_input",
                "ready_for_review",
                "accepted",
                "stopped",
            )
            if important or time.monotonic() - last_spoken_at.get(item["id"], 0) > 120:
                last_spoken_at[item["id"]] = time.monotonic()
                speak_update(item["title"] + ". " + current[1])
        with database() as conn:
            last = conn.execute(
                "SELECT status,result FROM requests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if last and last[0] == "error":
            status.set("Could not update task: " + last[1][-200:])

    first_connect = STATE / "browser-connection-offered"

    def poll():
        refresh()
        setup = read_task_status(STATE / "install-progress.json")
        start_auto_models(setup)
        if (
            not cloud_status()["connected"]
            and not first_connect.exists()
            and (not setup or setup.get("status") == "complete")
        ):
            first_connect.parent.mkdir(exist_ok=True)
            first_connect.write_text(
                "The browser sign-in was opened once on first run.", encoding="utf-8"
            )
            window.after(900, connect)
        window.after(5000, poll)

    voice_poll()
    poll()
    window.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    monitor() if args.monitor else gui()
