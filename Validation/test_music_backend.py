"""The music adapter must preserve local paths and never fetch remote results."""

import io
import json
import os
from contextlib import nullcontext
from unittest.mock import patch

import pytest

import music_backend

REAL_GPU_LEASE = music_backend._music_gpu_lease


@pytest.fixture(autouse=True)
def avoid_touching_the_live_gpu():
    with patch.object(music_backend, "_music_gpu_lease", return_value=nullcontext()):
        yield


def test_rejects_invalid_request_before_install(tmp_path):
    with patch.object(music_backend, "start_server") as start:
        with pytest.raises(ValueError, match="10 to 600"):
            music_backend.generate_music("jazz", str(tmp_path / "song.mp3"), duration_seconds=3)
        with pytest.raises(ValueError, match="Choose a new filename"):
            existing = tmp_path / "song.mp3"
            existing.write_bytes(b"old")
            music_backend.generate_music("jazz", str(existing))
        start.assert_not_called()


def test_generates_instrumental_from_loopback_only(tmp_path):
    destination = tmp_path / "song.mp3"
    responses = iter([
        {"code": 200, "data": {"task_id": "task-1"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=demo"}]'}]},
    ])
    with patch.object(music_backend, "start_server"), patch.object(
        music_backend, "_request", side_effect=lambda *_args, **_kwargs: next(responses)
    ) as request, patch.object(
        music_backend, "urlopen", return_value=io.BytesIO(b"audio")
    ) as download, patch.object(music_backend.time, "sleep"):
        result = music_backend.generate_music(
            "warm piano", str(destination), instrumental=True
        )
    assert destination.read_bytes() == b"audio"
    assert result["model"] == "ACE-Step 1.5"
    assert request.call_args_list[0].args[1]["audio_format"] == "mp3"
    assert request.call_args_list[0].args[1]["lyrics"] == "[Instrumental]"
    assert request.call_args_list[0].args[1]["batch_size"] == 1
    assert download.call_args.args[0].startswith(music_backend.BASE + "/v1/audio")


def test_rejects_remote_audio_url(tmp_path):
    responses = iter([
        {"code": 200, "data": {"task_id": "task-1"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"https://example.com/song.mp3"}]'}]},
    ])
    with patch.object(music_backend, "start_server"), patch.object(
        music_backend, "_request", side_effect=lambda *_args, **_kwargs: next(responses)
    ), patch.object(music_backend.time, "sleep"):
        with pytest.raises(RuntimeError, match="unexpected audio URL"):
            music_backend.generate_music("jazz", str(tmp_path / "song.mp3"))


def test_recovery_polls_existing_server_task_without_submitting_twice(tmp_path):
    response = {"code": 200, "data": [
        {"status": 1, "result": '[{"file":"/v1/audio?path=existing"}]'}
    ]}
    with patch.object(music_backend, "start_server"), patch.object(
        music_backend, "_request", return_value=response
    ) as request, patch.object(
        music_backend, "urlopen", return_value=io.BytesIO(b"song")
    ), patch.object(music_backend.time, "sleep"):
        music_backend.generate_music(
            "jazz", str(tmp_path / "song.mp3"), existing_task_id="existing"
        )
    assert request.call_args_list[0].args == (
        "/query_result", {"task_id_list": ["existing"]}
    )
    assert all(call.args[0] != "/release_task" for call in request.call_args_list)


def test_cover_uploads_source_audio_to_local_server(tmp_path):
    source = tmp_path / "original.wav"
    source.write_bytes(b"source audio")
    response = {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=cover"}]'}]}
    with patch.object(music_backend, "start_server"), patch.object(
        music_backend, "_request", return_value=response
    ), patch.object(
        music_backend, "_upload_cover", return_value={"code": 200, "data": {"task_id": "task-2"}}
    ) as upload, patch.object(
        music_backend, "urlopen", return_value=io.BytesIO(b"cover audio")
    ), patch.object(music_backend.time, "sleep"):
        result = music_backend.generate_music(
            "jazzy cover", str(tmp_path / "cover.mp3"), source_path=str(source)
        )
    assert result["source_path"] == str(source)
    assert upload.call_args.args[1] == source
    assert upload.call_args.args[0]["task_type"] == "cover"
    assert upload.call_args.args[0]["audio_cover_strength"] == 0.2
    assert "src_audio_path" not in upload.call_args.args[0]


def test_cover_upload_is_multipart_and_loopback_only(tmp_path):
    source = tmp_path / "original.mp3"
    source.write_bytes(b"source audio")
    response = type("Response", (), {"ok": True, "json": lambda self: {"code": 200}})()
    with patch.object(music_backend.requests, "post", return_value=response) as post:
        result = music_backend._upload_cover(
            {"task_type": "cover", "thinking": False}, source
        )
    assert result["code"] == 200
    assert post.call_args.args[0] == "http://127.0.0.1:8001/release_task"
    assert post.call_args.kwargs["data"]["thinking"] == "false"
    assert post.call_args.kwargs["files"]["src_audio"][0] == "original.mp3"


def test_prefetch_downloads_in_background_on_capable_pc(tmp_path):
    engine = tmp_path / "Media" / "music" / "ACE-Step-1.5"
    downloader = engine / ".venv" / "Scripts" / "acestep-download.exe"
    downloader.parent.mkdir(parents=True)
    downloader.write_bytes(b"launcher")
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(
        music_backend, "ENGINE", engine
    ), patch.object(music_backend, "healthy", return_value=False), patch.object(
        music_backend, "_capable_for_background", return_value=True
    ), patch.object(music_backend, "_setup_lock", return_value=nullcontext()), patch.object(
        music_backend, "install", return_value=engine
    ) as install, patch.object(music_backend.subprocess, "run") as run:
        music_backend.prefetch()
    install.assert_called_once()
    assert run.call_args.args[0] == [str(downloader)]
    assert (engine / ".cale-model-ready").read_text() == music_backend.REVISION
    assert '"status": "complete"' in (
        tmp_path / "LongTasks" / "music-install-progress.json"
    ).read_text()


def test_durable_music_job_survives_the_starting_process(tmp_path):
    python = tmp_path / "Toolbox" / "python" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"launcher")
    jobs = tmp_path / "LongTasks" / "music-jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(
        music_backend, "JOBS", jobs
    ), patch.object(music_backend.subprocess, "Popen") as process:
        started = music_backend.start_music_job("upbeat piano", str(output))
        assert started["status"] == "queued"
        assert music_backend.music_job_status(started["job_id"])["status"] == "queued"
        process.assert_called_once()
        def finish(*args):
            args[7]("server-task-1")
            assert music_backend.music_job_status(started["job_id"])["server_task_id"] == "server-task-1"
            return {"path": str(output)}

        with patch.object(music_backend, "generate_music", side_effect=finish):
            music_backend._run_job(started["job_id"])
        completed = music_backend.music_job_status(started["job_id"])
    assert completed["status"] == "complete"
    assert completed["result"]["path"] == str(output)


def test_music_job_id_cannot_escape_job_directory():
    with pytest.raises(ValueError, match="Invalid local music job ID"):
        music_backend.music_job_status("../outside")


def test_music_lease_unloads_cale_models_and_cleans_marker(tmp_path):
    with patch.object(music_backend, "ROOT", tmp_path), patch(
        "media_worker.unload_local_models"
    ) as unload:
        with REAL_GPU_LEASE():
            marker = tmp_path / "LongTasks" / f"music-gpu-active-{os.getpid()}.json"
            assert marker.is_file()
        assert not marker.exists()
    unload.assert_called_once()


def test_interrupted_music_job_recovers_without_restarting_live_worker(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    job_id = "a" * 32
    record = {
        "job_id": job_id, "status": "working", "current": "Generating",
        "output_path": str(tmp_path / "song.mp3"), "created": 1,
        "worker_pid": 12345,
    }
    (jobs / (job_id + ".json")).write_text(json.dumps(record))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=True
    ), patch.object(music_backend, "_launch_job") as launch:
        music_backend.recover_music_jobs()
        launch.assert_not_called()
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "_launch_job") as launch:
        music_backend.recover_music_jobs()
        launch.assert_called_once_with(job_id)
        resumed = music_backend.music_job_status(job_id)
    assert resumed["status"] == "queued"
    assert resumed["recoveries"] == 1
