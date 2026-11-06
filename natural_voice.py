"""Upgrade an existing installation's offline voice without blocking the UI."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from pathlib import Path

from model_library import save_download_status

ROOT = Path(__file__).resolve().parent
MEDIA_PYTHON = ROOT / "Media/python/Scripts/python.exe"
STATUS = ROOT / "LongTasks/natural-voice-progress.json"
VOICE = ROOT / "Media/models/natural_voice"
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _nonempty_file(path):
    try:
        info = path.stat()
        return stat.S_ISREG(info.st_mode) and info.st_size > 0
    except (FileNotFoundError, NotADirectoryError):
        # Missing files or non-directory ancestors mean the model needs repair.
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # A symlink loop cannot resolve to a usable model file.
            return False
        raise


def ready():
    return all(
        _nonempty_file(path)
        for path in (
            VOICE / "kokoro-v1.0.int8.onnx",
            VOICE / "voices-v1.0.bin",
        )
    )


def whisper_ready():
    """Both transcription files must exist and contain data."""
    folder = ROOT / "Media/models/whisper"
    return all(
        _nonempty_file(path)
        for path in (folder / "config.json", folder / "model.bin")
    )


def install():
    """Install the pinned synthesizer and verified weights once per PC."""
    try:
        import msvcrt

        STATUS.parent.mkdir(exist_ok=True)
        # Do not leave buffered writes for close() to retry after a disk error.
        lock_file = (STATUS.parent / "natural-voice.lock").open("a+b", buffering=0)
    except (ImportError, OSError) as exc:
        save_download_status(STATUS, "error", str(exc)[:400], 0)
        return
    with lock_file as lock:
        try:
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"1")
                lock.flush()
            lock.seek(0)
        except OSError as exc:
            save_download_status(STATUS, "error", str(exc)[:400], 0)
            return
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                save_download_status(STATUS, "error", str(exc)[:400], 0)
            return
        try:
            if not MEDIA_PYTHON.is_file():
                raise FileNotFoundError("The bundled media Python executable is missing.")
            save_download_status(STATUS, "working", "Checking the natural voice", 5)
            check = subprocess.run(
                [str(MEDIA_PYTHON), "-c", "import kokoro_onnx"],
                capture_output=True,
                timeout=30,
                creationflags=HIDE,
            )
            if check.returncode:
                save_download_status(
                    STATUS, "working", "Installing the natural voice engine", 20
                )
                subprocess.run(
                    [
                        str(MEDIA_PYTHON),
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "kokoro-onnx==0.6.1",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=600,
                    creationflags=HIDE,
                )
                subprocess.run(
                    [str(MEDIA_PYTHON), "-c", "import kokoro_onnx"],
                    check=True,
                    capture_output=True,
                    timeout=30,
                    creationflags=HIDE,
                )
            if not ready():
                save_download_status(
                    STATUS, "working", "Downloading the natural voice", 50
                )
                subprocess.run(
                    [
                        str(MEDIA_PYTHON),
                        str(ROOT / "media_worker.py"),
                        "--download-voice",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=600,
                    creationflags=HIDE,
                )
            if not whisper_ready():
                save_download_status(
                    STATUS, "working", "Restoring offline speech recognition", 75
                )
                subprocess.run(
                    [str(MEDIA_PYTHON), str(ROOT / "media_worker.py"), "--download-whisper"],
                    check=True, capture_output=True, timeout=900, creationflags=HIDE,
                )
            if not ready() or not whisper_ready():
                raise RuntimeError("Offline speech files are still missing after repair.")
            save_download_status(STATUS, "complete", "Natural voice ready", 100)
        except Exception as exc:
            save_download_status(STATUS, "error", str(exc)[:400], 0)


if __name__ == "__main__":
    install()
