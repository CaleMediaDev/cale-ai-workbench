"""Upgrade an existing installation's offline voice without blocking the UI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from model_library import save_download_status

ROOT = Path(__file__).resolve().parent
MEDIA_PYTHON = ROOT / "Media/python/Scripts/python.exe"
STATUS = ROOT / "LongTasks/natural-voice-progress.json"
VOICE = ROOT / "Media/models/natural_voice"
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def ready():
    return all(
        path.is_file() and path.stat().st_size > 0
        for path in (
            VOICE / "kokoro-v1.0.int8.onnx",
            VOICE / "voices-v1.0.bin",
        )
    )


def whisper_ready():
    """A config alone is insufficient if the transcription weights were lost."""
    folder = ROOT / "Media/models/whisper"
    return (folder / "config.json").is_file() and (folder / "model.bin").is_file()


def install():
    """Install the pinned synthesizer and verified weights once per PC."""
    import msvcrt

    STATUS.parent.mkdir(exist_ok=True)
    with (STATUS.parent / "natural-voice.lock").open("a+b") as lock:
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
