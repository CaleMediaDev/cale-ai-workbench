"""Validate the local ComfyUI job contract without a multi-gigabyte download."""

import json
import hashlib
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import video_generation


def test_video_setup_uses_bundled_git_without_system_git(tmp_path):
    bundled = tmp_path / "data/tools/git-2.53.0+3-win32-x64/cmd/git.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"managed git")
    with patch.object(video_generation.shutil, "which", return_value=None):
        assert video_generation._git_executable(tmp_path) == str(bundled)
    bundled.unlink()
    with patch.object(video_generation.shutil, "which", return_value=None):
        with pytest.raises(RuntimeError, match="managed Git tool is missing"):
            video_generation._git_executable(tmp_path)


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


@pytest.mark.skipif(os.name != "nt", reason="Windows GPU setup")
def test_disk_requirement_shrinks_after_verified_install(tmp_path):
    gb = 1024 ** 3
    output = tmp_path / "new" / "clip.mp4"
    gpu = subprocess.CompletedProcess([], 0, "12288\n", "")
    with patch.object(video_generation.subprocess, "run", return_value=gpu), patch.object(
        video_generation, "_verified_runtime", return_value=False
    ), patch.object(video_generation.shutil, "disk_usage", side_effect=[
        SimpleNamespace(free=34 * gb)
    ]):
        with pytest.raises(RuntimeError, match="35 GB"):
            video_generation._hardware_ready(output)
    with patch.object(video_generation.subprocess, "run", return_value=gpu), patch.object(
        video_generation, "_verified_runtime", return_value=True
    ), patch.object(video_generation.shutil, "disk_usage", side_effect=[
        SimpleNamespace(free=6 * gb), SimpleNamespace(free=2 * gb)
    ]):
        video_generation._hardware_ready(output)
    with patch.object(video_generation.subprocess, "run", return_value=gpu), patch.object(
        video_generation, "_verified_runtime", return_value=True
    ), patch.object(video_generation.shutil, "disk_usage", side_effect=[
        SimpleNamespace(free=6 * gb), SimpleNamespace(free=gb // 2)
    ]):
        with pytest.raises(RuntimeError, match="output drive"):
            video_generation._hardware_ready(output)


@pytest.mark.skipif(os.name != "nt", reason="Windows GPU setup")
def test_missing_nvidia_driver_reports_hardware_requirement(tmp_path):
    with patch.object(video_generation.subprocess, "run", side_effect=FileNotFoundError), patch.object(
        video_generation.shutil, "disk_usage", return_value=SimpleNamespace(free=50 * 1024**3)
    ):
        with pytest.raises(RuntimeError, match="NVIDIA GPU with at least 10 GB VRAM"):
            video_generation._hardware_ready(tmp_path / "clip.mp4")


def test_video_status_reports_latest_sampler_progress(tmp_path):
    job_id = "a" * 32
    with patch.object(video_generation, "JOBS", tmp_path):
        (tmp_path / f"{job_id}.json").write_text(
            json.dumps({"job_id": job_id, "state": "rendering", "detail": "Generating frames"}),
            encoding="utf-8",
        )
        log = tmp_path / f"{job_id}-comfy.log"
        log.write_bytes(b"earlier 40%|####\rnow 65%|######")
        result = video_generation.video_job_status(job_id)
        assert result["progress_percent"] == 65
        assert result["detail"] == "Generating frames"
        (tmp_path / f"{job_id}.json").write_text(
            json.dumps({"job_id": job_id, "state": "complete"}), encoding="utf-8"
        )
        assert video_generation.video_job_status(job_id)["progress_percent"] == 100


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
@pytest.mark.parametrize("lock_name", ["_installation_lock", "_render_lock"])
def test_video_resource_lock_waits_for_another_worker(tmp_path, lock_name):
    script = (
        "import sys,time\nfrom pathlib import Path\nimport video_generation\n"
        "video_generation.BASE=Path(sys.argv[1])\n"
        "with getattr(video_generation,sys.argv[3])():\n"
        " print('acquired', flush=True)\n time.sleep(float(sys.argv[2]))\n"
    )
    first = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), "2", lock_name],
                             cwd=str(video_generation.ROOT), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "acquired"
        started = time.monotonic()
        second = subprocess.run([sys.executable, "-c", script, str(tmp_path), "0", lock_name],
                                cwd=str(video_generation.ROOT), capture_output=True,
                                text=True, timeout=10, check=True)
        assert second.stdout.strip() == "acquired"
        assert time.monotonic() - started >= 1.5
    finally:
        first.wait(timeout=10)
