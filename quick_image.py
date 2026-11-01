"""Generate a simple image directly, without spending an agent's context."""

import json
import os
import re
import secrets
import subprocess
import uuid
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return prompt
    if not candidate or len(candidate) > 600 or "\n" in candidate:
        return prompt
    stop_words = {"a", "an", "the", "of", "with", "in", "on", "and", "against", "to"}
    terms = {word for word in re.findall(r"[a-z0-9]+", prompt.lower()) if word not in stop_words}
    matched = {word for word in terms if re.search(r"\b" + re.escape(word) + r"\b", candidate.lower())}
    if terms and len(matched) / len(terms) < 0.7:
        return prompt
    return candidate


def generate(root: Path, prompt: str) -> Path:
    """Run the installed local media worker and verify its PNG deliverable."""
    root = Path(root).resolve()
    python = root / "Media/python/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("The local image maker is still installing. Try again when setup finishes.")
    folder = root / "Projects/Quick Images"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"image-{uuid.uuid4().hex[:12]}.png"
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
    if result.returncode or reply.get("error"):
        raise RuntimeError(str(reply.get("error") or result.stderr[-500:]))
    if Path(reply.get("path", "")).resolve() != destination or not destination.is_file():
        raise RuntimeError("The image maker did not save the requested file.")
    with Image.open(destination) as picture:
        picture.verify()
    return destination
