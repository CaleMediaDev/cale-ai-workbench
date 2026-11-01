"""Local image, speech and bounded video/camera tools; JSON in and JSON out."""

import base64
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
MEDIA = ROOT / "Media"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"


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


def generate_image(prompt, output_path, width=512, height=512, seed=0):
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


def transcribe_audio(path):
    """Transcribe a local audio file with the installed offline speech model."""
    from faster_whisper import WhisperModel

    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError("Audio or video file not found.")
    model = WhisperModel(
        str(MEDIA / "models/whisper"),
        device="cpu",
        compute_type="int8",
        local_files_only=True,
    )
    segments, info = model.transcribe(str(source), beam_size=3, vad_filter=True)
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


def capture_camera(output_path, device=0):
    """Save one camera frame only while the user's permission is active."""
    if not camera_permission():
        raise ValueError(
            "Camera is off. Enable Camera in the desktop app before requesting a capture."
        )
    import cv2

    target = output_file(output_path, ".png")
    capture = cv2.VideoCapture(int(device), cv2.CAP_DSHOW)
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
        if not cv2.imwrite(str(target), frame):
            raise RuntimeError("Camera image could not be saved.")
        return {
            "path": str(target),
            "device": device,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
    import tempfile

    import cv2

    seconds = min(max(int(seconds), 1), 30)
    max_frames = min(max(int(max_frames), 1), 4)
    (ROOT / "Temp").mkdir(exist_ok=True)
    capture = cv2.VideoCapture(int(device), cv2.CAP_DSHOW)
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
                frame_path = Path(temp) / f"camera-{index}.jpg"
                if not cv2.imwrite(str(frame_path), frame):
                    raise RuntimeError("Could not sample camera frame.")
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
    if model.is_file() and voices.is_file():
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
    else:
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


def record_audio(seconds=10):
    """Capture a bounded clip from the default or first usable microphone."""
    import tempfile

    import sounddevice as sd
    import soundfile as sf

    seconds = min(max(int(seconds), 1), 30)
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
    (ROOT / "Temp").mkdir(exist_ok=True)
    audio = sd.rec(
        seconds * rate, samplerate=rate, channels=1, dtype="float32", device=chosen
    )
    sd.wait()
    with tempfile.TemporaryDirectory(dir=ROOT / "Temp") as temp:
        path = Path(temp) / "dictation.wav"
        sf.write(path, audio, rate)
        return transcribe_audio(path)


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


if __name__ == "__main__":
    try:
        if "--download-models" in sys.argv:
            result = download_models()
        elif "--download-image-upgrade" in sys.argv:
            result = download_image_upgrade()
        elif "--download-voice" in sys.argv:
            result = download_natural_voice()
        else:
            request = json.load(sys.stdin)
            action = request.pop("action")
            from music_analysis import analyze_music
            from small_training import (
                ask_adapter,
                predict_classifier,
                train_adapter,
                train_classifier,
            )

            allowed = {
                "generate_image": generate_image,
                "transcribe_audio": transcribe_audio,
                "recognize_image": recognize_image,
                "analyze_video": analyze_video,
                "capture_camera": capture_camera,
                "speak": speak,
                "record_audio": record_audio,
                "watch_camera": watch_camera,
                "train_classifier": train_classifier,
                "predict_classifier": predict_classifier,
                "train_adapter": train_adapter,
                "ask_adapter": ask_adapter,
                "analyze_music": analyze_music,
            }
            result = allowed[action](**request)
        # Keep the subprocess protocol ASCII-safe on Windows code pages. JSON
        # escapes preserve Unicode for callers without risking a charmap error.
        print(json.dumps(result), flush=True)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        sys.exit(1)
