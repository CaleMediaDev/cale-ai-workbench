"""Install and use Cale's offline voice clone without blocking first-run setup."""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
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


def _line_with_timeout(stream, seconds: int) -> str:
    output: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: output.put(stream.readline()), daemon=True).start()
    return output.get(timeout=seconds)


def _stop_worker():
    global _worker
    if _worker is not None:
        if _worker.poll() is None:
            try:
                _worker.terminate()
            except OSError:
                pass
        _worker = None


atexit.register(_stop_worker)


def speak(text: str, output_path: str | Path, play: bool = True) -> dict:
    """Send a short line to one persistent local GPU worker, then play it."""
    global _worker
    if not ready():
        raise RuntimeError("Cale's voice is still installing.")
    with _lock:
        try:
            if _worker is None or _worker.poll() is not None:
                env = dict(os.environ, HF_HUB_OFFLINE="1")
                _worker = subprocess.Popen(
                    [str(MEDIA_PYTHON), str(ROOT / "cale_voice_worker.py")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    bufsize=1, cwd=ROOT, env=env, creationflags=HIDE,
                )
                ready_line = json.loads(_line_with_timeout(_worker.stdout, 45))
                if not ready_line.get("ready"):
                    raise RuntimeError("The voice worker did not start.")
            _worker.stdin.write(json.dumps({"text": str(text)[:280], "output_path": str(output_path)}) + "\n")
            _worker.stdin.flush()
            answer = json.loads(_line_with_timeout(_worker.stdout, 120))
            if "error" in answer:
                raise RuntimeError(answer["error"])
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            _stop_worker()
            raise RuntimeError("Cale's voice worker could not finish this update.") from exc
    if play:
        import winsound

        winsound.PlaySound(answer["path"], winsound.SND_FILENAME)
    return answer


if __name__ == "__main__":
    install()
