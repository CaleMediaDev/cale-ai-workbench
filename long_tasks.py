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
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "LongTasks"
BOARD = "long-tasks"
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PS = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")


def environment():
    env = os.environ.copy()
    env.update(HERMES_HOME=str(ROOT / "data"), PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               TEMP=str(ROOT / "Temp"), TMP=str(ROOT / "Temp"),
               JAVA_HOME=str(ROOT / "Toolbox/jdk-21.0.13+9"),
               GHIDRA_INSTALL_DIR=str(ROOT / "Toolbox/ghidra_12.1.4_PUBLIC"))
    paths = [ROOT / "Toolbox/node-v26.13.0-win-x64", ROOT / "Toolbox/node_modules/.bin",
             ROOT / "Toolbox/python/Scripts", ROOT / "bin"]
    env["PATH"] = os.pathsep.join(map(str, paths)) + os.pathsep + env.get("PATH", "")
    env["NODE_PATH"] = str(ROOT / 'Toolbox/node_modules') + os.pathsep + str(Path(env["APPDATA"]) / "npm/node_modules/@playwright/mcp/node_modules")
    from cloud_ai import credentials
    env["LOCAL_AI_ROUTER_KEY"] = credentials()["router_token"]
    return env


def hermes(*args, parse=False, timeout=100):
    result = subprocess.run([str(ROOT / "data/bin/hermes.exe"), *map(str, args)],
                            env=environment(), cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout, creationflags=HIDE)
    if result.returncode:
        raise RuntimeError((result.stderr + "\n" + result.stdout)[-5000:])
    return json.loads(result.stdout) if parse else result.stdout


def board(*args, **kwargs):
    return hermes("kanban", "--board", BOARD, *args, **kwargs)


@contextlib.contextmanager
def database():
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
        conn.execute("UPDATE projects SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                     [*values.values(), pid])


def projects():
    with database() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY created")]


def request(pid, action, note=""):
    with database() as conn:
        conn.execute("INSERT INTO requests(project,action,note) VALUES(?,?,?)", (pid, action, note))


def add_project(title, folder, brief):
    title, brief = title.strip(), brief.strip()
    if not brief:
        raise ValueError("Tell the assistant what you would like done.")
    if not title:
        title = brief.splitlines()[0].strip()[:60] or "My task"
    pid = uuid.uuid4().hex[:12]
    folder = Path(folder).resolve() if folder.strip() else ROOT / "Projects" / pid
    if folder == ROOT or folder in ROOT.parents or folder.is_relative_to(ROOT / "app") or folder.is_relative_to(ROOT / "data"):
        raise ValueError("Choose a project folder, not the AI installation or a parent folder.")
    for item in projects():
        other = Path(item["folder"])
        if folder == other or folder.is_relative_to(other) or other.is_relative_to(folder):
            raise ValueError("That folder overlaps another queued project. Use its Add instructions button.")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".local-ai").mkdir(exist_ok=True)
    rules = folder / "AGENTS.md"
    if not rules.exists():
        rules.write_text("# Local project\n\nPreserve existing data and tests. Keep .local-ai/PLAN.md and PROGRESS.md current. "
                         "Work toward the user's full request in verified milestones. Run real behavior tests after the last edit. "
                         "Use the configured AI route only: local Ollama or user-connected free cloud. Do not configure paid AI or invoke Codex. Do not publish or send external messages without authorization.\n",
                         encoding="utf-8")
    with database() as conn:
        conn.execute("INSERT INTO projects(id,title,folder,brief,created,updated) VALUES(?,?,?,?,?,?)",
                     (pid, title, str(folder), brief, time.time(), time.time()))
    return pid


def project_prompt(p, extra=""):
    return f"""USER PROJECT: {p['title']}
PROJECT FOLDER: {p['folder']}
REQUEST AND ACCEPTANCE CRITERIA:
{p['brief']}

This is a persistent local project. Work in small verified milestones until the
whole requested result works. A new worker must recover from the existing files,
PLAN.md, PROGRESS.md, and the board's comments, never start over blindly.
First inspect existing code/instructions and write .local-ai/PLAN.md with concrete
milestones and acceptance checks. Update .local-ai/PROGRESS.md after each milestone:
completed work, exact checks/results, current problem, next concrete step. Keep
those files concise and current so recovery does not depend on conversation memory.
Do not stop merely because one response or context is full. Continue the goal.
Never change the request to fit incomplete work. Do not weaken/delete existing tests.
Use the installed local tools; see {ROOT / 'TOOLBOX.txt'} for specialist tools.
Use only the configured model route: local Ollama, or user-connected free cloud
with automatic local fallback. Never add AI providers, paid models, Codex, or delegation.
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


def create_card(p, extra=""):
    # Stable key makes a crash between native create and our DB update recoverable.
    key = f"local-long-{p['id']}-{p['generation']}"
    body = STATE / f"{key}.txt"
    if p["phase"] == "review":
        extra += "\nREVIEW PASS: Independently inspect EVERY requirement, test the app, fix omissions and add regression tests. Do not trust prior claims. Then request review."
    body.write_text(project_prompt(p, extra + "\nPrevious supervisor result:\n" + p.get("detail", "")), encoding="utf-8")
    task = board("create", p["title"] + (" — review" if p["phase"] == "review" else ""),
                 "--body-file", body, "--workspace", "dir:" + p["folder"],
                 "--assignee", "default", "--goal", "--goal-max-turns", "1000",
                 "--max-runtime", "30d", "--max-retries", "3",
                 "--idempotency-key", key, "--completion-contract", "local-only", "--json", parse=True)
    update(p["id"], card=task["id"], status="working", detail="Saved on the persistent Hermes board.")
    return task


SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".local-ai", ".next", "dist", "build", "coverage", ".pytest_cache"}


def fingerprint(folder):
    digest = hashlib.sha256()
    count = 0
    for base, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink():
                continue
            stat = path.stat()
            digest.update(str(path.relative_to(folder)).encode())
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
            count += 1
            if count > 100000:
                raise RuntimeError("Project too large for the completion freshness check.")
    # The actual verification entry point is always included.
    verify = Path(folder) / ".local-ai/verify.ps1"
    if verify.exists():
        digest.update(verify.read_bytes())
    return digest.hexdigest()


def verify_project(p, timeout=1200):
    folder = Path(p["folder"])
    script = folder / ".local-ai/verify.ps1"
    if not script.is_file() or not script.read_text(encoding="utf-8-sig").strip():
        return False, "Missing .local-ai/verify.ps1. Create and run real acceptance tests."
    log = STATE / f"{p['id']}-verification.log"
    with log.open("w", encoding="utf-8") as out:
        out.write(f"Independent verification: {time.ctime()}\nProject: {folder}\n")
        out.flush()
        # Start suspended, assign a Windows Job, then resume. If the supervisor
        # crashes, Windows closes the job and kills only this check's descendants.
        import ctypes
        from ctypes import wintypes
        import win32job
        job = win32job.CreateJobObject(None, 'Local\\LocalAIVerify-' + uuid.uuid4().hex)
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        process = subprocess.Popen([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                   cwd=folder, env=environment(), stdout=out, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=HIDE | 0x00000004)  # CREATE_SUSPENDED
        try:
            win32job.AssignProcessToJobObject(job, int(process._handle))
            resume = ctypes.WinDLL('ntdll').NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            resume.restype = wintypes.LONG
            if resume(int(process._handle)) < 0:
                raise RuntimeError('Could not resume verification process')
        except BaseException:
            process.kill(); process.wait(); job.Close()
            raise
        deadline = time.monotonic() + timeout
        try:
            while True:
                (STATE / "heartbeat.txt").write_text(time.ctime(), encoding="utf-8")
                with database() as conn:
                    pause = conn.execute("SELECT 1 FROM requests WHERE project=? AND action='pause' AND status='pending' LIMIT 1",
                                         (p['id'],)).fetchone()
                if pause:
                    win32job.TerminateJobObject(job, 125)
                    process.wait(timeout=15)
                    code = 125
                    break
                try:
                    code = process.wait(timeout=min(10, max(0.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline: raise
        except subprocess.TimeoutExpired:
            # Only our verification process tree, never the shared model/gateway.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=HIDE)
            process.wait(timeout=15)
            code = 124
        finally:
            job.Close()
        out.write(f"\nVerification exit code: {code}\n")
    return code == 0, log.read_text(encoding="utf-8", errors="replace")[-12000:]


def handle_handoff(p, task):
    update(p["id"], status="checking", detail="Running the project's verification independently (up to 20 minutes).")
    passed, evidence = verify_project(p)
    with database() as conn:
        pending = conn.execute("SELECT 1 FROM requests WHERE project=? AND status='pending' LIMIT 1", (p['id'],)).fetchone()
    if pending:
        process_requests()
        current = next(x for x in projects() if x['id'] == p['id'])
        if current['status'] != 'checking':
            return
    if not passed:
        failures = p["failures"] + 1
        update(p["id"], failures=failures, detail=evidence, status="needs_input" if failures >= 3 else "working")
        if failures >= 3:
            return
        note = "Independent verification failed. Fix the underlying behavior and rerun tests.\n" + evidence
        if task["status"] == "review":
            board("comment", task["id"], note)
            board("reopen-review", task["id"], "--reason", "Independent verification failed")
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
        update(p["id"], card="", phase="review", generation=p["generation"] + 1, detail=evidence)
        p = next(x for x in projects() if x["id"] == p["id"])
        create_card(p, "REVIEW PASS: Independently inspect the implementation against EVERY original requirement. "
                    "Read existing tests, exercise the app, repair omissions and add regression tests. "
                    "Do not trust the previous worker's claims. Then request review with evidence.")
    else:
        update(p["id"], status="ready_for_review", detail=evidence, fingerprint=fingerprint(Path(p["folder"])))


def process_requests():
    with database() as conn:
        pending = [dict(x) for x in conn.execute("SELECT * FROM requests WHERE status='pending' ORDER BY id")]
    for req in pending:
        try:
            p = next(x for x in projects() if x["id"] == req["project"])
            action, note = req["action"], req["note"]
            snapshot = board("show", p["card"], "--json", parse=True) if p["card"] else {}
            task = snapshot.get("task")
            if action == "pause":
                if task and task["status"] in ("running", "ready"):
                    board("block", p["card"], "Paused by user", "--kind", "needs_input")
                update(p["id"], status="paused", detail="Paused. A retiring worker may take up to two minutes to exit.")
            elif action in ("resume", "note"):
                ended = max((r.get("ended_at") or 0 for r in snapshot.get("runs", [])), default=0)
                if p["status"] == "paused" and ended and time.time() - ended < 180:
                    raise ValueError("The paused worker is retiring. Wait up to three minutes before resuming to avoid overlapping edits.")
                if note:
                    update(p["id"], brief=p["brief"] + "\n\nUSER FOLLOW-UP:\n" + note)
                    if task:
                        board("comment", p["card"], "USER FOLLOW-UP: " + note)
                if task and task["status"] in ("blocked", "scheduled"):
                    board("unblock", p["card"], "--reason", note or "User requested resume")
                elif task and task["status"] == "review":
                    board("reopen-review", p["card"], "--reason", note or "Continue and recheck the project")
                elif task and task["status"] in ("done", "archived", "triage"):
                    update(p["id"], card="", generation=p["generation"] + 1)
                update(p["id"], status="working", failures=0, fingerprint="", phase="build")
            elif action == "accept":
                if p["status"] != "ready_for_review" or fingerprint(Path(p["folder"])) != p["fingerprint"]:
                    raise ValueError("Checks are missing or stale. Resume and verify before accepting.")
                if task and task["status"] in ("review", "done"):
                    board("comment", p["card"], "User accepted the independently checked project.\n" + p["detail"][-8000:])
                    board("archive", p["card"])
                update(p["id"], status="accepted", detail="Accepted by user. Project files and history retained.")
            else:
                raise ValueError("Unknown action")
            result, status = "Applied", "done"
        except Exception as exc:
            result, status = str(exc), "error"
        with database() as conn:
            conn.execute("UPDATE requests SET status=?,result=? WHERE id=?", (status, result, req["id"]))


def tick():
    process_requests()
    for p in projects():
        if p["status"] in ("accepted", "paused", "needs_input"):
            continue
        if p["status"] == "ready_for_review":
            if fingerprint(Path(p["folder"])) != p["fingerprint"]:
                update(p["id"], status="needs_input", detail="Files changed after verification. Resume to check them again.")
            continue
        if not p["card"]:
            create_card(p)
            continue
        snapshot = board("show", p["card"], "--json", parse=True)
        task = snapshot["task"]
        if task["status"] in ("review", "done"):
            ended = max((r.get("ended_at") or 0 for r in snapshot.get("runs", [])), default=0)
            if ended and time.time() - ended < 150:
                update(p["id"], status="working", detail="Worker handed off. Letting it finish and release files before independent checks.")
                continue
            handle_handoff(p, task)
        elif task["status"] in ("blocked", "triage", "archived", "scheduled"):
            comments = snapshot.get("comments", [])
            update(p["id"], status="needs_input", detail=json.dumps({"task": task, "recent_comments": comments[-3:]}, indent=2)[-12000:])
        else:
            update(p["id"], status="working", detail=f"Hermes: {task['status']}. Card {p['card']}. Progress is saved in the project folder.")


def monitor():
    import msvcrt
    STATE.mkdir(exist_ok=True)
    lock = (STATE / "monitor.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"1"); lock.flush()
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
            active = any(p["status"] not in ("accepted", "paused", "needs_input", "ready_for_review") for p in projects())
            if active and time.monotonic() - last_start > 120:
                facts_file = max((ROOT / "data/installs").glob("*/facts.json"), key=lambda f: f.stat().st_mtime)
                facts = json.loads(facts_file.read_text())
                py = str(Path(facts["packages"]["venv"]["environment"]) / "Scripts/python.exe")
                settings = subprocess.run([py, str(ROOT / "read-settings.py")], env=environment(), check=True,
                                          capture_output=True, text=True, creationflags=HIDE)
                ensure_ollama(json.loads(settings.stdout)["model"])
                from cloud_ai import ensure_router
                ensure_router()
                hermes("gateway", "start", timeout=120)
                last_start = time.monotonic()
            tick()
            (STATE / "heartbeat.txt").write_text(time.ctime(), encoding="utf-8")
            (STATE / "service-status.txt").write_text("Supervisor online. " + time.ctime(), encoding="utf-8")
        except Exception:
            (STATE / "service-status.txt").write_text("Supervisor is retrying after an error. See monitor-errors.log. " + time.ctime(), encoding="utf-8")
            with (STATE / "monitor-errors.log").open("a", encoding="utf-8") as log:
                log.write(time.ctime() + "\n" + traceback.format_exc() + "\n")
        time.sleep(15)


def ensure_ollama(model):
    from urllib.request import urlopen, Request
    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
            json.load(response)
    except OSError:
        exe = Path(os.environ["LOCALAPPDATA"]) / "Programs/Ollama/ollama.exe"
        if not exe.exists(): exe = ROOT / 'Toolbox/ollama/ollama.exe'
        subprocess.Popen([str(exe), "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=HIDE)
        for attempt in range(15):
            time.sleep(1)
            try:
                with urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
                    json.load(response)
                break
            except OSError:
                if attempt == 14: raise RuntimeError("Ollama did not start")
    request = Request("http://127.0.0.1:11434/api/show", data=json.dumps({"model": model}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        info = json.load(response)
    if info.get("remote_host") or info.get("remote_model"):
        raise RuntimeError("Long tasks require local weights, not a remote Ollama model.")


def start_monitor():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw), str(Path(__file__).resolve()), "--monitor"],
                     cwd=ROOT, env=environment(), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDE)


def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    import queue
    import threading
    from cloud_ai import (connect_account, disconnect, save_openrouter_key, status as cloud_status, ensure_router,
                          select_profile, local_profile)
    from model_library import CATALOG, install as install_model
    start_monitor()
    window = tk.Tk()
    window.title("Cale AI Workbench")
    window.geometry("1120x790")
    window.minsize(900, 650)
    bg, card, field = "#0b1020", "#151d31", "#1c2940"
    text, muted, accent = "#edf3ff", "#9aaac6", "#6e82ff"
    window.configure(background=bg)
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure("TFrame", background=bg)
    style.configure("Card.TFrame", background=card)
    style.configure("TLabel", background=bg, foreground=text, font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=bg, foreground=muted)
    style.configure("Card.TLabel", background=card, foreground=text)
    style.configure("CardMuted.TLabel", background=card, foreground=muted)
    style.configure("TButton", background="#293955", foreground=text, borderwidth=0,
                    padding=(14, 9), font=("Segoe UI", 10, "bold"))
    style.map("TButton", background=[("active", "#3a4c6d"), ("disabled", "#1b263a")],
              foreground=[("disabled", "#73819a")])
    style.configure("Accent.TButton", background=accent, foreground="#ffffff", padding=(17, 10))
    style.map("Accent.TButton", background=[("active", "#8797ff"), ("disabled", "#35426e")])
    style.configure("TEntry", fieldbackground=field, foreground=text, bordercolor="#344663", padding=7)
    style.configure("TCombobox", fieldbackground=field, foreground=text, background=field,
                    arrowcolor=text, bordercolor="#344663", padding=7)
    style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", text)])
    style.configure("Treeview", background=card, fieldbackground=card, foreground=text,
                    rowheight=36, borderwidth=0, font=("Segoe UI", 10))
    style.configure("Treeview.Heading", background="#202d46", foreground="#c7d5ee",
                    borderwidth=0, padding=(10, 10), font=("Segoe UI", 10, "bold"))
    style.map("Treeview", background=[("selected", "#334a85")], foreground=[("selected", "#ffffff")])
    style.configure("TNotebook", background=bg, borderwidth=0)
    style.configure("TNotebook.Tab", background="#172137", foreground=muted,
                    borderwidth=0, padding=(22, 12), font=("Segoe UI", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", "#2b3c63"), ("active", "#22304e")],
              foreground=[("selected", "#ffffff"), ("active", "#ffffff")])

    frame = ttk.Frame(window, padding=(24, 18)); frame.pack(fill="both", expand=True)
    header = ttk.Frame(frame); header.pack(fill="x", pady=(0, 16))
    tk.Label(header, text="◆", bg=accent, fg="white", font=("Segoe UI", 21, "bold"),
             width=3, height=1).pack(side="left", padx=(0, 14))
    brand = ttk.Frame(header); brand.pack(side="left")
    ttk.Label(brand, text="CALE  /  AI", font=("Segoe UI", 20, "bold")).pack(anchor="w")
    ttk.Label(brand, text="A helpful assistant that keeps working and remembers your progress.",
              style="Muted.TLabel").pack(anchor="w")
    ttk.Label(header, text="WORKS ON YOUR PC  •  SAVES PROGRESS", style="Muted.TLabel",
              font=("Segoe UI", 9, "bold")).pack(side="right")
    hero = ttk.Frame(frame, style="Card.TFrame", padding=(20, 16)); hero.pack(fill="x", pady=(0, 16))
    ttk.Label(hero, text="What would you like help with?", style="Card.TLabel",
              font=("Segoe UI", 17, "bold")).pack(anchor="w")
    ttk.Label(hero, text="Describe a task in your own words. You can close this window and check back later.",
              style="CardMuted.TLabel").pack(anchor="w", pady=(4, 0))
    tabs = ttk.Notebook(frame); tabs.pack(fill="both", expand=True)
    project_panel = ttk.Frame(tabs, padding=(16, 16))
    tools_panel = ttk.Frame(tabs, padding=(16, 16))
    cloud_panel = ttk.Frame(tabs, padding=(16, 16))
    tabs.add(project_panel, text="  My tasks  ")
    tabs.add(tools_panel, text="  Extra tools  ")
    tabs.add(cloud_panel, text="  My account  ")
    ttk.Label(cloud_panel, text="Optional free online AI", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(cloud_panel, text="Click Connect, sign in in your browser, and return here. The app handles the key for you. Your computer's AI still works without an account.",
              style="Muted.TLabel", wraplength=900).pack(anchor="w", pady=(4, 18))
    cloud_row = ttk.Frame(cloud_panel); cloud_row.pack(fill="x", pady=(0, 12))
    cloud_text = tk.StringVar(value="Local mode. Connect an account to enable free cloud automatically.")
    cloud_events = queue.Queue()
    connect_controls = []
    def connect():
        for control in connect_controls: control.configure(state="disabled")
        cloud_text.set("Finish sign-in in your browser. Local AI keeps working while you connect.")
        def worker():
            try:
                ensure_router(); connect_account(); cloud_events.put("Connected. Free cloud is preferred; local Qwen covers limits and failures.")
            except Exception as exc: cloud_events.put(str(exc))
        threading.Thread(target=worker, daemon=True).start()
    def disconnect_cloud():
        disconnect(); cloud_text.set("Disconnected. Future model requests stay on this PC.")
    connect_button = ttk.Button(cloud_row, text="Connect my account", command=connect, style="Accent.TButton")
    connect_button.pack(side="left")
    connect_controls.append(connect_button)
    def paste_openrouter():
        key=simpledialog.askstring('OpenRouter API key','Paste an OpenRouter key. Only zero-price providers will be used.',
                                   parent=window,show='*')
        if not key: return
        try: save_openrouter_key(key.strip()); cloud_text.set('OpenRouter key saved securely. Free cloud routing is ready.')
        except Exception as exc: messagebox.showerror('OpenRouter key',str(exc),parent=window)
    ttk.Button(cloud_row, text="Use only my computer", command=disconnect_cloud).pack(side="left", padx=8)
    ttk.Button(cloud_panel,text='Advanced: paste an API key',command=paste_openrouter).pack(anchor='w',pady=(8, 12))
    ttk.Label(cloud_panel, textvariable=cloud_text, wraplength=900,
              font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 12))
    ttk.Label(cloud_panel, text="Connecting sends project prompts and tool results to OpenRouter and its free model providers. Paid routes are disabled.",
              style="Muted.TLabel", wraplength=900).pack(anchor="w")
    ttk.Label(tools_panel, text="Extra tools and models", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(tools_panel, text="You can leave these settings alone. Your assistant is ready with the model chosen during installation.",
              style="Muted.TLabel").pack(anchor="w", pady=(4, 18))
    model_row = ttk.Frame(tools_panel); model_row.pack(fill="x", pady=(0, 10))
    ttk.Label(model_row, text="Model:").pack(side="left", padx=(0, 8))
    model_choice = tk.StringVar(value=CATALOG[local_profile()]['label'])
    model_picker = ttk.Combobox(model_row, textvariable=model_choice, state="readonly", width=40,
        values=tuple(item['label'] for item in CATALOG.values()))
    model_picker.pack(side="left")
    def change_model(_):
        profile = next(key for key, item in CATALOG.items() if item['label'] == model_choice.get())
        try:
            select_profile(profile)
            model_text.set("This model is ready for the next task.")
        except Exception:
            if profile == 'main':
                messagebox.showinfo("Main model", "The model chosen during installation is still selected.", parent=window)
            else:
                download_model(profile)
    model_picker.bind('<<ComboboxSelected>>', change_model)
    model_events = queue.Queue()
    model_text = tk.StringVar(value="Optional downloads: GPT-OSS ~13 GB / 32 GB RAM; Llama ~43 / 80; GLM ~67 / 112; Qwen ~52 / 96. One model runs at a time.")
    def download_model(profile=None):
        if profile is None:
            profile = next(key for key, item in CATALOG.items() if item['label'] == model_choice.get())
        if profile == 'main': return
        download_button.configure(state='disabled')
        model_text.set("Downloading the selected model now. You can keep using the app while it downloads.")
        def worker():
            try:
                install_model(profile, lambda message: model_events.put((False, message)))
                select_profile(profile); model_events.put((True, 'Model installed and selected for the next request.'))
            except Exception as exc: model_events.put((True, str(exc)))
        threading.Thread(target=worker, daemon=True).start()
    download_button = ttk.Button(model_row, text="Retry download", command=download_model)
    download_button.pack(side='left', padx=8)
    ttk.Label(tools_panel, textvariable=model_text, style="Muted.TLabel", wraplength=900).pack(anchor='w', pady=(0, 24))
    ttk.Label(tools_panel, text="Create and understand media", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(tools_panel, text="Generate images, speak and transcribe, inspect video, and opt in to camera access.",
              style="Muted.TLabel").pack(anchor="w", pady=(4, 16))
    media_row = ttk.Frame(tools_panel); media_row.pack(fill='x', pady=(0, 12))
    media_text = tk.StringVar(value='Media tools: local images, vision, video samples, speech, and small-model training.')
    media_events = queue.Queue()
    consent_file = ROOT / 'Media/camera-consent.json'
    def camera_on():
        consent_file.parent.mkdir(exist_ok=True)
        consent_file.write_text(json.dumps({'until': time.time()+3600}), encoding='utf-8')
        media_text.set('Camera enabled for one hour. Agents may capture frames while it is on. Click Camera off to stop.')
    def camera_off():
        consent_file.parent.mkdir(exist_ok=True)
        consent_file.write_text(json.dumps({'until': 0}), encoding='utf-8')
        media_text.set('Camera off.')
    def media_action(action):
        media_text.set('Recording for 10 seconds...' if action == 'record_audio' else 'Preparing offline speech...')
        def worker():
            try:
                python = ROOT / 'Media/python/Scripts/python.exe'
                payload = {'action': action, 'seconds': 10} if action == 'record_audio' else {
                    'action': action, 'text': (STATE / 'service-status.txt').read_text(encoding='utf-8')[:1000],
                    'output_path': str(ROOT / 'Media' / ('status-' + uuid.uuid4().hex + '.wav')), 'play': True}
                done = subprocess.run([str(python), str(ROOT / 'media_worker.py')], input=json.dumps(payload),
                    text=True, capture_output=True, timeout=120, cwd=ROOT, creationflags=HIDE)
                result = json.loads(done.stdout.splitlines()[-1])
                if done.returncode: raise RuntimeError(result.get('error', done.stderr[-300:]))
                media_events.put(('dictation', result.get('text', '')) if action == 'record_audio' else ('status', 'Status spoken.'))
            except Exception as exc: media_events.put(('status', str(exc)))
        threading.Thread(target=worker, daemon=True).start()
    ttk.Button(media_row, text='Dictate 10 seconds', command=lambda: media_action('record_audio')).pack(side='left')
    ttk.Button(media_row, text='Speak status', command=lambda: media_action('speak')).pack(side='left', padx=8)
    ttk.Button(media_row, text='Camera on (1 hour)', command=camera_on).pack(side='left', padx=8)
    ttk.Button(media_row, text='Camera off', command=camera_off).pack(side='left')
    ttk.Label(tools_panel, textvariable=media_text, style="Muted.TLabel", wraplength=900).pack(anchor='w', pady=(0, 8))
    ttk.Label(project_panel, text="My tasks", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(project_panel, text="Start something new or check what your assistant has finished.",
              style="Muted.TLabel").pack(anchor="w", pady=(4, 14))
    connection_card = ttk.Frame(project_panel, style="Card.TFrame", padding=(16, 12))
    connection_card.pack(fill="x", pady=(0, 14))
    connection_copy = ttk.Frame(connection_card, style="Card.TFrame")
    connection_copy.pack(side="left", fill="x", expand=True)
    ttk.Label(connection_copy, text="OPTIONAL · CONNECT YOUR OWN ACCOUNT", style="Card.TLabel",
              font=("Segoe UI", 10, "bold")).pack(anchor="w")
    quick_text = tk.StringVar(value="One click opens a browser sign-in. You never need to copy a key.")
    ttk.Label(connection_copy, textvariable=quick_text, style="CardMuted.TLabel",
              wraplength=620).pack(anchor="w", pady=(3, 0))
    quick_connect = ttk.Button(connection_card, text="Connect account →", command=connect,
                               style="Accent.TButton")
    quick_connect.pack(side="right", padx=(16, 0))
    connect_controls.append(quick_connect)
    tree = ttk.Treeview(project_panel, columns=("name", "status", "phase"), show="headings", height=4)
    for key, label, width in (("name", "Task", 500), ("status", "Progress", 230), ("phase", "Step", 120)):
        tree.heading(key, text=label); tree.column(key, width=width)
    tree.pack(fill="x")
    detail = tk.Text(project_panel, wrap="word", height=6, font=("Consolas", 10),
                     background=card, foreground=text, insertbackground=text,
                     selectbackground="#334a85", relief="flat", padx=16, pady=14)
    detail.pack(fill="both", expand=True, pady=(12, 14))
    status = tk.StringVar(value="Closing this window leaves queued projects running in the background.")

    def selected():
        ids = tree.selection()
        return next((p for p in projects() if ids and p["id"] == ids[0]), None)

    def refresh_detail(*_):
        p = selected()
        if not p:
            return
        content = f"{p['title']}\nFolder: {p['folder']}\n\n{p['brief']}\n\n{p['detail']}\n"
        progress = Path(p["folder"]) / ".local-ai/PROGRESS.md"
        if progress.is_file():
            content += "\nSAVED PROGRESS\n" + progress.read_text(encoding="utf-8", errors="replace")[-12000:]
        detail.delete("1.0", "end"); detail.insert("1.0", content)
    tree.bind("<<TreeviewSelect>>", refresh_detail)

    def new():
        dialog = tk.Toplevel(window); dialog.title("New task"); dialog.geometry("720x520")
        dialog.configure(background=bg)
        pane = ttk.Frame(dialog, padding=18); pane.pack(fill="both", expand=True)
        folder = tk.StringVar()
        ttk.Label(pane, text="What would you like done?", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(pane, text="Write naturally. For example: Make a simple website for my bakery, with hours, photos, and a contact form.",
                  style="Muted.TLabel", wraplength=650).pack(anchor="w", pady=(6, 14))
        brief = tk.Text(pane, wrap="word", height=14, background=field, foreground=text,
                        insertbackground=text, selectbackground="#334a85", relief="flat", padx=12, pady=10)
        brief.pack(fill="both", expand=True)
        folder_label = tk.StringVar(value="A new folder will be made automatically for this task.")
        choice_row = ttk.Frame(pane); choice_row.pack(fill="x", pady=(14, 0))
        ttk.Label(choice_row, textvariable=folder_label, style="Muted.TLabel", wraplength=500).pack(side="left", fill="x", expand=True)
        def browse():
            choice = filedialog.askdirectory(parent=dialog)
            if choice:
                folder.set(choice)
                folder_label.set("Working in: " + choice)
        ttk.Button(choice_row, text="Use a folder I already have", command=browse).pack(side="right")
        ttk.Label(pane, text="Your computer needs to stay on while it works. You can close this window and come back later.",
                  style="Muted.TLabel", wraplength=650).pack(anchor="w", pady=(12, 8))
        def save():
            try:
                pid = add_project("", folder.get(), brief.get("1.0", "end"))
                dialog.destroy(); status.set("Task saved. Your assistant will start shortly."); refresh()
                tree.selection_set(pid)
            except Exception as exc: messagebox.showerror("Could not start task", str(exc), parent=dialog)
        ttk.Button(pane, text="Start my task", command=save, style="Accent.TButton").pack(anchor="e")
        brief.focus_set()

    def action(kind):
        p = selected()
        if not p: return
        note = ""
        if kind == "note":
            note = simpledialog.askstring("Instructions", "Add requirements, answer a blocker, or describe changes:", parent=window)
            if not note: return
        request(p["id"], kind, note)
        status.set("Request saved. It will be applied after the current supervisor check finishes.")

    buttons = ttk.Frame(project_panel); buttons.pack(fill="x", before=tree, pady=(0, 12))
    ttk.Button(buttons, text="+ New task", command=new, style="Accent.TButton").pack(side="left", padx=(0, 8))
    for text, kind in (("Pause", "pause"), ("Continue", "resume"), ("Tell it more", "note"), ("Looks good", "accept")):
        ttk.Button(buttons, text=text, command=lambda k=kind: action(k)).pack(side="left", padx=3)
    ttk.Button(buttons, text="Open files", command=lambda: os.startfile(selected()["folder"]) if selected() else None).pack(side="left", padx=3)
    ttk.Button(tools_panel, text="Toolbox guide", command=lambda: os.startfile(str(ROOT / "TOOLBOX.txt"))).pack(anchor="w", pady=(22, 0))
    ttk.Button(tools_panel, text="Technical logs", command=lambda: os.startfile(str(STATE))).pack(anchor="w", pady=(8, 0))
    ttk.Label(project_panel, textvariable=status, style="Muted.TLabel", wraplength=960).pack(
        before=tree, anchor="w", pady=(0, 6))
    empty_hint = ttk.Label(project_panel, text="No tasks yet. Start with the blue New task button.",
                           style="Muted.TLabel")
    empty_hint.pack(before=tree, anchor="w", pady=(0, 6))

    def refresh():
        try:
            while True:
                kind, message = media_events.get_nowait()
                if kind == 'dictation':
                    window.clipboard_clear(); window.clipboard_append(message)
                    media_text.set('Dictation copied to clipboard: ' + (message[:180] or '(nothing heard)'))
                else: media_text.set(message)
        except queue.Empty: pass
        try:
            while True:
                finished, message = model_events.get_nowait(); model_text.set(message)
                if finished: download_button.configure(state='normal')
        except queue.Empty: pass
        try:
            while True:
                cloud_text.set(cloud_events.get_nowait())
                for control in connect_controls: control.configure(state="normal")
        except queue.Empty: pass
        if str(connect_button.cget("state")) != "disabled":
            cloud = cloud_status()
            if cloud['connected']:
                cloud_text.set("Connected • Free cloud + local fallback • Last route: " + cloud.get('backend', 'pending') + " — " + cloud.get('reason', ''))
                quick_text.set("Connected. Free online help is available when quota permits; your computer can take over.")
                quick_connect.configure(text="Reconnect →")
            elif not cloud_text.get().startswith(('Sign-in', 'The browser')):
                cloud_text.set("Local only • " + cloud.get('reason', 'Connect your account to enable free cloud.'))
                quick_text.set("One click opens a browser sign-in. You never need to copy a key.")
                quick_connect.configure(text="Connect account →")
        existing = set(tree.get_children())
        current_projects = projects()
        empty_hint.configure(text="" if current_projects else "No tasks yet. Start with the blue New task button.")
        for p in current_projects:
            friendly_status = {"queued": "Waiting to start", "working": "Working",
                               "ready_for_review": "Ready for you", "needs_input": "Needs your help",
                               "paused": "Paused", "complete": "Done"}
            values = (p["title"], friendly_status.get(p["status"], p["status"].replace("_", " ").title()),
                      "Checking" if p["phase"] == "review" else "Building")
            if p["id"] in existing: tree.item(p["id"], values=values)
            else: tree.insert("", "end", iid=p["id"], values=values)
        refresh_detail()
        with database() as conn:
            last = conn.execute("SELECT status,result FROM requests ORDER BY id DESC LIMIT 1").fetchone()
        if last and last[0] == 'error': status.set("Last request error: " + last[1][-300:])
        elif (STATE / 'service-status.txt').exists():
            status.set((STATE / 'service-status.txt').read_text(encoding='utf-8') +
                       '  Closing this window leaves work running. Use Pause to stop a project.')
    def poll():
        refresh(); window.after(5000, poll)
    poll()
    first_connect = STATE / 'browser-connection-offered'
    if not cloud_status()['connected'] and not first_connect.exists():
        first_connect.parent.mkdir(exist_ok=True)
        first_connect.write_text('The browser sign-in was opened once on first run.', encoding='utf-8')
        window.after(900, connect)
    window.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    monitor() if args.monitor else gui()
