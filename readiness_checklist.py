"""Show what is installed and what has actually answered a local health check.

The checklist never opens user files, sends telemetry, or downloads models.
Optional media tools can remain on demand on PCs without enough resources.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _item(label: str, state: str, detail: str, essential: bool = False) -> dict:
    return {"label": label, "state": state, "detail": detail, "essential": essential}


def snapshot(root: Path = ROOT) -> dict:
    """Return a bounded set of honest, read-only setup checks for the UI."""
    root = Path(root)
    state = root / "LongTasks"
    media = root / "Media"
    setup = _json(state / "install-progress.json")
    self_check = _json(state / "self-check.json")
    rows = []

    base_ready = setup.get("status") == "complete"
    rows.append(_item("Core setup", "ready" if base_ready else "working",
                      str(setup.get("current") or "Preparing Cale"), True))

    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            tags = json.load(response).get("models", [])
        local_models = [str(model.get("name", "")) for model in tags
                        if str(model.get("name", "")).startswith("cale-")]
        local_state = "ready" if local_models else "working"
        local_detail = (f"Ollama responding; {len(local_models)} Cale model(s) installed"
                        if local_models else "Ollama responding; downloading a local model")
    except (OSError, ValueError, KeyError, TypeError):
        local_state, local_detail = "attention", "Ollama is not responding yet"
    rows.append(_item("Local AI", local_state, local_detail, True))

    heartbeat = state / "heartbeat.txt"
    active_monitor = heartbeat.is_file() and time.time() - heartbeat.stat().st_mtime < 90
    rows.append(_item("Task supervisor", "ready" if active_monitor else "attention",
                      "Running and checking tasks" if active_monitor else "Waiting for supervisor heartbeat", True))

    scan = _json(state / "computer-files-status.json")
    indexed = (state / "computer-files-first-scan.done").is_file() or scan.get("status") == "complete"
    rows.append(_item("Fast file search", "ready" if indexed else "working",
                      "First local index complete" if indexed else "Indexing files on this PC", True))

    media_python = (media / "python/Scripts/python.exe").is_file()
    whisper = (media / "models/whisper/config.json").is_file()
    voice_files = all((media / "models/natural_voice" / name).is_file() for name in
                      ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"))
    voice_ready = media_python and whisper and voice_files
    rows.append(_item("Voice and listening", "installed" if voice_ready else "working",
                      "Models installed; microphone and speakers are checked when used"
                      if voice_ready else "Installing offline speech models"))

    image_ready = media_python and (media / "models/lcm/model_index.json").is_file()
    rows.append(_item("Image maker", "installed" if image_ready else "working",
                      "Local image model installed; render checked on first image"
                      if image_ready else "Installing local image model"))

    music_ready = ((media / "music/ACE-Step-1.5/.cale-model-ready").is_file()
                   and (media / "music/ACE-Step-1.5/.venv/Scripts/python.exe").is_file())
    rows.append(_item("Music maker", "installed" if music_ready else "on_demand",
                      "Local music model installed; render checked on first song"
                      if music_ready else "Installs when this PC can run it or when music is requested"))

    browser_ready = ((root / "Updates/features-0.5.16.done").is_file()
                     and (root / "data/plugins/hermes-connector/plugin.yaml").is_file())
    rows.append(_item("Browser tools", "installed" if browser_ready else "working",
                      "Local companion installed; Chrome pairing is checked when used"
                      if browser_ready else "Preparing local browser tools"))

    video_ready = (media / "video/runtime/cale-video-ready.json").is_file()
    rows.append(_item("Video maker", "installed" if video_ready else "on_demand",
                      "Local video model installed; render checked on first video"
                      if video_ready else "Large video model installs on first use if this PC qualifies"))

    version = (root / ".cale-ai-install").read_text(encoding="utf-8").strip() if (
        root / ".cale-ai-install").is_file() else ""
    checked = self_check.get("version") == version and bool(version)
    check_state = (self_check.get("status", "working") if checked else "working")
    if check_state not in ("passed", "failed", "running"):
        check_state = "working"
    rows.append(_item("Software self-check", "ready" if check_state == "passed" else
                      "attention" if check_state == "failed" else "working",
                      "Packaged checks passed" if check_state == "passed" else
                      "A check failed; Cale will retry" if check_state == "failed" else
                      "Checks are running or waiting for idle time", True))

    essential = [row for row in rows if row["essential"]]
    return {"rows": rows, "core_ready": all(row["state"] == "ready" for row in essential),
            "installed": sum(row["state"] in ("ready", "installed") for row in rows),
            "total": len(rows)}
