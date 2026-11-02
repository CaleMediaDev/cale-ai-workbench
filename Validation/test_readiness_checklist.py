"""The visible checklist must not call installed-only files 'working'."""

import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import readiness_checklist


def test_checklist_distinguishes_installed_optional_tools_from_checked_core(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 0.5.258")
    (state / "install-progress.json").write_text(json.dumps({"status": "complete"}))
    (state / "heartbeat.txt").write_text("alive")
    (state / "computer-files-first-scan.done").write_text("complete")
    (state / "self-check.json").write_text(json.dumps({
        "version": "Cale AI Workbench 0.5.258", "status": "passed"}))
    (tmp_path / "Media/python/Scripts").mkdir(parents=True)
    (tmp_path / "Media/python/Scripts/python.exe").write_bytes(b"runtime")
    (tmp_path / "Media/models/lcm").mkdir(parents=True)
    (tmp_path / "Media/models/lcm/model_index.json").write_text("{}")
    response = io.BytesIO(b'{"models":[{"name":"cale-fast:latest"}]}')
    with patch.object(readiness_checklist, "urlopen", return_value=response):
        result = readiness_checklist.snapshot(tmp_path)
    assert result["core_ready"]
    assert result["verified"] == 5
    rows = {row["label"]: row for row in result["rows"]}
    assert rows["Image maker"]["state"] == "on_demand"
    assert rows["Music maker"]["state"] == "on_demand"
    assert rows["Local AI"]["state"] == "ready"

    (state / "self-check.json").write_text(json.dumps({
        "version": "Cale AI Workbench 0.5.258", "status": "failed"}))
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"models":[{"name":"cale-fast:latest"}]}')):
        result = readiness_checklist.snapshot(tmp_path)
    assert not result["core_ready"]
    assert result["rows"][-1]["state"] == "attention"


def test_completed_media_job_only_counts_when_its_output_still_exists(tmp_path):
    jobs = tmp_path / "image-jobs"
    jobs.mkdir()
    output = tmp_path / "image.png"
    (jobs / "one.json").write_text(json.dumps({
        "status": "complete", "output_path": str(output)}))
    assert not readiness_checklist._successful_job(jobs, "status")
    output.write_bytes(b"x" * 120)
    assert readiness_checklist._successful_job(jobs, "status")
    (jobs / "one.json").write_text(json.dumps({
        "status": "error", "output_path": str(output)}))
    assert not readiness_checklist._successful_job(jobs, "status")


def test_optional_checks_only_count_for_the_current_install(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    voice = tmp_path / "Media/models/natural_voice"
    voice.mkdir(parents=True)
    for name in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        (voice / name).write_bytes(b"model")
    whisper = tmp_path / "Media/models/whisper"
    whisper.mkdir(parents=True)
    (whisper / "config.json").write_text("{}")
    python = tmp_path / "Media/python/Scripts"
    python.mkdir(parents=True)
    (python / "python.exe").write_bytes(b"runtime")
    browser = tmp_path / "data/plugins/hermes-connector"
    browser.mkdir(parents=True)
    (browser / "plugin.yaml").write_text("version: 0.2.6")
    updates = tmp_path / "Updates"
    updates.mkdir()
    (updates / "features-0.5.16.done").write_text("done")
    (state / "readiness-probes.json").write_text(json.dumps({
        "version": "Cale AI Workbench 1",
        "voice": {"ok": True, "detail": "Voice test passed"},
        "browser": {"ok": False, "detail": "Chrome is not paired"},
    }))
    with patch.object(readiness_checklist, "urlopen", side_effect=OSError):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Voice and listening"]["state"] == "ready"
    assert rows["Browser tools"]["state"] == "attention"
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 2")
    with patch.object(readiness_checklist, "urlopen", side_effect=OSError):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Voice and listening"]["state"] == "installed"
    assert rows["Browser tools"]["state"] == "installed"
