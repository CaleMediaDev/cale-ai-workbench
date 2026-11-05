"""Install and use Cale's offline voice clone without blocking first-run setup."""

from __future__ import annotations

import atexit
import hashlib
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

from model_library import save_download_status

ROOT = Path(__file__).resolve().parent
CONTROLLER_SCRIPT = Path(__file__).resolve()
WORKER_SCRIPT = CONTROLLER_SCRIPT.with_name("cale_voice_worker.py")
MEDIA_PYTHON = ROOT / "Media/python/Scripts/python.exe"
STATUS = ROOT / "LongTasks/cale-voice-progress.json"
MARKER = ROOT / "LongTasks/cale-voice-ready.json"
REFERENCE = ROOT / "assets/cale-voice-reference.wav"
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_worker = None
_lock = threading.Lock()
_idle_timer = None
_generation = 0
IDLE_SECONDS = 300
VOICE_PACKAGES = {
    "transformers": "4.57.3",
    "accelerate": "1.12.0",
    "qwen_tts": "0.1.1",
}


class SpeechInterrupted(Exception):
    """A microphone turn interrupted speech; the caller should queue it again."""


def ready() -> bool:
    """Use the clone only with the tested package set and warmed model."""
    packages = ROOT / "Media/python/Lib/site-packages"
    return (MEDIA_PYTHON.is_file() and REFERENCE.is_file() and MARKER.is_file()
            and all((packages / f"{name}-{version}.dist-info").is_dir()
                    for name, version in VOICE_PACKAGES.items()))


PROBE_SECONDS = 180
PROBE_FORMAT = 1


def _probe_record() -> dict:
    path = ROOT / "LongTasks/cale-voice-probe.json"
    try:
        if path.stat().st_size > 8192:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_probe(value: dict) -> None:
    path = ROOT / "LongTasks/cale-voice-probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + f".{os.getpid()}.tmp")
    staged.write_text(json.dumps(value), encoding="utf-8")
    os.replace(staged, path)


def _voice_fingerprint(model_path: str) -> str:
    """Bound local metadata checks; never hash gigabytes of model weights."""
    def digest(path: Path, maximum: int) -> str:
        if not path.is_file() or not 0 < path.stat().st_size <= maximum:
            raise ValueError("Required voice file is missing or oversized")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    python = MEDIA_PYTHON.stat()
    parts = [PROBE_FORMAT, str(MEDIA_PYTHON.resolve()), python.st_size, python.st_mtime_ns,
             digest(REFERENCE, 16_000_000)]
    for script in (CONTROLLER_SCRIPT, WORKER_SCRIPT):
        parts.append(digest(script, 1_000_000))
    packages = ROOT / "Media/python/Lib/site-packages"
    for name in ("qwen_tts", "transformers", "accelerate", "torch", "torchaudio",
                 "soundfile", "librosa", "einops", "numpy", "safetensors", "huggingface_hub"):
        entries = []
        for entry in packages.glob(name + "-*.dist-info"):
            if len(entries) >= 8:
                raise ValueError("Voice package metadata is ambiguous")
            metadata = entry / "METADATA"
            entries.append([entry.name, digest(metadata, 1_000_000)])
        parts.append([name, sorted(entries)])
    model = Path(model_path).resolve(strict=True)
    if not model.is_dir():
        raise ValueError("The checked voice snapshot is missing")
    # snapshot_download(local_files_only=True) follows refs/main, not the old
    # snapshot saved by an earlier render. Retaining old weights is not proof
    # that the worker's currently selected revision has ever rendered audio.
    revision_ref = None
    if model.parent.name == "snapshots":
        revision_ref = model.parent.parent / "refs/main"
        if not revision_ref.is_file():
            raise ValueError("The cached voice model selection is missing")
        with revision_ref.open("rb") as stream:
            selected_revision = stream.read(129)
        revision = model.name.encode("ascii")
        if (len(revision) != 40 or any(c not in b"0123456789abcdef" for c in revision)
                or selected_revision not in (revision, revision + b"\n", revision + b"\r\n")):
            raise ValueError("The checked voice snapshot is not the current cached model revision")
        parts.append([str(revision_ref.resolve()), hashlib.sha256(selected_revision).hexdigest()])
    config = model / "config.json"
    # Configuration drives model loading and must remain a bounded JSON object.
    digest(config, 2_000_000)
    if not isinstance(json.loads(config.read_text(encoding="utf-8")), dict):
        raise ValueError("Voice model configuration must be a JSON object")
    files = []
    for index, path in enumerate(model.rglob("*")):
        if index >= 256:
            raise ValueError("Voice snapshot exceeds the bounded metadata probe")
        if path.is_file():
            stat = path.stat()
            files.append([path.relative_to(model).as_posix(), str(path.resolve()),
                          stat.st_size, stat.st_mtime_ns,
                          # Vocabularies/tokenizers can exceed small-config limits.
                          # Track large assets by path/size/mtime just like weights.
                          digest(path, 2_000_000) if path.suffix == ".json" and stat.st_size <= 2_000_000 else ""])
    if not any(row[0] == "config.json" for row in files) or not any(
            Path(row[0]).suffix in {".safetensors", ".bin"} and row[2] > 0 for row in files):
        raise ValueError("Voice snapshot has no configuration or model weights")
    if revision_ref is not None:
        with revision_ref.open("rb") as stream:
            if stream.read(129) != selected_revision:
                raise ValueError("The cached voice model revision changed during its check")
    parts.append(sorted(files))
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def capability_ready() -> bool:
    """Only an actual clone render with matching dependencies proves speech."""
    report = _probe_record()
    try:
        audio = report.get("audio")
        checked = report.get("checked_at")
        if (not isinstance(checked, (int, float)) or not math.isfinite(checked)
                or not 0 < checked <= time.time() + 60):
            return False
        if (not isinstance(audio, dict) or type(audio.get("frames")) is not int
                or type(audio.get("sample_rate")) is not int or not 8000 <= audio["sample_rate"] <= 96000
                or not 0.05 <= audio["frames"] / audio["sample_rate"] <= 15
                or audio.get("channels") not in (1, 2)
                or not isinstance(audio.get("sha256"), str) or len(audio["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in audio["sha256"])):
            return False
        return bool(ready() and report.get("format") == PROBE_FORMAT
                    and report.get("status") == "passed" and report.get("engine") == "Cale's local voice"
                    and report.get("fingerprint") == _voice_fingerprint(report["model_path"]))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError):
        return False


def _probe_busy() -> bool:
    import self_diagnostics
    import readiness_checklist
    return (self_diagnostics.active_projects(ROOT / "LongTasks")
            or readiness_checklist._heavy_gpu_active(ROOT))


def _checked_probe_wav(path: Path) -> dict:
    """Check complete, bounded PCM samples, not merely a WAV header or path."""
    if not path.is_file() or not 44 < path.stat().st_size <= 8_000_000:
        raise ValueError("Clone probe did not produce a bounded WAV")
    with wave.open(str(path), "rb") as audio:
        frames, rate = audio.getnframes(), audio.getframerate()
        channels, width = audio.getnchannels(), audio.getsampwidth()
        if (channels not in (1, 2) or width not in (1, 2, 3, 4) or not 8000 <= rate <= 96000
                or not 0.05 <= frames / rate <= 15):
            raise ValueError("Clone probe has invalid audio dimensions")
        samples = audio.readframes(frames)
        if len(samples) != frames * channels * width or not any(
                value != 128 if width == 1 else value != 0 for value in samples):
            raise ValueError("Clone probe audio is truncated or silent")
        # Nonzero PCM alone accepts a constant DC offset or inaudible one-bit
        # noise. Inspect bounded samples across the whole clip, independently
        # per channel so opposite stereo DC offsets cannot impersonate speech.
        # The WAV is already size- and duration-bounded. Inspect every frame:
        # fixed-stride subsampling can alias audible tones to a constant value.
        scale = float(1 << (width * 8 - 1))
        audible = False
        for channel in range(channels):
            count = 0
            total = squared = 0.0
            minimum, maximum = 1.0, -1.0
            for frame in range(frames):
                offset = (frame * channels + channel) * width
                raw = samples[offset:offset + width]
                value = ((raw[0] - 128) if width == 1 else int.from_bytes(raw, "little", signed=True)) / scale
                count += 1
                total += value
                squared += value * value
                minimum, maximum = min(minimum, value), max(maximum, value)
            ac_rms = math.sqrt(max(0.0, squared / count - (total / count) ** 2))
            if ac_rms >= 0.001 and maximum - minimum >= 0.005:
                audible = True
        if not audible:
            raise ValueError("Clone probe audio has no audible variation")
    return {"frames": frames, "sample_rate": rate, "channels": channels,
            "sha256": hashlib.sha256(samples).hexdigest()}


def schedule_probe() -> bool:
    """Queue one low-priority offline probe; polling never waits for the GPU."""
    if not ready() or capability_ready() or _probe_busy():
        return False
    import self_diagnostics
    with self_diagnostics._report_lock(ROOT / "LongTasks/voice-probe", blocking=False) as acquired:
        if not acquired:
            return False
        previous = _probe_record()
        try:
            age = time.time() - float(previous.get("checked_at") or 0)
        except (TypeError, ValueError, OverflowError):
            age = 60
        if 0 <= age < 60:
            return False
        process = subprocess.Popen(
            [sys.executable, str(CONTROLLER_SCRIPT), "--probe", "--runtime-root", str(ROOT)], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=HIDE | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        _save_probe({"format": PROBE_FORMAT, "status": "running", "pid": process.pid,
                     "checked_at": time.time()})
        return True


def run_probe() -> dict:
    """Render through the actual clone worker; never record, play or download."""
    import self_diagnostics
    with self_diagnostics._report_lock(ROOT / "LongTasks/voice-probe", blocking=True):
        if capability_ready():
            return _probe_record()
        report = {"format": PROBE_FORMAT, "status": "deferred", "checked_at": time.time()}
        if not ready() or _probe_busy():
            _save_probe(report)
            return report
        process = None
        try:
            release_for_gpu_task()  # Do not load a second copy beside an old persistent voice worker.
            with tempfile.TemporaryDirectory(prefix="cale-voice-probe-") as temporary:
                output = Path(temporary) / "clone.wav"
                log = ROOT / "LongTasks/cale-voice-probe.log"
                with log.open("w", encoding="utf-8") as errors:
                    process = subprocess.Popen(
                        [str(MEDIA_PYTHON), str(WORKER_SCRIPT), "--probe", str(output), str(ROOT)],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                        text=True, encoding="utf-8", env=dict(os.environ, HF_HUB_OFFLINE="1"),
                        creationflags=HIDE | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
                    deadline = time.monotonic() + PROBE_SECONDS
                    while process.poll() is None:
                        if _probe_busy():
                            self_diagnostics._stop_check(process)
                            _save_probe(report)
                            return report
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Clone capability probe timed out")
                        time.sleep(0.2)
                    stdout, _ = process.communicate(timeout=3)
                if process.returncode or len(stdout) > 8192:
                    raise RuntimeError("The clone capability worker failed")
                answer = json.loads(stdout)
                if (not isinstance(answer, dict) or answer.get("engine") != "Cale's local voice"
                        or Path(answer.get("path", "")).resolve() != output.resolve()):
                    raise ValueError("The capability probe did not use Cale's clone")
                # Worker reports fingerprints captured before loading/generation.
                fingerprint = _voice_fingerprint(answer["model_path"])
                if fingerprint != answer.get("fingerprint") or _probe_busy():
                    raise ValueError("Voice files or task priority changed during the probe")
                report.update(status="passed", engine=answer["engine"], model_path=answer["model_path"],
                              fingerprint=fingerprint, audio=_checked_probe_wav(output))
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError, wave.Error, EOFError, KeyError, TypeError) as error:
            if process is not None and process.poll() is None:
                self_diagnostics._stop_check(process)
            report.update(status="failed", detail=str(error)[:200])
        report["checked_at"] = time.time()
        _save_probe(report)
        return report


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
            MARKER.unlink(missing_ok=True)
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
            MARKER.unlink(missing_ok=True)
            save_download_status(STATUS, "error", str(exc)[:400], 0)


def _line_with_timeout(stream, seconds: int, cancel: threading.Event | None = None) -> str:
    output: queue.Queue[str | Exception] = queue.Queue(maxsize=1)

    def read_line():
        try:
            result = stream.readline()
        except (OSError, ValueError) as exc:
            result = exc
        output.put(result)

    threading.Thread(target=read_line, daemon=True).start()
    deadline = time.monotonic() + seconds
    while True:
        if cancel is not None and cancel.is_set():
            raise SpeechInterrupted
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise queue.Empty
        try:
            result = output.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            continue
        if isinstance(result, Exception):
            raise result
        return result


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
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
    _, alive = psutil.wait_procs(descendants, timeout=3)
    for child in alive:
        try:
            child.kill()
        except psutil.Error:
            pass
    if alive:
        # Killing is asynchronous; allow GPU-owning children to exit before
        # recovery starts a replacement worker.
        psutil.wait_procs(alive, timeout=3)


def _is_voice_worker_process(info: dict, script: str) -> bool:
    """Recognize the app's direct Python/script launch from OS process metadata."""
    command = info.get("cmdline")
    executable = info.get("exe")
    if (not isinstance(command, (list, tuple)) or len(command) < 2
            or not isinstance(command[1], str) or not isinstance(executable, str)):
        return False
    interpreter = Path(executable)
    if (not interpreter.is_absolute() or not re.fullmatch(
            r"python(?:w)?(?:[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?", interpreter.name.casefold())):
        return False
    # Cale launches this absolute script as argv[1], with no interpreter flags.
    # An editor or another Python program may merely have the path as data.
    entrypoint = Path(command[1])
    return entrypoint.is_absolute() and str(entrypoint.resolve()).casefold() == script


def release_for_gpu_task() -> None:
    """Free voice-model VRAM before a separate music or image worker starts.

    Those jobs run in other processes, so they cannot access this module's
    ``_worker`` handle. Match only this installation's voice worker script.
    The next spoken line starts a fresh worker when GPU work has ended.
    """
    import psutil

    script = str((ROOT / "cale_voice_worker.py").resolve()).casefold()
    for process in psutil.process_iter(["pid", "cmdline", "exe"]):
        try:
            if process.pid == os.getpid() or not _is_voice_worker_process(process.info, script):
                continue
            try:
                descendants = process.children(recursive=True)
            except psutil.Error:
                # An inaccessible child list must not leave the known worker
                # running when its parent can still be terminated.
                descendants = []
            for child in reversed(descendants):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            try:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            finally:
                _, alive = psutil.wait_procs(descendants, timeout=3)
                for child in alive:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
                if alive:
                    # Killing is asynchronous. Allow bounded exit time before
                    # another process starts allocating GPU model memory.
                    psutil.wait_procs(alive, timeout=3)
        except (OSError, psutil.Error, ValueError):
            continue


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
                log = ROOT / "LongTasks/cale-voice-worker.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a", encoding="utf-8") as errors:
                    _worker = subprocess.Popen(
                        [str(MEDIA_PYTHON), str(ROOT / "cale_voice_worker.py")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=errors, text=True, encoding="utf-8",
                        bufsize=1, cwd=ROOT, env=env, creationflags=HIDE,
                    )
                ready_line = json.loads(_line_with_timeout(_worker.stdout, 45, cancel))
                if not isinstance(ready_line, dict) or not ready_line.get("ready"):
                    raise RuntimeError("The voice worker did not start.")
            _worker.stdin.write(json.dumps({"text": str(text)[:280], "output_path": str(output_path)}) + "\n")
            _worker.stdin.flush()
            answer = json.loads(_line_with_timeout(_worker.stdout, 120, cancel))
            if not isinstance(answer, dict):
                raise RuntimeError("The voice worker returned an invalid response.")
            if "error" in answer:
                raise RuntimeError(answer["error"])
            if not isinstance(answer.get("path"), str) or not answer["path"]:
                raise RuntimeError("The voice worker returned an invalid audio path.")
        except SpeechInterrupted:
            _stop_worker()
            raise
        except (OSError, ValueError, queue.Empty, RuntimeError) as exc:
            _stop_worker()
            try:
                _save_probe({"format": PROBE_FORMAT, "status": "failed", "checked_at": time.time(),
                             "detail": "Clone speech failed; awaiting a fresh idle capability probe"})
            except OSError:
                pass  # Preserve the original speech failure if status storage is unavailable.
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


def _probe_runtime(root: Path) -> None:
    """Let a developer test candidate code against installed assets, offline."""
    global ROOT, MEDIA_PYTHON, REFERENCE, STATUS, MARKER
    ROOT = root.resolve(strict=True)
    MEDIA_PYTHON = ROOT / "Media/python/Scripts/python.exe"
    REFERENCE = ROOT / "assets/cale-voice-reference.wav"
    STATUS = ROOT / "LongTasks/cale-voice-progress.json"
    MARKER = ROOT / "LongTasks/cale-voice-ready.json"


if __name__ == "__main__":
    if "--probe" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description="Render a silent, offline clone capability check only while idle.")
        parser.add_argument("--probe", action="store_true")
        parser.add_argument("--runtime-root", type=Path, default=ROOT)
        args = parser.parse_args()
        _probe_runtime(args.runtime_root)
        report = run_probe()
        print(json.dumps(report))
        raise SystemExit(0 if report["status"] == "passed" else 1)
    install()
