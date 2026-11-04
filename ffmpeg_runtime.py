"""Keep a working FFmpeg executable available to every local agent tool.

The pinned imageio-ffmpeg wheel supplies the binary. We copy it to a stable
app-owned bin directory so command-line tools can invoke ``ffmpeg`` by name.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_thread: threading.Thread | None = None
_last_attempt: float | None = None


def executable(root: Path = ROOT) -> Path:
    return Path(root) / "Toolbox/bin/ffmpeg.exe"


def working(root: Path = ROOT) -> bool:
    """Execute the installed binary; a file existing is not enough."""
    target = executable(root)
    if not target.is_file():
        return False
    try:
        result = subprocess.run([str(target), "-version"], capture_output=True,
                                text=False, timeout=10, creationflags=HIDE)
        return result.returncode == 0 and result.stdout.startswith(b"ffmpeg version")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return False


def ensure(root: Path = ROOT) -> Path:
    """Install or repair the pinned local FFmpeg binary without a visible console."""
    root = Path(root)
    target = executable(root)
    if working(root):
        return target
    python = root / "Toolbox/python/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("The Toolbox Python runtime is not installed yet")
    target.parent.mkdir(parents=True, exist_ok=True)
    # The installer and supervisor may overlap. The file lock prevents both
    # from writing the same executable, including across different processes.
    import msvcrt

    lock_path = target.parent / "ffmpeg.lock"
    with lock_path.open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"1")
            lock.flush()
        lock.seek(0)
        for _ in range(60):
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if working(root):
                    return target
                time.sleep(1)
        else:
            raise RuntimeError("Another FFmpeg repair is still running")
        try:
            if working(root):
                return target
            locate = [str(python), "-c",
                      "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"]
            found = subprocess.run(locate, capture_output=True, text=True,
                                   timeout=30, creationflags=HIDE)
            if found.returncode or not Path(found.stdout.strip()).is_file():
                install = subprocess.run(
                    [str(python), "-m", "pip", "install", "--disable-pip-version-check",
                     "imageio-ffmpeg==0.6.0"], capture_output=True, text=True,
                    timeout=300, creationflags=HIDE,
                )
                if install.returncode:
                    raise RuntimeError("Could not install FFmpeg: " +
                                       (install.stderr or install.stdout)[-300:])
                found = subprocess.run(locate, capture_output=True, text=True,
                                       timeout=30, creationflags=HIDE)
            source = Path(found.stdout.strip())
            if found.returncode or not source.is_file():
                raise RuntimeError("The FFmpeg package did not provide an executable")
            staged = target.with_name(f"ffmpeg.{os.getpid()}.tmp.exe")
            try:
                shutil.copy2(source, staged)
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
            if not working(root):
                raise RuntimeError("FFmpeg did not pass its version check")
            return target
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def start_background(root: Path = ROOT) -> None:
    """Quietly repair a missing executable after an update or accidental deletion."""
    global _thread, _last_attempt
    if working(root) or (_thread is not None and _thread.is_alive()):
        return
    if _last_attempt is not None and time.monotonic() - _last_attempt < 300:
        return
    _last_attempt = time.monotonic()

    def repair() -> None:
        try:
            ensure(root)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass  # The checklist remains in attention; retry after five minutes.

    _thread = threading.Thread(target=repair, name="ffmpeg-repair", daemon=True)
    _thread.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure", action="store_true")
    args = parser.parse_args()
    if args.ensure:
        print(ensure())
