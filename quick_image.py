"""Generate a simple image directly, without spending an agent's context."""

import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from http.client import HTTPException
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image


def clarify_prompt(prompt: str) -> str:
    """Use the small local model to make ambiguous image subjects concrete.

    This is best effort: an unavailable or busy text model must not prevent the
    offline image maker from using the person's original request.
    """
    if not prompt.strip() or len(prompt) > 500:
        return prompt
    request = Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps({
            "model": "cale-fast", "stream": False, "think": False,
            "options": {"num_ctx": 8192, "num_predict": 100},
            "messages": [
                {"role": "system", "content": (
                    "Rewrite the user image request as one concise, concrete visual prompt. "
                    "Preserve every requested attribute. If a noun could mean different things, "
                    "name visible defining features of the most common intended object in this "
                    "context (shape, material, parts). Do not add other subjects, weather, props, "
                    "or backgrounds. Output only the prompt."
                )},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            candidate = json.load(response)["message"]["content"].strip().strip('"')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException):
        return prompt
    if not candidate or len(candidate) > 600 or "\n" in candidate:
        return prompt
    stop_words = {"a", "an", "the", "of", "with", "in", "on", "and", "against", "to"}
    terms = {word for word in re.findall(r"[a-z0-9]+", prompt.lower()) if word not in stop_words}
    matched = {word for word in terms if re.search(r"\b" + re.escape(word) + r"\b", candidate.lower())}
    if terms and len(matched) / len(terms) < 0.7:
        return prompt
    return candidate


def generate(root: Path, prompt: str, output_path: str = "") -> Path:
    """Run the installed local media worker and verify its PNG deliverable."""
    root = Path(root).resolve()
    python = root / "Media/python/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("The local image maker is still installing. Try again when setup finishes.")
    folder = root / "Projects/Quick Images"
    destination = Path(output_path).resolve() if output_path else folder / f"image-{uuid.uuid4().hex[:12]}.png"
    if destination.suffix.lower() != ".png" or destination.exists():
        raise ValueError("Choose a new PNG output file for this image.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = {
        "action": "generate_image", "prompt": clarify_prompt(prompt),
        "output_path": str(destination), "width": 512, "height": 512,
        "seed": secrets.randbelow(2**31),
    }
    result = subprocess.run(
        [str(python), str(root / "media_worker.py")],
        input=json.dumps(request), text=True, capture_output=True,
        timeout=900, cwd=root, env=os.environ.copy(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        reply = json.loads(result.stdout.splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("The image maker did not return a usable result.") from exc
    if not isinstance(reply, dict):
        raise RuntimeError("The image maker did not return a usable result.")
    if result.returncode != 0 or reply.get("error"):
        error_msg = reply.get("error") or result.stderr[-500:]
        raise RuntimeError(str(error_msg))
    reported_path = reply.get("path")
    if (not isinstance(reported_path, str)
            or Path(reported_path).resolve() != destination
            or not destination.is_file()):
        raise RuntimeError("The image maker did not save the requested file.")
    with Image.open(destination) as picture:
        if picture.format != "PNG":
            raise ValueError("The image maker did not save a PNG image.")
        picture.verify()
    with Image.open(destination) as picture:
        picture.load()
    return destination


def image_file_ok(path: str | Path) -> bool:
    """Check that an image has real decodable pixels before review."""
    source = Path(path)
    if not source.is_file():
        return False
    try:
        with Image.open(source) as picture:
            if picture.format != "PNG":
                return False
            picture.verify()
        with Image.open(source) as picture:
            picture.load()
        return True
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False


def _job_path(root: Path, job_id: str) -> Path:
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise ValueError("Invalid local image job ID.")
    return Path(root) / "LongTasks" / "image-jobs" / (job_id + ".json")


def _write_job(root: Path, job_id: str, info: dict) -> None:
    path = _job_path(root, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def image_job_status(root: Path, job_id: str) -> dict:
    path = _job_path(root, job_id)
    if not path.is_file():
        raise ValueError("Image job not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def _launch_job(root: Path, job_id: str) -> None:
    root = Path(root).resolve()
    python = root / "Toolbox/python/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("The local Toolbox runtime is missing.")
    log_path = _job_path(root, job_id).with_suffix(".log")
    with log_path.open("ab") as log:
        subprocess.Popen(
            [str(python), str(root / "quick_image.py"), "--run-job", job_id],
            cwd=root, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _worker_running(root: Path, job_id: str, info: dict) -> bool:
    import psutil

    try:
        pid = int(info.get("worker_pid") or 0)
        if pid <= 0:
            return False
        command = psutil.Process(pid).cmdline()
        return "--run-job" in command and job_id in command and any(
            str(item).endswith("quick_image.py") for item in command
        )
    except (psutil.NoSuchProcess, psutil.ZombieProcess, ValueError, TypeError, OverflowError):
        return False
    except psutil.AccessDenied:
        return True


def start_image_job(root: Path, prompt: str, output_path: str) -> dict:
    """Start a durable image render that survives the chat window closing."""
    root = Path(root).resolve()
    target = Path(output_path).resolve()
    if not prompt.strip() or target.suffix.lower() != ".png" or target.exists():
        raise ValueError("Describe an image and choose a new PNG output file.")
    job_id = uuid.uuid4().hex
    info = {
        "job_id": job_id, "status": "queued", "current": "Starting local image",
        "prompt": prompt, "output_path": str(target), "created": time.time(),
    }
    _write_job(root, job_id, info)
    try:
        _launch_job(root, job_id)
    except (OSError, RuntimeError) as exc:
        _write_job(root, job_id, {**info, "status": "error", "current": str(exc)[:300]})
        raise
    return {"job_id": job_id, "status": "queued", "output_path": str(target)}


def hold_image_job(root: Path, job_id: str, *, paused: bool = False) -> dict:
    """Stop this worker and its media subprocess, leaving other jobs alone."""
    import psutil

    info = image_job_status(root, job_id)
    if info.get("status") in ("complete", "error", "stopped", "paused"):
        return info
    if _worker_running(root, job_id, info):
        try:
            worker = psutil.Process(int(info["worker_pid"]))
            children = worker.children(recursive=True)
            for child in children:
                child.terminate()
            _, alive = psutil.wait_procs(children, timeout=10)
            for child in alive:
                child.kill()
            psutil.wait_procs(alive, timeout=5)
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except psutil.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        except psutil.AccessDenied as exc:
            raise RuntimeError("Permission denied stopping the image worker.") from exc
        except psutil.TimeoutExpired as exc:
            raise RuntimeError("The image worker has not stopped yet. Try again shortly.") from exc
    info = image_job_status(root, job_id)
    if info.get("status") == "complete":
        return info
    state = "paused" if paused else "stopped"
    held = {**info, "status": state, "current": state.capitalize() + " by user",
            "worker_pid": None, "finished": time.time()}
    _write_job(root, job_id, held)
    return held


def resume_image_job(root: Path, job_id: str) -> dict:
    info = image_job_status(root, job_id)
    if info.get("status") != "paused":
        raise ValueError("Only a paused image job can be continued.")
    output = Path(info["output_path"])
    if image_file_ok(output):
        completed = {**info, "status": "complete", "current": "Image is ready",
                     "result": {"path": str(output)}, "finished": time.time()}
        _write_job(root, job_id, completed)
        return completed
    output.unlink(missing_ok=True)
    resumed = {**info, "status": "queued", "current": "Resuming local image",
               "worker_pid": None, "created": time.time()}
    _write_job(root, job_id, resumed)
    try:
        _launch_job(root, job_id)
    except (OSError, RuntimeError) as exc:
        _write_job(root, job_id, {**resumed, "status": "error", "current": str(exc)[:300]})
        raise
    return resumed


def recover_image_jobs(root: Path) -> None:
    """Restart interrupted image jobs once, without duplicating live workers."""
    jobs = Path(root) / "LongTasks" / "image-jobs"
    if not jobs.is_dir():
        return
    for path in jobs.glob("*.json"):
        try:
            job_id = path.stem
            info = image_job_status(root, job_id)
            if not isinstance(info, dict):
                continue
            if info.get("job_id") != job_id or info.get("status") not in ("queued", "working"):
                continue
            if info["status"] == "queued" and time.time() - float(info.get("created", 0)) < 30:
                continue
            if _worker_running(root, job_id, info):
                continue
            output = Path(info["output_path"])
            if image_file_ok(output):
                _write_job(root, job_id, {**info, "status": "complete", "current": "Image is ready",
                                          "result": {"path": str(output)}, "finished": time.time()})
                continue
            attempts = int(info.get("recoveries", 0)) + 1
            if attempts > 3:
                _write_job(root, job_id, {**info, "status": "error",
                                          "current": "Image stopped after repeated retries.", "finished": time.time()})
                continue
            output.unlink(missing_ok=True)
            _write_job(root, job_id, {**info, "status": "queued", "current": "Resuming local image",
                                      "worker_pid": None, "recoveries": attempts, "created": time.time()})
            _launch_job(root, job_id)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, OverflowError):
            continue


def _run_job(root: Path, job_id: str) -> None:
    info = image_job_status(root, job_id)
    if info.get("status") not in ("queued", "working"):
        return
    working = {**info, "status": "working", "current": "Generating image locally",
               "worker_pid": os.getpid()}
    _write_job(root, job_id, working)
    try:
        path = generate(root, info["prompt"], info["output_path"])
        _write_job(root, job_id, {**info, "status": "complete", "current": "Image is ready",
                                  "result": {"path": str(path)}, "finished": time.time()})
    except Exception as exc:
        _write_job(root, job_id, {**info, "status": "error", "current": str(exc)[:300],
                                  "finished": time.time()})


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--run-job":
    _run_job(Path(__file__).resolve().parent, sys.argv[2])
