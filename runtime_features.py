"""Install new local tool dependencies after an in-app source update."""

import argparse
import importlib.util
import json
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


def ready() -> bool:
    """An installed version marker alone does not prove tools still exist."""
    return (MARKER.is_file() and importlib.util.find_spec("crawl4ai") is not None
            and chrome_connector.installed_version() == chrome_connector.EXPECTED_VERSION)


def progress(percent: int, current: str, status: str = "working") -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    value = {"percent": percent, "current": current, "status": status, "updated": time.time()}
    temporary = PROGRESS.with_name(PROGRESS.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, PROGRESS)


def _run(args: list[str], env: dict | None = None, timeout: int = 600) -> None:
    result = subprocess.run(
        args, env=env, capture_output=True, text=True, timeout=timeout,
        creationflags=HIDE,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown error").strip()[-500:]
        raise RuntimeError(detail)


def install() -> None:
    """Bring old installations up to the shipped local dependency set."""
    if ready():
        return
    import msvcrt

    lock_path = ROOT / "Updates/features.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
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
            progress(12, "Adding the local website crawler")
            _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "crawl4ai==0.9.5"])
            progress(50, "Downloading the crawler's local browser")
            _run([sys.executable, "-m", "playwright", "install", "chromium", "--only-shell"])
            progress(78, "Connecting the Chrome companion")
            facts_file = max((ROOT / "data/installs").glob("*/facts.json"), key=lambda f: f.stat().st_mtime)
            facts = json.loads(facts_file.read_text(encoding="utf-8"))
            runtime = Path(facts["packages"]["venv"]["environment"]) / "Scripts/python.exe"
            env = os.environ.copy()
            env["HERMES_HOME"] = str(ROOT / "data")
            env["PATH"] = str(ROOT / "data/bin") + os.pathsep + env.get("PATH", "")
            _run([
                str(runtime), str(ROOT / "vendor/hermes-connector/install_companion.py"),
                "--source", str(ROOT / "vendor/hermes-connector/plugin"),
                "--hermes-home", str(ROOT / "data"), "--no-show-code",
            ], env=env, timeout=120)
            MARKER.write_text("Crawl4AI 0.9.5; Hermes Connector 0.2.6", encoding="utf-8")
            progress(100, "Your local tools are ready", "complete")
        except Exception as exc:
            progress(0, "Tool update needs attention: " + str(exc)[:200], "failed")
            raise
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def start_background() -> bool:
    """Keep the desktop responsive while dependencies install or repair."""
    if ready():
        # A previous installer can leave a failed progress file after another
        # process has already installed and verified every required tool.
        # Tasks must not wait indefinitely on that stale status.
        try:
            previous = json.loads(PROGRESS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if previous.get("status") == "failed":
            progress(100, "Your local tools are ready", "complete")
        return False
    current = {}
    try:
        current = json.loads(PROGRESS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if current.get("status") == "working" and time.time() - current.get("updated", 0) < 900:
        return False
    progress(3, "Preparing new local tools")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--install"],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=HIDE,
    )
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    if args.install:
        install()
