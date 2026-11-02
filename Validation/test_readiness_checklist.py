"""The visible checklist must not call installed-only files 'working'."""

import io
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import readiness_checklist
import file_catalog


def test_checklist_distinguishes_installed_optional_tools_from_checked_core(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 0.5.258")
    (state / "install-progress.json").write_text(json.dumps({"status": "complete"}))
    (state / "heartbeat.txt").write_text("alive")
    (state / "computer-files-first-scan.done").write_text("complete")
    known = tmp_path / "known.txt"
    known.write_text("local file")
    with file_catalog.database(state) as connection:
        connection.execute("INSERT INTO entries(path,name,parent,kind,scan_id) VALUES (?,?,?,?,?)",
                           (str(known), known.name, str(tmp_path), "file", "check"))
    (state / "self-check.json").write_text(json.dumps({
        "version": "Cale AI Workbench 0.5.258", "status": "passed"}))
    (tmp_path / "Media/python/Scripts").mkdir(parents=True)
    (tmp_path / "Media/python/Scripts/python.exe").write_bytes(b"runtime")
    (tmp_path / "Media/models/lcm").mkdir(parents=True)
    (tmp_path / "Media/models/lcm/model_index.json").write_text("{}")
    response = io.BytesIO(b'{"models":[{"name":"cale-fast:latest"}]}')
    with patch.object(readiness_checklist, "urlopen", return_value=response):
        result = readiness_checklist.snapshot(tmp_path)
    assert not result["core_ready"]
    assert result["verified"] == 4
    rows = {row["label"]: row for row in result["rows"]}
    assert rows["Image maker"]["state"] == "on_demand"
    assert rows["Music maker"]["state"] == "on_demand"
    assert rows["Local AI"]["state"] == "working"
    assert rows["Fast file search"]["state"] == "ready"

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


def test_local_browser_is_ready_without_optional_chrome_pairing(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    (state / "readiness-probes.json").write_text(json.dumps({
        "version": "Cale AI Workbench 1",
        "browser": {"ok": False, "detail": "Chrome extension is not connected yet"},
    }))
    with (patch.object(readiness_checklist, "urlopen", side_effect=OSError),
          patch.object(readiness_checklist.core_tool_health, "missing", return_value=[])):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Browser tools"]["state"] == "ready"
    assert "Local browser tested" in rows["Browser tools"]["detail"]
    assert "Chrome tabs need your approval" in rows["Browser tools"]["detail"]


def test_real_local_completion_verifies_a_currently_installed_model(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    with sqlite3.connect(state / "usage.db") as connection:
        connection.execute("CREATE TABLE usage_events (timestamp REAL,source TEXT,model TEXT,completion_tokens INTEGER)")
        connection.execute("INSERT INTO usage_events VALUES (?,?,?,?)",
                           (time.time(), "ollama", "cale-fast", 5))
    assert readiness_checklist._recent_local_completion(tmp_path, ["cale-fast:latest"]) == "cale-fast"
    assert readiness_checklist._recent_local_completion(tmp_path, ["cale-other:latest"]) == ""
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"models":[{"name":"cale-fast:latest"}]}')):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Local AI"]["state"] == "ready"


def test_completed_scan_marker_does_not_hide_a_missing_search_database(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (state / "computer-files-first-scan.done").write_text("complete")
    assert not readiness_checklist._file_index_responds(state)
    with patch.object(readiness_checklist, "urlopen", side_effect=OSError):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Fast file search"]["state"] == "attention"


def test_local_ai_probe_requires_a_completed_generated_token():
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"done":true,"eval_count":0}')):
        assert not readiness_checklist._probe_local_ai("cale-fast:latest")["ok"]
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"done":true,"eval_count":1}')):
        assert readiness_checklist._probe_local_ai("cale-fast:latest")["ok"]


def test_background_checks_do_not_probe_a_model_with_real_usage(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    with sqlite3.connect(state / "usage.db") as connection:
        connection.execute("CREATE TABLE usage_events (timestamp REAL,source TEXT,model TEXT,completion_tokens INTEGER)")
        connection.execute("INSERT INTO usage_events VALUES (?,?,?,?)",
                           (time.time(), "ollama", "cale-fast", 3))
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    response = io.BytesIO(b'{"models":[{"name":"cale-fast:latest","size":1}]}')
    with patch.object(readiness_checklist, "urlopen", return_value=response), \
         patch.object(readiness_checklist, "_media_probe", return_value={"ok": False}), \
         patch.object(readiness_checklist, "_probe_local_ai") as probe:
        readiness_checklist.run_optional_checks(tmp_path, test_local=True)
    probe.assert_not_called()


def test_image_recognition_check_uses_bundled_image_and_requires_answer(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    image = tmp_path / "assets/cale-logo.png"
    image.parent.mkdir()
    image.write_bytes(b"bundled image")
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"runtime")

    def media_probe(_root, action, **kwargs):
        if action == "recognize_image":
            assert kwargs["path"] == str(image)
            return {"ok": True, "data": {"description": "A colorful C logo"}}
        return {"ok": False}

    tags = b'{"models":[{"name":"cale-vision:latest"}]}'
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(tags)), \
         patch.object(readiness_checklist, "_media_probe", side_effect=media_probe):
        probes = readiness_checklist.run_optional_checks(tmp_path, test_local=True)
    assert probes["vision"]["ok"]
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(tags)):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Image recognition"]["state"] == "ready"


def test_safe_repair_is_queued_automatically_once_without_browser_pairing(tmp_path):
    rows = {"rows": [
        {"label": "Fast file search", "state": "attention"},
        {"label": "Browser tools", "state": "attention"},
    ]}
    marker = tmp_path / "LongTasks/computer-files-recheck.request"
    assert readiness_checklist.request_safe_repairs(tmp_path, rows) == ["Fast file search"]
    assert marker.is_file()
    assert readiness_checklist.request_safe_repairs(tmp_path, rows) == []
    assert not any(tmp_path.rglob("*pair*"))
