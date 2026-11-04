"""Move older portable Ollama installs to the self-updating Windows app.

The native installer is pinned to an official GitHub asset and SHA-256. The
worker waits for both Cale tasks and its model downloader to become idle.
"""

from __future__ import annotations

import hashlib
import json
import msvcrt
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

from model_library import save_download_status

ROOT = Path(__file__).resolve().parent
STATUS = ROOT / "LongTasks/ollama-upgrade.json"
INSTALLER = ROOT / "Updates/runtime/OllamaSetup.exe"
MAX_SIZE = 2_500_000_000
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def native_path():
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe"


def migration_needed():
    """Only the older bundled CLI lacks Ollama's native update mechanism."""
    return (
        ROOT / "Toolbox/ollama/ollama.exe"
    ).is_file() and not native_path().is_file()


def model_download_running():
    """The actual Windows lock proves whether the optional downloader is live."""
    lock_path = ROOT / "LongTasks/auto-models.lock"
    if not lock_path.is_file():
        return False
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return False


def busy():
    from long_tasks import projects

    return model_download_running() or any(
        task["status"] in ("queued", "working", "checking") for task in projects()
    )


def pinned_installer():
    """Read only an official pinned setup URL with an exact published digest."""
    item = json.loads((ROOT / "downloads.json").read_text())["ollama_setup"]
    if not re.fullmatch(
        r"https://github\.com/ollama/ollama/releases/download/v[0-9]+\.[0-9]+\.[0-9]+/OllamaSetup\.exe",
        item["url"],
    ) or not re.fullmatch(r"[a-fA-F0-9]{64}", item["sha256"]):
        raise ValueError("Invalid Ollama setup pin.")
    return item["url"], item["sha256"].lower()


def verified_file(path, expected):
    """Avoid reading a multi-gigabyte installer into memory."""
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == expected


def download_installer(url, expected):
    INSTALLER.parent.mkdir(parents=True, exist_ok=True)
    if verified_file(INSTALLER, expected):
        return INSTALLER
    if shutil.disk_usage(INSTALLER.parent).free < MAX_SIZE + 4 * 2**30:
        raise RuntimeError("Not enough free storage for the Ollama update.")
    temporary = INSTALLER.with_suffix(".partial")
    digest = hashlib.sha256()
    total = 0
    try:
        with (
            urlopen(
                Request(url, headers={"User-Agent": "Cale-AI-Workbench"}), timeout=60
            ) as response,
            temporary.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_SIZE:
                    raise ValueError("Ollama installer exceeded the download limit.")
                output.write(chunk)
                digest.update(chunk)
                if total % (100 * 1024 * 1024) < len(chunk):
                    save_download_status(
                        STATUS,
                        "working",
                        f"Downloading Ollama update: {total / 1e9:.1f} GB",
                        30,
                    )
        if digest.hexdigest() != expected:
            raise ValueError("Ollama installer SHA-256 did not match its pinned release.")
        os.replace(temporary, INSTALLER)
        return INSTALLER
    finally:
        temporary.unlink(missing_ok=True)


def run():
    """Install when idle; keep the portable executable until native works."""
    if not migration_needed():
        return
    STATUS.parent.mkdir(exist_ok=True)
    with (STATUS.parent / "ollama-upgrade.lock").open("a+b") as lock:
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
            while busy():
                save_download_status(
                    STATUS,
                    "waiting",
                    "Waiting for tasks and model downloads to finish",
                    0,
                )
                time.sleep(30)
            url, expected = pinned_installer()
            save_download_status(
                STATUS, "working", "Preparing Ollama's auto-updating app", 10
            )
            installer = download_installer(url, expected)
            while busy():
                save_download_status(
                    STATUS, "waiting", "Waiting to install Ollama safely", 80
                )
                time.sleep(30)
            save_download_status(
                STATUS, "working", "Installing Ollama's Windows app", 90
            )
            subprocess.run(
                [
                    str(installer),
                    "/VERYSILENT",
                    "/NORESTART",
                    "/SUPPRESSMSGBOXES",
                    "/CLOSEAPPLICATIONS",
                    "/FORCECLOSEAPPLICATIONS",
                ],
                check=True,
                timeout=1800,
                creationflags=HIDE,
            )
            if not native_path().is_file():
                raise RuntimeError(
                    "Ollama setup finished without its application file."
                )
            save_download_status(
                STATUS, "complete", "Ollama can now update itself", 100
            )
        except Exception as exc:
            save_download_status(STATUS, "error", str(exc)[:400], 0)


if __name__ == "__main__":
    run()
