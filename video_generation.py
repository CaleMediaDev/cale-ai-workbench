"""Durable, local Wan 2.2 video jobs on a dedicated loopback ComfyUI runtime.

OpenVideo Studio installs pinned, checksum-verified Wan model files on first use.
This module never sends prompts or input images to a cloud generation service.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
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
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.stem + "-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
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
    if shutil.disk_usage(_existing_parent(BASE)).free < required_gb * 1024 ** 3:
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
    _write(job_id, state="starting", **payload)
    log = JOBS / f"{job_id}.log"
    try:
        with log.open("wb") as stream:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", job_id],
                                    cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=stream,
                                    stderr=subprocess.STDOUT, creationflags=HIDE)
    except OSError as exc:
        _write(job_id, state="failed", error=f"{type(exc).__name__}: {exc}", log_path=str(log))
        raise
    return _write(job_id, worker_pid=proc.pid, log_path=str(log))


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
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=marker.parent,
            prefix=marker.stem + "-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(fingerprints, stream)
        temporary.replace(marker)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
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
                        # Reject invalid renders before creating an export that
                        # would block retrying with the same destination.
                        if rendered.stat().st_size < 1000:
                            raise RuntimeError("The generated video file is empty.")
                        # Exclusively create the destination: another process may
                        # have created it since the existence check above.
                        with rendered.open("rb") as source:
                            target = destination.open("xb")
                            try:
                                with target:
                                    shutil.copyfileobj(source, target)
                            except BaseException:
                                # Do not leave a truncated export blocking a retry.
                                # Exclusive creation above must succeed before cleanup.
                                try:
                                    destination.unlink()
                                except OSError:
                                    pass
                                raise
                    else:
                        # A failed conversion must not leave a partial destination
                        # that prevents retrying the export.
                        with tempfile.TemporaryDirectory(prefix="cale-video-") as temporary:
                            converted = Path(temporary) / "converted.mp4"
                            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-i", str(rendered),
                                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(converted)],
                                           check=True, timeout=3600, creationflags=HIDE)
                            if not converted.is_file() or converted.stat().st_size < 1000:
                                raise RuntimeError("The generated video file is empty.")
                            with converted.open("rb") as source:
                                target = destination.open("xb")
                                try:
                                    with target:
                                        shutil.copyfileobj(source, target)
                                except BaseException:
                                    try:
                                        destination.unlink()
                                    except OSError:
                                        pass
                                    raise
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
            server.wait()



# Live Cale has an isolated ORT environment. The existing Media Torch runtime is
# reused read-only; installing/upgrading avatar packages must never mutate it.
AVATAR_ORT_PACKAGES = ("onnxruntime-gpu==1.26.0", "numpy==2.2.6",
                       "flatbuffers==25.12.19", "packaging==26.3", "protobuf==6.33.0")
AVATAR_GRAPH = {"sha256":"d3713a0ac98a248460604852844f44a692d174e337bdb5cda13264ad45328357",
                "size":202085302,
                "url":"https://github.com/CaleMediaDev/cale-ai-workbench/releases/download/v0.5.511/Cale-Avatar-Decoder-v1.onnx"}



def save_avatar_camera_mode(root, enabled):
    """Persist the privacy choice atomically before changing the camera owner."""
    if not isinstance(enabled,bool):raise ValueError("Camera mode must be boolean.")
    path=Path(root)/"Media/camera-avatar.json";path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name("camera-avatar-"+uuid.uuid4().hex+".tmp")
    try:
        temporary.write_text(json.dumps({"enabled":enabled}),encoding="utf-8")
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

def avatar_profile_root(root=None):
    return Path(root or ROOT) / "Media/avatar/v1"


def _avatar_write_status(profile, **value):
    """Only bounded setup facts are persisted; never camera frames or faces."""
    profile.mkdir(parents=True, exist_ok=True)
    value.update(updated_at=time.time())
    temporary=profile/("status-"+uuid.uuid4().hex+".tmp")
    try:
        temporary.write_text(json.dumps(value),encoding="utf-8")
        os.replace(temporary,profile/"status.json")
    finally:temporary.unlink(missing_ok=True)
    return value


def avatar_profile_status(root=None):
    profile=avatar_profile_root(root)
    try:
        with (profile/"status.json").open("rb") as stream:raw=stream.read(16385)
        value=json.loads(raw) if len(raw)<=16384 else {}
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def _avatar_assets():
    import avatar_video
    sources=getattr(avatar_video,"AVATAR_SOURCE_FILES",None)
    models=getattr(avatar_video,"AVATAR_MODEL_FILES",None)
    if not isinstance(sources,dict) or not isinstance(models,dict):
        raise RuntimeError("Live Cale engine is not included in this release yet.")
    source_commit=avatar_video.AVATAR_SOURCE_COMMIT
    assets=[]
    for relative,record in sources.items():
        assets.append(("source/"+relative,{**record,"url":f"https://raw.githubusercontent.com/KwaiVGI/LivePortrait/{source_commit}/{relative}","hash_kind":"git"}))
    for relative,record in models.items():assets.append(("models/"+relative,{**record,"hash_kind":"sha256"}))
    if "cale-warp-decoder-opset20-baked.onnx" not in models:
        assets.append(("models/cale-warp-decoder-opset20-baked.onnx",{**AVATAR_GRAPH,"hash_kind":"sha256"}))
    return assets


def _avatar_verified(path, record):
    """Streaming verification also applies to previously cached source/models."""
    if not path.is_file() or path.stat().st_size!=record["size"]:return False
    digest=hashlib.sha1(b"blob "+str(record["size"]).encode()+b"\0") if record["hash_kind"]=="git" else hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            part=stream.read(1024*1024)
            if not part:break
            digest.update(part)
    return digest.hexdigest()==record.get("blob",record.get("sha256"))


def _avatar_download(profile, relative, record, progress=None):
    """Pinned, byte-bounded downloads use the guarded Windows source writer."""
    import design_tools
    from urllib.parse import urlsplit
    from urllib.request import HTTPRedirectHandler,build_opener
    from urllib.error import HTTPError
    relative=design_tools._game_rel(relative)
    target=design_tools._game_plain_path(profile/relative)
    if _avatar_verified(target,record):return
    import pc_offline
    if pc_offline.offline():raise RuntimeError("Live Cale setup needs one internet download before offline use.")
    allowed={"raw.githubusercontent.com","github.com","release-assets.githubusercontent.com","huggingface.co","cdn-lfs.huggingface.co","cdn-lfs-us-1.hf.co","cas-bridge.xethub.hf.co"}
    def valid(url):
        parsed=urlsplit(url)
        return parsed.scheme=="https" and parsed.hostname in allowed and not parsed.username and not parsed.password
    if not valid(record["url"]):raise ValueError("Unrecognized Live Cale asset host.")
    class Redirect(HTTPRedirectHandler):
        def redirect_request(self,request,fp,code,message,headers,url):
            if not valid(url):raise ValueError("Live Cale download left its verified asset hosts.")
            return super().redirect_request(request,fp,code,message,headers,url)
    try:response=build_opener(Redirect()).open(Request(record["url"],headers={"User-Agent":"Cale-Live-Avatar"}),timeout=30)
    except HTTPError as error:
        if error.code==404:raise RuntimeError("The verified Live Cale asset has not been published yet; camera stays private.") from error
        raise
    # Download to a private app cache first, then promote only verified bytes.
    with response, tempfile.TemporaryFile() as spool:
        count=0
        while True:
            data=response.read(min(1024*1024,record["size"]-count+1))
            if not data:break
            count+=len(data)
            if count>record["size"]:raise ValueError("Live Cale asset exceeded its pinned byte size.")
            spool.write(data)
            if progress:progress(count,record["size"])
        if count!=record["size"]:raise ValueError("Live Cale asset download was incomplete.")
        spool.seek(0)
        digest=hashlib.sha1(b"blob "+str(count).encode()+b"\0") if record["hash_kind"]=="git" else hashlib.sha256()
        while True:
            data=spool.read(1024*1024)
            if not data:break
            digest.update(data)
        if digest.hexdigest()!=record.get("blob",record.get("sha256")):raise ValueError("Live Cale asset checksum failed.")
        # Source writer accepts Git-blob hashes; derive that hash from verified
        # disk bytes for SHA256 model assets without buffering model-sized data.
        spool.seek(0);git=hashlib.sha1(b"blob "+str(count).encode()+b"\0")
        while True:
            data=spool.read(1024*1024)
            if not data:break
            git.update(data)
        spool.seek(0)
        with design_tools._game_directory_guard(Path(profile.anchor),profile):
            if target.exists():raise RuntimeError("A changed Live Cale asset was preserved. Remove its optional profile to repair it.")
            design_tools._game_copy_verified(spool,profile,relative,{"size":count,"sha":git.hexdigest()},uuid.uuid4().hex)


def _avatar_environment():
    env=os.environ.copy();env["PYTHONNOUSERSITE"]="1";env["PYTHONDONTWRITEBYTECODE"]="1";env.pop("PYTHONPATH",None);env.pop("PYTHONHOME",None)
    return env


class _AvatarProbeDeferred(RuntimeError):
    """A foreground GPU job superseded qualification; retry after it finishes."""


def _avatar_gpu_busy(root):
    from readiness_checklist import _heavy_gpu_active
    return _heavy_gpu_active(Path(root))


def avatar_profile_probe_worker(profile, parent_pid, parent_started):
    """Explicit trusted CLI role, with a visible live-process GPU lease."""
    import threading
    root=Path(__file__).resolve().parent
    profile=Path(profile).resolve()
    if profile!=avatar_profile_root(root).resolve():raise ValueError("Avatar probe profile is not owned by this app.")
    # Isolated Python excludes caller cwd; only the actual app directory is
    # added, after this script's standard-library imports have completed.
    sys.path.insert(0,str(root))
    from camera_presence import AvatarGpuLease,owner_alive,watch_owner
    if not owner_alive(parent_pid,parent_started):raise RuntimeError("Avatar setup owner closed.")
    threading.Thread(target=watch_owner,args=(parent_pid,parent_started),daemon=True).start()
    engine=None;lease=AvatarGpuLease()
    try:
        import torch,onnxruntime as ort,avatar_video
        media=root/"Media/python"
        if not Path(ort.__file__).resolve().is_relative_to(profile/"python"):raise RuntimeError("Wrong avatar ORT origin.")
        if not Path(torch.__file__).resolve().is_relative_to(media):raise RuntimeError("Wrong Media Torch origin.")
        if ort.__version__!="1.26.0" or torch.__version__!="2.9.1+cu128":raise RuntimeError("Avatar runtime versions changed.")
        if torch.version.cuda!="12.8" or not torch.cuda.is_available():raise RuntimeError("Avatar CUDA is unavailable.")
        engine=avatar_video.CaleAvatar(profile)
        result=engine.probe();lease.measured(result)
        if not isinstance(result,dict) or result.get("ok") is not True:raise RuntimeError("Avatar inference failed.")
        print(json.dumps({"ok":True,"ort":ort.__version__,"torch":torch.__version__,"cuda":torch.version.cuda,
            "cudnn":torch.backends.cudnn.version(),"probe":result}),flush=True)
    finally:
        try:
            if engine is not None:engine.close()
        finally:lease.close()


def probe_avatar_profile(profile, root=None, cancelled=None):
    """Qualify in an isolated child; defer/reap it when foreground GPU work wins."""
    import psutil
    root=Path(root or ROOT);profile=Path(profile)
    if _avatar_gpu_busy(root):raise _AvatarProbeDeferred("Waiting for music/image GPU work.")
    python=profile/"python/Scripts/python.exe"
    command=[str(python),"-I","-B",str(root/"video_generation.py"),"avatar-profile-probe","--profile-root",str(profile),
             "--parent-pid",str(os.getpid()),"--parent-started",str(psutil.Process(os.getpid()).create_time())]
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(command,stdout=output,stderr=errors,env=_avatar_environment(),creationflags=HIDE,cwd=root)
        try:started=psutil.Process(process.pid).create_time()
        except (psutil.Error,TypeError,ValueError):started=None
        deadline=time.monotonic()+120
        try:
            while process.poll() is None:
                if cancelled and cancelled():raise RuntimeError("Live Cale setup cancelled.")
                if _avatar_gpu_busy(root):raise _AvatarProbeDeferred("Foreground GPU work started; qualification paused.")
                if time.monotonic()>deadline:raise TimeoutError("Live Cale inference qualification timed out.")
                if output.tell()>16384 or errors.tell()>65536:raise RuntimeError("Live Cale probe output exceeded its bound.")
                time.sleep(.05)
            if process.returncode:raise RuntimeError("Live Cale CUDA/inference qualification failed; camera stays private.")
            output.seek(0);raw=output.read(16385)
            if len(raw)>16384:raise RuntimeError("Live Cale probe response exceeded its bound.")
            result=json.loads(raw.decode("utf-8").strip().splitlines()[-1])
            if result.get("ok") is not True:raise RuntimeError("Live Cale inference probe did not pass.")
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            else:process.wait()
            # Forced termination does not run child finally blocks. Remove only
            # the exact reaped process's marker; PID alone is not ownership.
            marker=root/"LongTasks"/f"avatar-gpu-active-{process.pid}.json"
            try:
                with marker.open("rb") as stream:record=stream.read(4097)
                value=json.loads(record) if len(record)<=4096 else {}
                if started is not None and value.get("pid")==process.pid and value.get("started")==started:marker.unlink()
            except (OSError,ValueError):pass
    return result


def ensure_avatar_profile(root=None, progress=None, cancelled=None, virtual_output=False):
    """Install optional verified assets, then qualify real inference before ready.

    Call only on a background thread. Cancelling or setup failure cannot turn on
    a raw camera fallback. No camera is opened by installation or qualification.
    """
    root=Path(root or ROOT);profile=avatar_profile_root(root)
    def report(phase,detail,**extra):
        if cancelled and cancelled():raise RuntimeError("Live Cale setup cancelled.")
        value=_avatar_write_status(profile,phase=phase,ready=False,detail=detail,**extra)
        if progress:progress(value)
    with _process_lock("avatar-profile-install.lock",1800):
        try:
            import music_backend,pc_offline
            assets=_avatar_assets();profile.mkdir(parents=True,exist_ok=True)
            if shutil.disk_usage(profile).free<2*1024**3:raise RuntimeError("Live Cale setup needs at least2GB free disk space.")
            report("checking","Checking verified Live Cale files")
            for index,(relative,record) in enumerate(assets):
                report("downloading","Preparing Live Cale models",files_done=index,files_total=len(assets))
                _avatar_download(profile,relative,record,lambda done,total:report("downloading","Downloading Live Cale model",bytes_done=done,bytes_total=total,files_done=index,files_total=len(assets)))
            python=profile/"python/Scripts/python.exe";media=root/"Media/python/Scripts/python.exe"
            if not media.is_file():raise RuntimeError("The base Media Python runtime is still installing.")
            package_marker=profile/"packages.json"
            try:
                with package_marker.open("rb") as stream:package_bytes=stream.read(4097)
                packages_ok=len(package_bytes)<=4096 and json.loads(package_bytes)==list(AVATAR_ORT_PACKAGES)
            except (OSError,ValueError):packages_ok=False
            if not python.is_file() or not packages_ok:
                if pc_offline.offline():raise RuntimeError("Live Cale Python needs first-time internet setup.")
                report("installing","Installing isolated Live Cale runtime")
                uv=music_backend._tool("uv")
                if not python.is_file():
                    subprocess.run([uv,"venv",str(profile/"python"),"--python",str(media)],check=True,timeout=120,env=_avatar_environment(),creationflags=HIDE)
                subprocess.run([uv,"pip","install","--python",str(python),"--only-binary",":all:","--no-deps",*AVATAR_ORT_PACKAGES],check=True,timeout=600,env=_avatar_environment(),creationflags=HIDE,capture_output=True)
                site=profile/"python/Lib/site-packages";site.mkdir(parents=True,exist_ok=True)
                (site/"cale-media-libraries.pth").write_text(str((root/"Media/python/Lib/site-packages").resolve())+"\n",encoding="utf-8")
                package_marker.write_text(json.dumps(list(AVATAR_ORT_PACKAGES)),encoding="utf-8")
            expected_pth=str((root/"Media/python/Lib/site-packages").resolve())+"\n"
            with (profile/"python/Lib/site-packages/cale-media-libraries.pth").open("rb") as stream:pth=stream.read(4097)
            if len(pth)>4096 or pth.decode("utf-8").replace("\r\n","\n")!=expected_pth:
                raise RuntimeError("Live Cale runtime path changed; optional profile must be repaired before it can run.")
            if virtual_output:
                if pc_offline.offline():
                    completed=subprocess.run([str(python),"-I","-B","-c","import pyvirtualcam"],timeout=20,capture_output=True,creationflags=HIDE,cwd=root,env=_avatar_environment())
                    if completed.returncode:raise RuntimeError("Optional virtual-camera package needs one online setup.")
                else:
                    report("installing","Preparing optional GPL virtual-camera bridge; existing OBS driver required")
                    subprocess.run([music_backend._tool("uv"),"pip","install","--python",str(python),"--no-deps","pyvirtualcam==0.15.0"],check=True,timeout=120,capture_output=True,creationflags=HIDE,env=_avatar_environment())
                    (profile/"virtual-camera-provenance.json").write_text(json.dumps({"package":"pyvirtualcam","version":"0.15.0","license":"GPL-2.0","source":"https://github.com/letmaik/pyvirtualcam","execution":"separate optional avatar-only frame sender","obs_driver":"existing Windows OBS installation required","virtual_output_tested":False}),encoding="utf-8")
            report("testing","Testing Live Cale CUDA imports and actual avatar inference")
            waiting=False
            while True:
                if cancelled and cancelled():raise RuntimeError("Live Cale setup cancelled.")
                if _avatar_gpu_busy(root):
                    if not waiting:report("waiting_gpu","Animated Cale setup paused for local music/image work; your camera stays private.")
                    waiting=True;time.sleep(.1);continue
                if waiting:report("testing","Foreground GPU work finished; testing Live Cale inference")
                waiting=False
                try:
                    probe=probe_avatar_profile(profile,root,cancelled=cancelled)
                    break
                except _AvatarProbeDeferred:
                    report("waiting_gpu","Animated Cale qualification paused for foreground GPU work.")
                    waiting=True
            value=_avatar_write_status(profile,phase="ready",ready=True,detail="Live Cale inference tested",probe=probe,virtual_output_requested=bool(virtual_output),virtual_output_tested=False)
            if progress:progress(value)
            return python
        except Exception as error:
            _avatar_write_status(profile,phase="failed",ready=False,detail=str(error)[:400],error_type=type(error).__name__)
            raise


def virtual_avatar_sender(stream=None, parent_pid=None, parent_started=None):
    """Optional GPL bridge: bounded RGB frames only; never imports/opens capture.

    Wire format is a fixed big-endian length followed by RGB JPEG bytes. The
    caller supplies processed avatars only. EOF closes the existing OBS camera.
    """
    import struct,io
    import numpy as np
    from PIL import Image,ImageOps
    import pyvirtualcam  # Optional GPL2 dependency stays in this child process.
    stream=stream or sys.stdin.buffer
    if parent_pid is not None:
        import threading
        from camera_presence import owner_alive,watch_owner
        if not owner_alive(parent_pid,parent_started):return 0
        threading.Thread(target=watch_owner,args=(parent_pid,parent_started),daemon=True).start()
    with pyvirtualcam.Camera(width=1920,height=1080,fps=15,backend="obs",fmt=pyvirtualcam.PixelFormat.RGB) as camera:
        while True:
            header=stream.read(4)
            if not header:return 0
            if len(header)!=4:raise ValueError("Incomplete avatar frame header.")
            size=struct.unpack("!I",header)[0]
            if not 1<=size<=2*1024*1024:raise ValueError("Avatar virtual frame exceeds its bound.")
            raw=stream.read(size)
            if len(raw)!=size:raise ValueError("Incomplete avatar frame.")
            with Image.open(io.BytesIO(raw)) as image:
                width,height=image.size
                if not (1<=width<=1920 and 1<=height<=1080):raise ValueError("Avatar virtual pixels exceed their bound.")
                # Preserve the native scene's aspect ratio. A 1920x1080 virtual
                # device gets a letterbox, never a stretched face or cropped art.
                fitted=ImageOps.contain(image.convert("RGB"),(1920,1080),Image.Resampling.LANCZOS)
                canvas=Image.new("RGB",(1920,1080),(0,0,0))
                canvas.paste(fitted,((1920-fitted.width)//2,(1080-fitted.height)//2))
                frame=np.asarray(canvas)
            camera.send(frame)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2]==["avatar-profile-probe"]:
        import argparse
        parser=argparse.ArgumentParser()
        parser.add_argument("--profile-root",required=True)
        parser.add_argument("--parent-pid",type=int,required=True)
        parser.add_argument("--parent-started",type=float,required=True)
        options=parser.parse_args(sys.argv[2:])
        avatar_profile_probe_worker(options.profile_root,options.parent_pid,options.parent_started)
        raise SystemExit(0)
    if sys.argv[1:2]==["avatar-virtual-sender"]:
        import argparse
        parser=argparse.ArgumentParser()
        parser.add_argument("--parent-pid",type=int,required=True)
        parser.add_argument("--parent-started",type=float,required=True)
        options=parser.parse_args(sys.argv[2:])
        raise SystemExit(virtual_avatar_sender(parent_pid=options.parent_pid,parent_started=options.parent_started))
    if len(sys.argv) != 3 or sys.argv[1] != "worker":
        raise SystemExit("Run this module through start_video_job.")
    job = sys.argv[2]
    try:
        _run_job(job)
    except Exception as exc:
        _write(job, state="failed", error=f"{type(exc).__name__}: {exc}")
        raise
