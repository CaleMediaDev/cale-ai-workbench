"""Download verified workbench releases and apply source updates while idle.

Only the public CaleMediaDev release feed is trusted. User projects, task state,
models, runtime folders and account credentials are never part of an update.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
RELEASES = (
    "https://api.github.com/repos/CaleMediaDev/cale-ai-workbench/releases?per_page=10"
)
MAX_ZIP = 100 * 1024 * 1024


@contextmanager
def update_lock(timeout=600):
    """Allow one updater at a time, even when two desktop windows are open."""
    import msvcrt

    path = ROOT / "Updates/update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"1")
            lock.flush()
        deadline = time.monotonic() + timeout
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another Cale AI update is still running.")
                time.sleep(2)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def current_version():
    marker = (ROOT / ".cale-ai-install").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", marker)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def release_candidate(releases, installed):
    """Select the newest three-part release with a published SHA256 digest."""
    choices = []
    for release in releases:
        if release.get("draft"):
            continue
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", release.get("tag_name", ""))
        if not match:
            continue
        version = tuple(map(int, match.groups()))
        if version <= installed:
            continue
        expected_name = "Cale-AI-Workbench-" + ".".join(map(str, version)) + ".zip"
        for asset in release.get("assets", []):
            digest = asset.get("digest", "")
            if (
                asset.get("name") == expected_name
                and re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest)
                and 0 < int(asset.get("size", 0)) <= MAX_ZIP
                and asset.get("browser_download_url", "").startswith(
                    "https://github.com/CaleMediaDev/cale-ai-workbench/releases/download/"
                )
            ):
                choices.append((version, asset))
    return max(choices, default=None, key=lambda choice: choice[0])


def fetch_json(url):
    # GitHub's release list can be cached for a newly published version. Each
    # check needs a fresh URL so an idle app can see the newest signed asset.
    separator = "&" if "?" in url else "?"
    fresh_url = f"{url}{separator}checked_at={time.time_ns()}"
    with urlopen(
        Request(fresh_url, headers={"User-Agent": "Cale-AI-Workbench",
                                    "Cache-Control": "no-cache"}), timeout=15
    ) as response:
        return json.load(response)


def validated_manifest(archive):
    """Accept only flat relative release paths declared by its source manifest."""
    names = set(archive.namelist())
    if "install-files.json" not in names:
        raise ValueError("Release has no install manifest.")
    manifest = json.loads(archive.read("install-files.json"))
    if not isinstance(manifest, list) or not manifest or len(manifest) > 250:
        raise ValueError("Invalid release manifest.")
    files = [*manifest, "install-files.json"]
    if len(set(files)) != len(files):
        raise ValueError("Duplicate release file.")
    protected = {
        "LongTasks",
        "Projects",
        "Cloud",
        "Media",
        "data",
        "Toolbox",
        "Updates",
        "Temp",
        "Downloads",
    }
    expanded = 0
    for name in files:
        if not isinstance(name, str):
            raise ValueError("Invalid release path.")
        pure = PurePosixPath(name)
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or ":" in name
            or name != pure.as_posix()
            or any(part in ("", ".", "..") for part in pure.parts)
            or pure.parts[0] in protected
            or name not in names
            or archive.getinfo(name).is_dir()
        ):
            raise ValueError("Unsafe or missing release file.")
        expanded += archive.getinfo(name).file_size
        if (
            archive.getinfo(name).file_size > 20 * 1024 * 1024
            or expanded > 80 * 1024 * 1024
        ):
            raise ValueError("Release contents are too large.")
    return files


def stage():
    """Download and verify a newer release; return its archive path or None."""
    choice = release_candidate(fetch_json(RELEASES), current_version())
    if not choice:
        return None
    version, asset = choice
    folder = ROOT / "Updates"
    folder.mkdir(exist_ok=True)
    destination = folder / asset["name"]
    digest = asset["digest"].split(":", 1)[1].lower()
    if (
        not destination.is_file()
        or hashlib.sha256(destination.read_bytes()).hexdigest() != digest
    ):
        temporary = destination.with_suffix(".partial")
        total = 0
        checksum = hashlib.sha256()
        with (
            urlopen(
                Request(
                    asset["browser_download_url"],
                    headers={"User-Agent": "Cale-AI-Workbench"},
                ),
                timeout=30,
            ) as response,
            temporary.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ZIP:
                    raise ValueError("Release archive is too large.")
                output.write(chunk)
                checksum.update(chunk)
        if checksum.hexdigest() != digest or total != int(asset["size"]):
            temporary.unlink(missing_ok=True)
            raise ValueError(
                "Release checksum does not match GitHub's published digest."
            )
        os.replace(temporary, destination)
    with ZipFile(destination) as archive:
        validated_manifest(archive)
    return destination, version


def retire_terminal_board_worker(process):
    """Retire only a managed worker tied to a board run that already ended.

    Hermes can leave the inner agent process alive after its board client has
    handed off. That orphan keeps every automatic update waiting forever. The
    session admission binds the process's session to one exact board run; no
    matching terminal run means the process is left alone.
    """
    state_db = ROOT / "data/state.db"
    board_db = ROOT / "data/kanban/boards/long-tasks/kanban.db"
    if not state_db.is_file() or not board_db.is_file():
        return False
    try:
        started = process.create_time()
        cwd = Path(process.cwd()).resolve()
        with closing(sqlite3.connect(state_db, timeout=0.2)) as state:
            sessions = state.execute(
                "SELECT id, cwd FROM sessions WHERE started_at BETWEEN ? AND ?",
                (started - 5, started + 5),
            ).fetchall()
            matching = [row[0] for row in sessions if row[1] and Path(row[1]).resolve() == cwd]
            if len(matching) != 1:
                return False
            admissions = state.execute(
                "SELECT payload_json FROM session_admissions "
                "WHERE target_session_id=? ORDER BY seq DESC LIMIT 1",
                (matching[0],),
            ).fetchone()
        if not admissions:
            return False
        payload = json.loads(admissions[0])
        if not isinstance(payload, dict):
            return False
        card = payload.get("kanban_card_v1") or {}
        if not isinstance(card, dict):
            return False
        if card.get("board") != "long-tasks":
            return False
        run_id, task_id = int(card["run_id"]), str(card["task_id"])
        with closing(sqlite3.connect(board_db, timeout=0.2)) as board:
            run = board.execute(
                "SELECT ended_at FROM task_runs WHERE id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
        if not run or not run[0] or time.time() - run[0] < 180:
            return False
        if process.cmdline()[1:] != ["-m", "agent.managed_worker"] or process.create_time() != started:
            return False
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            # A surviving process remains a blocker; never swap source under it.
            return False
        log = ROOT / "Updates/orphan-recovery.log"
        log.parent.mkdir(exist_ok=True)
        with log.open("a", encoding="utf-8") as output:
            output.write(f"{time.ctime()} retired managed worker {process.pid} for run {run_id}\n")
        return True
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return False


def active_tasks():
    """Source files may change only when no project worker is active.

    A pause or stop updates the project row before Hermes has necessarily
    retired its worker. Check the actual process too, or an update could swap
    the source underneath that still-running worker.
    """
    # A fresh install may have no user projects yet. Updating its source while
    # the installer is importing it would leave a mixed runtime on disk.
    try:
        marker = (ROOT / ".cale-ai-install").read_text(encoding="utf-8")
    except OSError:
        marker = ""
    if "setup pending" in marker:
        return True
    try:
        setup = json.loads((ROOT / "LongTasks/install-progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        setup = {}
    if setup.get("status") == "working":
        return True

    from long_tasks import projects

    if any(
        task["status"] in ("queued", "working", "checking") for task in projects()
    ):
        return True
    import psutil

    install = ROOT.resolve()
    for process in psutil.process_iter(["exe", "cmdline"]):
        try:
            command = process.info["cmdline"] or []
            executable = process.info["exe"]
            if (
                executable
                and "agent.managed_worker" in command
                and Path(executable).resolve().is_relative_to(install)
            ):
                if retire_terminal_board_worker(process):
                    continue
                return True
            if (executable and len(command) >= 4
                    and Path(executable).resolve().is_relative_to(install)
                    and Path(command[1]).resolve() == install / "native_agent.py"
                    and command[2] == "run"):
                return True
            # A music render may outlive its parent project row (or be started
            # directly by a tool). Keep its Python and media scripts in place
            # until that exact worker exits; stale job JSON alone never blocks.
            if (executable and len(command) >= 4
                    and Path(executable).resolve().is_relative_to(install)
                    and Path(command[1]).resolve() == install / "music_backend.py"
                    and command[2] == "--run-job"):
                return True
            if (executable and len(command) >= 4
                    and Path(executable).resolve().is_relative_to(install)
                    and Path(command[1]).resolve() == install / "media_worker.py"
                    and command[2] in {"--3d-job", "--3d-infer"}):
                return True
        except (OSError, psutil.Error):
            continue
    return False


def update_pause_path():
    return ROOT / "Updates/resume-after-update.json"


def pause_projects_for_update(version):
    """Checkpoint active projects before a verified update takes priority.

    The existing monitor applies pause requests. Only projects paused by this
    update are recorded for automatic continuation afterward.
    """
    from long_tasks import projects, request

    path = update_pause_path()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    tasks = saved.get("tasks", {}) if isinstance(saved.get("tasks"), dict) else {}
    active = [p for p in projects() if p["status"] in ("queued", "working", "checking")
              and p["id"] not in tasks]
    for project in active:
        tasks[project["id"]] = str(Path(project["folder"]).resolve())
    target_version = ".".join(map(str, version))
    if not active and saved.get("version") == target_version:
        return list(tasks)
    if not tasks:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({
        "version": target_version, "tasks": tasks,
        "created": saved.get("created", time.time()),
    }), encoding="utf-8")
    os.replace(temporary, path)
    for project in active:
        request(project["id"], "pause", "Pausing for a verified Workbench update")
    return list(tasks)


def retire_update_paused_workers():
    """Stop only managed workers belonging to projects paused for an update."""
    import psutil

    from long_tasks import projects

    try:
        saved = json.loads(update_pause_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    project_rows = projects()
    paused = {p["id"] for p in project_rows if p["status"] == "paused"}
    active_folders = {Path(p["folder"]).resolve() for p in project_rows
                      if p["status"] in ("queued", "working", "checking")}
    folders = {Path(folder).resolve() for pid, folder in saved.get("tasks", {}).items()
               if pid in paused} - active_folders
    stopped = 0
    install = ROOT.resolve()
    for process in psutil.process_iter(["exe", "cmdline"]):
        try:
            command = process.info["cmdline"] or []
            executable = process.info["exe"]
            native = (executable and len(command) >= 4
                      and Path(executable).resolve().is_relative_to(install)
                      and Path(command[1]).resolve() == install / "native_agent.py"
                      and command[2] == "run" and command[3] in paused)
            if native:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                stopped += 1
                continue
            if (not executable or command[1:] != ["-m", "agent.managed_worker"]
                    or not Path(executable).resolve().is_relative_to(install)
                    or Path(process.cwd()).resolve() not in folders):
                continue
            process.terminate()
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            stopped += 1
        except (OSError, psutil.Error):
            continue
    return stopped


def resume_projects_after_update(force=False):
    """Queue continuation after the retired board run has safely ended."""
    from long_tasks import board, database, projects, request

    path = update_pause_path()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not force and current_version() < tuple(map(int, saved.get("version", "999.999.999").split("."))):
        return 0
    remaining = dict(saved.get("tasks", {}))
    by_id = {p["id"]: p for p in projects()}
    resumed = 0
    for pid in tuple(remaining):
        project = by_id.get(pid)
        if not project or project["status"] != "paused":
            del remaining[pid]
            continue
        try:
            card = str(project.get("card") or "")
            if project.get("engine") == "native":
                import native_agent

                if native_agent.process_alive(native_agent.read_state(project)):
                    continue
                runs = []
            else:
                runs = (board("show", card, "--json", parse=True).get("runs", [])
                    if card and not card.startswith(("music-job:", "image-job:")) else [])
            if any(run.get("started_at") and not run.get("ended_at") for run in runs):
                continue
            ended = max((run.get("ended_at") or 0 for run in runs), default=0)
            if ended and time.time() - ended < 180:
                continue
            with database() as conn:
                pending = conn.execute(
                    "SELECT 1 FROM requests WHERE project=? AND action='resume' "
                    "AND note=? AND status='pending' LIMIT 1",
                    (pid, "Continuing after Workbench update"),
                ).fetchone()
            if pending:
                continue
            request(pid, "resume", "Continuing after Workbench update")
            resumed += 1
        except (OSError, ValueError, KeyError):
            continue
    if remaining:
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({**saved, "tasks": remaining}), encoding="utf-8")
        os.replace(temporary, path)
    else:
        path.unlink(missing_ok=True)
    return resumed


def allow_resume_after_failed_update():
    """A failed swap must not strand projects behind a newer version gate."""
    path = update_pause_path()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({
        **saved, "version": ".".join(map(str, current_version())),
    }), encoding="utf-8")
    os.replace(temporary, path)


def add_design_tools_to_config(root, backup):
    """Migrate only known local settings while preserving user preferences."""
    config = root / "data/config.yaml"
    if not config.is_file():
        return None
    original = config.read_text(encoding="utf-8")
    updated = original
    # Hermes now rejects agent windows below 64K. Older installations pinned
    # the main model to 32K even when its local weights support a larger window.
    model_block = updated.split("fallback_providers:", 1)[0]
    context = re.search(r"(?m)^  context_length: ([0-9]+)[ \t]*$", model_block)
    if context and int(context.group(1)) < 64000:
        updated = updated.replace(context.group(0), "  context_length: 65536", 1)
    # Keep short board-planning calls on the installed 9B vision/text model.
    # Touch only the app's prior default aliases; preserve custom user choices.
    for section in ("triage_specifier", "kanban_decomposer"):
        updated = re.sub(
            rf"(?m)(^  {section}:\r?\n(?:    [^\r\n]*\r?\n)*?    model: )"
            r"(qwen3-coder-local|cale-local)(?=\r?$)",
            r"\g<1>cale-vision",
            updated,
            count=1,
        )
    if "mcp-design" not in updated:
        anchor = "  - mcp-toolbox\n"
        if anchor not in updated:
            raise RuntimeError("Could not locate the tool list in the existing config.")
        updated = updated.replace(anchor, anchor + "  - mcp-design\n", 1)
    if "  design:\n" not in updated:
        anchor = "skills:\n"
        if anchor not in updated:
            raise RuntimeError("Could not locate the MCP section in the existing config.")
        block = (
            "  design:\n"
            f"    command: '{(root / 'Toolbox/python/Scripts/python.exe').as_posix()}'\n"
            "    args:\n"
            f"    - '{(root / 'design_tools.py').as_posix()}'\n"
            "    timeout: 120\n"
            "    sampling:\n"
            "      enabled: false\n"
        )
        updated = updated.replace(anchor, block + anchor, 1)
    if updated == original:
        return None
    previous = backup / "data/config.yaml"
    previous.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config, previous)
    config.write_text(updated, encoding="utf-8")
    return config, previous


def write_version_marker(root, version):
    """Commit the installed version only after its source files are in place."""
    marker = root / ".cale-ai-install"
    temporary = root / ".cale-ai-install.updating"
    try:
        temporary.write_text(
            "Cale AI Workbench " + ".".join(map(str, version)), encoding="utf-8"
        )
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def apply(archive_path, version):
    """Keep the old source recoverable and swap only files in the manifest."""
    if active_tasks():
        raise RuntimeError("Wait for current tasks to finish before updating.")
    # Install new app-owned Python dependencies before swapping source. A
    # source update must not expose tools that the installed runtimes lack.
    with ZipFile(archive_path) as archive:
        validated_manifest(archive)
        sync_new_media_requirements(archive)
        sync_new_toolbox_requirements(archive)
    backup = (
        ROOT
        / "Updates/backups"
        / (".".join(map(str, current_version())) + "-" + str(int(time.time())))
    )
    backup.mkdir(parents=True)
    replaced = []
    with ZipFile(archive_path) as archive:
        files = validated_manifest(archive)
        try:
            for name in files:
                target = ROOT / name
                target.parent.mkdir(parents=True, exist_ok=True)
                previous = backup / name
                if target.is_file():
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, previous)
                staged = target.with_name(target.name + ".updating")
                staged.write_bytes(archive.read(name))
                os.replace(staged, target)
                replaced.append((target, previous))
            migration = add_design_tools_to_config(ROOT, backup)
            if migration:
                replaced.append(migration)
            write_version_marker(ROOT, version)
        except Exception:
            for target, previous in reversed(replaced):
                if previous.is_file():
                    shutil.copy2(previous, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return backup


def sync_new_media_requirements(archive):
    """Install only added or changed media pins for an existing installation."""
    _sync_new_requirements(archive, "media-requirements.txt",
                           ROOT / "Media/python/Scripts/python.exe", "local media")


def sync_new_toolbox_requirements(archive):
    """Install added Toolbox pins before a release starts using them."""
    _sync_new_requirements(archive, "requirements-windows.txt",
                           ROOT / "Toolbox/python/Scripts/python.exe", "Toolbox")


def _sync_new_requirements(archive, filename, python, label):
    """Apply changed pins to one managed runtime, leaving user Python alone."""
    if filename not in archive.namelist():
        return
    if not python.is_file():
        return  # First-run setup prepares a missing managed runtime.

    def pins(text):
        return [line.strip() for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    current = ROOT / filename
    old = set(pins(current.read_text(encoding="utf-8"))) if current.is_file() else set()
    additions = [pin for pin in pins(archive.read(filename).decode("utf-8"))
                 if pin not in old]
    if not additions:
        return
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", *additions],
        capture_output=True, text=True, timeout=1800,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(f"Could not install a new {label} dependency: "
                           + result.stderr[-600:])


def wait_for_exit(pid):
    if os.name != "nt" or not pid:
        return
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if handle:
        try:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 60_000)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def restart_router_after_update():
    """Replace the old in-memory routing code after the UI has gone idle."""
    import psutil

    pid_file = ROOT / "Cloud/router-pid.txt"
    try:
        pid = int(pid_file.read_text(encoding="utf-8"))
        process = psutil.Process(pid)
        command = process.cmdline()
        script = str((ROOT / "cloud_ai.py").resolve()).lower()
        if script in [part.lower() for part in command] and "--serve" in command:
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    except (OSError, ValueError, psutil.Error):
        pass
    # active_tasks() imported the old long_tasks/model_library modules earlier
    # in this updater process. Starting the new router in a fresh interpreter
    # avoids mixing those cached modules with newly swapped cloud_ai.py.
    result = subprocess.run(
        [sys.executable, str(ROOT / "cloud_ai.py"), "--ensure"],
        cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("The updated local AI router could not start.")


def reopen_desktop():
    """Return the user to a usable window after success or rollback."""
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    subprocess.Popen(
        [
            str(pythonw if pythonw.is_file() else executable),
            str(ROOT / "long_tasks.py"),
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
    )


def run_update(archive: Path, version: tuple[int, int, int], parent_pid: int) -> None:
    """Serialize app swaps and restore the desktop after a failed attempt."""
    wait_for_exit(parent_pid)
    with update_lock():
        if current_version() >= version:
            # A second updater can reach here after the first installed the
            # release but while the old GUI still held its single-instance
            # lock. Its relaunch then exits, and this updater must supply the
            # replacement window after its own parent has closed.
            reopen_desktop()
            return
        try:
            apply(archive, version)
            restart_router_after_update()
        except Exception as exc:
            log = ROOT / "Updates/last-error.txt"
            log.parent.mkdir(exist_ok=True)
            log.write_text(str(exc), encoding="utf-8")
            try:
                allow_resume_after_failed_update()
                resume_projects_after_update(force=True)
            except Exception:
                pass  # Reopening the usable old desktop takes priority.
            reopen_desktop()
            raise
        (ROOT / "Updates/last-error.txt").unlink(missing_ok=True)
        reopen_desktop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    if not args.apply:
        result = stage()
        if result:
            print(
                json.dumps(
                    {
                        "archive": str(result[0]),
                        "version": ".".join(map(str, result[1])),
                    }
                )
            )
        return
    match = re.fullmatch(r"\d+\.\d+\.\d+", args.version or "")
    if not match:
        raise ValueError("Invalid update version.")
    run_update(args.apply, tuple(map(int, args.version.split("."))), args.parent_pid)


if __name__ == "__main__":
    main()
