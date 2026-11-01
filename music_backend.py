"""Install and use ACE-Step 1.5 as a loopback-only local music tool.

ACE-Step runs in its own Python environment because its audio dependencies do
not share the workbench's media runtime. The first music request can take a
while as the runtime and model download. No account or cloud inference is used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "Media" / "music" / "ACE-Step-1.5"
REVISION = "ca1e85fe9430179831e6bc6be790c332190a3866"
BASE = "http://127.0.0.1:8001"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
JOBS = ROOT / "LongTasks" / "music-jobs"


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
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def healthy() -> bool:
    try:
        result = _request("/health", timeout=2)
        return result.get("data", {}).get("service") == "ACE-Step API"
    except (OSError, ValueError, KeyError):
        return False


def install() -> Path:
    """Install a pinned upstream revision into the workbench's local data."""
    marker = ENGINE / ".cale-installed"
    if marker.is_file() and (ENGINE / ".venv" / "Scripts" / "python.exe").is_file():
        return ENGINE
    if shutil.disk_usage(ROOT).free < 18 * 1024**3:
        raise RuntimeError("Local music needs at least 18 GB of free disk space.")
    git = _tool("git")
    uv = _tool("uv")
    ENGINE.mkdir(parents=True, exist_ok=True)
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
        [uv, "sync", "--no-dev", "--python", "3.11"],
        cwd=ENGINE, env=env, check=True, timeout=3600, creationflags=HIDE,
    )
    marker.write_text(REVISION, encoding="ascii")
    return ENGINE


def start_server() -> None:
    """Start the local API on demand, using its own model/runtime directory."""
    if healthy():
        return
    with _setup_lock():
        if healthy():
            return
        install()
    env = os.environ.copy()
    env.update({
        "ACESTEP_API_HOST": "127.0.0.1",
        "ACESTEP_API_PORT": "8001",
        "ACESTEP_INIT_LLM": "false",
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
            return
        if process.poll() is not None:
            break
        time.sleep(2)
    raise RuntimeError("The local music engine did not start. See Media/music/server.log.")


def _generate_music(
    prompt: str, output_path: str, lyrics: str = "", duration_seconds: int = 30,
    instrumental: bool = False, source_path: str = "",
    existing_task_id: str = "", on_task_submitted=None,
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
    if source and (not source.is_file() or source.suffix.lower() not in (".wav", ".mp3", ".flac")):
        raise ValueError("The cover source must be an existing WAV, MP3 or FLAC file.")
    start_server()
    request = {
        "prompt": prompt.strip(), "lyrics": "[Instrumental]" if instrumental else lyrics,
        "audio_format": target.suffix.lower()[1:], "audio_duration": duration_seconds,
        "thinking": False, "inference_steps": 8, "batch_size": 1,
    }
    if source:
        request.update({"task_type": "cover", "src_audio_path": str(source)})
    def submit() -> str:
        submitted = _request("/release_task", request)
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
    while time.monotonic() < deadline:
        time.sleep(3)
        result = _request("/query_result", {"task_id_list": [task_id]})
        rows = result.get("data") or []
        if not rows:
            if existing_task_id:
                missing_polls += 1
                if missing_polls >= 10:
                    # The API server restarted and forgot the old in-memory task.
                    # Only then submit a replacement; a live task is never doubled.
                    task_id = submit()
                    existing_task_id = ""
            continue
        missing_polls = 0
        row = rows[0]
        if row.get("status") == 2:
            raise RuntimeError("The local music engine could not finish this song.")
        if row.get("status") != 1:
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
    existing_task_id: str = "", on_task_submitted=None,
) -> dict:
    """Reserve GPU space, then generate or resume a local music request."""
    with _music_gpu_lease():
        return _generate_music(
            prompt, output_path, lyrics, duration_seconds, instrumental,
            source_path, existing_task_id, on_task_submitted,
        )


def _job_path(job_id: str) -> Path:
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise ValueError("Invalid local music job ID.")
    return JOBS / (job_id + ".json")


def _write_job(job_id: str, info: dict) -> None:
    path = _job_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def music_job_status(job_id: str) -> dict:
    """Read a durable music job without waiting for model setup or generation."""
    path = _job_path(job_id)
    if not path.is_file():
        raise ValueError("Music job not found.")
    return json.loads(path.read_text(encoding="utf-8"))


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


def recover_music_jobs() -> None:
    """Resume queued or working jobs whose worker died during a reboot."""
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
            if output.is_file() and output.stat().st_size:
                _write_job(job_id, {
                    **info, "status": "complete", "current": "Music is ready",
                    "result": {"path": str(output), "model": "ACE-Step 1.5"},
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
                **info, "status": "queued", "current": "Resuming local music",
                "recoveries": attempts, "worker_pid": None, "created": time.time(),
            }
            _write_job(job_id, resumed)
            _launch_job(job_id)
        except (OSError, ValueError, KeyError, RuntimeError):
            continue  # One damaged record must not prevent recovery of others.


def start_music_job(
    prompt: str, output_path: str, lyrics: str = "", duration_seconds: int = 30,
    instrumental: bool = False, source_path: str = "",
) -> dict:
    """Launch a persistent local job; the agent can continue while it runs."""
    if not prompt.strip() or not 10 <= duration_seconds <= 600:
        raise ValueError("Describe 10 to 600 seconds of music to create.")
    target = Path(output_path).resolve()
    if target.suffix.lower() not in (".wav", ".mp3", ".flac") or target.exists():
        raise ValueError("Choose a new WAV, MP3 or FLAC output file.")
    if source_path and not Path(source_path).resolve().is_file():
        raise ValueError("The cover source file does not exist.")
    job_id = uuid.uuid4().hex
    info = {
        "job_id": job_id, "status": "queued", "current": "Starting local music",
        "prompt": prompt, "output_path": str(target), "lyrics": lyrics,
        "duration_seconds": duration_seconds, "instrumental": bool(instrumental),
        "source_path": source_path, "created": time.time(),
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
    working = {
        **info, "status": "working", "current": "Generating music locally",
        "worker_pid": os.getpid(),
    }
    _write_job(job_id, working)

    def remember_task(task_id: str) -> None:
        _write_job(job_id, {**working, "server_task_id": task_id})

    try:
        result = generate_music(
            info["prompt"], info["output_path"], info["lyrics"],
            info["duration_seconds"], info["instrumental"], info["source_path"],
            info.get("server_task_id", ""), remember_task,
        )
        _write_job(job_id, {
            **info, "status": "complete", "current": "Music is ready",
            "result": result, "finished": time.time(),
        })
    except Exception as exc:
        _write_job(job_id, {
            **info, "status": "error", "current": str(exc)[:300],
            "finished": time.time(),
        })


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--run-job":
    _run_job(sys.argv[2])
