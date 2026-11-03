"""Durable, local Wan 2.2 video jobs on a dedicated loopback ComfyUI runtime.

OpenVideo Studio installs pinned, checksum-verified Wan model files on first use.
This module never sends prompts or input images to a cloud generation service.
"""

from __future__ import annotations

import json
import hashlib
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent
BASE = ROOT / "Media" / "video"
SOURCE = BASE / "openvideo-studio"
RUNTIME = BASE / "runtime"
JOBS = BASE / "jobs"
UPSTREAM = "https://github.com/melayyoub/openvideo-studio.git"
COMMIT = "2dc15aa7656a111fe4b956ed7f4abbc5edc28c60"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FINISHED = {"complete", "failed"}
MODEL_HASHES = {
    "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors": "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
    "vae/wan2.2_vae.safetensors": "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
    "diffusion_models/wan2.2_ti2v_5B_fp16.safetensors": "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
}


def _git_executable(root: Path = ROOT) -> str:
    """Use the Git that first-run setup installs, even on a bare Windows PC."""
    candidates = sorted((Path(root) / "data" / "tools").glob("git-*/cmd/git.exe"),
                        reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    system = shutil.which("git")
    if system:
        return system
    raise RuntimeError("The managed Git tool is missing; run Cale setup to repair it.")


def _job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid video job ID.")
    return JOBS / f"{job_id}.json"


def _write(job_id: str, **changes) -> dict:
    path = _job_path(job_id)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"job_id": job_id}
    data.update(changes, updated_at=time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return data


def _existing_parent(path: Path) -> Path:
    """Find the drive that will hold a new output without creating folders yet."""
    candidate = path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.is_dir():
        raise ValueError("Choose an output location on an available drive.")
    return candidate


def _hardware_ready(output: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Automatic Wan video setup currently supports Windows with an NVIDIA GPU.")
    # The 35 GB allowance covers the first model/runtime download. Once those
    # verified files exist, require only scratch space for a short render.
    required_gb = 5 if _verified_runtime() else 35
    if shutil.disk_usage(BASE.parent).free < required_gb * 1024 ** 3:
        raise RuntimeError(f"Video generation needs {required_gb} GB free on Cale's runtime drive.")
    try:
        output_free = shutil.disk_usage(_existing_parent(output)).free
    except OSError as exc:
        raise ValueError("Choose an output location on an available drive.") from exc
    if output_free < 1 * 1024 ** 3:
        raise RuntimeError("The video output drive needs at least 1 GB free.")
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=15, creationflags=HIDE)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Automatic Wan video setup needs an NVIDIA GPU with at least 10 GB VRAM.") from exc
    if result.returncode or not any(int(match) >= 10000 for match in re.findall(r"\d+", result.stdout)):
        raise RuntimeError("Automatic Wan video setup needs an NVIDIA GPU with at least 10 GB VRAM.")


def start_video_job(prompt: str, output_path: str, image_path: str = "",
                    seconds: float = 2, layout: str = "wide") -> dict:
    """Start a background generation job; get progress with video_job_status."""
    if not 5 <= len(prompt.strip()) <= 2000:
        raise ValueError("Describe the video in 5 to 2000 characters.")
    if layout not in ("wide", "vertical", "square"):
        raise ValueError("Layout must be wide, vertical or square.")
    if not 1 <= float(seconds) <= 5:
        raise ValueError("Generate a 1 to 5 second clip, then join clips for longer videos.")
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".mp4" or output.exists():
        raise ValueError("Choose a new .mp4 output filename.")
    image = None
    if image_path:
        image = Path(image_path).expanduser().resolve(strict=True)
        if not image.is_file() or image.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            raise ValueError("Choose a local PNG, JPG or WebP starting image.")
        if image.stat().st_size > 20_000_000:
            raise ValueError("The starting image must be under 20 MB.")
    _hardware_ready(output)
    job_id = uuid.uuid4().hex
    payload = {"prompt": prompt.strip(), "output_path": str(output), "image_path": str(image or ""),
               "seconds": float(seconds), "layout": layout}
    _write(job_id, state="queued", **payload)
    log = JOBS / f"{job_id}.log"
    try:
        with log.open("wb") as stream:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", job_id],
                                    cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=stream,
                                    stderr=subprocess.STDOUT, creationflags=HIDE)
    except OSError as exc:
        _write(job_id, state="failed", error=f"{type(exc).__name__}: {exc}", log_path=str(log))
        raise
    return _write(job_id, state="starting", worker_pid=proc.pid, log_path=str(log))


def video_job_status(job_id: str) -> dict:
    """Read a durable job and detect a worker that exited unexpectedly."""
    path = _job_path(job_id)
    if not path.is_file():
        raise ValueError("Video job not found.")
    data = json.loads(path.read_text(encoding="utf-8"))
    pid = data.get("worker_pid")
    if data.get("state") not in FINISHED and pid and os.name == "nt":
        check = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=10, creationflags=HIDE)
        if check.returncode == 0 and f'"{pid}"' not in check.stdout:
            data = _write(job_id, state="failed", error="Video worker exited unexpectedly; see log_path.")
    status = {key: data.get(key) for key in (
        "job_id", "state", "detail", "output_path", "error", "log_path", "updated_at"
    )}
    if data.get("state") == "complete":
        status["progress_percent"] = 100
    elif data.get("state") == "rendering":
        status["progress_percent"] = _render_percent(job_id)
    else:
        status["progress_percent"] = None
    return status


def _render_percent(job_id: str) -> int:
    """Read ComfyUI's latest sampler progress without loading a large log."""
    path = JOBS / f"{job_id}-comfy.log"
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read()
    except OSError:
        return 0
    matches = re.findall(rb"(\d{1,3})%\|", tail)
    return max(0, min(100, int(matches[-1]))) if matches else 0


@contextmanager
def _process_lock(name: str, timeout_seconds: float | None = None):
    """Serialize a resource across independent agent worker processes."""
    if os.name != "nt":
        raise RuntimeError("Automatic video setup currently requires Windows.")
    import msvcrt
    BASE.mkdir(parents=True, exist_ok=True)
    lock_file = BASE / name
    with lock_file.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for video resource {name}.")
                time.sleep(2)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _installation_lock():
    """Keep concurrent jobs from installing the runtime at the same time."""
    return _process_lock("runtime-install.lock", 18000)


def _render_lock():
    """Only one Wan render may hold the GPU on this PC at a time."""
    return _process_lock("gpu-render.lock")


def _model_fingerprints() -> dict | None:
    """Cheap validation after the first checksum pass; a partial download has no marker."""
    model_root = RUNTIME / "ComfyUI" / "models"
    fingerprints = {}
    for relative in MODEL_HASHES:
        path = model_root / relative
        if not path.is_file():
            return None
        stat = path.stat()
        if stat.st_size < 100_000_000:
            return None
        fingerprints[relative] = [stat.st_size, stat.st_mtime_ns]
    return fingerprints


def _verified_runtime() -> bool:
    python = RUNTIME / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
    marker = RUNTIME / "cale-video-ready.json"
    fingerprints = _model_fingerprints()
    if not python.is_file() or not fingerprints:
        return False
    if marker.is_file():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == fingerprints:
                return True
        except (OSError, ValueError):
            pass
    # Existing installs predate the marker. Hash all three files once before
    # trusting them, and rehash whenever their size or timestamp changes.
    for relative, expected in MODEL_HASHES.items():
        try:
            with (RUNTIME / "ComfyUI" / "models" / relative).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256")
        except OSError:
            return False
        if digest.hexdigest() != expected:
            return False
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps(fingerprints), encoding="utf-8")
    temporary.replace(marker)
    return True


def _ensure_runtime(job_id: str) -> None:
    with _installation_lock():
        _ensure_runtime_locked(job_id)


def _ensure_runtime_locked(job_id: str) -> None:
    if _verified_runtime():
        return
    _write(job_id, state="installing", detail="Installing local ComfyUI and verified Wan 2.2 models")
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    git = _git_executable()
    if not SOURCE.is_dir():
        subprocess.run([git, "init", str(SOURCE)], check=True, timeout=120, creationflags=HIDE)
        subprocess.run([git, "-C", str(SOURCE), "remote", "add", "origin", UPSTREAM],
                       check=True, timeout=30, creationflags=HIDE)
    current = subprocess.run([git, "-C", str(SOURCE), "rev-parse", "HEAD"],
                             capture_output=True, text=True, creationflags=HIDE)
    if current.returncode or current.stdout.strip() != COMMIT:
        subprocess.run([git, "-C", str(SOURCE), "fetch", "--depth", "1", "origin", COMMIT],
                       check=True, timeout=600, creationflags=HIDE)
        subprocess.run([git, "-C", str(SOURCE), "checkout", "--detach", COMMIT],
                       check=True, timeout=120, creationflags=HIDE)
    installer = SOURCE / "install.ps1"
    if not installer.is_file():
        raise RuntimeError("Pinned video installer is missing.")
    _patch_installer(installer)
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer),
               "-Cuda", "cu128", "-Models", "5b", "-PyVersion", "3.11", "-BasePath", str(RUNTIME),
               "-SkipNodes", "-ReuseVenv", "-LockedVenvAction", "Fail"]
    try:
        subprocess.run(command, cwd=str(SOURCE), check=True, timeout=14400, creationflags=HIDE)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Local video runtime installation failed; inspect the job log.") from exc
    if not _verified_runtime():
        raise RuntimeError("Video runtime installation did not produce verified model files and Python runtime.")


def _patch_installer(installer: Path) -> None:
    """Repair two PowerShell 5.1 Python invocations in the pinned installer."""
    # Windows PowerShell 5.1 strips quotes from Python -c arguments in this
    # upstream installer. Put those two fixed checks in .py files instead so
    # the default Windows shell runs the same code without quote mangling.
    script = installer.read_text(encoding="utf-8")
    for marker, variable, filename, args in (
        ("Invoke-Native -FilePath $venvPython -ArgumentList @('-c', $venvCheckCode, $venvPath)",
         "$venvCheckCode", "cale-check-venv.py", "$venvPath"),
        ("Invoke-Native -FilePath $venvPython -ArgumentList @('-c', $verifyCode)",
         "$verifyCode", "cale-check-torch.py", ""),
    ):
        replacement = (f"[IO.File]::WriteAllText((Join-Path $BasePath '{filename}'), {variable})\n"
                       f"Invoke-Native -FilePath $venvPython -ArgumentList @((Join-Path $BasePath '{filename}')"
                       + (f", {args}" if args else "") + ")")
        if marker in script:
            script = script.replace(marker, replacement, 1)
        elif replacement not in script:
            raise RuntimeError("Pinned video installer changed unexpectedly.")
    warning_marker = "& $venvPython -m pip show dac *> $null\nif ($LASTEXITCODE -eq 0) {"
    warning_replacement = (
        "$calePreviousErrorAction = $ErrorActionPreference\n"
        "$ErrorActionPreference = 'Continue'\n"
        "try { & $venvPython -m pip show dac *> $null; $caleLegacyDacInstalled = ($LASTEXITCODE -eq 0) }\n"
        "finally { $ErrorActionPreference = $calePreviousErrorAction }\n"
        "if ($caleLegacyDacInstalled) {"
    )
    if warning_marker in script:
        script = script.replace(warning_marker, warning_replacement, 1)
    elif warning_replacement not in script:
        raise RuntimeError("Pinned video installer package check changed unexpectedly.")
    installer.write_text(script, encoding="utf-8")


def _request(url: str, payload: dict | None = None, timeout: int = 20) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _workflow(prompt: str, seconds: float, layout: str, seed: int) -> dict:
    # This is the built-in ComfyUI Wan 2.2 5B graph from the official example,
    # with a short 480p clip so a 12 GB card can attempt a first render.
    dims = {"wide": (832, 480), "vertical": (480, 832), "square": (640, 640)}
    width, height = dims[layout]
    length = min(121, max(17, int(seconds * 16) // 4 * 4 + 1))
    return {
        "37": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_ti2v_5B_fp16.safetensors", "weight_dtype": "default"}},
        "38": {"class_type": "CLIPLoader", "inputs": {"clip_name": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "type": "wan", "device": "default"}},
        "39": {"class_type": "VAELoader", "inputs": {"vae_name": "wan2.2_vae.safetensors"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["38", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, low quality, artifacts, distorted anatomy, static frame", "clip": ["38", 0]}},
        "55": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"width": width, "height": height, "length": length, "batch_size": 1, "vae": ["39", 0]}},
        "48": {"class_type": "ModelSamplingSD3", "inputs": {"shift": 8, "model": ["37", 0]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 20, "cfg": 5, "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1, "model": ["48", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["55", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["39", 0]}},
        "57": {"class_type": "CreateVideo", "inputs": {"fps": 16, "bit_depth": "auto", "color_space": "sRGB", "codec": "none", "images": ["8", 0]}},
        "58": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "CaleAI", "format": "auto", "video": ["57", 0], "format.codec": "auto"}},
    }


def _run_job(job_id: str) -> None:
    data = json.loads(_job_path(job_id).read_text(encoding="utf-8"))
    _ensure_runtime(job_id)
    _write(job_id, state="waiting_for_gpu", detail="Waiting for another local video render to finish")
    with _render_lock():
        _render_job(job_id, data)


def _render_job(job_id: str, data: dict) -> None:
    port = _free_port()
    python = RUNTIME / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
    main = RUNTIME / "ComfyUI" / "main.py"
    _write(job_id, state="loading", detail="Loading Wan 2.2 on the local GPU")
    with (JOBS / f"{job_id}-comfy.log").open("wb") as log:
        server = subprocess.Popen([str(python), str(main), "--listen", "127.0.0.1",
                                   "--port", str(port), "--disable-auto-launch"],
                                  cwd=str(main.parent), stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=subprocess.STDOUT, creationflags=HIDE)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 300
        while True:
            if server.poll() is not None:
                raise RuntimeError("Local ComfyUI exited during startup; inspect the job's Comfy log.")
            try:
                _request(base + "/system_stats", timeout=3)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise TimeoutError("Local ComfyUI did not start within five minutes.")
                time.sleep(2)
        graph = _workflow(data["prompt"], data["seconds"], data["layout"], random.randrange(2**48))
        if data["image_path"]:
            # Copy the selected image into this private ComfyUI checkout rather
            # than uploading it to a service or exposing an arbitrary file path.
            source = Path(data["image_path"])
            name = f"cale-{job_id}{source.suffix.lower()}"
            image = main.parent / "input" / name
            image.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, image)
            graph["59"] = {"class_type": "LoadImage", "inputs": {"image": name}}
            graph["55"]["inputs"]["start_image"] = ["59", 0]
        _write(job_id, state="rendering", detail="Generating video frames locally")
        queued = _request(base + "/prompt", {"prompt": graph, "client_id": job_id})
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI rejected the video workflow: {str(queued)[:500]}")
        deadline = time.monotonic() + 10800
        while True:
            if server.poll() is not None:
                raise RuntimeError("Local ComfyUI exited while rendering.")
            history = _request(base + "/history/" + prompt_id)
            if prompt_id in history:
                record = history[prompt_id]
                if record.get("status", {}).get("status_str") == "error":
                    raise RuntimeError("ComfyUI reported a video render error; inspect its log.")
                result = record.get("outputs", {}).get("58", {})
                # ComfyUI versions have serialized PreviewVideo under either
                # `videos` or the older generic `images` preview key.
                video = result.get("videos") or result.get("images") or []
                if video:
                    entry = video[0]
                    output_root = (main.parent / "output").resolve()
                    rendered = (output_root / entry.get("subfolder", "") / entry["filename"]).resolve()
                    if not rendered.is_relative_to(output_root) or not rendered.is_file():
                        raise RuntimeError("ComfyUI returned a missing or unsafe output path.")
                    destination = Path(data["output_path"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        raise RuntimeError("Output path already exists; preserving the existing file.")
                    if rendered.suffix.lower() == ".mp4":
                        shutil.copy2(rendered, destination)
                    else:
                        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-i", str(rendered),
                                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(destination)],
                                       check=True, timeout=3600, creationflags=HIDE)
                    if not destination.is_file() or destination.stat().st_size < 1000:
                        raise RuntimeError("The generated video file is empty.")
                    _write(job_id, state="complete", detail="Video saved", bytes=destination.stat().st_size)
                    return
                if record.get("status", {}).get("completed") is True:
                    raise RuntimeError("ComfyUI finished without a saved video; inspect its log.")
            if time.monotonic() > deadline:
                raise TimeoutError("Video render exceeded three hours.")
            time.sleep(5)
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "worker":
        raise SystemExit("Run this module through start_video_job.")
    job = sys.argv[2]
    try:
        _run_job(job)
    except Exception as exc:
        _write(job, state="failed", error=f"{type(exc).__name__}: {exc}")
        raise
