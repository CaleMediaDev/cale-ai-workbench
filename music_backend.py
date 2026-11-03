"""Install and use ACE-Step 1.5 as a loopback-only local music tool.

ACE-Step runs in its own Python environment because its audio dependencies do
not share the workbench's media runtime. The first music request can take a
while as the runtime and model download. No account or cloud inference is used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import requests

import music_style

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "Media" / "music" / "ACE-Step-1.5"
REVISION = "ca1e85fe9430179831e6bc6be790c332190a3866"
BASE = "http://127.0.0.1:8001"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
JOBS = ROOT / "LongTasks" / "music-jobs"
AUDIO_REFERENCES = frozenset({".wav", ".mp3", ".flac"})
TRANSCODE_AUDIO_REFERENCES = frozenset({".m4a", ".aac", ".ogg", ".opus"})
VIDEO_REFERENCES = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv"})


@contextmanager
def _setup_lock():
    """Keep first-run setup from racing a music request in another process."""
    import msvcrt

    lock_path = ENGINE.parent / "setup.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        for _ in range(7200):
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(1)
        else:
            raise TimeoutError("Local music setup is still busy after two hours.")
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _write_setup_status(status: str, current: str, percent: int) -> None:
    path = ROOT / "LongTasks" / "music-install-progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"status": status, "current": current, "progress": percent}),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _capable_for_background() -> bool:
    """Start optional model downloads only on PCs that can run them well."""
    python = ROOT / "Media" / "python" / "Scripts" / "python.exe"
    if not python.is_file() or shutil.disk_usage(ROOT).free < 18 * 1024**3:
        return False
    probe = subprocess.run(
        [str(python), "-c", "import torch; print(int(torch.cuda.is_available() and "
         "torch.cuda.get_device_properties(0).total_memory >= 4_000_000_000))"],
        capture_output=True, text=True, timeout=45, creationflags=HIDE,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "1"


@contextmanager
def _music_gpu_lease():
    """Give ACE-Step GPU room and keep routine voice lines from reloading Ollama."""
    marker = ROOT / "LongTasks" / f"music-gpu-active-{os.getpid()}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    try:
        try:
            import cale_voice

            cale_voice.release_for_gpu_task()
        except (OSError, RuntimeError):
            pass  # Music can continue if the optional cloned voice is absent.
        try:
            from media_worker import unload_local_models

            unload_local_models()
        except (OSError, RuntimeError, KeyError, ValueError):
            pass  # Music also works if Ollama was not running.
        yield
    finally:
        marker.unlink(missing_ok=True)


def prefetch() -> None:
    """Install the model quietly after launch on a suitable PC."""
    marker = ENGINE / ".cale-model-ready"
    try:
        if marker.is_file() or healthy() or not _capable_for_background():
            return
        with _setup_lock():
            if marker.is_file() or healthy():
                return
            _write_setup_status("working", "Installing local music tools", 5)
            install()
            executable = ENGINE / ".venv" / "Scripts" / "acestep-download.exe"
            if not executable.is_file():
                raise RuntimeError("ACE-Step installed without its model downloader.")
            _write_setup_status("working", "Downloading the local music model", 35)
            log_path = ENGINE.parent / "download.log"
            with log_path.open("ab") as log:
                subprocess.run(
                    [str(executable)], cwd=ENGINE, stdout=log,
                    stderr=subprocess.STDOUT, check=True, timeout=7200,
                    creationflags=HIDE,
                )
            marker.write_text(REVISION, encoding="ascii")
            _write_setup_status("complete", "Local music is ready", 100)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        _write_setup_status("error", str(exc)[:300], 0)


def _tool(name: str) -> str:
    bundled = sorted((ROOT / "data" / "tools").glob(f"{name}-*/**/{name}.exe"))
    found = next((path for path in bundled if path.is_file()), None)
    executable = str(found) if found else shutil.which(name)
    if not executable:
        raise RuntimeError(f"The local {name} runtime is missing. Run the Workbench installer again.")
    return executable


def _request(path: str, data: dict | None = None, timeout: int = 30):
    request = Request(
        BASE + path,
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"Local music API error {exc.code}: {detail[:300]}") from exc


def _upload_cover(request: dict, source: Path, field: str = "src_audio") -> dict:
    """Upload an edit source or a style reference to the loopback music API."""
    if field not in ("src_audio", "reference_audio"):
        raise ValueError("Unknown music audio upload field")
    fields = {key: json.dumps(value) if isinstance(value, bool) else str(value)
              for key, value in request.items()}
    with source.open("rb") as audio:
        response = requests.post(
            BASE + "/release_task", data=fields,
            files={field: (source.name, audio)}, timeout=300,
        )
    if not response.ok:
        raise RuntimeError(
            f"Local music API error {response.status_code}: {response.text[:300]}"
        )
    return response.json()


def _upload_reference_media(request: dict, source: Path, field: str) -> dict:
    """Convert compressed audio or a video's track for the local music API."""
    if source.suffix.lower() in AUDIO_REFERENCES:
        return _upload_cover(request, source, field)
    import imageio_ffmpeg

    temporary_root = ROOT / "Temp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cale-audio-reference-", dir=temporary_root) as temp:
        audio = Path(temp) / "reference.wav"
        try:
            result = subprocess.run(
                [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", str(source), "-map", "0:a:0", "-vn", "-t", "600",
                 "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(audio)],
                capture_output=True, text=True, timeout=300, creationflags=HIDE,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Could not read the reference's audio track") from exc
        if result.returncode or not audio.is_file() or audio.stat().st_size <= 44:
            raise RuntimeError("The reference has no readable audio track: "
                               + result.stderr[-180:])
        return _upload_cover(request, audio, field)


def healthy() -> bool:
    try:
        result = _request("/health", timeout=2)
        return result.get("data", {}).get("service") == "ACE-Step API"
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def music_lm_ready() -> bool:
    """Use the LM only when the engine confirms it loaded successfully."""
    try:
        return bool(_request("/health", timeout=2).get("data", {}).get("llm_initialized"))
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def _runtime_healthy(python: Path, uv: str) -> bool:
    """Check the installed music environment, not just its marker file."""
    if not python.is_file() or not (ENGINE / ".venv/Scripts/acestep-api.exe").is_file():
        return False
    probe = (
        "import importlib.util,sys; "
        "names=('acestep','torch','diffusers','fastapi','soundfile','gradio','uvicorn'); "
        "missing=[name for name in names if importlib.util.find_spec(name) is None]; "
        "print(', '.join(missing)); sys.exit(bool(missing))"
    )
    try:
        imports = subprocess.run(
            [str(python), "-c", probe], capture_output=True, text=True,
            timeout=30, creationflags=HIDE,
        )
        if imports.returncode:
            return False
        dependencies = subprocess.run(
            [uv, "pip", "check", "--python", str(python)],
            capture_output=True, text=True, timeout=60, creationflags=HIDE,
        )
        return dependencies.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install() -> Path:
    """Install a pinned upstream revision into the workbench's local data."""
    marker = ENGINE / ".cale-installed"
    python = ENGINE / ".venv" / "Scripts" / "python.exe"
    uv = _tool("uv")
    if marker.is_file() and marker.read_text(encoding="ascii").strip() == REVISION and _runtime_healthy(python, uv):
        return ENGINE
    fresh_checkout = (not marker.is_file()
                      or marker.read_text(encoding="ascii").strip() != REVISION
                      or not (ENGINE / ".git").is_dir()
                      or not (ENGINE / "pyproject.toml").is_file())
    if fresh_checkout and shutil.disk_usage(ROOT).free < 18 * 1024**3:
        raise RuntimeError("Local music needs at least 18 GB of free disk space.")
    git = _tool("git") if fresh_checkout else ""
    ENGINE.mkdir(parents=True, exist_ok=True)
    if fresh_checkout:
        if not (ENGINE / ".git").is_dir():
            subprocess.run([git, "init", str(ENGINE)], check=True, timeout=60, creationflags=HIDE)
        subprocess.run(
            [git, "-C", str(ENGINE), "fetch", "--depth", "1",
             "https://github.com/ace-step/ACE-Step-1.5.git", REVISION],
            check=True, timeout=900, creationflags=HIDE,
        )
        subprocess.run(
            [git, "-C", str(ENGINE), "checkout", "--force", "FETCH_HEAD"],
            check=True, timeout=120, creationflags=HIDE,
        )
    env = os.environ.copy()
    env["UV_PYTHON_INSTALL_DIR"] = str(ROOT / "Media" / "runtime")
    env["UV_CACHE_DIR"] = str(ROOT / "Media" / "uv-cache")
    subprocess.run(
        [uv, "sync", "--no-dev", "--python", str(python) if python.is_file() else "3.11"],
        cwd=ENGINE, env=env, check=True, timeout=3600, creationflags=HIDE,
    )
    if not _runtime_healthy(python, uv):
        raise RuntimeError("Local music dependencies are incomplete after repair; see Media/music/server.log.")
    marker.write_text(REVISION, encoding="ascii")
    return ENGINE


def start_server() -> bool:
    """Start the local API and report whether its music-planning LM is ready."""
    if healthy():
        return music_lm_ready()
    with _setup_lock():
        if healthy():
            return music_lm_ready()
        install()
    env = os.environ.copy()
    env.update({
        "ACESTEP_API_HOST": "127.0.0.1",
        "ACESTEP_API_PORT": "8001",
        # Let ACE-Step's GPU tiers enable its lyric/music planner on machines
        # that can run it; forcing it off reduced vocal-song quality on 12 GB.
        "ACESTEP_INIT_LLM": "auto",
        "ACESTEP_LM_BACKEND": "pt",
        "HF_HOME": str(ROOT / "Media" / "music" / "huggingface"),
    })
    executable = ENGINE / ".venv" / "Scripts" / "acestep-api.exe"
    if not executable.is_file():
        raise RuntimeError("ACE-Step installed without its API launcher.")
    log_path = ROOT / "Media" / "music" / "server.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [str(executable)], cwd=ENGINE, env=env,
            stdout=log, stderr=subprocess.STDOUT, creationflags=HIDE,
        )
    for _ in range(180):
        if healthy():
            return music_lm_ready()
        if process.poll() is not None:
            break
        time.sleep(2)
    raise RuntimeError("The local music engine did not start. See Media/music/server.log.")


def _generate_music(
    prompt: str, output_path: str, lyrics: str = "", duration_seconds: int = 30,
    instrumental: bool = False, source_path: str = "",
    existing_task_id: str = "", on_task_submitted=None, on_progress=None,
    reference_mode: str = "cover",
) -> dict:
    """Generate or cover music and save it to a new WAV/MP3/FLAC file."""
    if not prompt.strip():
        raise ValueError("Describe the music to create.")
    if not 10 <= duration_seconds <= 600:
        raise ValueError("Music duration must be 10 to 600 seconds.")
    target = Path(output_path).resolve()
    if target.suffix.lower() not in (".wav", ".mp3", ".flac"):
        raise ValueError("Music output must be a WAV, MP3 or FLAC file.")
    if target.exists():
        raise ValueError("Output already exists. Choose a new filename.")
    source = Path(source_path).resolve() if source_path else None
    if source and (not source.is_file() or source.suffix.lower() not in
                   AUDIO_REFERENCES | TRANSCODE_AUDIO_REFERENCES | VIDEO_REFERENCES):
        raise ValueError("Choose an existing WAV, MP3, FLAC, MP4, MOV, M4V, WebM or MKV reference.")
    reference_mode = effective_reference_mode(source, duration_seconds, reference_mode)
    lm_ready = start_server()
    request = {
        "prompt": music_style.enhance(prompt.strip(), instrumental),
        "lyrics": "[Instrumental]" if instrumental else lyrics,
        "audio_format": target.suffix.lower()[1:], "audio_duration": duration_seconds,
        "thinking": bool(lm_ready is True and (not source or reference_mode == "style")),
        "inference_steps": 8, "batch_size": 1,
    }
    tempo = music_style.requested_bpm(prompt)
    if tempo is not None:
        request["bpm"] = tempo
    if source:
        request.update({"task_type": "text2music" if reference_mode == "style" else "cover",
                        "audio_cover_strength": 0.2})
    def submit() -> str:
        submitted = (_upload_reference_media(request, source, "reference_audio")
                     if source and reference_mode == "style" else
                     _upload_reference_media(request, source, "src_audio") if source else
                     _request("/release_task", request))
        task_id = submitted.get("data", {}).get("task_id")
        if submitted.get("code") != 200 or not task_id:
            raise RuntimeError(f"Music generation was not accepted: {submitted.get('error')}")
        if on_task_submitted:
            on_task_submitted(task_id)
        return task_id

    task_id = existing_task_id or submit()
    # First use can include several gigabytes of model downloads. Keep the
    # request alive while that one-time setup finishes on slower connections.
    deadline = time.monotonic() + 7200
    missing_polls = 0
    lost_task_retries = 0
    while time.monotonic() < deadline:
        time.sleep(3)
        result = _request("/query_result", {"task_id_list": [task_id]})
        rows = result.get("data") or []
        row = rows[0] if rows else None
        # ACE-Step returns a placeholder row for an unknown task ID. It has
        # status 0 and result "[]", unlike a queued task, whose result holds
        # a stage/progress record. A server restart can lose either a fresh or
        # recovered submission; neither should leave the card at 35% for hours.
        missing = (row is None or
                   row.get("status") == 0 and row.get("result") in ("[]", [], None)
                   and not row.get("progress_text"))
        if missing:
            missing_polls += 1
            if missing_polls >= 10:
                if lost_task_retries >= 2:
                    raise RuntimeError(
                        "The local music engine repeatedly lost this song. "
                        "Restart the music engine and try again."
                    )
                # The server has not known this ID for 30 seconds. Submit one
                # replacement and persist its ID through on_task_submitted.
                task_id = submit()
                lost_task_retries += 1
                missing_polls = 0
            continue
        missing_polls = 0
        if row.get("status") == 2:
            raise RuntimeError("The local music engine could not finish this song.")
        if row.get("status") != 1:
            if on_progress:
                try:
                    state = row.get("result") or "[]"
                    details = json.loads(state) if isinstance(state, str) else state
                    fraction = float(details[0]["progress"])
                    if 0 <= fraction <= 1:
                        on_progress(fraction)
                except (IndexError, KeyError, TypeError, ValueError, OverflowError):
                    pass  # An older engine may not expose numerical progress.
            continue
        details = row.get("result") or "[]"
        files = json.loads(details) if isinstance(details, str) else details
        if not files or not files[0].get("file"):
            raise RuntimeError("The music engine finished without an audio file.")
        audio_url = files[0]["file"]
        parsed = urlsplit(audio_url)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/v1/audio"):
            raise RuntimeError("The music engine returned an unexpected audio URL.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        try:
            with urlopen(BASE + audio_url, timeout=300) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
            if temporary.stat().st_size == 0:
                raise RuntimeError("The music engine returned an empty file.")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        # A successful render proves the local weights are usable. Future
        # launches need not repeat the background model prefetch.
        if (ENGINE / "checkpoints" / "acestep-v15-turbo").is_dir():
            (ENGINE / ".cale-model-ready").write_text(REVISION, encoding="ascii")
        return {
            "path": str(target), "model": "ACE-Step 1.5",
            "duration_seconds": duration_seconds,
            "source_path": str(source) if source else None,
        }
    raise TimeoutError("Music generation exceeded two hours.")


def generate_music(
    prompt: str, output_path: str, lyrics: str = "", duration_seconds: int = 30,
    instrumental: bool = False, source_path: str = "",
    existing_task_id: str = "", on_task_submitted=None, on_progress=None,
    reference_mode: str = "cover",
) -> dict:
    """Reserve GPU space, then generate or resume a local music request."""
    with _music_gpu_lease():
        return _generate_music(
            prompt, output_path, lyrics, duration_seconds, instrumental,
            source_path, existing_task_id, on_task_submitted, on_progress,
            reference_mode,
        )


def _job_path(job_id: str) -> Path:
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise ValueError("Invalid local music job ID.")
    return JOBS / (job_id + ".json")


def _write_job(job_id: str, info: dict) -> None:
    path = _job_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A worker and the supervisor can update the same job at nearly the same
    # time. They must not overwrite one shared .tmp file. Windows can also
    # briefly lock a JSON file while a reader or scanner has it open.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=job_id + "-", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(info, stream, ensure_ascii=False)
        for attempt in range(7):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 6:
                    raise
                time.sleep(min(0.05 * 2**attempt, 1.0))
    finally:
        temporary.unlink(missing_ok=True)


def music_job_status(job_id: str) -> dict:
    """Read a durable music job without waiting for model setup or generation."""
    path = _job_path(job_id)
    if not path.is_file():
        raise ValueError("Music job not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def project_for_output(output_path: str) -> str:
    """Attribute an agent-started render to its registered Workbench task."""
    database = ROOT / "LongTasks/projects.db"
    if not database.is_file():
        return ""
    target = Path(output_path).resolve()
    try:
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True,
                             timeout=2) as connection:
            rows = connection.execute("SELECT id,folder FROM projects").fetchall()
        matches = [(len(str(Path(folder).resolve())), task_id) for task_id, folder in rows
                   if folder and re.fullmatch(r"[a-f0-9]{12}", str(task_id))
                   and target.is_relative_to(Path(folder).resolve())]
        return max(matches, default=(0, ""))[1]
    except (OSError, sqlite3.Error, ValueError):
        return ""


def project_jobs(task_id: str, folder: str | Path) -> list[dict]:
    """Find durable music jobs belonging to one task, including older records."""
    if not re.fullmatch(r"[a-f0-9]{12}", task_id):
        return []
    project = Path(folder).resolve()
    found = []
    for path in JOBS.glob("*.json"):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            output_path = info.get("output_path")
            owned_path = bool(output_path) and Path(str(output_path)).resolve().is_relative_to(project)
            if info.get("task_id") == task_id or owned_path:
                found.append(info)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(found, key=lambda job: float(job.get("created") or 0))


def hold_project_jobs(task_id: str, folder: str | Path, *, paused: bool) -> int:
    """Pause or stop all active renders when their parent task is controlled."""
    count = 0
    for job in project_jobs(task_id, folder):
        if job.get("status") in ("queued", "working"):
            hold_music_job(job["job_id"], paused=paused)
            count += 1
    return count


def resume_project_jobs(task_id: str, folder: str | Path) -> int:
    """Reattach renders the task was waiting for before its worker resumes."""
    count = 0
    for job in project_jobs(task_id, folder):
        if job.get("status") == "paused":
            resume_music_job(job["job_id"])
            count += 1
    return count


def hold_music_job(job_id: str, *, paused: bool = False) -> dict:
    """Retire this job's worker without touching other local music jobs."""
    import psutil

    info = music_job_status(job_id)
    if info.get("status") in ("complete", "error", "stopped", "paused"):
        return info
    if _worker_running(job_id, info):
        try:
            worker = psutil.Process(int(info["worker_pid"]))
            worker.terminate()
            worker.wait(timeout=10)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        except psutil.TimeoutExpired as exc:
            raise RuntimeError("The music worker has not stopped yet. Try again shortly.") from exc
    # Re-read after the worker exits so a server task ID saved meanwhile survives.
    info = music_job_status(job_id)
    if info.get("status") == "complete":
        return info
    state = "paused" if paused else "stopped"
    stopped = {**info, "status": state,
               "current": "Paused; local render may finish in the engine" if paused else "Stopped by user",
               "worker_pid": None, "finished": time.time()}
    _write_job(job_id, stopped)
    return stopped


def stop_music_job(job_id: str) -> dict:
    """Stop one durable music job."""
    return hold_music_job(job_id)


def pause_music_job(job_id: str) -> dict:
    """Pause Cale's worker; the ACE-Step server may finish its current render."""
    return hold_music_job(job_id, paused=True)


def resume_music_job(job_id: str) -> dict:
    """Reattach to an in-flight ACE-Step task, or start one if none exists."""
    info = music_job_status(job_id)
    if info.get("status") != "paused":
        raise ValueError("Only a paused music job can be continued.")
    resumed = {**info, "status": "queued", "current": "Resuming local music",
               "worker_pid": None, "created": time.time()}
    _write_job(job_id, resumed)
    _launch_job(job_id)
    return resumed


def resume_failed_sdk_job(job_id: str) -> dict:
    """Retry one saved song after its model SDK import has been repaired."""
    info = music_job_status(job_id)
    reason = str(info.get("current", ""))
    if (info.get("status") != "error" or "response_format_json_object" not in reason
            or int(info.get("sdk_retries", 0)) >= 1):
        raise ValueError("This music job is not eligible for an SDK retry.")
    resumed = {
        **info, "status": "queued", "current": "Resuming music after tool repair",
        "worker_pid": None, "created": time.time(),
        "sdk_retries": int(info.get("sdk_retries", 0)) + 1,
    }
    _write_job(job_id, resumed)
    try:
        _launch_job(job_id)
    except OSError as exc:
        _write_job(job_id, {**resumed, "status": "error", "current": str(exc)[:300]})
        raise
    return resumed


def audio_file_ok(path: str | Path) -> bool:
    """Decode the whole recording and reject files with no audible signal."""
    import imageio_ffmpeg

    source = Path(path)
    if not source.is_file() or source.stat().st_size == 0:
        return False
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-xerror",
             "-i", str(source), "-map", "0:a:0", "-af", "volumedetect",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=90, creationflags=HIDE,
        )
        if result.returncode:
            return False
        volume = re.search(r"max_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s+dB", result.stderr)
        return bool(volume and float(volume.group(1)) >= -60)
    except (OSError, subprocess.TimeoutExpired):
        return False


def audio_duration_seconds(path: str | Path) -> float | None:
    """Read a local audio file's duration without decoding or changing it."""
    import imageio_ffmpeg

    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=15, creationflags=HIDE,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def effective_reference_mode(source: Path | None, duration_seconds: int,
                             reference_mode: str) -> str:
    """A longer requested version needs style conditioning, not fixed-length cover."""
    if reference_mode not in ("cover", "style") or (reference_mode == "style" and not source):
        raise ValueError("Choose cover or style with a source audio file")
    if source and reference_mode == "cover":
        source_seconds = audio_duration_seconds(source)
        if source_seconds is not None and abs(source_seconds - duration_seconds) > 5:
            return "style"
    return reference_mode


def compose_lyrics(prompt: str, duration_seconds: int) -> str:
    """Write lyrics locally before rendering, escalating after weak drafts.

    ACE-Step can produce a mostly instrumental track when its lyrics field is
    empty. This bounded call runs in the durable music worker, not the UI.
    """
    import cloud_ai
    import routing_mode
    import usage_history

    sparse = music_style.sparse_vocal_style(prompt)
    target_words = (max(20, min(160, round(duration_seconds * 0.45))) if sparse
                    else max(20, min(450, round(duration_seconds * 1.6))))
    minimum_words = (max(12, round(duration_seconds * 0.25)) if sparse
                     else max(20, round(duration_seconds * 0.7)))
    failures = []
    for attempt in range(3):
        try:
            # The main model is slower but can rescue a song when the fast
            # writer twice returns truncated or unusable lyrics.
            model = cloud_ai.LOCAL_MODEL if attempt == 2 else cloud_ai.FAST_MODEL
            payload = {"model": model, "stream": False, "temperature": 0.7,
                 "max_tokens": min(2048, max(1024, target_words * (4 + attempt))),
                 "reasoning_effort": "none",
                 "messages": [
                     {"role": "system", "content": (
                         "Write original English song lyrics for a local music generator. "
                         f"Aim for about {target_words} words. "
                         + ("Use [Hook] and [Instrumental Drop] labels. Write short, "
                            "repeatable vocal phrases instead of long verses. Use any "
                            "requested hook words. Leave room for instrumental drops. "
                            if sparse else
                            "Include [Verse] and [Chorus] labels. Begin with a sung or "
                            "rapped line and keep words going through most of the track. ")
                         + "Return lyrics only, no explanation. "
                         + (f"Use short lines, write at least {minimum_words} words, "
                            "and finish the whole song before your response ends."
                            if attempt else "")
                     )},
                     {"role": "user", "content": prompt[:3000]},
                 ]}
            task_id = usage_history.CURRENT_TASK.get()
            cloud_scope = routing_mode.load_scope(cloud_ai.STATE) in ("cloud_only", "free_cloud_only")
            if cloud_scope and cloud_ai.task_route(task_id) != "local_only":
                try:
                    result = cloud_ai.Router().complete(
                        {**payload, "model": cloud_ai.LOCAL_MODEL},
                        task_id=task_id,
                    )
                except Exception:
                    # Local lyrics are part of the offline music tool. Cloud
                    # quota or connectivity must not prevent a song render.
                    result = cloud_ai.post(
                        "http://127.0.0.1:11434/v1/chat/completions",
                        payload, timeout=120,
                    )
            else:
                result = cloud_ai.post(
                    "http://127.0.0.1:11434/v1/chat/completions",
                    payload, timeout=120,
                )
            choice = result["choices"][0]
            lyrics = str(choice["message"].get("content") or "").strip()
        except (OSError, RuntimeError, KeyError, IndexError, ValueError, TypeError) as exc:
            failures.append(f"draft {attempt + 1}: {type(exc).__name__}")
            continue
        lyrics = re.sub(r"^```[^\n]*\n|\n```$", "", lyrics).strip()
        words = len(lyrics.split())
        finish = choice.get("finish_reason")
        if minimum_words <= words <= 650 and (finish != "length" or words >= target_words):
            return lyrics
        failures.append(f"draft {attempt + 1}: {words} words, finish={finish or 'unknown'}")
    raise RuntimeError("The local lyric writer could not finish a usable song ("
                       + "; ".join(failures) + ").")


def vocal_lines(lyrics: str) -> str:
    """Exclude instrumental stage directions from words expected to be sung."""
    lines = []
    singing = True
    for line in lyrics.splitlines():
        section = re.match(r"\s*\[([^]]+)\]\s*(.*)", line)
        if section:
            singing = not bool(re.search(
                r"\b(?:instrumental|break|drop|solo)\b", section.group(1), re.I,
            ))
            line = section.group(2)
        if singing and line.strip():
            lines.append(line)
    return "\n".join(lines)


def lyric_match(transcript: str, lyrics: str, *, vocal_sections_only: bool = False) -> tuple[int, int]:
    """Count distinct authored words heard in the song, ignoring stage tags."""
    def terms(value: str) -> set[str]:
        return {
            word for word in re.findall(r"\w+", value.casefold())
            if len(word) >= 3 and word not in {
                "and", "are", "but", "for", "from", "had", "has", "her",
                "his", "our", "she", "the", "them", "then", "there",
                "they", "this", "was", "were", "with", "you", "your",
            }
        }

    had_lyrics = bool(lyrics.strip())
    if vocal_sections_only:
        lyrics = vocal_lines(lyrics)
    if not had_lyrics:
        return 0, 0  # No authored words exist to compare with the transcript.
    authored = terms(re.sub(r"\[[^]]+\]|\([^)]*\)", " ", lyrics))
    if len(authored) < 4:
        return 0, 4  # Repeated syllables cannot establish a real lyric match.
    required = min(15, max(4, round(len(authored) * 0.2)))
    return len(authored & terms(transcript)), required


def sparse_vocal_style(prompt: str) -> bool:
    """Detect dance tracks asking for short, repeated hooks rather than verses."""
    return music_style.sparse_vocal_style(prompt)


def vocal_evidence(path: str | Path, duration_seconds: int, lyrics: str = "",
                   *, sparse_vocals: bool = False) -> dict:
    """Check vocals against the requested style and recognizable lyric words."""
    python = ROOT / "Media/python/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("The offline speech runtime is missing; vocal music cannot be verified.")
    request = json.dumps({
        "action": "transcribe_audio", "path": str(Path(path).resolve()), "music": True,
    })
    result = subprocess.run(
        [str(python), str(ROOT / "media_worker.py")], input=request,
        capture_output=True, text=True, timeout=180, creationflags=HIDE,
    )
    if result.returncode:
        raise RuntimeError("The offline vocal check failed: " + result.stdout[-300:])
    transcript = json.loads(result.stdout)
    if transcript.get("error"):
        raise RuntimeError("The offline vocal check failed: " + str(transcript["error"])[:200])
    segments = transcript.get("segments") or []
    words = len(str(transcript.get("text") or "").split())
    final_second = max((float(row.get("end", 0)) for row in segments), default=0)
    # A few sung lines near the beginning and end are not a lead vocal. The
    # previous 0.35 words/second floor accepted a 160-second song with only
    # 118 recognized words that the listener found mostly instrumental.
    # Require enough words for verses and choruses across the song, while
    # allowing instrumental breaks and bass drops between them.
    required_words = max(5, round(duration_seconds * (0.3 if sparse_vocals else 0.85)))
    matched_terms, required_terms = lyric_match(
        str(transcript.get("text") or ""), lyrics,
        vocal_sections_only=sparse_vocals,
    )
    return {"recognized_words": words, "last_vocal_second": round(final_second, 1),
            "required_words": required_words,
            "matched_lyric_terms": matched_terms, "required_lyric_terms": required_terms,
            "passed": (words >= required_words and final_second >= duration_seconds * 0.7
                       and matched_terms >= required_terms),
            "segments": segments}


def _launch_job(job_id: str) -> None:
    python = ROOT / "Toolbox" / "python" / "Scripts" / "python.exe"
    if not python.is_file():
        raise RuntimeError("The local Toolbox runtime is missing.")
    log_path = JOBS / (job_id + ".log")
    with log_path.open("ab") as log:
        subprocess.Popen(
            [str(python), str(ROOT / "music_backend.py"), "--run-job", job_id],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            creationflags=HIDE,
        )


def _worker_running(job_id: str, info: dict) -> bool:
    """Check the exact worker command, since Windows can reuse process IDs."""
    import psutil

    try:
        pid = int(info.get("worker_pid") or 0)
        if pid <= 0:
            return False
        command = psutil.Process(pid).cmdline()
        return "--run-job" in command and job_id in command and any(
            str(item).endswith("music_backend.py") for item in command
        )
    except (psutil.NoSuchProcess, psutil.ZombieProcess, ValueError, TypeError):
        return False
    except psutil.AccessDenied:
        return True  # Never launch a duplicate when ownership is uncertain.


def repair_saved_music_video(job_id: str, *, retry: bool = False, can_queue=None) -> bool:
    """Keep transient filesystem/record failures retryable without new audio."""
    try:
        return _queue_saved_music_video(job_id, retry=retry, can_queue=can_queue)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _queue_saved_music_video(job_id: str, *, retry: bool = False, can_queue=None) -> bool:
    """Queue only a damaged branded export, keeping the original audio pinned.

    A nonblocking per-job OS lock prevents UI/supervisor races from launching
    duplicate repairs. Failed process launches leave the durable queue intact
    for normal worker recovery. A persistent failure gets at most three repairs.
    """
    import msvcrt

    import song_video

    path = _job_path(job_id)
    if not path.is_file():
        return False
    with path.with_suffix(".video-repair.lock").open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        try:
            info = music_job_status(job_id)
            retry_failed = (retry and info.get("status") == "error"
                            and (info.get("video_repair_only") is True
                                 or (info.get("make_video") and int(info.get("video_retries", 0)) > 0)))
            if (info.get("status") != "complete" and not retry_failed) or _worker_running(job_id, info):
                return False
            result = info.get("result") or {}
            video = result.get("video") or {}
            if retry_failed and not video:
                audio_path = Path(info["output_path"])
                video = {"path": str(audio_path.with_name(audio_path.stem + "-facebook.mp4")), "kind": "branded_song_video"}
            if video.get("kind") != "branded_song_video" or (song_video.branded_export_matches(video) and not retry_failed):
                return False
            source = Path(info["output_path"]).resolve()
            if not source.is_file() or source != Path(result.get("path", "")).resolve():
                return False
            if Path(video.get("path", "")).resolve().parent != source.parent:
                return False
            digest = song_video._digest(source)
            receipt = song_video._saved_record(Path(video["path"])) or {}
            expected = video.get("source_sha256") or receipt.get("source_sha256")
            if expected and expected != digest:
                return False  # Changed audio needs its own review, not stale vocal evidence.
            attempts = 1 if retry else int(info.get("completed_video_repairs", 0)) + 1
            if attempts > 3:
                return False
            if (can_queue is not None and not can_queue()) or music_job_status(job_id) != info:
                return False  # Pause/Stop or another writer won during hashing.
            queued = {**info, "status": "queued", "current": "Repairing saved song video; audio preserved",
                      "progress": 88, "created": time.time(), "worker_pid": None,
                      "make_video": True, "video_repair_only": True, "video_repair_source_sha256": digest,
                      "completed_video_repairs": attempts, "recoveries": 0, "video_retries": 0}
            queued.pop("finished", None)
            _write_job(job_id, queued)
            try:
                _launch_job(job_id)
            except (OSError, RuntimeError):
                pass  # Normal recovery retries this queued job without regenerating audio.
            return True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _retry_captioned_video(job_id: str, info: dict, error: Exception) -> None:
    """Keep the saved audio and retry only the video after a transient failure."""
    attempts = int(info.get("video_retries", 0)) + 1
    retrying = attempts < 3
    _write_job(job_id, {
        **info, "status": "queued" if retrying else "error",
        "current": ("Retrying the song video" if retrying
                    else "The audio is saved, but its video failed after three tries: "
                    + str(error)[:180]),
        "video_retries": attempts, "video_error": str(error)[:300],
        "worker_pid": None, "created": time.time(),
        **({} if retrying else {"finished": time.time()}),
    })


def recover_music_jobs() -> None:
    """Reattach dead jobs without doing transcription or video work in the supervisor."""
    if not JOBS.is_dir():
        return
    for path in JOBS.glob("*.json"):
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            job_id = path.stem
            _job_path(job_id)
            if info.get("job_id") != job_id or info.get("status") not in ("queued", "working"):
                continue
            if info["status"] == "queued" and time.time() - float(info.get("created", 0)) < 30:
                continue
            if _worker_running(job_id, info):
                continue
            output = Path(info["output_path"])
            if output.is_file() and not audio_file_ok(output):
                # Preserve a damaged render for diagnosis. The worker must not
                # overwrite it or claim that the song passed verification.
                _write_job(job_id, {
                    **info, "status": "error",
                    "current": "The saved song could not be played. Try a new render.",
                    "finished": time.time(),
                })
                continue
            attempts = int(info.get("recoveries", 0)) + 1
            if attempts > 3:
                _write_job(job_id, {
                    **info, "status": "error", "current": "Music stopped after repeated recovery attempts.",
                    "finished": time.time(),
                })
                continue
            resumed = {
                **info, "status": "queued",
                "current": ("Finishing saved music" if output.is_file()
                            else "Resuming local music"),
                "progress": ((70 if info.get("make_video") else 90)
                             if output.is_file() else 10),
                "recoveries": attempts,
                "worker_pid": None, "created": time.time(),
            }
            _write_job(job_id, resumed)
            _launch_job(job_id)
        except (OSError, ValueError, KeyError, RuntimeError):
            continue  # One damaged record must not prevent recovery of others.


def start_music_job(
    prompt: str, output_path: str, lyrics: str = "", duration_seconds: int = 30,
    instrumental: bool = False, source_path: str = "", task_id: str = "",
    vocal_retry: int = 0,
    reference_mode: str = "cover",
    make_video: bool = True,
) -> dict:
    """Launch a persistent local job; the agent can continue while it runs."""
    if not prompt.strip() or not 10 <= duration_seconds <= 600:
        raise ValueError("Describe 10 to 600 seconds of music to create.")
    # Tool-using models have selected instrumental=True for briefs that
    # explicitly requested a singer or rapper. Honor the request itself so a
    # wrong tool argument cannot silently remove the entire vocal track.
    if instrumental and music_style.unambiguous_vocal_song(prompt, lyrics):
        instrumental = False
    target = Path(output_path).resolve()
    if target.suffix.lower() not in (".wav", ".mp3", ".flac") or target.exists():
        raise ValueError("Choose a new WAV, MP3 or FLAC output file.")
    if source_path and (not Path(source_path).resolve().is_file() or
                        Path(source_path).suffix.lower() not in
                        AUDIO_REFERENCES | TRANSCODE_AUDIO_REFERENCES | VIDEO_REFERENCES):
        raise ValueError("Choose an existing audio or video reference file.")
    reference_mode = effective_reference_mode(
        Path(source_path).resolve() if source_path else None,
        duration_seconds, reference_mode,
    )
    job_id = uuid.uuid4().hex
    import task_eta

    estimate = task_eta.music_estimate_seconds(
        JOBS, duration_seconds, bool(instrumental), source_path, reference_mode,
        bool(make_video),
    )
    started = time.time()
    info = {
        "job_id": job_id, "status": "queued", "current": "Starting local music", "progress": 5,
        "prompt": prompt, "output_path": str(target), "lyrics": lyrics,
        "duration_seconds": duration_seconds, "instrumental": bool(instrumental),
        "source_path": source_path, "reference_mode": reference_mode,
        "make_video": bool(make_video),
        "task_id": task_id, "created": started,
        "requested_at": started, "estimated_seconds": estimate,
        "vocal_retry": int(vocal_retry),
    }
    _write_job(job_id, info)
    try:
        _launch_job(job_id)
    except OSError as exc:
        _write_job(job_id, {**info, "status": "error", "current": str(exc)[:300]})
        raise
    return {"job_id": job_id, "status": "queued", "output_path": str(target)}


def _run_job(job_id: str) -> None:
    info = music_job_status(job_id)
    if info.get("status") not in ("queued", "working"):
        return
    # Also repair queued jobs written by an older version before rendering.
    repair_only = info.get("video_repair_only") is True
    if not repair_only and info.get("instrumental") and music_style.unambiguous_vocal_song(
            info.get("prompt", ""), info.get("lyrics", "")):
        info["instrumental"] = False
        _write_job(job_id, info)
    working = {
        **info, "status": "working",
        "current": "Repairing saved song video; audio preserved" if repair_only else "Preparing music locally",
        "progress": 88 if repair_only else 12, "worker_pid": os.getpid(),
    }
    _write_job(job_id, working)

    def remember_task(task_id: str) -> None:
        working["server_task_id"] = task_id
        _write_job(job_id, working)

    def remember_progress(fraction: float) -> None:
        # The engine's 0..1 fraction covers audio generation only. An ordinary
        # song needs room only for its final vocal check. Explicit video jobs
        # retain the larger video-rendering stage after audio is complete.
        ceiling = 70 if info.get("make_video") else 90
        percent = min(ceiling, max(35, 35 + round(fraction * (ceiling - 35))))
        if percent > working["progress"]:
            working["progress"] = percent
            working["current"] = f"Generating music locally · audio {round(fraction * 100)}%"
            _write_job(job_id, working)

    import usage_history

    attribution = usage_history.CURRENT_TASK.set(info.get("task_id", ""))
    try:
        if repair_only:
            import song_video
            pinned = info.get("video_repair_source_sha256")
            audio = Path(info["output_path"])
            if not pinned or not audio.is_file() or song_video._digest(audio) != pinned:
                raise ValueError("The saved audio changed during video repair; no audio was regenerated.")
        lyrics = info["lyrics"]
        needs_vocals = (not info["source_path"] or info.get("reference_mode") == "style") and music_style.requires_sustained_vocals(
            info["prompt"], info.get("lyrics", ""), info["instrumental"]
        )
        if needs_vocals and not repair_only and not lyrics.strip():
            working["current"] = "Writing lyrics locally"
            working["progress"] = 20
            _write_job(job_id, working)
            lyrics = compose_lyrics(info["prompt"], info["duration_seconds"])
            working["lyrics"] = lyrics
            _write_job(job_id, working)
            lyric_file = Path(info["output_path"]).with_suffix(".lyrics.txt")
            if not lyric_file.exists():
                lyric_file.parent.mkdir(parents=True, exist_ok=True)
                lyric_file.write_text(lyrics + "\n", encoding="utf-8")
        saved_audio = Path(info["output_path"])
        if saved_audio.is_file() and audio_file_ok(saved_audio):
            result = info.get("result") or {"path": str(saved_audio), "model": "ACE-Step 1.5"}
        else:
            if repair_only:
                raise ValueError("The saved audio could not be decoded; video repair cannot regenerate it.")
            working["current"] = "Generating music locally"
            working["progress"] = 35
            _write_job(job_id, working)
            result = generate_music(
                info["prompt"], info["output_path"], lyrics,
                info["duration_seconds"], info["instrumental"], info["source_path"],
                info.get("server_task_id", ""), remember_task, remember_progress,
                info.get("reference_mode", "cover"),
            )
        working["result"] = result
        _write_job(job_id, working)
        if not repair_only and info.get("reference_mode") == "style":
            rendered_seconds = audio_duration_seconds(result["path"])
            if (rendered_seconds is None or
                    abs(rendered_seconds - info["duration_seconds"]) >
                    max(5, info["duration_seconds"] * 0.05)):
                raise RuntimeError(
                    "The music engine returned the source clip's length instead of "
                    "the requested song length. The audio is saved for review."
                )
        check = info.get("vocal_check")
        if needs_vocals and not repair_only:
            if not check or not check.get("passed"):
                working["current"] = "Checking the vocals"
                working["progress"] = 75 if info.get("make_video") else 94
                _write_job(job_id, working)
                if sparse_vocal_style(info["prompt"]):
                    check = vocal_evidence(result["path"], info["duration_seconds"], lyrics,
                                           sparse_vocals=True)
                else:
                    check = vocal_evidence(result["path"], info["duration_seconds"], lyrics)
            if not check["passed"]:
                raise RuntimeError(
                    "The song played, but sustained vocals were not detected. "
                    "The audio is saved; retry or provide your own lyrics."
                )
        working["vocal_check"] = check
        video = None
        if info.get("make_video"):
            import song_video

            working["current"] = "Making your Cale Media song video"
            working["progress"] = 88
            _write_job(job_id, working)
            try:
                video = song_video.render_branded(
                    result["path"], segments=(check or {}).get("segments", []),
                    lyrics=lyrics, title=info["prompt"],
                    instrumental=bool(info["instrumental"]),
                    duration=info["duration_seconds"],
                    **({"expected_source_sha256": info["video_repair_source_sha256"]} if repair_only else {}),
                )
            except Exception as exc:
                _retry_captioned_video(job_id, working, exc)
                return
        _write_job(job_id, {
            **working, "status": "complete",
            "current": "Music and video are ready" if video else "Music is ready",
            "progress": 100,
            "result": {**result, **({"video": video} if video else {})},
            "vocal_check": check,
            "video_repair_only": False,
            "finished": time.time(),
        })
    except Exception as exc:
        # Keep the full local traceback beside the song. A short task-card
        # error cannot identify which of Cale's Python runtimes failed.
        stage = str(working.get("current", "Preparing music locally"))
        diagnostic_path = ""
        try:
            diagnostic = Path(info["output_path"]).parent / ".local-ai" / f"music-error-{job_id}.log"
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(
                f"Music job: {job_id}\nWorker Python: {sys.executable}\n"
                f"Stage: {stage}\n\n{traceback.format_exc()[-32000:]}",
                encoding="utf-8",
            )
            diagnostic_path = str(diagnostic)
        except OSError:
            pass  # A full disk must not hide the original render error.
        _write_job(job_id, {
            **working, "status": "error", "current": str(exc)[:300],
            "error_stage": stage, "diagnostic_path": diagnostic_path,
            "finished": time.time(),
        })
    finally:
        usage_history.CURRENT_TASK.reset(attribution)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--run-job":
    _run_job(sys.argv[2])
