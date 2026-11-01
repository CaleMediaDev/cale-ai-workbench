"""Offline speech helpers for the desktop task screen.

Audio is handled by the installed media runtime. No microphone recording or
spoken task text is sent to an online provider by this module.
"""

import json
import os
import re
import subprocess
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIDE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
WAKE = re.compile(r"^\s*hey\s+(?:cale|kale)\b[\s,.:;!-]*", re.IGNORECASE)
IDLE_TIMES = (120, 600, 1800, 3600)
STUCK_TIMES = (120, 480, 1200, 3600)


def speech_chunks(message, limit=280):
    """Split a chat reply without dropping text at the voice engine's limit."""
    return textwrap.wrap(
        str(message), width=limit, break_long_words=True,
        break_on_hyphens=False, replace_whitespace=True, drop_whitespace=True,
    )


def spoken_text(message):
    """Remove common Markdown marks before reading a saved chat reply aloud."""
    value = str(message)
    value = re.sub(r"```[\s\S]*?```", "", value)
    value = re.sub(r"!?(?:\[([^\]]+)\])\([^)]*\)", r"\1", value)
    value = re.sub(r"(?m)^\s*[-=]{3,}\s*$", "", value)
    value = re.sub(r"[*_`#>]", "", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def opening_line(mode, subject="", sequence=0):
    """Rotate short, truthful greetings across app launches."""
    if mode == "setup":
        lines = (
            "Hey, Cale here. I'm getting your AI ready. First up: {subject}.",
            "Cale checking in. I'm setting things up, and right now it's {subject}.",
            "Hey, it's Cale. I'm on the setup job. Current step: {subject}.",
            "Cale here. Give me a minute to get the workshop ready. I'm {subject}.",
        )
    elif mode == "working":
        lines = (
            "Hey, Cale here. I'm back on {subject}.",
            "Cale checking in. I'm working through {subject}.",
            "Hey, it's Cale. {subject} is on my bench right now.",
            "Cale here. I'm still moving on {subject}.",
        )
    else:
        lines = (
            "Hey, Cale here. I'm ready when you are.",
            "Cale checking in. Give me something interesting to work on.",
            "Hey, it's Cale. The workshop is open. What are we building?",
            "Cale here. I'm awake, the tools are ready, and I'm listening when you need me.",
        )
    return lines[int(sequence) % len(lines)].format(subject=str(subject).strip())


def progress_line(title, step, sequence=0):
    """Put a little character around a real task step without inventing work."""
    title = str(title).strip()
    step = str(step).strip().rstrip(".")
    lines = (
        "{title}. {step}.",
        "Quick update on {title}: {step}.",
        "Here's where {title} stands: {step}.",
        "On {title}, the current step is {step}.",
    )
    return lines[int(sequence) % len(lines)].format(title=title, step=step)


def rephrase_status(line, recent=(), required=()):
    """Give scripted speech fresh wording with the fast *local* model.

    This is only for app-authored status lines. Chat replies are spoken exactly
    as displayed. Model failure leaves the original, factual line intact.
    """
    from cloud_ai import FAST_MODEL, post

    original = str(line).strip()
    if not original:
        return original
    previous = [str(item).strip() for item in recent if str(item).strip()][-6:]
    facts = [str(item).strip() for item in required if str(item).strip()]
    messages = [
        {"role": "system", "content": (
            "You write one short spoken status line for Cale, a friendly, witty male "
            "desktop assistant. Reword the supplied line so it sounds natural and "
            "different from recent lines. Keep every fact and the same intent. "
            "Include each required phrase exactly. Do not invent progress, completed "
            "work, dates, features, or user decisions. No quotation marks, labels, "
            "markdown, profanity, or extra sentences. Stay under 35 words."
        )},
        {"role": "user", "content": (
            f"Line to vary: {original}\n"
            f"Required phrases: {', '.join(facts) if facts else '(none)'}\n"
            f"Recently spoken: {' | '.join(previous) if previous else '(none)'}"
        )},
    ]
    try:
        result = post(
            "http://127.0.0.1:11434/v1/chat/completions",
            {"model": FAST_MODEL, "messages": messages, "stream": False,
             "max_tokens": 72, "temperature": 0.85, "reasoning_effort": "none"},
            timeout=12,
        )
        candidate = str(result["choices"][0]["message"].get("content") or "").strip().strip('"')
    except (KeyError, IndexError, OSError, ValueError, TimeoutError):
        return original
    if (
        not candidate or len(candidate) > 240 or "\n" in candidate
        or candidate.casefold() == original.casefold()
        or candidate.casefold() in {item.casefold() for item in previous}
        or any(phrase.casefold() not in candidate.casefold() for phrase in facts)
    ):
        return original
    return candidate


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
    if re.fullmatch(
        r"(?:can you hear me|do you hear me|are you listening|mic check|test (?:the )?mic)",
        normalized,
    ):
        return ("hearing", "")
    if normalized.startswith("new task "):
        return ("new", phrase[9:].strip())
    return ("note", phrase)


def background_prompt(
    kind, elapsed, previous_slot=-1, title="", mic_off=True, variation=0
):
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
            (
                "I hit a snag on {name}. I need your call to keep going.",
                "Okay, {name} needs your input. Tell me what you want me to try.",
                "I've reached a fork in {name}, and you're the tie breaker.",
            ),
            (
                "Still stuck on {name}. I have tools, but I need a decision from you.",
                "I'm getting impatient with this block on {name}. Point me in a direction.",
                "That roadblock on {name} is wearing out its welcome. I need your help.",
            ),
            (
                "All right, now I'm annoyed. {name} is waiting on your answer.",
                "This block on {name} is officially getting on my nerves. Help me break it.",
                "I could finish {name} if this one hold-up would move. What do you think?",
            ),
            (
                "I'm properly mad at this roadblock on {name}. Give me a way around it.",
                "{name} is still blocked, and yes, I'm grumpy about it. I need your call.",
                "I refuse to pretend {name} is done. This block needs your decision.",
            ),
        )
        rate = (0, 1, 2, 3)[min(slot, 3)]
    else:
        context = f"I finished {title}. " if title else "I'm all caught up. "
        lines = (
            (
                "Hey, I'm bored. Give me a task. {mic}",
                "The tools are sitting here looking lonely. Got a job for me? {mic}",
                "I'm free, and I'd rather be making something. What's next? {mic}",
            ),
            (
                "I'm still here. I could research, build, or sort something out for you.",
                "I have a clear desk and a suspicious amount of energy. What's next?",
                "All quiet on my end. Give me a problem worth chewing on.",
            ),
            (
                "Okay, now I'm very bored. Please give me something to build.",
                "I've been idle long enough to name the progress bars. Got a project?",
                "This is the part where you hand me a wild idea and I get to work.",
            ),
            (
                "Still waiting. I could really use a new project.",
                "I'm here, I'm ready, and this is getting comically quiet.",
                "Your AI is fully warmed up and dramatically underemployed.",
            ),
        )
        rate = (-1, 0, 1, 1)[min(slot, 3)]
    choices = lines[min(slot, 3)]
    line = choices[(int(variation) + slot) % len(choices)]
    if kind == "stuck":
        line = line.format(name=name)
    else:
        mic = (
            "If you're talking to me, I can't hear you because your mic is off."
            if mic_off
            else ""
        )
        line = context + line.format(mic=mic).strip()
    return (slot, line, rate)


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
        import cale_voice

        if cale_voice.ready():
            try:
                return cale_voice.speak(str(message)[:280], Path(folder) / "update.wav")
            except (OSError, RuntimeError, ValueError):
                # Keep spoken updates available if the clone worker exits or
                # a particular phrase cannot be synthesized.
                pass
        return media_call(
            "speak",
            text=str(message)[:280],
            output_path=str(Path(folder) / "update.wav"),
            play=True,
            rate=rate,
        )
