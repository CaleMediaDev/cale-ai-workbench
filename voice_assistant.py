"""Offline speech helpers for the desktop task screen.

Audio is handled by the installed media runtime. No microphone recording or
spoken task text is sent to an online provider by this module.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
WAKE = re.compile(r"^\s*hey\s+(?:cale|kale)\b[\s,.:;!-]*", re.IGNORECASE)
IDLE_TIMES = (120, 600, 1800, 3600)
STUCK_TIMES = (120, 480, 1200, 3600)


def spoken_command(transcript, require_wake=False):
    """Map a spoken phrase to a task action without an extra AI call.

    Hands-free clips require the assistant's name so ambient conversation does
    not become a task. Talk-button clips are already user-initiated.
    """
    phrase = str(transcript).strip()
    wake = WAKE.match(phrase)
    if require_wake and not wake:
        return None
    if wake:
        phrase = phrase[wake.end() :].strip()
    phrase = phrase.rstrip(".!? ")
    if not phrase:
        return None
    normalized = phrase.lower()
    if re.fullmatch(r"(?:pause|pause (?:the |my )?task)", normalized):
        return ("pause", "")
    if re.fullmatch(
        r"(?:continue|resume|keep going|continue (?:the |my )?task)", normalized
    ):
        return ("resume", "")
    if re.fullmatch(r"(?:stop|stop (?:the |my )?task)", normalized):
        return ("stop", "")
    if re.fullmatch(r"(?:open (?:the |my )?(?:files|task files|folder))", normalized):
        return ("open", "")
    if re.fullmatch(
        r"(?:what(?:'s| is) (?:the )?status|status|what are you doing)", normalized
    ):
        return ("status", "")
    if normalized.startswith("new task "):
        return ("new", phrase[9:].strip())
    return ("note", phrase)


def background_prompt(kind, elapsed, previous_slot=-1, title="", mic_off=True):
    """Return one progressively stronger spoken prompt for a new time slot.

    The final tone repeats at most once every four hours. The caller owns the
    clock and remembers the last spoken slot, so ordinary UI polling stays quiet.
    """
    thresholds = STUCK_TIMES if kind == "stuck" else IDLE_TIMES
    slot = sum(elapsed >= seconds for seconds in thresholds) - 1
    if slot >= 3:
        slot += max(0, int((elapsed - thresholds[-1]) // 14400))
    if slot < 0 or slot <= previous_slot:
        return None
    name = title or "this task"
    if kind == "stuck":
        lines = (
            f"I'm stuck on {name}. I need your help to get moving.",
            f"I'm getting frustrated. {name} is still blocked. Please tell me what to try next.",
            f"I'm really annoyed at this roadblock on {name}. I still need your help.",
            f"Now I'm mad at this roadblock on {name}. I cannot finish until we get past it.",
        )
        rate = (0, 1, 2, 3)[min(slot, 3)]
    else:
        context = f"I finished {title}. " if title else "I'm all caught up. "
        lines = (
            context
            + (
                "Hey, if you're talking to me, I can't hear you because your mic is off. I'm bored and I need you to give me a task."
                if mic_off
                else "Hey, I'm bored and I need you to give me a task."
            ),
            context + "I'm still here and ready for something new.",
            context + "Okay, I'm very bored now. Please give me something to build.",
            context + "I could really use a new project. I'm waiting right here.",
        )
        rate = (-1, 0, 1, 1)[min(slot, 3)]
    return (slot, lines[min(slot, 3)], rate)


def media_call(action, **arguments):
    """Run one bounded media action and return its final JSON response."""
    python = ROOT / "Media/python/Scripts/python.exe"
    voice_files = ROOT / "Media/models/natural_voice"
    natural_ready = (voice_files / "kokoro-v1.0.int8.onnx").is_file() and (
        voice_files / "voices-v1.0.bin"
    ).is_file()
    if action == "speak" and not natural_ready:
        # The lighter toolbox runtime is ready before image and audio models.
        # This lets setup speak while those larger downloads are still running.
        python = ROOT / "Toolbox/python/Scripts/python.exe"
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


def say(message, rate=0):
    """Speak one short status message through Windows' offline voice."""
    temp = ROOT / "Temp"
    temp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp) as folder:
        return media_call(
            "speak",
            text=str(message)[:280],
            output_path=str(Path(folder) / "update.wav"),
            play=True,
            rate=rate,
        )
