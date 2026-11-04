"""Validate the local ComfyUI job contract without a multi-gigabyte download."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
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
def test_hardware_check_handles_missing_runtime_parent(tmp_path):
    base = tmp_path / "Media" / "video"
    output = tmp_path / "exports" / "clip.mp4"
    checked_paths = []

    def disk_usage(path):
        path = Path(path)
        if not path.is_dir():
            raise FileNotFoundError(str(path))
        checked_paths.append(path)
        return SimpleNamespace(free=50 * 1024 ** 3)

    gpu = subprocess.CompletedProcess([], 0, "12288\n", "")
    with patch.object(video_generation, "BASE", base), patch.object(
        video_generation, "_verified_runtime", return_value=False
    ), patch.object(video_generation.shutil, "disk_usage", side_effect=disk_usage), patch.object(
        video_generation.subprocess, "run", return_value=gpu
    ):
        video_generation._hardware_ready(output)

    assert checked_paths == [tmp_path, tmp_path]
    assert not base.parent.exists()
    assert not output.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows GPU setup")
def test_missing_nvidia_driver_reports_hardware_requirement(tmp_path):
    with patch.object(video_generation.subprocess, "run", side_effect=FileNotFoundError), patch.object(
        video_generation.shutil, "disk_usage", return_value=SimpleNamespace(free=50 * 1024**3)
    ):
        with pytest.raises(RuntimeError, match="NVIDIA GPU with at least 10 GB VRAM"):
            video_generation._hardware_ready(tmp_path / "clip.mp4")


def test_worker_launch_failure_persists_failed_status(tmp_path):
    jobs = tmp_path / "jobs"
    with patch.object(video_generation, "JOBS", jobs), patch.object(
        video_generation, "_hardware_ready"
    ), patch.object(
        video_generation.subprocess, "Popen",
        side_effect=FileNotFoundError("worker executable missing"),
    ):
        with pytest.raises(FileNotFoundError, match="worker executable missing"):
            video_generation.start_video_job(
                "A calm forest in the rain", str(tmp_path / "clip.mp4")
            )
        records = list(jobs.glob("*.json"))
        assert len(records) == 1
        stored = json.loads(records[0].read_text(encoding="utf-8"))
        assert stored["state"] == "failed"
        assert "worker_pid" not in stored
        status = video_generation.video_job_status(stored["job_id"])
        assert status["state"] == "failed"
        assert status["error"] == "FileNotFoundError: worker executable missing"
        assert status["log_path"] == str(jobs / f"{stored['job_id']}.log")
        assert status["progress_percent"] is None



def test_worker_log_failure_is_visible_and_preserves_request(tmp_path):
    jobs = tmp_path / "jobs"
    original_open = Path.open

    def open_file(path, *args, **kwargs):
        if path.parent == jobs and path.suffix == ".log":
            raise PermissionError("worker log denied")
        return original_open(path, *args, **kwargs)

    with patch.object(video_generation, "JOBS", jobs), patch.object(
        video_generation, "_hardware_ready"
    ), patch.object(video_generation.subprocess, "Popen") as launch, patch.object(
        Path, "open", open_file
    ):
        with pytest.raises(PermissionError, match="worker log denied"):
            video_generation.start_video_job("A calm forest", str(tmp_path / "clip.mp4"))
        launch.assert_not_called()
        stored = json.loads(next(jobs.glob("*.json")).read_text(encoding="utf-8"))
        assert stored["prompt"] == "A calm forest"
        assert stored["output_path"] == str((tmp_path / "clip.mp4").resolve())
        assert "worker_pid" not in stored
        status = video_generation.video_job_status(stored["job_id"])
        assert status["state"] == "failed"
        assert status["error"] == "PermissionError: worker log denied"
        assert status["progress_percent"] is None


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


def test_mp4_export_preserves_destination_created_after_exists_check(tmp_path):
    job_id = "b" * 32
    runtime = tmp_path / "runtime"
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    rendered = runtime / "ComfyUI" / "output" / "render.mp4"
    rendered.parent.mkdir(parents=True)
    rendered.write_bytes(b"rendered video" * 200)
    destination = tmp_path / "clip.mp4"
    original = b"existing user video" * 200
    destination.write_bytes(original)
    original_exists = Path.exists

    def exists_before_concurrent_creation(path):
        # Model a file created just after the export's existence check.
        if path == destination:
            return False
        return original_exists(path)

    history = {
        "prompt-1": {
            "status": {"completed": True},
            "outputs": {"58": {"videos": [{"filename": "render.mp4"}]}},
        }
    }
    data = {
        "prompt": "A calm forest", "seconds": 2, "layout": "wide",
        "image_path": "", "output_path": str(destination),
    }
    with patch.object(video_generation, "RUNTIME", runtime), patch.object(
        video_generation, "JOBS", jobs
    ), patch.object(video_generation, "_write"), patch.object(
        video_generation, "_free_port", return_value=8188
    ), patch.object(video_generation, "_workflow", return_value={}), patch.object(
        video_generation, "_request",
        side_effect=[{}, {"prompt_id": "prompt-1"}, history],
    ), patch.object(video_generation.subprocess, "Popen") as launch, patch.object(
        Path, "exists", exists_before_concurrent_creation
    ):
        launch.return_value.poll.return_value = None
        with pytest.raises(FileExistsError):
            video_generation._render_job(job_id, data)
        launch.return_value.terminate.assert_called_once()
        launch.return_value.wait.assert_called_once_with(timeout=20)

    assert destination.read_bytes() == original
    assert rendered.read_bytes() == b"rendered video" * 200


@pytest.mark.parametrize("size", [0, 999])
def test_undersized_mp4_leaves_destination_available_for_retry(tmp_path, size):
    job_id = "f" * 32
    runtime = tmp_path / "runtime"
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    rendered = runtime / "ComfyUI" / "output" / "render.mp4"
    rendered.parent.mkdir(parents=True)
    content = b"x" * size
    rendered.write_bytes(content)
    destination = tmp_path / "clip.mp4"
    history = {
        "prompt-1": {
            "status": {"completed": True},
            "outputs": {"58": {"videos": [{"filename": "render.mp4"}]}},
        }
    }
    data = {
        "prompt": "A calm forest", "seconds": 2, "layout": "wide",
        "image_path": "", "output_path": str(destination),
    }

    with patch.object(video_generation, "RUNTIME", runtime), patch.object(
        video_generation, "JOBS", jobs
    ), patch.object(video_generation, "_write") as write, patch.object(
        video_generation, "_free_port", return_value=8188
    ), patch.object(video_generation, "_workflow", return_value={}), patch.object(
        video_generation, "_request",
        side_effect=[{}, {"prompt_id": "prompt-1"}, history],
    ), patch.object(video_generation.subprocess, "Popen") as launch:
        launch.return_value.poll.return_value = None
        with pytest.raises(RuntimeError, match="generated video file is empty"):
            video_generation._render_job(job_id, data)
        assert not any(call.kwargs.get("state") == "complete" for call in write.call_args_list)
        launch.return_value.terminate.assert_called_once()
        launch.return_value.wait.assert_called_once_with(timeout=20)

    assert not destination.exists()
    assert rendered.read_bytes() == content


def test_failed_mp4_copy_removes_partial_destination(tmp_path):
    job_id = "d" * 32
    runtime = tmp_path / "runtime"
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    rendered = runtime / "ComfyUI" / "output" / "render.mp4"
    rendered.parent.mkdir(parents=True)
    content = b"rendered video" * 200
    rendered.write_bytes(content)
    destination = tmp_path / "clip.mp4"
    history = {
        "prompt-1": {
            "status": {"completed": True},
            "outputs": {"58": {"videos": [{"filename": "render.mp4"}]}},
        }
    }
    data = {
        "prompt": "A calm forest", "seconds": 2, "layout": "wide",
        "image_path": "", "output_path": str(destination),
    }

    def fail_after_partial_copy(source, target):
        target.write(source.read(100))
        raise OSError("export write failed")

    with patch.object(video_generation, "RUNTIME", runtime), patch.object(
        video_generation, "JOBS", jobs
    ), patch.object(video_generation, "_write") as write, patch.object(
        video_generation, "_free_port", return_value=8188
    ), patch.object(video_generation, "_workflow", return_value={}), patch.object(
        video_generation, "_request",
        side_effect=[{}, {"prompt_id": "prompt-1"}, history],
    ), patch.object(video_generation.subprocess, "Popen") as launch, patch.object(
        video_generation.shutil, "copyfileobj", side_effect=fail_after_partial_copy
    ):
        launch.return_value.poll.return_value = None
        with pytest.raises(OSError, match="export write failed"):
            video_generation._render_job(job_id, data)
        assert not any(call.kwargs.get("state") == "complete" for call in write.call_args_list)
        launch.return_value.terminate.assert_called_once()
        launch.return_value.wait.assert_called_once_with(timeout=20)

    assert not destination.exists()
    assert rendered.read_bytes() == content


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, "ffmpeg"),
    subprocess.TimeoutExpired("ffmpeg", 3600),
])
def test_failed_conversion_leaves_no_partial_export(tmp_path, failure):
    job_id = "e" * 32
    runtime = tmp_path / "runtime"
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    rendered = runtime / "ComfyUI" / "output" / "render.webm"
    rendered.parent.mkdir(parents=True)
    rendered.write_bytes(b"original render" * 200)
    destination = tmp_path / "clip.mp4"
    conversion_paths = []
    history = {
        "prompt-1": {
            "status": {"completed": True},
            "outputs": {"58": {"videos": [{"filename": "render.webm"}]}},
        }
    }
    data = {
        "prompt": "A calm forest", "seconds": 2, "layout": "wide",
        "image_path": "", "output_path": str(destination),
    }

    def fail_conversion(command, **kwargs):
        partial = Path(command[-1])
        conversion_paths.append(partial)
        partial.write_bytes(b"partial conversion")
        raise failure

    with patch.object(video_generation, "RUNTIME", runtime), patch.object(
        video_generation, "JOBS", jobs
    ), patch.object(video_generation, "_write") as write, patch.object(
        video_generation, "_free_port", return_value=8188
    ), patch.object(video_generation, "_workflow", return_value={}), patch.object(
        video_generation, "_request",
        side_effect=[{}, {"prompt_id": "prompt-1"}, history],
    ), patch.object(video_generation.subprocess, "Popen") as launch, patch.object(
        video_generation.imageio_ffmpeg, "get_ffmpeg_exe", return_value="ffmpeg"
    ), patch.object(video_generation.subprocess, "run", side_effect=fail_conversion):
        launch.return_value.poll.return_value = None
        with pytest.raises(type(failure)):
            video_generation._render_job(job_id, data)
        assert not any(call.kwargs.get("state") == "complete" for call in write.call_args_list)
        launch.return_value.terminate.assert_called_once()
        launch.return_value.wait.assert_called_once_with(timeout=20)

    assert not destination.exists()
    assert len(conversion_paths) == 1
    assert not conversion_paths[0].exists()
    assert rendered.read_bytes() == b"original render" * 200


def test_render_cleanup_waits_for_killed_server(tmp_path):
    job_id = "c" * 32
    events = []

    def wait_for_exit(timeout=None):
        events.append(("wait", timeout))
        if timeout is not None:
            raise subprocess.TimeoutExpired("ComfyUI", timeout)
        return 0

    with patch.object(video_generation, "JOBS", tmp_path), patch.object(
        video_generation, "_write"
    ), patch.object(video_generation, "_free_port", return_value=8188), patch.object(
        video_generation.subprocess, "Popen"
    ) as launch:
        server = launch.return_value
        server.poll.return_value = 1
        server.terminate.side_effect = lambda: events.append(("terminate", None))
        server.kill.side_effect = lambda: events.append(("kill", None))
        server.wait.side_effect = wait_for_exit

        with pytest.raises(RuntimeError, match="exited during startup"):
            video_generation._render_job(job_id, {})

    assert events == [
        ("terminate", None), ("wait", 20), ("kill", None), ("wait", None)
    ]


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


def test_runtime_marker_publication_uses_independent_temporary_files(tmp_path):
    runtime = tmp_path / "runtime"
    python = runtime / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    model = runtime / "ComfyUI" / "models" / "model.safetensors"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"verified model")
    hashes = {"model.safetensors": hashlib.sha256(model.read_bytes()).hexdigest()}
    fingerprints = {"model.safetensors": [100_000_001, 1]}
    marker = runtime / "cale-video-ready.json"
    original_replace = Path.replace
    publications = []

    def publish_after_competing_check(path, target):
        if Path(target) == marker:
            publications.append(path)
            if len(publications) == 1:
                # Another caller finishes verification before this caller
                # publishes its marker. Both must retain their own temp file.
                assert not marker.exists()
                assert video_generation._verified_runtime()
        return original_replace(path, target)

    with patch.object(video_generation, "RUNTIME", runtime), patch.object(
        video_generation, "MODEL_HASHES", hashes
    ), patch.object(
        video_generation, "_model_fingerprints", return_value=fingerprints
    ), patch.object(Path, "replace", publish_after_competing_check):
        assert video_generation._verified_runtime()

    assert len(publications) == 2
    assert publications[0] != publications[1]
    assert all(not path.exists() for path in publications)
    assert json.loads(marker.read_text(encoding="utf-8")) == fingerprints


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
