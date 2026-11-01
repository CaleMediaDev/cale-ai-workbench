"""Durable, local Wan 2.2 video jobs on a dedicated loopback ComfyUI runtime.

OpenVideo Studio installs pinned, checksum-verified Wan model files on first use.
This module never sends prompts or input images to a cloud generation service.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
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


def _hardware_ready() -> None:
    if os.name != "nt":
        raise RuntimeError("Automatic Wan video setup currently supports Windows with an NVIDIA GPU.")
    if shutil.disk_usage(BASE.parent).free < 35 * 1024 ** 3:
        raise RuntimeError("Video generation needs at least 35 GB free disk space.")
    result = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=15, creationflags=HIDE)
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
    _hardware_ready()
    job_id = uuid.uuid4().hex
    payload = {"prompt": prompt.strip(), "output_path": str(output), "image_path": str(image or ""),
               "seconds": float(seconds), "layout": layout}
    _write(job_id, state="queued", **payload)
    log = JOBS / f"{job_id}.log"
    with log.open("wb") as stream:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", job_id],
                                cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT, creationflags=HIDE)
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
    return {key: data.get(key) for key in ("job_id", "state", "output_path", "error", "log_path", "updated_at")}


def _ensure_runtime(job_id: str) -> None:
    python = RUNTIME / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
    model = RUNTIME / "ComfyUI" / "models" / "diffusion_models" / "wan2.2_ti2v_5B_fp16.safetensors"
    if python.is_file() and model.is_file():
        return
    _write(job_id, state="installing", detail="Installing local ComfyUI and verified Wan 2.2 models")
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    if not SOURCE.is_dir():
        subprocess.run(["git", "init", str(SOURCE)], check=True, timeout=120, creationflags=HIDE)
        subprocess.run(["git", "-C", str(SOURCE), "remote", "add", "origin", UPSTREAM],
                       check=True, timeout=30, creationflags=HIDE)
    current = subprocess.run(["git", "-C", str(SOURCE), "rev-parse", "HEAD"],
                             capture_output=True, text=True, creationflags=HIDE)
    if current.returncode or current.stdout.strip() != COMMIT:
        subprocess.run(["git", "-C", str(SOURCE), "fetch", "--depth", "1", "origin", COMMIT],
                       check=True, timeout=600, creationflags=HIDE)
        subprocess.run(["git", "-C", str(SOURCE), "checkout", "--detach", COMMIT],
                       check=True, timeout=120, creationflags=HIDE)
    installer = SOURCE / "install.ps1"
    if not installer.is_file():
        raise RuntimeError("Pinned video installer is missing.")
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
    installer.write_text(script, encoding="utf-8")
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(installer),
               "-Cuda", "cu128", "-Models", "5b", "-PyVersion", "3.11", "-BasePath", str(RUNTIME),
               "-SkipNodes", "-ReuseVenv", "-LockedVenvAction", "Fail"]
    try:
        subprocess.run(command, cwd=str(SOURCE), check=True, timeout=14400, creationflags=HIDE)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Local video runtime installation failed; inspect the job log.") from exc
    if not python.is_file() or not model.is_file():
        raise RuntimeError("Video runtime installation did not produce its model and Python runtime.")


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
