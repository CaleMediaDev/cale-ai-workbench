"""Validate the local ComfyUI job contract without a multi-gigabyte download."""

import json
from unittest.mock import patch

import pytest

import video_generation


def test_wan_graph_uses_local_5b_nodes_and_bounded_clip():
    graph = video_generation._workflow("A lighthouse at dawn", 2, "vertical", 42)
    assert graph["6"]["inputs"]["text"] == "A lighthouse at dawn"
    assert graph["37"]["inputs"]["unet_name"] == "wan2.2_ti2v_5B_fp16.safetensors"
    assert graph["55"]["inputs"]["width"] == 480
    assert graph["55"]["inputs"]["length"] == 33
    assert graph["3"]["inputs"]["seed"] == 42


def test_video_job_persists_and_validates_inputs(tmp_path):
    jobs = tmp_path / "jobs"
    with patch.object(video_generation, "JOBS", jobs), patch.object(
        video_generation, "_hardware_ready"
    ), patch.object(video_generation.subprocess, "Popen") as popen:
        popen.return_value.pid = 12345
        result = video_generation.start_video_job("A calm forest in the rain",
                                                   str(tmp_path / "clip.mp4"))
        stored = json.loads((jobs / f"{result['job_id']}.json").read_text())
        assert stored["state"] == "starting"
        assert stored["worker_pid"] == 12345
        with pytest.raises(ValueError):
            video_generation.start_video_job("Hi", str(tmp_path / "another.mp4"))
        with pytest.raises(ValueError):
            video_generation.video_job_status("../outside")
