"""Generate a simple image directly, without spending an agent's context."""

import json
import os
import subprocess
import uuid
from pathlib import Path

from PIL import Image


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
        "action": "generate_image", "prompt": prompt,
        "output_path": str(destination), "width": 512, "height": 512,
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
