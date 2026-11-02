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
    assert rows["Image maker"]["state"] == "installed"
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
