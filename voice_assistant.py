"""Offline speech helpers for the desktop task screen.

Audio is handled by the installed media runtime. No microphone recording or
spoken task text is sent to an online provider by this module.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def media_call(action, **arguments):
    """Run one bounded media action and return its final JSON response."""
    python = ROOT / "Media/python/Scripts/python.exe"
    if not python.is_file():
        raise RuntimeError("Voice is still installing. Please try again after setup.")
    result = subprocess.run(
        [str(python), str(ROOT / "media_worker.py")],
        input=json.dumps({"action": action, **arguments}),
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        timeout=180,
        creationflags=HIDE,
    )
    try:
        answer = json.loads(result.stdout.splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("The local voice service did not respond.") from exc
    if result.returncode or "error" in answer:
        raise RuntimeError(str(answer.get("error", "Local voice failed.")))
    return answer


def listen(seconds=8):
    """Record a short local clip, then transcribe it offline."""
    answer = media_call("record_audio", seconds=seconds)
    return answer.get("text", "").strip()


def say(message):
    """Speak one short status message through Windows' offline voice."""
    temp = ROOT / "Temp"
    temp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp) as folder:
        media_call(
            "speak",
            text=str(message)[:280],
            output_path=str(Path(folder) / "update.wav"),
            play=True,
        )
