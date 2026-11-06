"""Install new local tool dependencies after an in-app source update."""

import argparse
import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import chrome_connector

ROOT = Path(__file__).resolve().parent
MARKER = ROOT / "Updates/features-0.5.16.done"
PROGRESS = ROOT / "LongTasks/install-progress.json"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RETRY_DELAYS = (10, 30, 60)


def ready() -> bool:
    """An installed version marker alone does not prove tools still exist."""
    return (MARKER.is_file() and importlib.util.find_spec("crawl4ai") is not None
            and chrome_connector.installed_version() == chrome_connector.EXPECTED_VERSION)


def progress(percent: int, current: str, status: str = "working", attempts: int | None = None) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    if attempts is None:
        try:
            previous = json.loads(PROGRESS.read_text(encoding="utf-8"))
            attempts = int(previous.get("attempts", 0)) if isinstance(previous, dict) else 0
        except (OSError, ValueError, TypeError, OverflowError):
            attempts = 0
    value = {"percent": percent, "current": current, "status": status,
             "updated": time.time(), "attempts": attempts, "owner": "runtime_features",
             "pid": os.getpid()}
    temporary = PROGRESS.with_name(PROGRESS.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, PROGRESS)


def _run(args: list[str], env: dict | None = None, timeout: int = 600) -> None:
    result = subprocess.run(
        args, env=env, capture_output=True, text=True, errors="replace", timeout=timeout,
        creationflags=HIDE,
    )
    if result.returncode:
        detail = ((result.stderr or "").strip() or (result.stdout or "").strip()
                  or "unknown error")[-500:]
        raise RuntimeError(detail)


def _ensure_pip(python: str) -> None:
    """Repair uv-created Python environments that omit pip by default."""
    try:
        _run([python, "-m", "pip", "--version"], timeout=30)
    except RuntimeError:
        progress(8, "Repairing the local Python package installer")
        _run([python, "-m", "ensurepip", "--default-pip"], timeout=120)
        _run([python, "-m", "pip", "--version"], timeout=30)


def _installer_running(pid=None) -> bool:
    """Recognize only this feature installer, so stale or reused PIDs cannot block repair."""
    import psutil

    script = Path(__file__).resolve()
    # argparse accepts unambiguous long-option abbreviations by default.
    install_flags = {"--install"[:length] for length in range(3, len("--install") + 1)}
    try:
        processes = [psutil.Process(int(pid))] if pid else psutil.process_iter(["cmdline"])
        for process in processes:
            try:
                command = process.cmdline()
                if not install_flags.intersection(command):
                    continue
                # An absolute script is sufficient even when cwd is protected.
                # Options such as -u must not force a cwd lookup before it.
                # Only the interpreter's script argument identifies the program.
                # Paths passed to another script, module, or inline command do not.
                candidates = []
                arguments = iter(command[1:])
                for argument in arguments:
                    # Short interpreter flags can be bundled (for example,
                    # -uW ignore). The first value-taking option consumes
                    # the remainder of the bundle, or the next argument.
                    if argument.startswith("-") and not argument.startswith("--"):
                        for index, option in enumerate(argument[1:], 1):
                            if option in "cmWX":
                                argument = "-" + argument[index:]
                                break
                    if argument == "-" or argument.startswith(("-c", "-m")):
                        break
                    if argument in ("-W", "-X", "--check-hash-based-pycs"):
                        next(arguments, None)
                        continue
                    if argument == "--":
                        script_argument = next(arguments, None)
                        if script_argument is not None:
                            candidates = [Path(script_argument)]
                        break
                    if argument.startswith("-"):
                        continue
                    candidates = [Path(argument)]
                    break
                # Interpreter option values do not enable installation; only
                # arguments following the script are parsed by its CLI.
                installing = False
                for argument in arguments:
                    if argument == "--":
                        break
                    if argument in install_flags:
                        installing = True
                if not installing:
                    continue
                if any(part.is_absolute() and part.resolve() == script for part in candidates):
                    return True
                relative = [part for part in candidates if not part.is_absolute()
                            and not str(part).startswith("-")]
                if relative:
                    working = Path(process.cwd())
                    if any((working / part).resolve() == script for part in relative):
                        return True
            except (OSError, ValueError, psutil.Error):
                continue
    except (OSError, ValueError, TypeError, OverflowError, psutil.Error):
        return False
    return False


def install() -> None:
    """Bring old installations up to the shipped local dependency set."""
    if ready():
        return
    import msvcrt

    lock_path = ROOT / "Updates/features.lock"
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
            return
        try:
            if ready():
                return
            _ensure_pip(sys.executable)
            progress(12, "Adding the local website crawler")
            _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "crawl4ai==0.9.5"])
            progress(50, "Downloading the crawler's local browser")
            _run([sys.executable, "-m", "playwright", "install", "chromium", "--only-shell"])
            progress(78, "Connecting the Chrome companion")
            facts_file = max((ROOT / "data/installs").glob("*/facts.json"), key=lambda f: f.stat().st_mtime)
            facts = json.loads(facts_file.read_text(encoding="utf-8"))
            runtime = Path(facts["packages"]["venv"]["environment"]) / "Scripts/python.exe"
            _ensure_pip(str(runtime))
            env = os.environ.copy()
            env["HERMES_HOME"] = str(ROOT / "data")
            env["PATH"] = str(ROOT / "data/bin") + os.pathsep + env.get("PATH", "")
            _run([
                str(runtime), str(ROOT / "vendor/hermes-connector/install_companion.py"),
                "--source", str(ROOT / "vendor/hermes-connector/plugin"),
                "--hermes-home", str(ROOT / "data"), "--no-show-code",
            ], env=env, timeout=120)
            MARKER.write_text("Crawl4AI 0.9.5; Hermes Connector 0.2.6", encoding="utf-8")
            progress(100, "Your local tools are ready", "complete", attempts=0)
        except Exception as exc:
            progress(0, "Tool update needs attention: " + str(exc)[:200], "failed")
            raise
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def start_background() -> bool:
    """Keep the desktop responsive while dependencies install or repair."""
    current = {}
    try:
        current = json.loads(PROGRESS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if not isinstance(current, dict):
        current = {}
    owned = current.get("owner") == "runtime_features" or str(current.get("current", "")).startswith(
        ("Tool update needs attention:", "Preparing new local tools")
    )
    if ready():
        # A previous installer can leave a failed progress file after another
        # process has already installed and verified every required tool.
        # Tasks must not wait indefinitely on that stale status.
        if owned and current.get("status") != "complete":
            progress(100, "Your local tools are ready", "complete", attempts=0)
        return False
    # The base installer also writes this file while downloading models.
    # Never mistake its long-running work for a stalled feature update.
    if current and not owned and current.get("status") != "complete":
        return False
    try:
        updated = float(current.get("updated", 0))
        if not math.isfinite(updated):
            updated = 0
    except (TypeError, ValueError, OverflowError):
        updated = 0
    now = time.time()
    if updated > now:
        updated = 0
    age = now - updated
    try:
        attempts = max(0, int(current.get("attempts", 0)))
    except (TypeError, ValueError, OverflowError):
        attempts = 0
    if current.get("status") == "working":
        # Installation commands can be quiet for a long time. A live process
        # is progress; a dead one should be repaired promptly.
        if _installer_running(current.get("pid")):
            return False
        if age < 90:
            return False
    if attempts >= len(RETRY_DELAYS):
        if current.get("status") != "failed":
            progress(0, "Automatic setup repair could not finish. Check the tool installation.", "failed")
        return False
    if current.get("status") == "failed" and age < RETRY_DELAYS[attempts]:
        return False
    progress(3, "Preparing local tools" if attempts == 0 else "Repairing local tools", attempts=attempts + 1)
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--install"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=HIDE,
        )
    except OSError as exc:
        progress(0, "Tool update needs attention: " + str(exc)[:200], "failed")
        raise
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.install:
        install()
