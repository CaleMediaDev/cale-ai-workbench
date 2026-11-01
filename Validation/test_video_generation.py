"""Validate the local ComfyUI job contract without a multi-gigabyte download."""

import json
import hashlib
import os
import subprocess
import sys
import time
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


def test_runtime_requires_all_verified_weights_before_reuse(tmp_path):
    runtime = tmp_path / "runtime"
    models = runtime / "ComfyUI" / "models"
    python = runtime / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    model_hashes = {
        "text_encoders/text.safetensors": hashlib.sha256(b"text").hexdigest(),
        "vae/vae.safetensors": hashlib.sha256(b"vae").hexdigest(),
        "diffusion_models/model.safetensors": hashlib.sha256(b"model").hexdigest(),
    }
    fingerprints = {name: [100_000_001, 1] for name in model_hashes}
    with patch.object(video_generation, "RUNTIME", runtime), patch.object(
        video_generation, "MODEL_HASHES", model_hashes
    ), patch.object(video_generation, "_model_fingerprints", return_value=fingerprints):
        for name, content in (("text_encoders/text.safetensors", b"text"),
                              ("vae/vae.safetensors", b"vae")):
            path = models / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        assert not video_generation._verified_runtime()
        last = models / "diffusion_models/model.safetensors"
        last.parent.mkdir(parents=True)
        last.write_bytes(b"partial")
        assert not video_generation._verified_runtime()
        last.write_bytes(b"model")
        assert video_generation._verified_runtime()
        assert json.loads((runtime / "cale-video-ready.json").read_text()) == fingerprints
        assert video_generation._verified_runtime()


def test_windows_installer_patch_is_exact_and_idempotent(tmp_path):
    installer = tmp_path / "install.ps1"
    installer.write_text(
        "Invoke-Native -FilePath $venvPython -ArgumentList @('-c', $venvCheckCode, $venvPath)\n"
        "Invoke-Native -FilePath $venvPython -ArgumentList @('-c', $verifyCode)\n"
        "& $venvPython -m pip show dac *> $null\nif ($LASTEXITCODE -eq 0) {\n",
        encoding="utf-8",
    )
    video_generation._patch_installer(installer)
    once = installer.read_text(encoding="utf-8")
    assert "cale-check-venv.py" in once
    assert "cale-check-torch.py" in once
    assert "$caleLegacyDacInstalled" in once
    video_generation._patch_installer(installer)
    assert installer.read_text(encoding="utf-8") == once


@pytest.mark.skipif(os.name != "nt", reason="Windows file locking")
def test_runtime_install_lock_waits_for_another_worker(tmp_path):
    script = (
        "import sys,time\nfrom pathlib import Path\nimport video_generation\n"
        "video_generation.BASE=Path(sys.argv[1])\n"
        "with video_generation._installation_lock():\n"
        " print('acquired', flush=True)\n time.sleep(float(sys.argv[2]))\n"
    )
    first = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), "2"],
                             cwd=str(video_generation.ROOT), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "acquired"
        started = time.monotonic()
        second = subprocess.run([sys.executable, "-c", script, str(tmp_path), "0"],
                                cwd=str(video_generation.ROOT), capture_output=True,
                                text=True, timeout=10, check=True)
        assert second.stdout.strip() == "acquired"
        assert time.monotonic() - started >= 1.5
    finally:
        first.wait(timeout=10)
