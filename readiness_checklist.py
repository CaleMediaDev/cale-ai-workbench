"""Show what is installed and what has actually answered a local health check.

The checklist only reads local status records and file metadata. It never
sends telemetry, opens a person's deliverables, or downloads models.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _item(label: str, state: str, detail: str, essential: bool = False) -> dict:
    return {"label": label, "state": state, "detail": detail, "essential": essential}


def _successful_job(folder: Path, status_key: str, *, min_bytes: int = 100) -> bool:
    """A completed job with a saved deliverable is evidence of real local use.

    Only the newest 100 small job records are examined. The output itself is
    never opened, and a deleted output cannot count as a successful check.
    """
    try:
        records = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime,
                         reverse=True)[:100]
    except OSError:
        return False
    for path in records:
        job = _json(path)
        if job.get(status_key) != "complete":
            continue
        output = job.get("output_path")
        if not isinstance(output, str) or not output:
            continue
        try:
            if Path(output).is_file() and Path(output).stat().st_size >= min_bytes:
                return True
        except OSError:
            continue
    return False


def _media_probe(root: Path, action: str, **arguments) -> dict:
    """Run an actual offline media operation with a bounded timeout."""
    python = root / "Media/python/Scripts/python.exe"
    if not python.is_file():
        return {"ok": False, "detail": "Media runtime is missing"}
    try:
        result = subprocess.run(
            [str(python), str(root / "media_worker.py")],
            input=json.dumps({"action": action, **arguments}), text=True,
            capture_output=True, timeout=45, cwd=root,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        answer = json.loads(result.stdout) if result.stdout.strip() else {}
        if result.returncode or not isinstance(answer, dict) or answer.get("error"):
            return {"ok": False, "detail": str(answer.get("error") or "Check failed")[:180]}
        return {"ok": True, "data": answer}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": type(exc).__name__}


def _recent_local_completion(root: Path, available: list[str]) -> str:
    """A recorded, completed local call proves more than Ollama's model list."""
    database = root / "LongTasks/usage.db"
    if not database.is_file() or not available:
        return ""
    aliases = {name.split(":", 1)[0] for name in available}
    try:
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True,
                             timeout=2) as connection:
            rows = connection.execute(
                "SELECT model FROM usage_events WHERE source='ollama' "
                "AND completion_tokens > 0 AND timestamp > ? "
                "ORDER BY timestamp DESC LIMIT 100", (time.time() - 7 * 86400,)
            ).fetchall()
        return next((name for (name,) in rows if str(name).split(":", 1)[0] in aliases), "")
    except (OSError, sqlite3.Error):
        return ""


def _file_index_responds(state: Path) -> bool:
    """Query the actual filename FTS index without opening file contents."""
    database = state / "computer-files.sqlite3"
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True,
                             timeout=2) as connection:
            rows = connection.execute(
                "SELECT name,path FROM entries WHERE kind='file' LIMIT 100"
            ).fetchall()
            if not rows:
                # A completed scan of an empty drive is still a valid index.
                connection.execute("SELECT rowid FROM entry_fts LIMIT 1").fetchone()
                return True
            for name, path in rows:
                words = re.findall(r"[^\W_]+", name, re.UNICODE)
                if not words or not Path(path).is_file():
                    continue
                match = connection.execute(
                    "SELECT rowid FROM entry_fts WHERE entry_fts MATCH ? LIMIT 1",
                    (f'"{words[0]}"',),
                ).fetchone()
                if match:
                    return True
        return False
    except (OSError, sqlite3.Error):
        return False


def _probe_local_ai(model: str) -> dict:
    """Generate one token only when the task supervisor says Ollama is idle."""
    payload = json.dumps({"model": model, "prompt": "Say OK.", "stream": False,
                          "think": False, "options": {"num_predict": 1},
                          "keep_alive": "1m"}).encode("utf-8")
    request = Request("http://127.0.0.1:11434/api/generate", data=payload,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=45) as response:
            answer = json.load(response)
        good = bool(answer.get("done") and int(answer.get("eval_count") or 0) > 0)
        return {"ok": good, "model": model,
                "detail": f"{model} answered a local test" if good
                          else f"{model} did not complete the local test"}
    except (OSError, ValueError, TypeError, TimeoutError) as exc:
        return {"ok": False, "model": model,
                "detail": f"{model} did not answer within 45 seconds ({type(exc).__name__})"}


def run_optional_checks(root: Path = ROOT, *, force: bool = False,
                        test_local: bool = False) -> dict:
    """Test speech generation, speech recognition, microphone access and Chrome.

    This makes a short temporary WAV without playing it or recording the mic.
    Browser status is read from the local broker; no tab is opened or changed.
    """
    root = Path(root)
    result = {"version": (root / ".cale-ai-install").read_text(encoding="utf-8").strip()
              if (root / ".cale-ai-install").is_file() else "", "checked_at": time.time()}
    previous = _json(root / "LongTasks/readiness-probes.json")
    if previous.get("version") == result["version"] and previous.get("local_ai", {}).get("ok"):
        result["local_ai"] = previous["local_ai"]
    elif test_local:
        try:
            with urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
                models = json.load(response).get("models", [])
            candidates = [model for model in models if str(model.get("name", "")).startswith("cale-")]
            candidates.sort(key=lambda model: int(model.get("size") or 0))
            preferred = next((model for model in candidates if model.get("name") == "cale-fast:latest"),
                             candidates[0] if candidates else None)
            if preferred and not _recent_local_completion(
                    root, [str(model["name"]) for model in candidates]):
                result["local_ai"] = _probe_local_ai(str(preferred["name"]))
        except (OSError, ValueError, TypeError):
            pass
    if (not force and previous.get("version") == result["version"]
            and previous.get("voice", {}).get("ok")):
        result["voice"] = previous["voice"]
    else:
        microphone = _media_probe(root, "microphone_status")
        sample = root / "assets/cale-wake-ack.wav"
        recognition = (_media_probe(root, "transcribe_audio", path=str(sample), fast=True)
                       if sample.is_file() else {"ok": False, "detail": "Speech sample is missing"})
        recognition_ok = bool(recognition["ok"] and
                              str(recognition.get("data", {}).get("text", "")).strip())
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "voice-check.wav"
            speech = _media_probe(root, "speak", text="Cale is ready.",
                                  output_path=str(output), play=False)
            speech_ok = bool(speech["ok"] and output.is_file() and output.stat().st_size > 1000)
        result["voice"] = {
            "ok": bool(microphone["ok"] and speech_ok and recognition_ok),
            "microphone": microphone["ok"], "speech": speech_ok,
            "recognition": recognition_ok,
            "detail": ("Microphone responds; speech rendered and recognized offline"
                       if microphone["ok"] and speech_ok and recognition_ok else
                       "Mic " + ("✓" if microphone["ok"] else "failed") +
                       " · speaking " + ("✓" if speech_ok else "failed") +
                       " · recognition " + ("✓" if recognition_ok else "failed")),
        }
    plugin_parent = root / "data/plugins/hermes-connector"
    if plugin_parent.is_dir():
        code = ("import json,sys;sys.path.insert(0,sys.argv[1]);"
                "from plugin.bridge_client import BridgeClient;"
                "client=BridgeClient('readiness',root=sys.argv[2],auto_start_broker=False);"
                "client.start(wait=2);print(json.dumps(client.status()));client.stop()")
        try:
            check = subprocess.run(
                [sys.executable, "-c", code, str(plugin_parent), str(root / "data")],
                text=True, capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            browser = json.loads(check.stdout) if check.returncode == 0 else {}
            result["browser"] = {"ok": bool(browser.get("paired")),
                                 "detail": "Chrome extension is connected" if browser.get("paired")
                                 else "Chrome extension is not connected yet"}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result["browser"] = {"ok": False, "detail": "Could not reach the local browser companion"}
    else:
        result["browser"] = {"ok": False, "detail": "Browser companion is missing"}
    destination = root / "LongTasks/readiness-probes.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result), encoding="utf-8")
    return result


def snapshot(root: Path = ROOT) -> dict:
    """Return a bounded set of honest, read-only setup checks for the UI."""
    root = Path(root)
    state = root / "LongTasks"
    media = root / "Media"
    setup = _json(state / "install-progress.json")
    self_check = _json(state / "self-check.json")
    probes = _json(state / "readiness-probes.json")
    version = (root / ".cale-ai-install").read_text(encoding="utf-8").strip() if (
        root / ".cale-ai-install").is_file() else ""
    if probes.get("version") != version:
        probes = {}
    rows = []

    base_ready = setup.get("status") == "complete"
    rows.append(_item("Core setup", "ready" if base_ready else "working",
                      str(setup.get("current") or "Preparing Cale"), True))

    try:
        with urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
            tags = json.load(response).get("models", [])
        local_models = [str(model.get("name", "")) for model in tags
                        if str(model.get("name", "")).startswith("cale-")]
        completed_model = _recent_local_completion(root, local_models)
        local_probe = probes.get("local_ai", {})
        local_state = ("ready" if local_models and (completed_model or local_probe.get("ok")) else
                       "attention" if local_probe else "working")
        local_detail = (f"{completed_model} has answered a real task locally"
                        if completed_model else local_probe.get("detail", "Local model installed; testing when idle"
                                                                if local_models else "Downloading a local model"))
    except (OSError, ValueError, KeyError, TypeError):
        local_state, local_detail = "attention", "Ollama is not responding yet"
    rows.append(_item("Local AI", local_state, local_detail, True))

    heartbeat = state / "heartbeat.txt"
    active_monitor = heartbeat.is_file() and time.time() - heartbeat.stat().st_mtime < 90
    rows.append(_item("Task supervisor", "ready" if active_monitor else "attention",
                      "Running and checking tasks" if active_monitor else "Waiting for supervisor heartbeat", True))

    scan = _json(state / "computer-files-status.json")
    indexed = (state / "computer-files-first-scan.done").is_file() or scan.get("status") == "complete"
    index_responds = indexed and _file_index_responds(state)
    rows.append(_item("Fast file search", "ready" if index_responds else
                      "attention" if indexed else "working",
                      "Filename search answered a local index query" if index_responds else
                      "Index is present but search did not answer; Cale will retry the scan"
                      if indexed else "Indexing files on this PC", True))

    media_python = (media / "python/Scripts/python.exe").is_file()
    whisper = (media / "models/whisper/config.json").is_file()
    voice_files = all((media / "models/natural_voice" / name).is_file() for name in
                      ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"))
    voice_ready = media_python and whisper and voice_files
    voice_probe = probes.get("voice", {})
    voice_state = ("ready" if voice_probe.get("ok") else "attention" if voice_probe
                   else "installed" if voice_ready else "working")
    rows.append(_item("Voice and listening", voice_state,
                      voice_probe.get("detail", "Models installed; choose Run checks to test mic and speech"
                                      if voice_ready else "Installing offline speech models")))

    image_ready = media_python and (media / "models/lcm/model_index.json").is_file()
    image_verified = image_ready and _successful_job(state / "image-jobs", "status")
    rows.append(_item("Image maker", "ready" if image_verified else
                      "on_demand" if image_ready else "working",
                      "Rendered an image successfully on this PC" if image_verified else
                      "Model ready; Cale verifies it automatically on the first image"
                      if image_ready else "Installing local image model"))

    music_ready = ((media / "music/ACE-Step-1.5/.cale-model-ready").is_file()
                   and (media / "music/ACE-Step-1.5/.venv/Scripts/python.exe").is_file())
    music_verified = music_ready and _successful_job(state / "music-jobs", "status", min_bytes=1000)
    rows.append(_item("Music maker", "ready" if music_verified else "on_demand",
                      "Rendered a song successfully on this PC" if music_verified else
                      "Model ready; Cale verifies it automatically on the first song"
                      if music_ready else "Installs when this PC can run it or when music is requested"))

    browser_ready = ((root / "Updates/features-0.5.16.done").is_file()
                     and (root / "data/plugins/hermes-connector/plugin.yaml").is_file())
    browser_probe = probes.get("browser", {})
    browser_state = ("ready" if browser_probe.get("ok") else "attention" if browser_probe
                     else "installed" if browser_ready else "working")
    rows.append(_item("Browser tools", browser_state,
                      browser_probe.get("detail", "Companion installed; choose Run checks to test Chrome pairing"
                                        if browser_ready else "Preparing local browser tools")))

    video_ready = (media / "video/runtime/cale-video-ready.json").is_file()
    video_verified = video_ready and _successful_job(media / "video/jobs", "state", min_bytes=1000)
    rows.append(_item("Video maker", "ready" if video_verified else "on_demand",
                      "Rendered a video successfully on this PC" if video_verified else
                      "Model ready; Cale verifies it automatically on the first video"
                      if video_ready else "Large video model installs on first use if this PC qualifies"))

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
            "verified": sum(row["state"] == "ready" for row in rows),
            "installed": sum(row["state"] in ("ready", "installed") for row in rows),
            "total": len(rows)}


def request_safe_repairs(root: Path = ROOT, result: dict | None = None) -> list[str]:
    """Queue local fixes that need no browser approval or user interaction.

    The supervisor owns the actual scan and setup recovery. A small cooldown
    avoids restarting a slow scan every time the checklist refreshes.
    """
    root = Path(root)
    result = result or snapshot(root)
    rows = {row["label"]: row for row in result.get("rows", [])}
    requested = []
    if rows.get("Fast file search", {}).get("state") == "attention":
        marker = root / "LongTasks/computer-files-recheck.request"
        try:
            if not marker.is_file() or time.time() - marker.stat().st_mtime > 300:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(str(time.time()), encoding="ascii")
                requested.append("Fast file search")
        except OSError:
            pass
    return requested
