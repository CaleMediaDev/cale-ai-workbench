"""The visible checklist must not call installed-only files 'working'."""

import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import file_catalog
import readiness_checklist


def test_cpu_mesh_success_does_not_claim_neural_3d_is_tested(tmp_path):
    output = tmp_path / "mesh.glb"
    output.write_bytes(b"x" * 200)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "cpu.json").write_text(json.dumps({"state": "complete", "engine": "trimesh",
                                             "output_path": str(output)}))
    assert readiness_checklist._successful_job(jobs, "state", engine="trimesh")
    assert not readiness_checklist._successful_job(jobs, "state", engine="TripoSR")


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
    (whisper / "model.bin").write_bytes(b"weights")
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


def test_removed_voice_or_vision_model_cannot_keep_a_cached_ready_status(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    (state / "readiness-probes.json").write_text(json.dumps({
        "version": "Cale AI Workbench 1",
        "voice": {"ok": True, "detail": "Voice test passed"},
        "vision": {"ok": True, "detail": "Image test passed"},
    }))
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(b'{"models":[]}')):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Voice and listening"]["state"] == "attention"
    assert "missing" in rows["Voice and listening"]["detail"]
    assert rows["Image recognition"]["state"] == "attention"
    assert "missing" in rows["Image recognition"]["detail"]


@pytest.mark.parametrize("vision_model, expected_state", [
    ("cale-vision", "ready"),
    ("cale-vision:latest", "ready"),
    ("cale-vision:other", "attention"),
])
def test_vision_installation_matches_configured_model_tag(tmp_path, vision_model, expected_state):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    (state / "readiness-probes.json").write_text(json.dumps({
        "version": "Cale AI Workbench 1",
        "vision": {"ok": True, "detail": "Image test passed"},
    }))
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    (tmp_path / "Media/settings.json").write_text(json.dumps({
        "vision_model": vision_model,
    }))
    with (patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"models":[{"name":"cale-vision:latest"}]}')),
          patch.object(readiness_checklist.capability_index, "health",
                       return_value={"ok": False}),
          patch.object(readiness_checklist.core_tool_health, "missing", return_value=[]),
          patch.object(readiness_checklist.ffmpeg_runtime, "working", return_value=False)):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Image recognition"]["state"] == expected_state


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


@pytest.mark.parametrize("stdout", ["[]", "null", '"unexpected"', "1"])
@pytest.mark.parametrize("returncode", [0, 1])
def test_media_probe_rejects_non_object_json_without_crashing(tmp_path, stdout, returncode):
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    completed = readiness_checklist.subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr="")
    with patch.object(readiness_checklist.subprocess, "run", return_value=completed):
        result = readiness_checklist._media_probe(tmp_path, "microphone_status")
    assert result == {"ok": False, "detail": "Check failed"}


@pytest.mark.parametrize("stdout", ["[]", "null", '"unexpected"', "1"])
def test_optional_checks_save_results_when_browser_status_is_not_an_object(tmp_path, stdout):
    plugin = tmp_path / "data/plugins/hermes-connector"
    plugin.mkdir(parents=True)
    completed = readiness_checklist.subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr="")
    with (patch.object(readiness_checklist.subprocess, "run", return_value=completed),
          patch.object(readiness_checklist, "_media_probe", return_value={"ok": False})):
        result = readiness_checklist.run_optional_checks(tmp_path)
    assert result["browser"] == {
        "ok": False,
        "detail": "Chrome extension is not connected yet",
    }
    saved = json.loads((tmp_path / "LongTasks/readiness-probes.json").read_text(
        encoding="utf-8"))
    assert saved == result


@pytest.mark.parametrize("payload", [
    b"null", b"[]", b"1", b'{"models":null}',
    b'{"models":{}}', b'{"models":[null]}',
])
def test_malformed_ollama_tags_do_not_abort_readiness_checks(tmp_path, payload):
    with (patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(payload)),
          patch.object(readiness_checklist.capability_index, "health",
                       return_value={"ok": False}),
          patch.object(readiness_checklist.core_tool_health, "missing", return_value=[]),
          patch.object(readiness_checklist.ffmpeg_runtime, "working", return_value=False)):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Local AI"]["state"] == "attention"
    assert rows["Local AI"]["detail"] == "Ollama is not responding yet"

    with (patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(payload)),
          patch.object(readiness_checklist, "_heavy_gpu_active", return_value=False),
          patch.object(readiness_checklist, "_media_probe", return_value={"ok": False}),
          patch.object(readiness_checklist, "_probe_local_ai") as probe):
        result = readiness_checklist.run_optional_checks(tmp_path, test_local=True)
    probe.assert_not_called()
    assert "local_ai" not in result
    saved = json.loads((tmp_path / "LongTasks/readiness-probes.json").read_text(
        encoding="utf-8"))
    assert saved == result


def test_local_ai_probe_requires_a_completed_generated_token():
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"done":true,"eval_count":0}')):
        assert not readiness_checklist._probe_local_ai("cale-fast:latest")["ok"]
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"done":true,"eval_count":1}')):
        assert readiness_checklist._probe_local_ai("cale-fast:latest")["ok"]


@pytest.mark.parametrize("payload", [b"[]", b"null", b'"unexpected"', b"1"])
def test_local_ai_probe_rejects_non_object_json_without_crashing(payload):
    model = "cale-fast:latest"
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(payload)):
        result = readiness_checklist._probe_local_ai(model)
    assert result == {
        "ok": False,
        "model": model,
        "detail": f"{model} did not complete the local test",
    }


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


@pytest.mark.parametrize("kind", ["music", "image"])
def test_background_checks_defer_gpu_model_probes_during_media_work(tmp_path, kind):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    (state / f"{kind}-gpu-active-{os.getpid()}.json").write_text(
        json.dumps({"pid": os.getpid()}))
    image = tmp_path / "assets/cale-logo.png"
    image.parent.mkdir()
    image.write_bytes(b"bundled image")
    with (patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"models":[{"name":"cale-fast:latest","size":1}]}')),
          patch.object(readiness_checklist, "_media_probe", return_value={"ok": False}) as media,
          patch.object(readiness_checklist, "_probe_local_ai") as local):
        readiness_checklist.run_optional_checks(tmp_path, test_local=True)
    local.assert_not_called()
    assert all(call.args[1] != "recognize_image" for call in media.call_args_list)


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


def test_missing_voice_files_queue_background_repair_but_mic_failure_does_not(tmp_path):
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    (tmp_path / "natural_voice.py").touch()
    rows = {"rows": [{"label": "Voice and listening", "state": "attention"}]}
    with patch.object(readiness_checklist.subprocess, "Popen") as launch:
        assert readiness_checklist.request_safe_repairs(tmp_path, rows) == ["Voice and listening"]
    assert str(tmp_path / "natural_voice.py") in launch.call_args.args[0]
    voice = tmp_path / "Media/models/natural_voice"
    whisper = tmp_path / "Media/models/whisper"
    voice.mkdir(parents=True)
    whisper.mkdir(parents=True)
    for name in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        (voice / name).touch()
    for name in ("config.json", "model.bin"):
        (whisper / name).touch()
    with patch.object(readiness_checklist.subprocess, "Popen") as launch:
        assert readiness_checklist.request_safe_repairs(tmp_path, rows) == []
    launch.assert_not_called()


def test_repaired_speech_files_wait_for_a_fresh_probe(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    (state / "readiness-probes.json").write_text(json.dumps({
        "version": "Cale AI Workbench 1", "checked_at": 100,
        "voice": {"ok": True, "detail": "Old speech check"},
    }))
    (state / "natural-voice-progress.json").write_text(json.dumps({
        "status": "complete", "updated": 200,
    }))
    media = tmp_path / "Media"
    python = media / "python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    for name in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        file = media / "models/natural_voice" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    for name in ("config.json", "model.bin"):
        file = media / "models/whisper" / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    with patch.object(readiness_checklist, "urlopen", side_effect=OSError):
        rows = {row["label"]: row for row in readiness_checklist.snapshot(tmp_path)["rows"]}
    assert rows["Voice and listening"]["state"] == "installed"


def test_missing_image_reader_queues_idle_repair_and_invalidates_old_probe(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (tmp_path / ".cale-ai-install").write_text("Cale AI Workbench 1")
    (tmp_path / "model_library.py").touch()
    (state / "readiness-probes.json").write_text(json.dumps({
        "version": "Cale AI Workbench 1", "checked_at": 100,
        "vision": {"ok": True, "detail": "Old image check"},
    }))
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(b'{"models":[]}')):
        result = readiness_checklist.snapshot(tmp_path)
    row = next(item for item in result["rows"] if item["label"] == "Image recognition")
    assert row["state"] == "attention"
    with patch.object(readiness_checklist.subprocess, "Popen") as launch:
        assert readiness_checklist.request_safe_repairs(
            tmp_path, {"rows": [row]}) == ["Image recognition"]
    assert "--repair-vision" in launch.call_args.args[0]
    (state / "vision-repair.json").write_text(json.dumps({"status": "complete", "updated": 200}))
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(b'{"models":[]}')):
        row = next(item for item in readiness_checklist.snapshot(tmp_path)["rows"]
                   if item["label"] == "Image recognition")
    assert row["state"] == "attention"
    runtime = tmp_path / "Media/python/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    with patch.object(readiness_checklist, "urlopen", return_value=io.BytesIO(
            b'{"models":[{"name":"cale-vision:latest"}]}')):
        row = next(item for item in readiness_checklist.snapshot(tmp_path)["rows"]
                   if item["label"] == "Image recognition")
    assert row["state"] == "installed"
