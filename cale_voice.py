"""Install and use Cale's offline voice clone without blocking first-run setup."""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
import wave
from pathlib import Path

from model_library import save_download_status

ROOT = Path(__file__).resolve().parent
MEDIA_PYTHON = ROOT / "Media/python/Scripts/python.exe"
STATUS = ROOT / "LongTasks/cale-voice-progress.json"
MARKER = ROOT / "LongTasks/cale-voice-ready.json"
REFERENCE = ROOT / "assets/cale-voice-reference.wav"
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_worker = None
_lock = threading.Lock()
_idle_timer = None
_generation = 0
IDLE_SECONDS = 60


class SpeechInterrupted(Exception):
    """A microphone turn interrupted speech; the caller should queue it again."""


def ready() -> bool:
    """Use the clone only after dependencies and model pass their warmup."""
    return MEDIA_PYTHON.is_file() and REFERENCE.is_file() and MARKER.is_file()


def _run(args: list[str], timeout: int = 900):
    subprocess.run(
        [str(MEDIA_PYTHON), *args], check=True, capture_output=True,
        timeout=timeout, cwd=ROOT, creationflags=HIDE,
    )


def install():
    """Prepare the permissively licensed 0.6B clone only on capable PCs."""
    import msvcrt

    if not MEDIA_PYTHON.is_file() or not REFERENCE.is_file() or ready():
        return
    STATUS.parent.mkdir(exist_ok=True)
    with (STATUS.parent / "cale-voice.lock").open("a+b") as lock:
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
            save_download_status(STATUS, "working", "Checking voice hardware", 5)
            probe = subprocess.run(
                [str(MEDIA_PYTHON), "-c", "import torch; print(int(torch.cuda.is_available() and torch.cuda.get_device_properties(0).total_memory >= 6_000_000_000))"],
                capture_output=True, text=True, timeout=45, creationflags=HIDE,
            )
            if probe.returncode or probe.stdout.strip() != "1":
                save_download_status(STATUS, "skipped", "This PC uses the lighter local voice", 100)
                return
            save_download_status(STATUS, "working", "Installing Cale's voice engine", 20)
            _run(["-m", "pip", "install", "--disable-pip-version-check",
                  "librosa==0.11.0", "sox==1.5.0", "einops==0.8.2",
                  "torchaudio==2.9.1", "transformers==4.57.3",
                  "accelerate==1.12.0"], timeout=1800)
            _run(["-m", "pip", "install", "--disable-pip-version-check",
                  "--no-deps", "qwen-tts==0.1.1"], timeout=300)
            save_download_status(STATUS, "working", "Downloading Cale's local voice model", 65)
            _run(["-c", "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-TTS-12Hz-0.6B-Base')"], timeout=1800)
            _run([str(ROOT / "cale_voice_worker.py"), "--warm-model"], timeout=1800)
            MARKER.write_text(json.dumps({"model": "Qwen3-TTS-12Hz-0.6B-Base"}), encoding="utf-8")
            save_download_status(STATUS, "complete", "Cale's voice is ready", 100)
        except (OSError, subprocess.SubprocessError) as exc:
            save_download_status(STATUS, "error", str(exc)[:400], 0)


def _line_with_timeout(stream, seconds: int, cancel: threading.Event | None = None) -> str:
    output: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: output.put(stream.readline()), daemon=True).start()
    deadline = time.monotonic() + seconds
    while True:
        if cancel is not None and cancel.is_set():
            raise SpeechInterrupted
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        try:
            return output.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            continue


def _stop_worker():
    global _worker
    process, _worker = _worker, None
    if process is None:
        return
    # Windows venv launchers can spawn a second Python process that owns the
    # GPU model. Stopping only the launcher leaves that expensive child alive.
    import psutil

    try:
        descendants = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        descendants = []
    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.Error:
            pass
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    _, alive = psutil.wait_procs(descendants, timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass


def _release_after_idle(generation: int) -> None:
    """Free voice-model memory only if no newer speech has begun."""
    with _lock:
        if generation == _generation:
            _stop_worker()


atexit.register(_stop_worker)


def speak(text: str, output_path: str | Path, play: bool = True,
          cancel: threading.Event | None = None) -> dict:
    """Send a short line to one persistent local GPU worker, then play it."""
    global _worker, _idle_timer, _generation
    if not ready():
        raise RuntimeError("Cale's voice is still installing.")
    with _lock:
        _generation += 1
        if _idle_timer is not None:
            _idle_timer.cancel()
        try:
            if _worker is None or _worker.poll() is not None:
                env = dict(os.environ, HF_HUB_OFFLINE="1")
                _worker = subprocess.Popen(
                    [str(MEDIA_PYTHON), str(ROOT / "cale_voice_worker.py")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    bufsize=1, cwd=ROOT, env=env, creationflags=HIDE,
                )
                ready_line = json.loads(_line_with_timeout(_worker.stdout, 45, cancel))
                if not ready_line.get("ready"):
                    raise RuntimeError("The voice worker did not start.")
            _worker.stdin.write(json.dumps({"text": str(text)[:280], "output_path": str(output_path)}) + "\n")
            _worker.stdin.flush()
            answer = json.loads(_line_with_timeout(_worker.stdout, 120, cancel))
            if "error" in answer:
                raise RuntimeError(answer["error"])
        except SpeechInterrupted:
            _stop_worker()
            raise
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            _stop_worker()
            raise RuntimeError("Cale's voice worker could not finish this update.") from exc
        _idle_timer = threading.Timer(IDLE_SECONDS, _release_after_idle, args=(_generation,))
        _idle_timer.daemon = True
        _idle_timer.start()
    if play:
        import winsound

        if cancel is None:
            winsound.PlaySound(answer["path"], winsound.SND_FILENAME)
        else:
            if cancel.is_set():
                raise SpeechInterrupted
            with wave.open(answer["path"], "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
            winsound.PlaySound(answer["path"], winsound.SND_FILENAME | winsound.SND_ASYNC)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                if cancel.wait(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                    winsound.PlaySound(None, 0)
                    raise SpeechInterrupted
    return answer


if __name__ == "__main__":
    install()
