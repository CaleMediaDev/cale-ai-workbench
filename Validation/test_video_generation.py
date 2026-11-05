"""Validate the local ComfyUI job contract without a multi-gigabyte download."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import pytest

import video_generation


def test_avatar_cli_module_loads_without_video_export_packages():
    """The Media-based avatar runtime intentionally has no imageio-ffmpeg."""
    script = str(Path(video_generation.__file__).resolve())
    code = "import runpy; value=runpy.run_path(" + repr(script) + "); assert callable(value['avatar_profile_probe_worker'])"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize("worker_state", ["installing", "complete", "failed"])
def test_launch_preserves_state_published_before_popen_returns(tmp_path, worker_state):
    jobs = tmp_path / "jobs"

    def launch_worker(command, **kwargs):
        job_id = command[-1]
        stored = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
        assert stored["state"] == "starting"
        video_generation._write(job_id, state=worker_state, detail="Worker published progress")
        return SimpleNamespace(pid=12345)

    with patch.object(video_generation, "JOBS", jobs), patch.object(
        video_generation, "_hardware_ready"
    ), patch.object(video_generation.subprocess, "Popen", side_effect=launch_worker):
        result = video_generation.start_video_job(
            "A calm forest in the rain", str(tmp_path / "clip.mp4")
        )
        stored = json.loads((jobs / f"{result['job_id']}.json").read_text(encoding="utf-8"))

    assert result["state"] == worker_state
    assert stored["state"] == worker_state
    assert stored["detail"] == "Worker published progress"
    assert stored["worker_pid"] == 12345
    assert stored["log_path"] == str(jobs / f"{result['job_id']}.log")


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


def test_concurrent_job_writes_use_independent_temporary_files(tmp_path):
    job_id = "a" * 32
    record = tmp_path / f"{job_id}.json"
    original_replace = Path.replace
    publications = []

    def publish_after_competing_write(path, target):
        if Path(target) == record:
            publications.append(path)
            if len(publications) == 1:
                competing = video_generation._write(job_id, detail="competing writer")
                assert json.loads(record.read_text(encoding="utf-8")) == competing
        return original_replace(path, target)

    with patch.object(video_generation, "JOBS", tmp_path), patch.object(
        Path, "replace", publish_after_competing_write
    ):
        result = video_generation._write(job_id, detail="original writer")

    assert len(publications) == 2
    assert publications[0] != publications[1]
    assert all(not path.exists() for path in publications)
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(record.read_text(encoding="utf-8")) == result
    assert result["job_id"] == job_id
    assert result["detail"] == "original writer"


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
    ), patch.object(video_generation.subprocess, "Popen") as launch, patch(
        "imageio_ffmpeg.get_ffmpeg_exe", return_value="ffmpeg"
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


@pytest.fixture
def avatar_profile_fixture(tmp_path,monkeypatch):
    import contextlib
    module=video_generation;profile=module.avatar_profile_root(tmp_path)
    profile.mkdir(parents=True)
    media=tmp_path/"Media/python/Scripts/python.exe";media.parent.mkdir(parents=True);media.write_bytes(b"test interpreter")
    python=profile/"python/Scripts/python.exe";python.parent.mkdir(parents=True);python.write_bytes(b"test interpreter")
    site=profile/"python/Lib/site-packages";site.mkdir(parents=True)
    (site/"cale-media-libraries.pth").write_text(str((tmp_path/"Media/python/Lib/site-packages").resolve())+"\n",encoding="utf-8")
    (profile/"packages.json").write_text(json.dumps(list(module.AVATAR_ORT_PACKAGES)),encoding="utf-8")
    monkeypatch.setattr(module,'_process_lock',lambda *_:contextlib.nullcontext())
    monkeypatch.setattr(module,'_avatar_assets',lambda:[])
    monkeypatch.setattr('pc_offline.offline',lambda:True)
    return module,profile,python


def test_avatar_profile_never_ready_until_inference_probe_passes(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,python=avatar_profile_fixture
    monkeypatch.setattr(module,'probe_avatar_profile',Mock(side_effect=RuntimeError('CUDA engine failed')))
    with pytest.raises(RuntimeError,match='CUDA engine failed'):module.ensure_avatar_profile(tmp_path)
    assert module.avatar_profile_status(tmp_path)['ready'] is False
    monkeypatch.setattr(module,'probe_avatar_profile',lambda *_,**__:{'ok':True,'probe':{'ok':True,'provider':'CUDAExecutionProvider','frames':4,'finite':True}})
    assert module.ensure_avatar_profile(tmp_path)==python
    assert module.avatar_profile_status(tmp_path)['ready'] is True


def test_avatar_profile_cancel_and_pth_tampering_prevent_execution(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,_=avatar_profile_fixture
    probe=Mock();monkeypatch.setattr(module,'probe_avatar_profile',probe)
    with pytest.raises(RuntimeError,match='cancelled'):module.ensure_avatar_profile(tmp_path,cancelled=lambda:True)
    (profile/'python/Lib/site-packages/cale-media-libraries.pth').write_text('import malicious\n',encoding='utf-8')
    with pytest.raises(RuntimeError,match='path changed'):module.ensure_avatar_profile(tmp_path)
    probe.assert_not_called()


def test_avatar_partial_environment_retries_only_isolated_packages(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,python=avatar_profile_fixture
    (profile/'packages.json').unlink()
    monkeypatch.setattr('pc_offline.offline',lambda:False)
    monkeypatch.setattr('music_backend._tool',lambda _: 'managed-uv')
    commands=[]
    monkeypatch.setattr(module.subprocess,'run',lambda command,**kwargs:commands.append(command))
    monkeypatch.setattr(module,'probe_avatar_profile',lambda *_,**__:{'ok':True})
    module.ensure_avatar_profile(tmp_path)
    assert len(commands)==1 and commands[0][:3]==['managed-uv','pip','install']
    assert commands[0][commands[0].index('--python')+1]==str(python)
    assert 'onnxruntime-gpu==1.26.0' in commands[0] and not any('1.31' in x for x in commands[0])
    assert not any('venv' in command for command in commands)


def test_avatar_download_hash_size_and_guarded_cache(tmp_path,monkeypatch):
    import io,urllib.request
    module=video_generation;data=b'pinned fixture bytes';profile=tmp_path/'profile';profile.mkdir()
    record={'size':len(data),'hash_kind':'sha256','sha256':hashlib.sha256(data).hexdigest(),'url':'https://raw.githubusercontent.com/owner/repo/pinned/file'}
    monkeypatch.setattr('pc_offline.offline',lambda:False)
    monkeypatch.setattr(urllib.request,'build_opener',lambda *_:SimpleNamespace(open=lambda *_args,**_kwargs:io.BytesIO(data)))
    module._avatar_download(profile,'models/model.bin',record)
    assert (profile/'models/model.bin').read_bytes()==data
    assert module._avatar_verified(profile/'models/model.bin',record)
    bad={**record,'sha256':'0'*64}
    with pytest.raises(ValueError,match='checksum'):module._avatar_download(profile,'models/bad.bin',bad)
    assert not (profile/'models/bad.bin').exists()
    with pytest.raises(ValueError):module._avatar_download(profile,'../outside.bin',record)


def test_avatar_virtual_sender_bounded_only_processed_frames(monkeypatch):
    import io,struct,numpy as np
    from PIL import Image
    import types
    sent=[];closed=[]
    class Camera:
        def __init__(self,**kwargs):assert kwargs['backend']=='obs';assert kwargs['width']==1920 and kwargs['height']==1080
        def __enter__(self):return self
        def __exit__(self,*_):closed.append(True)
        def send(self,frame):sent.append(frame)
    monkeypatch.setitem(sys.modules,'pyvirtualcam',types.SimpleNamespace(Camera=Camera,PixelFormat=types.SimpleNamespace(RGB='RGB')))
    buffer=io.BytesIO();Image.fromarray(np.full((512,512,3),22,dtype=np.uint8)).save(buffer,format='JPEG');raw=buffer.getvalue()
    assert video_generation.virtual_avatar_sender(io.BytesIO(struct.pack('!I',len(raw))+raw))==0
    assert len(sent)==1 and sent[0].shape==(1080,1920,3) and closed==[True]
    with pytest.raises(ValueError,match='bound'):video_generation.virtual_avatar_sender(io.BytesIO(struct.pack('!I',3*1024*1024)))
    assert len(sent)==1


def test_avatar_mode_save_is_atomic_and_keeps_prior_choice_on_failure(tmp_path,monkeypatch):
    video_generation.save_avatar_camera_mode(tmp_path,False)
    actual=video_generation.os.replace
    def failed(source,destination):raise OSError('simulated save failure')
    monkeypatch.setattr(video_generation.os,'replace',failed)
    with pytest.raises(OSError):video_generation.save_avatar_camera_mode(tmp_path,True)
    assert json.loads((tmp_path/'Media/camera-avatar.json').read_text())=={'enabled':False}
    assert not list((tmp_path/'Media').glob('camera-avatar-*.tmp'))


def test_avatar_virtual_sender_rejects_dead_parent_without_opening_driver(monkeypatch):
    import io,types,camera_presence
    opened=Mock()
    monkeypatch.setitem(sys.modules,'pyvirtualcam',types.SimpleNamespace(Camera=opened,PixelFormat=types.SimpleNamespace(RGB='RGB')))
    monkeypatch.setattr(camera_presence,'owner_alive',lambda *_:False)
    assert video_generation.virtual_avatar_sender(io.BytesIO(),parent_pid=123,parent_started=456)==0
    opened.assert_not_called()


def test_avatar_environment_never_creates_unpinned_bytecode(monkeypatch):
    monkeypatch.setenv('PYTHONPATH','untrusted inherited directory');monkeypatch.setenv('PYTHONHOME','other Python')
    env=video_generation._avatar_environment()
    assert env['PYTHONNOUSERSITE']=='1' and env['PYTHONDONTWRITEBYTECODE']=='1'
    assert 'PYTHONPATH' not in env and 'PYTHONHOME' not in env


def test_avatar_setup_probe_cancel_reaps_owned_child(tmp_path,monkeypatch):
    process=Mock();alive=[True]
    process.poll.side_effect=lambda:None if alive[0] else 0
    process.terminate.side_effect=lambda:alive.__setitem__(0,False)
    launched=[]
    def popen(command,**kwargs):launched.append((command,kwargs));return process
    monkeypatch.setattr(video_generation.subprocess,'Popen',popen)
    with pytest.raises(RuntimeError,match='cancelled'):
        video_generation.probe_avatar_profile(tmp_path,tmp_path,cancelled=lambda:True)
    process.terminate.assert_called_once();process.wait.assert_called_once_with(timeout=3)
    assert '-B' in launched[0][0] and launched[0][1]['env']['PYTHONDONTWRITEBYTECODE']=='1'


def test_avatar_virtual_sender_letterboxes_native_scene_without_stretch(monkeypatch):
    import io,struct,types,numpy as np
    from PIL import Image
    sent=[]
    class Camera:
        def __init__(self,**kwargs):assert (kwargs['width'],kwargs['height'])==(1920,1080)
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def send(self,frame):sent.append(frame)
    monkeypatch.setitem(sys.modules,'pyvirtualcam',types.SimpleNamespace(Camera=Camera,PixelFormat=types.SimpleNamespace(RGB='RGB')))
    stream=io.BytesIO();Image.new('RGB',(880,360),(120,80,40)).save(stream,format='JPEG');raw=stream.getvalue()
    assert video_generation.virtual_avatar_sender(io.BytesIO(struct.pack('!I',len(raw))+raw))==0
    assert len(sent)==1 and sent[0].shape==(1080,1920,3)
    ys,xs=np.where(sent[0].max(axis=2)>20)
    assert xs.min()==0 and xs.max()==1919
    assert abs((ys.max()-ys.min()+1)-360*1920/880)<2
    assert np.all(sent[0][0]==0) and np.all(sent[0][-1]==0)


def test_avatar_probe_ignores_untrusted_cwd_before_any_gpu_import(tmp_path,monkeypatch):
    import subprocess,sys
    caller=tmp_path/'untrusted-caller';caller.mkdir()
    trusted=tmp_path/'trusted-app';trusted.mkdir()
    executed=caller/'cwd-code-executed';reached=trusted/'before-gpu-import'
    (caller/'json.py').write_text(f"from pathlib import Path\nPath({str(executed)!r}).write_text('untrusted cwd executed')\nraise RuntimeError('cwd json shadow')\n",encoding='utf-8')
    # Fail before importing any real GPU library. The tripwire is reached only
    # after the probe imports canonical JSON and inserts the explicit app root.
    (trusted/'video_generation.py').write_text(Path(video_generation.__file__).read_text(encoding='utf-8'),encoding='utf-8')
    import camera_presence
    (trusted/'camera_presence.py').write_text(Path(camera_presence.__file__).read_text(encoding='utf-8'),encoding='utf-8')
    (trusted/'Media/avatar/v1').mkdir(parents=True)
    (trusted/'torch.py').write_text(f"from pathlib import Path\nPath({str(reached)!r}).write_text('trusted stage reached')\nraise RuntimeError('test stops before real GPU imports')\n",encoding='utf-8')
    actual=subprocess.Popen;commands=[]
    def launch(command,**kwargs):
        commands.append((command,kwargs))
        return actual([sys.executable,*command[1:]],**kwargs)
    monkeypatch.setattr(video_generation.subprocess,'Popen',launch)
    monkeypatch.chdir(caller)
    with pytest.raises(RuntimeError,match='qualification failed'):
        video_generation.probe_avatar_profile(trusted/'Media/avatar/v1',trusted)
    assert not executed.exists(), 'Untrusted caller json.py ran before GPU validation'
    assert reached.read_text()=='trusted stage reached'
    assert '-I' in commands[0][0] and commands[0][1]['cwd']==trusted


def test_avatar_setup_waits_for_heavy_gpu_then_qualifies_once(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,python=avatar_profile_fixture
    states=iter((True,True,False));busy=[];notices=[]
    def active(_root):value=next(states,False);busy.append(value);return value
    monkeypatch.setattr(module,'_avatar_gpu_busy',active)
    monkeypatch.setattr(module.time,'sleep',lambda *_:None)
    def probe(*_args,**_kwargs):assert busy==[True,True,False];return {'ok':True}
    called=Mock(side_effect=probe);monkeypatch.setattr(module,'probe_avatar_profile',called)
    assert module.ensure_avatar_profile(tmp_path,progress=lambda value:notices.append(value['phase']))==python
    called.assert_called_once();assert notices.count('waiting_gpu')==1
    assert module.avatar_profile_status(tmp_path)['ready'] is True


def test_avatar_setup_waiting_gpu_is_cancellable_without_probe(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,_=avatar_profile_fixture;cancelled=[False]
    monkeypatch.setattr(module,'_avatar_gpu_busy',lambda _:True)
    monkeypatch.setattr(module.time,'sleep',lambda *_:cancelled.__setitem__(0,True))
    probe=Mock();monkeypatch.setattr(module,'probe_avatar_profile',probe)
    with pytest.raises(RuntimeError,match='cancelled'):module.ensure_avatar_profile(tmp_path,cancelled=lambda:cancelled[0])
    probe.assert_not_called();assert module.avatar_profile_status(tmp_path)['ready'] is False


def test_avatar_busy_work_reaps_probe_and_cleans_exact_child_lease(tmp_path,monkeypatch):
    import psutil
    process=Mock();process.pid=123456;alive=[True]
    process.poll.side_effect=lambda:None if alive[0] else 0
    process.terminate.side_effect=lambda:alive.__setitem__(0,False)
    monkeypatch.setattr(psutil,'Process',lambda *_:SimpleNamespace(create_time=lambda:42.5))
    busy=iter((False,True));monkeypatch.setattr(video_generation,'_avatar_gpu_busy',lambda _:next(busy,True))
    marker=tmp_path/'LongTasks/avatar-gpu-active-123456.json';marker.parent.mkdir()
    def launch(command,**kwargs):
        assert command[1:3]==['-I','-B'] and command[4]=='avatar-profile-probe'
        assert kwargs['cwd']==tmp_path
        marker.write_text(json.dumps({'pid':123456,'started':42.5,'reserved_bytes':2*1024**3}))
        return process
    monkeypatch.setattr(video_generation.subprocess,'Popen',launch)
    with pytest.raises(video_generation._AvatarProbeDeferred):video_generation.probe_avatar_profile(tmp_path/'Media/avatar/v1',tmp_path)
    process.terminate.assert_called_once();process.wait.assert_called_once_with(timeout=3)
    assert not marker.exists()


def test_avatar_probe_cli_lease_precedes_engine_and_closes_after_it(tmp_path,monkeypatch,capsys):
    import types,threading
    events=[];profile=tmp_path/'Media/avatar/v1';profile.mkdir(parents=True)
    monkeypatch.setattr(video_generation,'__file__',str(tmp_path/'video_generation.py'))
    class Lease:
        def __init__(self):events.append('lease starting')
        def measured(self,value):assert value['ok'];events.append('lease measured')
        def close(self):events.append('lease close')
    class Engine:
        def __init__(self,_):assert events==['lease starting'];events.append('engine')
        def probe(self):events.append('probe');return {'ok':True,'gpu_allocation_bytes':123}
        def close(self):events.append('engine close')
    monkeypatch.setitem(sys.modules,'camera_presence',types.SimpleNamespace(AvatarGpuLease=Lease,owner_alive=lambda *_:True,watch_owner=Mock(),start_launcher_guard=Mock()))
    monkeypatch.setitem(sys.modules,'torch',types.SimpleNamespace(__file__=str(tmp_path/'Media/python/Lib/site-packages/torch/__init__.py'),__version__='2.9.1+cu128',version=types.SimpleNamespace(cuda='12.8'),cuda=types.SimpleNamespace(is_available=lambda:True),backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(version=lambda:90100))))
    monkeypatch.setitem(sys.modules,'onnxruntime',types.SimpleNamespace(__file__=str(profile/'python/Lib/site-packages/onnxruntime/__init__.py'),__version__='1.26.0'))
    monkeypatch.setitem(sys.modules,'avatar_video',types.SimpleNamespace(CaleAvatar=Engine))
    monkeypatch.setattr(threading.Thread,'start',lambda *_:None)
    video_generation.avatar_profile_probe_worker(profile,5,10)
    assert events==['lease starting','engine','probe','lease measured','engine close','lease close']
    assert json.loads(capsys.readouterr().out)['ok'] is True


def test_avatar_probe_accepts_canonical_shared_media_but_rejects_unrelated_origin(tmp_path,monkeypatch,capsys):
    import types,threading
    if os.name!='nt':pytest.skip('Windows shared Media junction fixture')
    app=tmp_path/'app';(app/'Media/avatar/v1').mkdir(parents=True)
    shared=tmp_path/'shared-media';(shared/'Lib/site-packages/torch').mkdir(parents=True)
    linked=app/'Media/python'
    subprocess.run(['cmd','/c','mklink','/J',str(linked),str(shared)],check=True,capture_output=True)
    profile=app/'Media/avatar/v1'
    monkeypatch.setattr(video_generation,'__file__',str(app/'video_generation.py'))
    class Lease:
        def measured(self,_):pass
        def close(self):pass
    class Engine:
        def __init__(self,_):pass
        def probe(self):return {'ok':True,'gpu_allocation_bytes':123}
        def close(self):pass
    monkeypatch.setitem(sys.modules,'camera_presence',types.SimpleNamespace(AvatarGpuLease=Lease,owner_alive=lambda *_:True,watch_owner=Mock(),start_launcher_guard=Mock()))
    torch=types.SimpleNamespace(__file__=str(shared/'Lib/site-packages/torch/__init__.py'),__version__='2.9.1+cu128',version=types.SimpleNamespace(cuda='12.8'),cuda=types.SimpleNamespace(is_available=lambda:True),backends=types.SimpleNamespace(cudnn=types.SimpleNamespace(version=lambda:90100)))
    monkeypatch.setitem(sys.modules,'torch',torch)
    monkeypatch.setitem(sys.modules,'onnxruntime',types.SimpleNamespace(__file__=str(profile/'python/Lib/site-packages/onnxruntime/__init__.py'),__version__='1.26.0'))
    monkeypatch.setitem(sys.modules,'avatar_video',types.SimpleNamespace(CaleAvatar=Engine))
    monkeypatch.setattr(threading.Thread,'start',lambda *_:None)
    try:
        video_generation.avatar_profile_probe_worker(profile,5,10)
        assert json.loads(capsys.readouterr().out)['ok'] is True
        torch.__file__=str(tmp_path/'unrelated/torch/__init__.py')
        with pytest.raises(RuntimeError,match='Wrong Media Torch origin'):video_generation.avatar_profile_probe_worker(profile,5,10)
    finally:linked.rmdir()  # Remove only this fixture junction, never the shared target.


def test_avatar_camera_preparation_defers_only_gpu_probe(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,python=avatar_profile_fixture
    checked=[]
    monkeypatch.setattr(module,'_avatar_assets',lambda:[('source/fixture.py',{'fixture':True})])
    monkeypatch.setattr(module,'_avatar_download',lambda *args:checked.append(args[1]))
    probe=Mock(side_effect=AssertionError('No duplicate setup GPU load'))
    monkeypatch.setattr(module,'probe_avatar_profile',probe)
    notices=[]
    assert module.ensure_avatar_profile(tmp_path,progress=notices.append,defer_probe=True)==python
    assert checked==['source/fixture.py']
    assert notices[-1]['phase']=='prepared' and notices[-1]['ready'] is False
    probe.assert_not_called()
    # A prepared marker never qualifies the default setup path by itself.
    probe.side_effect=None;probe.return_value={'ok':True,'probe':{'ok':True}}
    assert module.ensure_avatar_profile(tmp_path)==python
    probe.assert_called_once()
    assert module.avatar_profile_status(tmp_path)['ready'] is True


def test_avatar_deferred_first_install_still_prepares_packages(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,python=avatar_profile_fixture
    (profile/'packages.json').unlink()
    monkeypatch.setattr('pc_offline.offline',lambda:False)
    monkeypatch.setattr('music_backend._tool',lambda _: 'managed-uv')
    installer=Mock();monkeypatch.setattr(module.subprocess,'run',installer)
    probe=Mock();monkeypatch.setattr(module,'probe_avatar_profile',probe)
    assert module.ensure_avatar_profile(tmp_path,defer_probe=True)==python
    installer.assert_called_once()
    assert installer.call_args.args[0][:3]==['managed-uv','pip','install']
    assert json.loads((profile/'packages.json').read_text())==list(module.AVATAR_ORT_PACKAGES)
    assert module.avatar_profile_status(tmp_path)['ready'] is False
    probe.assert_not_called()


def test_avatar_deferred_preparation_keeps_pth_cancel_and_virtual_checks(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,_=avatar_profile_fixture
    probe=Mock();monkeypatch.setattr(module,'probe_avatar_profile',probe)
    with pytest.raises(RuntimeError,match='cancelled'):
        module.ensure_avatar_profile(tmp_path,defer_probe=True,cancelled=lambda:True)
    (profile/'python/Lib/site-packages/cale-media-libraries.pth').write_text('import malicious\n')
    with pytest.raises(RuntimeError,match='path changed'):
        module.ensure_avatar_profile(tmp_path,defer_probe=True)
    (profile/'python/Lib/site-packages/cale-media-libraries.pth').write_text(str((tmp_path/'Media/python/Lib/site-packages').resolve())+'\n')
    monkeypatch.setattr(module.subprocess,'run',lambda *_,**__:SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError,match='virtual-camera package'):
        module.ensure_avatar_profile(tmp_path,defer_probe=True,virtual_output=True)
    assert module.avatar_profile_status(tmp_path)['ready'] is False
    probe.assert_not_called()


def test_avatar_deferred_new_environment_still_creates_runtime_before_camera(tmp_path,monkeypatch,avatar_profile_fixture):
    module,profile,python=avatar_profile_fixture
    python.unlink();(profile/'packages.json').unlink()
    monkeypatch.setattr('pc_offline.offline',lambda:False)
    monkeypatch.setattr('music_backend._tool',lambda _: 'managed-uv')
    def install(command,**_):
        if command[1]=='venv':python.write_bytes(b'new isolated interpreter fixture')
    installer=Mock(side_effect=install);monkeypatch.setattr(module.subprocess,'run',installer)
    probe=Mock();monkeypatch.setattr(module,'probe_avatar_profile',probe)
    assert module.ensure_avatar_profile(tmp_path,defer_probe=True)==python
    assert [call.args[0][1] for call in installer.call_args_list]==['venv','pip']
    assert python.is_file() and module.avatar_profile_status(tmp_path)['phase']=='prepared'
    probe.assert_not_called()


def test_deferred_readiness_cannot_be_published_after_preparation_changes(tmp_path,avatar_profile_fixture):
    module,profile,_=avatar_profile_fixture
    module._avatar_write_status(profile,phase='prepared',ready=False)
    def changed():
        module._avatar_write_status(profile,phase='failed',ready=False,detail='Preparation superseded')
        return False
    assert not module._avatar_finish_deferred_probe(tmp_path,profile,{}, {'ok':True},changed)
    assert module.avatar_profile_status(tmp_path)['detail']=='Preparation superseded'
    assert module.avatar_profile_status(tmp_path)['ready'] is False
    # An old prepared caller also cannot turn an existing failure into ready.
    assert not module._avatar_finish_deferred_probe(tmp_path,profile,{}, {'ok':True},lambda:True)


@pytest.mark.parametrize("deferred", [False, True])
def test_avatar_full_startup_keeps_default_two_probes_but_gui_defer_loads_once(tmp_path,monkeypatch,avatar_profile_fixture,deferred):
    """Run real preparation + real worker loop using a fake neural engine."""
    import queue,camera_presence as camera,avatar_video
    module,profile,python=avatar_profile_fixture
    events=[];authorized=[True]
    def setup_probe(*_,**__):
        events.append('isolated setup probe')
        return {'ok':True,'probe':{'ok':True}}
    monkeypatch.setattr(module,'probe_avatar_profile',setup_probe)
    # The default call deliberately omits the new keyword, preserving old API.
    assert (module.ensure_avatar_profile(tmp_path,defer_probe=True) if deferred
            else module.ensure_avatar_profile(tmp_path))==python
    assert module.avatar_profile_status(tmp_path)['ready'] is (not deferred)
    class Engine:
        def __init__(self,*_):events.append('live load')
        def probe(self):events.append('live probe');return {'ok':True,'frames':4}
        def close(self):events.append('live close')
    class Lease:
        def measured(self,_):pass
        def close(self):pass
    class Pending:
        def get(self,**_):authorized[0]=False;raise queue.Empty
    monkeypatch.setattr(camera,'ROOT',tmp_path)
    monkeypatch.setattr(camera,'avatar_heavy_work_active',lambda:False)
    monkeypatch.setattr(camera,'AvatarGpuLease',Lease)
    monkeypatch.setattr(module,'_avatar_runtime_facts',lambda *_:{'ort':'fixture','torch':'fixture'})
    monkeypatch.setattr(avatar_video,'CaleAvatar',Engine)
    monkeypatch.setattr(avatar_video,'neutral_camera_avatar',lambda:None)
    monkeypatch.setattr(camera,'emit',Mock())
    worker=camera.AvatarFrameWorker.__new__(camera.AvatarFrameWorker)
    worker.profile=profile;worker.virtual_output=False;worker.sender_worker=None
    worker.authorized=lambda:authorized[0];worker._publish=lambda _:None;worker.pending=Pending()
    worker._run()
    assert events==(['live load','live probe','live close'] if deferred else
                    ['isolated setup probe','live load','live probe','live close'])
    assert module.avatar_profile_status(tmp_path)['ready'] is True
