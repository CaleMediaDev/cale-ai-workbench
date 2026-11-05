"""Local image, speech, camera tools and durable 3D jobs; JSON in and JSON out."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "Media"
# A faint human voice can still be recognized below the clip-end detector's
# level. Only skip recognition for near-silence; diagnose other faint clips
# after Whisper has had a chance to find words.
MIC_SILENCE_LEVEL = 0.003
MIC_VOICE_LEVEL = 0.012
MIC_VOICE_RMS_LEVEL = 0.005
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
_whisper_model = None
_whisper_lock = threading.Lock()


def output_file(path, suffix):
    target = Path(path).resolve()
    if target.suffix.lower() != suffix:
        raise ValueError(f"Output must be a {suffix} file.")
    if target.exists():
        raise ValueError("Output already exists. Choose a new filename.")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def ollama(path, data=None, timeout=600):
    request = Request(
        "http://127.0.0.1:11434" + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def unload_local_models():
    # Leave unrelated models owned by other applications alone.
    legacy = {"qwen3-coder-local", "qwen3-coder-permissive", "qwen3.5-local-fast"}
    for model in ollama("/api/ps", timeout=10).get("models", []):
        name = model["name"].split(":")[0]
        if name.startswith("cale-") or name in legacy:
            ollama(
                "/api/generate", {"model": model["name"], "keep_alive": 0}, timeout=120
            )


@contextmanager
def _image_gpu_lease():
    """Keep cloned speech from reclaiming VRAM during image generation."""
    marker = ROOT / "LongTasks" / f"image-gpu-active-{os.getpid()}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    try:
        import cale_voice

        cale_voice.release_for_gpu_task()
        yield
    finally:
        marker.unlink(missing_ok=True)


def generate_image(prompt, output_path, width=512, height=512, seed=0):
    with _image_gpu_lease():
        return _generate_image(prompt, output_path, width, height, seed)


def _generate_image(prompt, output_path, width=512, height=512, seed=0):
    """Use the stronger local model when installed, otherwise keep LCM available."""
    import torch
    from diffusers import DiffusionPipeline

    if width not in (256, 512, 768) or height not in (256, 512, 768):
        raise ValueError("Use 256, 512 or 768 pixel dimensions.")
    target = output_file(output_path, ".png")
    unload_local_models()
    started = time.monotonic()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    enhanced = (
        device == "cuda"
        and torch.cuda.get_device_properties(0).total_memory >= 8 * 1024**3
        and image_upgrade_complete()
    )
    model_path = MEDIA / "models" / ("ssd-1b" if enhanced else "lcm")
    pipeline = DiffusionPipeline.from_pretrained(
        str(model_path), local_files_only=True,
        torch_dtype=torch.float16 if enhanced and device == "cuda" else torch.float32,
        variant="fp16" if enhanced else None, use_safetensors=True,
    )
    pipeline.to(device)
    pipeline.set_progress_bar_config(disable=True)
    result = pipeline(
        prompt=prompt,
        negative_prompt="blurry, distorted, poor quality" if enhanced else None,
        num_inference_steps=25 if enhanced else 4,
        guidance_scale=9.0 if enhanced else 8.0,
        width=width,
        height=height,
        generator=torch.Generator(device=device).manual_seed(int(seed)),
    )
    if getattr(result, "nsfw_content_detected", None) and any(
        result.nsfw_content_detected
    ):
        raise ValueError(
            "The image pipeline did not return a usable image for this request."
        )
    result.images[0].save(target)
    return {
        "path": str(target),
        "width": width,
        "height": height,
        "device": device,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model": "Segmind SSD-1B" if enhanced else "LCM Dreamshaper v7",
        "steps": 25 if enhanced else 4,
        "seed": seed,
    }


def transcribe_audio(path, fast=False, music=False):
    """Transcribe offline; keep sung words that a speech VAD would discard."""
    from faster_whisper import WhisperModel

    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError("Audio or video file not found.")
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            _whisper_model = WhisperModel(
                str(MEDIA / "models/whisper"), device="cpu",
                compute_type="int8", local_files_only=True,
            )
        model = _whisper_model
    # Live microphone turns favor a fast first answer; longer file analysis
    # retains its wider beam for accuracy.
    segments, info = model.transcribe(
        str(source), beam_size=1 if fast else 3, vad_filter=not music,
    )
    rows = [
        {
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": segment.text.strip(),
        }
        for segment in segments
    ]
    return {
        "text": " ".join(row["text"] for row in rows),
        "language": info.language,
        "segments": rows,
    }


def recognize_image(
    path, question="Describe this image accurately, including readable text."
):
    """Ask the local vision model about a still image."""
    import io

    from PIL import Image

    source = Path(path).resolve()
    with Image.open(source) as original:
        original.thumbnail((1280, 1280))
        image = original.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
    model = "qwen3.5-local-fast"
    settings = MEDIA / "settings.json"
    if settings.exists():
        model = json.loads(settings.read_text()).get("vision_model", model)
    result = ollama(
        "/api/chat",
        {
            "model": model,
            "stream": False,
            "think": False,
            "messages": [
                {
                    "role": "user",
                    "content": question,
                    "images": [base64.b64encode(buffer.getvalue()).decode()],
                }
            ],
            "options": {"num_ctx": 8192, "num_predict": 1200, "temperature": 0.2},
        },
        timeout=900,
    )
    if result.get("error"):
        raise RuntimeError(result["error"])
    return {
        "description": result["message"]["content"],
        "model": model,
        "source": str(source),
    }


def analyze_video(
    path,
    question="Summarize visible events and useful details.",
    max_frames=4,
    include_audio=False,
):
    """Sample bounded video frames and optionally add an audio transcript."""
    import tempfile

    import cv2

    max_frames = min(max(int(max_frames), 1), 8)
    source = Path(path).resolve(strict=True)
    (ROOT / "Temp").mkdir(exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError("The video could not be opened.")
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
        if fps <= 0 or count <= 0:
            raise ValueError("This video has no seekable frame timeline.")
        duration = count / fps
        observations = []
        with tempfile.TemporaryDirectory(dir=ROOT / "Temp") as temp:
            for index in range(max_frames):
                position = (count - 1) * index / max(1, max_frames - 1)
                capture.set(cv2.CAP_PROP_POS_FRAMES, int(position))
                ok, frame = capture.read()
                if not ok:
                    continue
                frame_path = Path(temp) / f"frame-{index}.jpg"
                cv2.imwrite(str(frame_path), frame)
                result = recognize_image(frame_path, question)
                observations.append(
                    {
                        "timestamp_seconds": round(position / fps, 2),
                        "description": result["description"],
                    }
                )
        result = {
            "duration_seconds": round(duration, 2),
            "sampled_frames": observations,
            "limitation": "Only the listed frames were inspected. Events between samples may be missed; no model training occurred.",
        }
        if include_audio:
            result["audio"] = transcribe_audio(path)
        return result
    finally:
        capture.release()


def camera_permission():
    """Require recent, time-limited permission written by the desktop UI."""
    try:
        return (
            json.loads((MEDIA / "camera-consent.json").read_text()).get("until", 0)
            > time.time()
        )
    except (OSError, ValueError):
        return False



def require_raw_camera_mode():
    """A tool must not bypass the visible avatar-only camera choice."""
    path=MEDIA/"camera-avatar.json"
    try:
        with path.open("rb") as stream:raw=stream.read(8193)
    except FileNotFoundError:return  # Existing installs predate the mode switch.
    except OSError:raise ValueError("Camera mode cannot be checked; raw capture is disabled.")
    try:
        value=json.loads(raw) if len(raw)<=8192 else None
        if not isinstance(value,dict) or not isinstance(value.get("enabled"),bool):raise ValueError()
    except (ValueError,TypeError):raise ValueError("Camera mode cannot be checked; raw capture is disabled.")
    if value["enabled"]:
        raise ValueError("Animated Cale is on. Physical camera capture is hidden; explicitly select Raw camera in the app before saving or analyzing raw frames.")

def open_camera(device):
    """Open a USB camera index or a previously registered local stream."""
    import cv2

    from camera_network import camera_source

    require_raw_camera_mode()
    source = camera_source(device)
    from camera_presence import negotiate_capture
    camera = (cv2.VideoCapture(source, cv2.CAP_DSHOW)
              if isinstance(source, int) else cv2.VideoCapture(source))
    if camera.isOpened():
        negotiate_capture(camera, cv2, isinstance(source, int))
    return camera


def capture_camera(output_path, device=0):
    """Save one camera frame only while the user's permission is active."""
    if not camera_permission():
        raise ValueError(
            "Camera is off. Enable Camera in the desktop app before requesting a capture."
        )
    require_raw_camera_mode()
    import cv2

    require_raw_camera_mode()
    target = output_file(output_path, ".png")
    capture = open_camera(device)
    try:
        if not capture.isOpened():
            raise ValueError(
                "Camera unavailable. Check the selected device and Windows camera permissions."
            )
        deadline = time.monotonic() + 5
        frame = None
        while time.monotonic() < deadline:
            if not camera_permission():
                raise ValueError("Camera was turned off.")
            ok, frame = capture.read()
            if ok:
                break
        if frame is None:
            raise ValueError("No camera frame received.")
        if not camera_permission():raise ValueError("Camera was turned off.")
        require_raw_camera_mode()
        if not cv2.imwrite(str(target), frame):
            raise RuntimeError("Camera image could not be saved.")
        return {
            "path": str(target),
            "device": device,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "width": int(frame.shape[1]), "height": int(frame.shape[0]),
        }
    finally:
        capture.release()


def watch_camera(
    seconds=10,
    device=0,
    max_frames=3,
    question="Describe visible activity in this camera frame.",
):
    """Observe a short, bounded set of frames while permission is active."""
    if not camera_permission():
        raise ValueError("Camera is off. Enable Camera in the desktop app first.")
    require_raw_camera_mode()
    import tempfile

    import cv2

    require_raw_camera_mode()
    seconds = min(max(int(seconds), 1), 30)
    max_frames = min(max(int(max_frames), 1), 4)
    (ROOT / "Temp").mkdir(exist_ok=True)
    capture = open_camera(device)
    if not capture.isOpened():
        capture.release()
        raise ValueError("Camera unavailable. Check Windows camera permissions.")
    observations = []
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(dir=ROOT / "Temp") as temp:
            for index in range(max_frames):
                if not camera_permission():
                    raise ValueError("Camera was turned off.")
                target_time = started + index * seconds / max_frames
                while time.monotonic() < target_time:
                    time.sleep(min(0.2, target_time - time.monotonic()))
                ok, frame = capture.read()
                if not ok:
                    raise RuntimeError("The camera stopped returning frames.")
                if not camera_permission():raise ValueError("Camera was turned off.")
                require_raw_camera_mode()
                frame_path = Path(temp) / f"camera-{index}.jpg"
                if not cv2.imwrite(str(frame_path), frame):
                    raise RuntimeError("Could not sample camera frame.")
                if not camera_permission():raise ValueError("Camera was turned off.")
                require_raw_camera_mode()
                description = recognize_image(frame_path, question)["description"]
                observations.append(
                    {
                        "at_seconds": round(time.monotonic() - started, 2),
                        "description": description,
                    }
                )
        return {
            "observations": observations,
            "device": device,
            "limitation": "Only these sampled frames were inspected; no continuous recording or automatic model training occurred.",
        }
    finally:
        capture.release()


def speak(text, output_path, play=False, rate=0):
    target = output_file(output_path, ".wav")
    voice_dir = MEDIA / "models/natural_voice"
    model = voice_dir / "kokoro-v1.0.int8.onnx"
    voices = voice_dir / "voices-v1.0.bin"
    engine = None
    if model.is_file() and voices.is_file():
        try:
            import soundfile as sf
            from kokoro_onnx import Kokoro

            speaker = Kokoro(str(model), str(voices))
            samples, sample_rate = speaker.create(
                str(text)[:12000],
                # Michael is bundled in the local Kokoro voice pack. Keep a calm,
                # conversational pace so status updates sound less robotic.
                voice="am_michael",
                speed=max(0.78, min(1.18, 0.96 + int(rate) * 0.06)),
                lang="en-us",
            )
            sf.write(target, samples, sample_rate)
            engine = "Kokoro natural voice"
        except Exception:
            # A corrupt voice file, missing package, or failed synthesis must
            # not turn a spoken chat reply into silence. SAPI works offline.
            target.unlink(missing_ok=True)
    if engine is None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = min(max(int(rate), -5), 5)
            stream = win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Open(str(target), 3, False)
            try:
                voice.AudioOutputStream = stream
                voice.Speak(str(text)[:12000])
            finally:
                stream.Close()
        finally:
            pythoncom.CoUninitialize()
        engine = "Windows offline speech"
    if play:
        import winsound

        winsound.PlaySound(str(target), winsound.SND_FILENAME)
    return {"path": str(target), "engine": engine}


def _capture_microphone(sd, device: int, rate: int, seconds: int):
    """End a spoken clip after a short pause instead of always waiting eight seconds.

    The whole clip, including leading and trailing silence, still goes to the
    offline recognizer. A silent room returns early so hands-free mode can
    listen again without spending the full long-command window.
    """
    import numpy as np

    block_frames = max(1, round(rate * 0.2))
    maximum = seconds * rate
    pieces = []
    captured = 0
    speaking = False
    last_voice_frame = 0
    with sd.InputStream(samplerate=rate, channels=1, dtype="float32", device=device) as stream:
        while captured < maximum:
            block, _overflowed = stream.read(min(block_frames, maximum - captured))
            if len(block) == 0:
                raise RuntimeError("Microphone returned no audio samples.")
            pieces.append(block)
            captured += len(block)
            # A single sample can spike above the peak threshold in an idle
            # microphone. Require sustained energy as well before treating a
            # block as speech and starting the pause-after-speech timer.
            if (float(np.max(np.abs(block))) >= MIC_VOICE_LEVEL
                    and float(np.sqrt(np.mean(np.square(block)))) >= MIC_VOICE_RMS_LEVEL):
                speaking = True
                last_voice_frame = captured
            if speaking and captured >= rate and captured - last_voice_frame >= round(rate * 1.5):
                break
            if not speaking and captured >= min(seconds, 4) * rate:
                break
    return np.concatenate(pieces, axis=0)


def _select_microphone(sd):
    """Choose the same usable input for preflight and subsequent recording."""
    devices = sd.query_devices()
    preferred = sd.default.device[0]
    candidates = [preferred] if isinstance(preferred, int) and preferred >= 0 else []
    microphones = [
        index
        for index, device in enumerate(devices)
        if device["max_input_channels"] > 0 and "microphone" in device["name"].lower()
    ]
    other_inputs = [
        index
        for index, device in enumerate(devices)
        if device["max_input_channels"] > 0 and index not in microphones
    ]
    candidates.extend(
        index for index in microphones + other_inputs if index not in candidates
    )
    chosen = None
    for index in candidates:
        rate = int(devices[index]["default_samplerate"])
        try:
            sd.check_input_settings(device=index, samplerate=rate, channels=1)
            chosen = index
            break
        except (ValueError, sd.PortAudioError):
            continue
    if chosen is None:
        raise RuntimeError(
            "No working microphone was found. Connect a microphone and try again."
        )
    return chosen, rate, str(devices[chosen]["name"])


def microphone_status():
    """Check the selected input without recording or storing any sound."""
    import sounddevice as sd

    chosen, rate, name = _select_microphone(sd)
    return {"input_device": name, "device_index": chosen, "sample_rate": rate}


def record_audio(seconds=10):
    """Capture a bounded clip from the default or first usable microphone."""
    import tempfile

    import sounddevice as sd
    import soundfile as sf

    seconds = min(max(int(seconds), 1), 30)
    chosen, rate, microphone = _select_microphone(sd)
    (ROOT / "Temp").mkdir(exist_ok=True)
    if hasattr(sd, "InputStream"):
        try:
            audio = _capture_microphone(sd, chosen, rate, seconds)
        except (ValueError, sd.PortAudioError):
            # Keep older audio drivers working if their stream interface fails.
            audio = sd.rec(
                seconds * rate, samplerate=rate, channels=1, dtype="float32", device=chosen
            )
            sd.wait()
    else:
        audio = sd.rec(
            seconds * rate, samplerate=rate, channels=1, dtype="float32", device=chosen
        )
        sd.wait()
    peak = float(abs(audio).max())
    rms = float((audio.astype("float64") ** 2).mean() ** 0.5)
    if peak < MIC_SILENCE_LEVEL:
        # Muted or disconnected inputs otherwise look exactly like a missed
        # wake phrase. Skip Whisper and tell the UI what the device captured.
        return {"text": "", "segments": [], "language": "",
                "input_device": microphone, "input_peak": round(peak, 4),
                "quiet": True}
    with tempfile.TemporaryDirectory(dir=ROOT / "Temp") as temp:
        path = Path(temp) / "dictation.wav"
        sf.write(path, audio, rate)
        transcript = transcribe_audio(path, fast=True)
        # Background hiss can cross the near-silence cutoff. If no words were
        # found and the whole clip stayed below the speech level, give the
        # user the actionable low-input diagnosis. Recognized soft speech wins.
        quiet = not str(transcript.get("text", "")).strip() and (
            peak < MIC_VOICE_LEVEL or rms < MIC_VOICE_RMS_LEVEL
        )
        return {**transcript, "input_device": microphone,
                "input_peak": round(peak, 4), "quiet": quiet}


def download_natural_voice(item=None):
    """Verify both offline voice files before making the new voice available."""
    if item is None:
        item = json.loads((ROOT / "media-models.json").read_text())["natural_voice"]
    folder = MEDIA / "models/natural_voice"
    folder.mkdir(parents=True, exist_ok=True)
    for filename, url_key, hash_key in (
        ("kokoro-v1.0.int8.onnx", "model_url", "model_sha256"),
        ("voices-v1.0.bin", "voices_url", "voices_sha256"),
    ):
        destination = folder / filename
        expected = item[hash_key]
        if (
            destination.is_file()
            and hashlib.sha256(destination.read_bytes()).hexdigest() == expected
        ):
            continue
        temporary_file = destination.with_suffix(destination.suffix + ".partial")
        digest = hashlib.sha256()
        with (
            urlopen(
                Request(item[url_key], headers={"User-Agent": "Cale-AI-Workbench"}),
                timeout=60,
            ) as response,
            temporary_file.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected:
            temporary_file.unlink(missing_ok=True)
            raise RuntimeError("Natural voice file failed its SHA-256 check.")
        os.replace(temporary_file, destination)
    return {"installed": "natural_voice"}


def download_models():
    """Fetch pinned media weights and report each stage to the setup card."""
    from huggingface_hub import snapshot_download

    models = json.loads((ROOT / "media-models.json").read_text())
    installed = []
    for index, (name, item) in enumerate(models.items()):
        if name == "ssd-1b" and not image_upgrade_supported():
            continue
        path = ROOT / "LongTasks/install-progress.json"
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "percent": 80 + round(10 * index / max(1, len(models))),
                    "current": "Downloading a more natural voice"
                    if name == "natural_voice"
                    else "Downloading " + name + " model files",
                    "status": "working",
                    "updated": time.time(),
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        print("Downloading " + name + " model files...", file=sys.stderr, flush=True)
        if name == "natural_voice":
            download_natural_voice(item)
        else:
            snapshot_download(
                item["repo"],
                revision=item["revision"],
                local_dir=str(MEDIA / "models" / name),
                allow_patterns=item["patterns"],
                max_workers=4,
            )
        installed.append(name)
    return {"installed": installed}


def download_whisper():
    """Restore only the pinned offline transcription model after file loss."""
    from huggingface_hub import snapshot_download

    item = json.loads((ROOT / "media-models.json").read_text(encoding="utf-8"))["whisper"]
    snapshot_download(
        item["repo"], revision=item["revision"],
        local_dir=str(MEDIA / "models/whisper"),
        allow_patterns=item["patterns"], max_workers=4,
    )
    return {"installed": "whisper"}


def image_upgrade_complete():
    """Ignore interrupted downloads until all required fp16 weights exist."""
    folder = MEDIA / "models/ssd-1b"
    return all((folder / name).is_file() for name in (
        "model_index.json",
        "unet/diffusion_pytorch_model.fp16.safetensors",
        "vae/diffusion_pytorch_model.fp16.safetensors",
        "text_encoder/model.fp16.safetensors",
        "text_encoder_2/model.fp16.safetensors",
    ))


def image_upgrade_supported():
    """Reserve the larger image model for PCs that can run and store it."""
    import torch

    if image_upgrade_complete():
        return True
    MEDIA.mkdir(exist_ok=True)
    return (
        torch.cuda.is_available()
        and torch.cuda.get_device_properties(0).total_memory >= 8 * 1024**3
        and shutil.disk_usage(MEDIA).free >= 8 * 1024**3
    )


def download_image_upgrade():
    """Fill a pinned model snapshot after an app update, without an account."""
    from huggingface_hub import snapshot_download

    if not image_upgrade_supported():
        return {"installed": False, "reason": "GPU memory or disk space is insufficient"}
    item = json.loads((ROOT / "media-models.json").read_text())["ssd-1b"]
    snapshot_download(
        item["repo"], revision=item["revision"],
        local_dir=str(MEDIA / "models/ssd-1b"),
        allow_patterns=item["patterns"], max_workers=4,
    )
    return {"installed": True, "model": "Segmind SSD-1B"}


def serve_microphone(parent_pid: int) -> None:
    """Handle mic clips in one local process so Whisper stays warm.

    The parent check releases model memory if the desktop is terminated
    without a graceful shutdown. An idle server exits after 90 seconds.
    """
    import psutil

    parent = psutil.Process(parent_pid)
    parent_started = parent.create_time()
    last_request = [time.monotonic()]

    def retire_orphan() -> None:
        while True:
            time.sleep(5)
            if time.monotonic() - last_request[0] > 90:
                os._exit(0)
            try:
                if psutil.Process(parent_pid).create_time() != parent_started:
                    os._exit(0)
            except psutil.Error:
                os._exit(0)

    threading.Thread(target=retire_orphan, daemon=True).start()
    allowed = {"record_audio": record_audio, "microphone_status": microphone_status}
    for line in sys.stdin:
        last_request[0] = time.monotonic()
        try:
            request = json.loads(line)
            action = request.pop("action")
            result = allowed[action](**request)
        except Exception as exc:
            result = {"error": str(exc)}
        print(json.dumps(result), flush=True)


# 3D jobs use a separate Python environment. Their dependencies never replace
# the working speech, image, music or agent packages in this installation.
TRIPOSR_COMMIT = "107cefdc244c39106fa830359024f6a2f1c78871"
TRIPOSR_REVISION = "5b521936b01fbe1890f6f9baed0254ab6351c04a"
U2NETP_SHA256 = "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8"
THREE_FILES = {
    "config.yaml": (f"https://huggingface.co/stabilityai/TripoSR/resolve/{TRIPOSR_REVISION}/config.yaml",
                    "74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62"),
    "model.ckpt": (f"https://huggingface.co/stabilityai/TripoSR/resolve/{TRIPOSR_REVISION}/model.ckpt",
                   "429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee"),
    "dino-config.json": ("https://huggingface.co/facebook/dino-vitb16/resolve/f205d5d8e640a89a2b8ef0369670dfc37cc07fc2/config.json",
                         "b87c0270b97db085fd82cf114a761fd0f62ae7914fbd407c752a2260646b689c"),
}
_three_state_lock = threading.RLock()


def three_home():
    """The override supports isolated developer tests; normal jobs stay in Media."""
    return Path(os.environ.get("CALE_3D_HOME", str(MEDIA / "3d"))).resolve()


def three_job_path(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid 3D job ID.")
    return three_home() / "jobs" / (job_id + ".json")


@contextmanager
def _three_record_lock(path):
    """Serialize progress and cancellation across both processes and threads."""
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with _three_state_lock, path.with_suffix(".lock").open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        for _ in range(200):
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(.025)
        else:
            raise TimeoutError("3D progress record is busy.")
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def three_write(job_id, **changes):
    path = three_job_path(job_id)
    with _three_record_lock(path):
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"job_id": job_id}
        if value.get("state") == "cancelled":
            return value  # A late download/render callback cannot resurrect it.
        value.update(changes, updated_at=time.time())
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        return value


def validate_3d_objects(objects):
    """Accept a bounded declarative scene, never generated Python or shell code."""
    import math

    if not isinstance(objects, list) or not 1 <= len(objects) <= 64:
        raise ValueError("Use 1 to 64 box, sphere, cylinder or cone objects.")
    validated = []
    for item in objects:
        if not isinstance(item, dict) or set(item) - {"type", "size", "position", "rotation", "color"}:
            raise ValueError("Objects support type, size, position, rotation and color only.")
        kind = item.get("type")
        if kind not in {"box", "sphere", "cylinder", "cone"}:
            raise ValueError("Choose box, sphere, cylinder or cone.")
        value = {"type": kind}
        for field, default, low, high in (("size", [1, 1, 1], .001, 1000),
                                          ("position", [0, 0, 0], -10000, 10000),
                                          ("rotation", [0, 0, 0], -360, 360)):
            vector = item.get(field, default)
            if (not isinstance(vector, list) or len(vector) != 3
                    or any(type(x) not in (int, float) or not math.isfinite(x) or not low <= x <= high for x in vector)):
                raise ValueError(f"{field} needs three finite numbers in [{low}, {high}].")
            value[field] = vector
        color = item.get("color", [70, 160, 230, 255])
        if not isinstance(color, list) or len(color) != 4 or any(type(x) is not int or not 0 <= x <= 255 for x in color):
            raise ValueError("color needs four integer RGBA values from 0 to 255.")
        value["color"] = color
        validated.append(value)
    return validated


def three_hardware(neural=True):
    """A single GPU must fit; two small cards do not become one large card."""
    if not neural:
        return {"device": "cpu"}
    import psutil

    if psutil.virtual_memory().total < 16 * 1024**3:
        raise RuntimeError("Image-to-3D needs 16 GB RAM; use CPU scene geometry on smaller PCs.")
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=12, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cards = [tuple(map(int, line.split(","))) for line in result.stdout.splitlines() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError):
        cards = []
    eligible = [(index, total) for index, total in cards if total >= 6000]
    if not eligible:
        raise RuntimeError("Image-to-3D needs one NVIDIA GPU with at least 6 GB VRAM. Use build_3d_scene for CPU geometry.")
    index, total = max(eligible, key=lambda item: item[1])
    return {"device": "cuda", "gpu_index": index, "vram_mb": total}


def start_3d_job(output_path, image_path="", prompt="", objects=None, resolution=128):
    """Return immediately; install/download/generate progress lives in a job record."""
    from PIL import Image

    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() not in {".glb", ".obj", ".stl"} or output.exists():
        raise ValueError("Choose a new GLB, OBJ or STL output filename.")
    if type(resolution) is not int or resolution not in {128, 192, 256}:
        raise ValueError("resolution must be 128, 192 or 256.")
    if not isinstance(prompt, str) or len(prompt) > 2000:
        raise ValueError("Use a prompt of at most 2000 characters.")
    scene = validate_3d_objects(objects) if objects is not None else None
    if scene is not None and (image_path or prompt.strip()):
        raise ValueError("Use either scene objects or image/prompt generation.")
    source = ""
    if image_path:
        image = Path(image_path).expanduser().resolve(strict=True)
        if not image.is_file() or image.stat().st_size > 20_000_000:
            raise ValueError("Choose a local image under 20 MB.")
        with Image.open(image) as opened:
            if opened.width * opened.height > 16_000_000:
                raise ValueError("Use an image with at most 16 million pixels.")
            opened.verify()
        source = str(image)
    if scene is None and not source and len(prompt.strip()) < 5:
        raise ValueError("Supply an image, a descriptive prompt, or scene objects.")
    hardware = three_hardware(scene is None)
    home = three_home()
    existing = home
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < (12 if scene is None else 1) * 1024**3:
        raise RuntimeError("3D setup needs 12 GB free for neural generation, or 1 GB for CPU geometry.")
    job_id = uuid.uuid4().hex
    import music_backend

    task_id = music_backend.project_for_output(str(output))
    three_write(job_id, state="queued", progress_percent=0, engine="TripoSR" if scene is None else "trimesh",
        output_path=str(output), image_path=source, prompt=prompt.strip(), objects=scene,
        resolution=resolution, hardware=hardware, task_id=task_id)
    log = home / "jobs" / (job_id + ".log")
    with log.open("wb") as stream:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--3d-job", job_id],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # The child also writes its identity; the parent must not reset a state
    # which the child has already advanced past queued.
    return {"job_id": job_id, "state": "queued", "worker_pid": proc.pid, "log_path": str(log)}


def three_job_status(job_id):
    import psutil

    path = three_job_path(job_id)
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["state"] == "complete":
        output = Path(value["output_path"])
        if (not output.is_file() or _three_digest(output) != value.get("mesh", {}).get("sha256")):
            value = three_write(job_id, state="failed", error="The exported 3D file is missing or changed since validation.")
    if value["state"] not in {"complete", "failed", "cancelled"} and value.get("worker_pid"):
        try:
            process = psutil.Process(value["worker_pid"])
            alive = _owns_3d_process(process, job_id, value["worker_started"])
        except psutil.Error:
            alive = False
        if not alive:
            value = three_write(job_id, state="failed", error="3D worker exited unexpectedly; inspect the job log.")
    return {key: value[key] for key in ("job_id", "state", "engine", "progress_percent", "detail",
        "output_path", "error", "mesh", "seconds", "updated_at") if key in value}


def _owns_3d_process(process, job_id, started):
    command = process.cmdline()
    return (abs(process.create_time() - started) < .01 and len(command) >= 4
            and Path(command[1]).resolve() == Path(__file__).resolve()
            and command[2:4] == ["--3d-job", job_id])


def cancel_3d_job(job_id):
    """Stop only this exact job's process tree, never a reused PID."""
    import psutil

    value = json.loads(three_job_path(job_id).read_text(encoding="utf-8"))
    if value["state"] in {"complete", "failed", "cancelled"}:
        return three_job_status(job_id)
    if value.get("worker_pid"):
        try:
            process = psutil.Process(value["worker_pid"])
            if not _owns_3d_process(process, job_id, value["worker_started"]):
                raise ValueError("The recorded process no longer belongs to this 3D job.")
            # Save cancellation before terminating; this can also be called by
            # the worker's own task-stop watcher, which terminates this process.
            three_write(job_id, state="cancelled", detail="3D generation cancelled")
            children = process.children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            process.terminate()
            _, alive = psutil.wait_procs(children + [process], timeout=5)
            for child in alive:
                child.kill()
        except psutil.NoSuchProcess:
            pass
    three_write(job_id, state="cancelled", detail="3D generation cancelled")
    return three_job_status(job_id)


def three_task_stopped(task_id):
    """Respect the owning task's Pause/Stop without scanning unrelated tasks."""
    import sqlite3

    if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{12}", task_id):
        return False
    database = ROOT / "LongTasks/projects.db"
    if not database.is_file():
        return True  # An attributed task disappeared; anonymous jobs use no ID.
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
            row = connection.execute("SELECT status FROM projects WHERE id=?", (task_id,)).fetchone()
        return not row or row[0] in {"paused", "stopped"}
    except (OSError, sqlite3.Error):
        return False


def install_3d_runtime(job_id, neural):
    """Install compiler-free pinned dependencies in an isolated Python 3.11."""
    import music_backend

    home = three_home()
    python = home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = home / ("neural-ready-v1" if neural else "geometry-ready-v1")
    if marker.is_file() and python.is_file():
        return python
    import pc_offline

    if pc_offline.offline():
        raise RuntimeError("This 3D runtime is not installed yet. Connect once to download it before working offline.")
    env = os.environ.copy()
    env.update(UV_PYTHON_INSTALL_DIR=str(home / "python"), UV_CACHE_DIR=str(home / "uv-cache"),
               U2NET_HOME=str(home / "background"))
    uv = music_backend._tool("uv")
    hide = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    three_write(job_id, state="installing", progress_percent=5, detail="Installing the local 3D runtime")
    home.mkdir(parents=True, exist_ok=True)
    if not python.is_file():
        subprocess.run([uv, "venv", str(home / ".venv"), "--python", "3.11"],
            env=env, check=True, timeout=600, creationflags=hide)
    packages = ["numpy==1.26.4", "trimesh==4.6.8", "Pillow==10.4.0"]
    if neural:
        subprocess.run([uv, "pip", "install", "--python", str(python), "torch==2.6.0",
                        "--index-url", "https://download.pytorch.org/whl/cu124"],
            env=env, check=True, timeout=3600, creationflags=hide)
        packages += ["transformers==4.35.2", "omegaconf==2.3.0", "einops==0.7.0",
                     "rembg==2.0.57", "onnxruntime==1.20.1",
                     "opencv-python-headless==4.10.0.84", "scikit-image==0.24.0",
                     "scipy==1.14.1", "imageio==2.36.1"]
    subprocess.run([uv, "pip", "install", "--python", str(python), *packages],
        env=env, check=True, timeout=3600, creationflags=hide)
    if neural:
        import video_generation

        source = home / "TripoSR"
        git = video_generation._git_executable(ROOT)
        if not (source / ".git").is_dir():
            subprocess.run([git, "init", str(source)], check=True, timeout=60, creationflags=hide)
            subprocess.run([git, "-C", str(source), "remote", "add", "origin",
                "https://github.com/VAST-AI-Research/TripoSR.git"], check=True, timeout=60, creationflags=hide)
        subprocess.run([git, "-C", str(source), "fetch", "--depth", "1", "origin", TRIPOSR_COMMIT],
            check=True, timeout=600, creationflags=hide)
        subprocess.run([git, "-C", str(source), "checkout", "--detach", TRIPOSR_COMMIT],
            check=True, timeout=60, creationflags=hide)
        models = home / "models"
        models.mkdir(exist_ok=True)
        for name, (url, expected) in THREE_FILES.items():
            target = models / name
            if target.is_file() and _three_digest(target) == expected:
                continue
            three_write(job_id, state="downloading", progress_percent=20,
                        detail="Downloading verified TripoSR weights: " + name)
            temporary = target.with_suffix(".partial")
            digest = hashlib.sha256()
            with urlopen(Request(url, headers={"User-Agent": "Cale-AI-Workbench"}), timeout=90) as response, temporary.open("wb") as output:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                report_at = time.monotonic()
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > 2_000_000_000:
                        raise RuntimeError("3D model download exceeded its size limit.")
                    output.write(chunk)
                    digest.update(chunk)
                    if time.monotonic() - report_at >= 2:
                        three_write(job_id, progress_percent=20 + min(35, int(35 * received / total)) if total else None,
                                    detail=f"Downloading {name}: {received // 1048576} MB")
                        report_at = time.monotonic()
            if digest.hexdigest() != expected:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("3D model checksum mismatch: " + name)
            os.replace(temporary, target)
        # rembg verifies its official u2netp download before caching it. This
        # small background model does not require the separately licensed Bria model.
        three_write(job_id, state="installing", progress_percent=55, detail="Preparing the local background remover")
        subprocess.run([str(python), "-c", "import rembg;rembg.new_session('u2netp')"],
            env=env, check=True, timeout=600, creationflags=hide)
        if _three_digest(home / "background/u2netp.onnx") != U2NETP_SHA256:
            raise RuntimeError("Background-removal model checksum mismatch.")
        (home / "CALE-3D-NOTICE.txt").write_text(
            "TripoSR source and model: MIT, Tripo AI and Stability AI. See TripoSR/LICENSE.\n"
            "Cale uses a scikit-image CPU marching-cubes adapter instead of compiling torchmcubes.\n"
            "Outputs use vertex colors, not baked UV/PBR textures.\n", encoding="utf-8")
    subprocess.run([uv, "pip", "check", "--python", str(python)], env=env, check=True, timeout=60, creationflags=hide)
    marker.write_text(TRIPOSR_COMMIT if neural else "trimesh-4.6.8", encoding="ascii")
    return python


def _three_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_3d_preview(mesh, path):
    """Render an honest mesh thumbnail in software, without OpenGL or a cloud API."""
    import numpy as np
    from PIL import Image, ImageDraw

    vertices = np.asarray(mesh.vertices, dtype=float).copy()
    vertices -= (vertices.min(axis=0) + vertices.max(axis=0)) / 2
    forward = np.array([2., -3., 1.8])
    forward /= np.linalg.norm(forward)
    right = np.cross([0., 0., 1.], forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    coordinates = vertices @ np.array([right, up, forward]).T
    extent = max(float(np.ptp(vertices, axis=0).max()), .001)
    projected = coordinates[:, :2] / (extent * 3 - coordinates[:, 2:])
    span = max(float(np.ptp(projected, axis=0).max()), .001)
    projected -= (projected.min(axis=0) + projected.max(axis=0)) / 2
    pixels = projected * (1200 / span) * np.array([1, -1]) + [768, 768]
    triangles = np.asarray(mesh.faces)
    depth = coordinates[triangles, 2].mean(axis=1)
    colors = np.asarray(mesh.visual.to_color().vertex_colors) if hasattr(mesh.visual, "to_color") else np.asarray(mesh.visual.vertex_colors)
    light = np.array([.4, -.6, .7])
    light /= np.linalg.norm(light)
    shades = .35 + .65 * np.abs(np.asarray(mesh.face_normals) @ light)
    image = Image.new("RGB", (1536, 1536), (13, 20, 34))
    draw = ImageDraw.Draw(image)
    for face in np.argsort(depth):
        color = np.clip(colors[triangles[face], :3].mean(axis=0) * shades[face], 0, 255).astype(int)
        draw.polygon([tuple(point) for point in pixels[triangles[face]]], fill=tuple(map(int, color)))
    image.resize((768, 768), Image.Resampling.LANCZOS).save(path)


def infer_3d(job_id):
    """Run in the isolated environment; never contact an inference service."""
    import numpy as np
    import trimesh

    value = json.loads(three_job_path(job_id).read_text(encoding="utf-8"))
    objects = value.get("objects")
    if objects is not None:
        scene = trimesh.Scene()
        for index, item in enumerate(validate_3d_objects(objects)):
            makers = {"box": lambda: trimesh.creation.box(),
                      "sphere": lambda: trimesh.creation.icosphere(subdivisions=2, radius=.5),
                      "cylinder": lambda: trimesh.creation.cylinder(radius=.5, height=1, sections=32),
                      "cone": lambda: trimesh.creation.cone(radius=.5, height=1, sections=32)}
            mesh = makers[item["type"]]()
            mesh.apply_scale(item["size"])
            mesh.apply_transform(trimesh.transformations.euler_matrix(*np.deg2rad(item["rotation"])))
            mesh.apply_translation(item["position"])
            mesh.visual.vertex_colors = item["color"]
            scene.add_geometry(mesh, node_name=f"{item['type']}-{index}")
        mesh = trimesh.util.concatenate(list(scene.geometry.values()))
    else:
        import types

        import huggingface_hub
        import rembg
        import torch
        from omegaconf import OmegaConf
        from PIL import Image
        from skimage.measure import marching_cubes

        # The upstream helper flips z/y/x after extraction. Match torchmcubes'
        # coordinate convention, so that flip produces scikit-image's xyz.
        def cpu_marching(level, threshold):
            vertices, faces, _, _ = marching_cubes(level.detach().cpu().numpy(), level=threshold)
            return torch.from_numpy(vertices[:, [2, 1, 0]].copy()), torch.from_numpy(faces.astype(np.int64))
        adapter = types.ModuleType("torchmcubes")
        adapter.marching_cubes = cpu_marching
        sys.modules["torchmcubes"] = adapter
        home = three_home()
        for name, (_, expected) in THREE_FILES.items():
            if _three_digest(home / "models" / name) != expected:
                raise RuntimeError("Installed 3D model checksum mismatch: " + name)
        if _three_digest(home / "background/u2netp.onnx") != U2NETP_SHA256:
            raise RuntimeError("Installed background-removal model checksum mismatch.")
        def offline_config(repo_id, filename, **_):
            if repo_id != "facebook/dino-vitb16" or filename != "config.json":
                raise RuntimeError("Unexpected network model dependency blocked.")
            return str(home / "models/dino-config.json")
        huggingface_hub.hf_hub_download = offline_config
        sys.path.insert(0, str(home / "TripoSR"))
        from tsr.system import TSR
        from tsr.utils import resize_foreground

        if not torch.cuda.is_available():
            raise RuntimeError("The 3D PyTorch runtime cannot use CUDA; check the NVIDIA driver.")
        if torch.cuda.mem_get_info()[0] < 5.5 * 1024**3:
            raise RuntimeError("3D generation needs 5.5 GB currently free VRAM; another application is using the GPU.")
        image = Image.open(value["image_path"]).convert("RGBA")
        if image.getextrema()[3][0] == 255:
            image = rembg.remove(image, session=rembg.new_session("u2netp"))
        image = resize_foreground(image, .85)
        pixels = np.asarray(image).astype(np.float32) / 255
        image = Image.fromarray(((pixels[:, :, :3] * pixels[:, :, 3:] + .5 * (1 - pixels[:, :, 3:])) * 255).astype(np.uint8))
        config = OmegaConf.load(home / "models/config.yaml")
        OmegaConf.resolve(config)
        model = TSR(config)
        model.load_state_dict(torch.load(home / "models/model.ckpt", map_location="cpu", weights_only=True))
        model.eval().to("cuda")
        model.renderer.set_chunk_size(4096)
        with torch.inference_mode():
            codes = model([image], device="cuda")
            mesh = model.extract_mesh(codes, has_vertex_color=True, resolution=value["resolution"])[0]
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    if len(mesh.vertices) < 4 or len(mesh.faces) < 4 or not np.isfinite(mesh.vertices).all():
        raise RuntimeError("3D generation produced empty or invalid geometry.")
    target = Path(value["output_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive publication avoids overwriting a file created after the job began.
    payload = mesh.export(file_type=target.suffix[1:])
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    with target.open("xb") as output:
        output.write(payload)
    # STL stores independent triangles; welding equal vertices on reload is
    # necessary before interpreting its watertightness/topology statistics.
    reloaded = trimesh.load(str(target), force="mesh", process=True)
    if len(reloaded.faces) < 4 or not np.isfinite(reloaded.vertices).all():
        raise RuntimeError("The exported 3D model could not be reloaded and validated.")
    preview = three_home() / "jobs" / (job_id + "-preview.png")
    render_3d_preview(reloaded, preview)
    return {"vertices": int(len(reloaded.vertices)), "triangles": int(len(reloaded.faces)),
            "watertight": bool(reloaded.is_watertight), "bounds": reloaded.bounds.tolist(),
            "bytes": target.stat().st_size, "sha256": _three_digest(target),
            "preview_path": str(preview),
            "appearance": "vertex colors" if target.suffix.lower() != ".stl" else "geometry only",
            "note": "Inspect shape and topology before using in a game or 3D printing; no rigging or UV/PBR texture bake is provided."}


def run_3d_job(job_id):
    """Own the long setup and worker lifetime outside the LLM's context window."""
    global ROOT

    import psutil

    import music_backend

    # A development checkout may test against an installed runtime's 3D home.
    # Keep its tools, voice lease and reference-image runtime in that install.
    runtime_root = three_home().parent.parent
    if (runtime_root / "Toolbox").is_dir():
        ROOT = runtime_root
        music_backend.ROOT = ROOT
        music_backend.ENGINE = ROOT / "Media/music/ACE-Step-1.5"
        import cale_voice

        cale_voice.ROOT = ROOT
    started = time.monotonic()
    if json.loads(three_job_path(job_id).read_text(encoding="utf-8"))["state"] == "cancelled":
        return
    three_write(job_id, worker_pid=os.getpid(), worker_started=psutil.Process().create_time())
    if json.loads(three_job_path(job_id).read_text(encoding="utf-8"))["state"] == "cancelled":
        return
    finished = threading.Event()

    def follow_task():
        while not finished.wait(2):
            current = json.loads(three_job_path(job_id).read_text(encoding="utf-8"))
            if current["state"] in {"complete", "failed", "cancelled"}:
                return
            if three_task_stopped(current.get("task_id")):
                cancel_3d_job(job_id)
                return

    threading.Thread(target=follow_task, daemon=True).start()
    try:
        # Share the existing setup lock to keep dependency installers serialized.
        with music_backend._setup_lock():
            value = json.loads(three_job_path(job_id).read_text(encoding="utf-8"))
            python = install_3d_runtime(job_id, value["objects"] is None)
        if value["objects"] is None and not value["image_path"]:
            import quick_image

            three_write(job_id, state="reference", progress_percent=60, detail="Drawing a local reference image")
            reference = three_home() / "jobs" / (job_id + "-reference.png")
            quick_image.generate(ROOT, value["prompt"] + ", single isolated object, plain background, three-quarter view", str(reference))
            three_write(job_id, image_path=str(reference))
        env = os.environ.copy()
        env.update(CALE_3D_HOME=str(three_home()), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   U2NET_HOME=str(three_home() / "background"), OMP_NUM_THREADS="4")
        if value["objects"] is None:
            env["CUDA_VISIBLE_DEVICES"] = str(value["hardware"]["gpu_index"])
        three_write(job_id, state="generating", progress_percent=70, detail="Building and validating the 3D mesh locally")
        with _image_gpu_lease() if value["objects"] is None else nullcontext():
            if value["objects"] is None:
                unload_local_models()
            completed = subprocess.run([str(python), str(Path(__file__).resolve()), "--3d-infer", job_id],
                env=env, cwd=ROOT, capture_output=True, text=True, errors="replace", timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode:
            raise RuntimeError((completed.stderr or completed.stdout)[-1600:])
        mesh = json.loads(completed.stdout.splitlines()[-1])
        if "error" in mesh:
            raise RuntimeError(mesh["error"])
        if json.loads(three_job_path(job_id).read_text(encoding="utf-8"))["state"] == "cancelled":
            return
        three_write(job_id, state="complete", progress_percent=100, mesh=mesh,
                    seconds=round(time.monotonic() - started, 2), detail="3D model exported and reloaded successfully")
    except Exception as error:
        if json.loads(three_job_path(job_id).read_text(encoding="utf-8"))["state"] != "cancelled":
            three_write(job_id, state="failed", error=str(error)[-1800:], detail="3D generation did not finish")
        raise
    finally:
        finished.set()


if __name__ == "__main__":
    try:
        if "--3d-job" in sys.argv:
            run_3d_job(sys.argv[-1])
            sys.exit(0)
        if "--3d-infer" in sys.argv:
            print(json.dumps(infer_3d(sys.argv[-1])))
            sys.exit(0)
        if "--voice-server" in sys.argv:
            serve_microphone(int(sys.argv[sys.argv.index("--voice-server") + 1]))
            sys.exit(0)
        if "--download-models" in sys.argv:
            result = download_models()
        elif "--download-image-upgrade" in sys.argv:
            result = download_image_upgrade()
        elif "--download-voice" in sys.argv:
            result = download_natural_voice()
        elif "--download-whisper" in sys.argv:
            result = download_whisper()
        else:
            request = json.load(sys.stdin)
            action = request.pop("action")
            from camera_network import (
                add_camera,
                discover_onvif,
                list_cameras,
                remove_camera,
                set_greeting_camera,
            )
            from music_analysis import analyze_music
            from scanned_pdf import read_scanned_pdf
            from small_training import (
                ask_adapter,
                predict_classifier,
                train_adapter,
                train_classifier,
            )
            from visitor_memory import forget_visitor, list_visitors

            allowed = {
                "generate_image": generate_image,
                "transcribe_audio": transcribe_audio,
                "recognize_image": recognize_image,
                "analyze_video": analyze_video,
                "capture_camera": capture_camera,
                "speak": speak,
                "record_audio": record_audio,
                "microphone_status": microphone_status,
                "watch_camera": watch_camera,
                "discover_cameras": discover_onvif,
                "add_network_camera": add_camera,
                "list_network_cameras": list_cameras,
                "remove_network_camera": remove_camera,
                "set_greeting_camera": set_greeting_camera,
                "list_visitors": list_visitors,
                "forget_visitor": forget_visitor,
                "train_classifier": train_classifier,
                "predict_classifier": predict_classifier,
                "train_adapter": train_adapter,
                "ask_adapter": ask_adapter,
                "analyze_music": analyze_music,
                "read_scanned_pdf": read_scanned_pdf,
            }
            result = allowed[action](**request)
        # Keep the subprocess protocol ASCII-safe on Windows code pages. JSON
        # escapes preserve Unicode for callers without risking a charmap error.
        print(json.dumps(result), flush=True)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        sys.exit(1)
