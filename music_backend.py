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
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "Media" / "music" / "ACE-Step-1.5"
REVISION = "ca1e85fe9430179831e6bc6be790c332190a3866"
BASE = "http://127.0.0.1:8001"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def generate_music(
    prompt: str, output_path: str, lyrics: str = "", duration_seconds: int = 30,
    instrumental: bool = False, source_path: str = "",
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
        "thinking": False, "inference_steps": 8,
    }
    if source:
        request.update({"task_type": "cover", "src_audio_path": str(source)})
    submitted = _request("/release_task", request)
    task_id = submitted.get("data", {}).get("task_id")
    if submitted.get("code") != 200 or not task_id:
        raise RuntimeError(f"Music generation was not accepted: {submitted.get('error')}")
    # First use can include several gigabytes of model downloads. Keep the
    # request alive while that one-time setup finishes on slower connections.
    deadline = time.monotonic() + 7200
    while time.monotonic() < deadline:
        time.sleep(3)
        result = _request("/query_result", {"task_id_list": [task_id]})
        rows = result.get("data") or []
        if not rows:
            continue
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
        return {
            "path": str(target), "model": "ACE-Step 1.5",
            "duration_seconds": duration_seconds,
            "source_path": str(source) if source else None,
        }
    raise TimeoutError("Music generation exceeded two hours.")
