"""The music adapter must preserve local paths and never fetch remote results."""

import io
import json
import math
import os
import struct
import subprocess
import wave
from contextlib import nullcontext
from unittest.mock import patch

import imageio_ffmpeg
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
        music_backend, "urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b"audio")
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


def test_sends_user_lyrics_to_local_music_engine_unchanged(tmp_path):
    lyrics = "[Verse]\nWe made it home\n[Chorus]\nSing it again"
    responses = iter([
        {"code": 200, "data": {"task_id": "task-lyrics"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=lyrics"}]'}]},
    ])
    with patch.object(music_backend, "start_server"), patch.object(
        music_backend, "_request", side_effect=lambda *_args, **_kwargs: next(responses)
    ) as request, patch.object(
        music_backend, "urlopen", return_value=io.BytesIO(b"audio")
    ), patch.object(music_backend.time, "sleep"):
        music_backend.generate_music("soul song", str(tmp_path / "song.mp3"), lyrics=lyrics)
    assert request.call_args_list[0].args[1]["lyrics"] == lyrics


def test_music_engine_receives_female_pop_and_electronic_techno_cues(tmp_path):
    responses = iter([
        {"code": 200, "data": {"task_id": "one"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=one"}]'}]},
        {"code": 200, "data": {"task_id": "two"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=two"}]'}]},
    ])
    with patch.object(music_backend, "start_server"), patch.object(
        music_backend, "_request", side_effect=lambda *_args, **_kwargs: next(responses)
    ) as request, patch.object(
        music_backend, "urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b"audio")
    ), patch.object(music_backend.time, "sleep"):
        music_backend.generate_music("Make a song like Katy Perry", str(tmp_path / "pop.mp3"))
        music_backend.generate_music("Make a techno song", str(tmp_path / "techno.mp3"),
                                     instrumental=True)
    submissions = [call.args[1] for call in request.call_args_list
                   if call.args[0] == "/release_task"]
    assert "female lead vocals" in submissions[0]["prompt"]
    assert "drum machine" in submissions[1]["prompt"]
    assert submissions[1]["lyrics"] == "[Instrumental]"


def test_empty_vocal_request_writes_lyrics_and_checks_the_finished_audio(tmp_path):
    jobs = tmp_path / "jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ):
        started = music_backend.start_music_job(
            "rap about Michigan", str(output), duration_seconds=60,
            task_id="a" * 12,
        )
        lyrics = "[Verse]\n" + "Michigan rhythm and lakeside light " * 10
        with patch.object(music_backend, "compose_lyrics", return_value=lyrics) as compose, patch.object(
            music_backend, "generate_music", return_value={"path": str(output)}
        ) as generate, patch.object(
            music_backend, "vocal_evidence", return_value={"passed": True,
                "recognized_words": 50, "last_vocal_second": 58}
        ) as check, patch("song_video.render", return_value={
            "path": "song-video.mp4", "captions_path": "song.captions.srt"
        }) as render:
            music_backend._run_job(started["job_id"])
        job = music_backend.music_job_status(started["job_id"])
    compose.assert_called_once_with("rap about Michigan", 60)
    assert generate.call_args.args[2] == lyrics
    check.assert_called_once_with(str(output), 60)
    assert (tmp_path / "song.lyrics.txt").read_text(encoding="utf-8").strip() == lyrics.strip()
    assert job["status"] == "complete"
    assert job["vocal_check"]["recognized_words"] == 50
    assert job["result"]["video"]["path"] == "song-video.mp4"
    assert render.call_args.kwargs["lyrics"] == lyrics


def test_sparse_vocal_chops_do_not_trigger_lyric_or_singing_checks(tmp_path):
    jobs = tmp_path / "jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ):
        started = music_backend.start_music_job(
            "Heavy dubstep with minimal robotic vocal chops", str(output),
            duration_seconds=60,
        )
        with (patch.object(music_backend, "compose_lyrics") as compose,
              patch.object(music_backend, "generate_music",
                           return_value={"path": str(output)}) as generate,
              patch.object(music_backend, "vocal_evidence") as check,
              patch("song_video.render", return_value={
                  "path": "song-video.mp4", "captions_path": "song.captions.srt"
              }) as render):
            music_backend._run_job(started["job_id"])
        job = music_backend.music_job_status(started["job_id"])
    compose.assert_not_called()
    check.assert_not_called()
    assert generate.call_args.args[2] == ""
    assert render.call_args.kwargs["lyrics"] == ""
    assert job["status"] == "complete"
    assert job["vocal_check"] is None


def test_saved_vocal_chop_song_recovers_without_singing_check(tmp_path):
    jobs = tmp_path / "jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ):
        started = music_backend.start_music_job(
            "Techno with occasional vocal chops", str(output), duration_seconds=60,
        )
        output.write_bytes(b"saved song")
        job = music_backend.music_job_status(started["job_id"])
        music_backend._write_job(started["job_id"], {**job, "status": "working"})
        with (patch.object(music_backend, "_worker_running", return_value=False),
              patch.object(music_backend, "audio_file_ok", return_value=True),
              patch.object(music_backend, "vocal_evidence") as check,
              patch("song_video.render", return_value={
                  "path": "song-video.mp4", "captions_path": "song.captions.srt"
              })):
            music_backend.recover_music_jobs()
        recovered = music_backend.music_job_status(started["job_id"])
    check.assert_not_called()
    assert recovered["status"] == "complete"


def test_vocal_song_with_no_sustained_words_is_not_marked_complete(tmp_path):
    jobs = tmp_path / "jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ):
        started = music_backend.start_music_job(
            "rap about Michigan", str(output), lyrics="[Verse] Michigan rap " * 10,
            duration_seconds=60,
        )
        with patch.object(music_backend, "generate_music", return_value={"path": str(output)}), patch.object(
            music_backend, "vocal_evidence", return_value={"passed": False,
                "recognized_words": 4, "last_vocal_second": 5}
        ):
            music_backend._run_job(started["job_id"])
        job = music_backend.music_job_status(started["job_id"])
    assert job["status"] == "error"
    assert "sustained vocals" in job["current"]


def test_compose_lyrics_uses_small_local_model_and_rejects_truncation():
    from cloud_ai import FAST_MODEL

    good = {"choices": [{"finish_reason": "stop", "message": {
        "content": "[Verse]\n" + "Michigan lights and water rolling " * 10}}]}
    with patch("cloud_ai.post", return_value=good) as post:
        lyrics = music_backend.compose_lyrics("Michigan rap", 60)
    assert "Michigan lights" in lyrics
    assert post.call_args.args[1]["model"] == FAST_MODEL
    bad = {"choices": [{"finish_reason": "length", "message": {
        "content": good["choices"][0]["message"]["content"]}}]}
    with patch("cloud_ai.post", side_effect=[bad, good]) as retry:
        assert music_backend.compose_lyrics("Michigan rap", 60).startswith("[Verse]")
    assert retry.call_count == 2
    with patch("cloud_ai.post", side_effect=[TimeoutError("model busy"), good]) as retry:
        assert music_backend.compose_lyrics("Michigan rap", 60).startswith("[Verse]")
    assert retry.call_count == 2
    with patch("cloud_ai.post", return_value=bad):
        with pytest.raises(RuntimeError, match="could not finish"):
            music_backend.compose_lyrics("Michigan rap", 60)


def test_vocal_evidence_requires_words_through_most_of_the_song(tmp_path):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"runtime")
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    sparse = {"text": "yeah yeah", "segments": [{"end": 10.0}]}
    sustained = {"text": "Michigan " * 40, "segments": [{"end": 55.0}]}
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(
        music_backend.subprocess, "run", side_effect=[
            type("Result", (), {"returncode": 0, "stdout": json.dumps(sparse)})(),
            type("Result", (), {"returncode": 0, "stdout": json.dumps(sustained)})(),
        ],
    ):
        assert not music_backend.vocal_evidence(audio, 60)["passed"]
        assert music_backend.vocal_evidence(audio, 60)["passed"]


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
        started = music_backend.start_music_job("upbeat piano", str(output), instrumental=True)
        assert started["status"] == "queued"
        assert music_backend.music_job_status(started["job_id"])["status"] == "queued"
        process.assert_called_once()
        def finish(*args):
            args[7]("server-task-1")
            assert music_backend.music_job_status(started["job_id"])["server_task_id"] == "server-task-1"
            return {"path": str(output)}

        with patch.object(music_backend, "generate_music", side_effect=finish), patch(
            "song_video.render", return_value={
                "path": "song-video.mp4", "captions_path": "song.captions.srt"
            }
        ):
            music_backend._run_job(started["job_id"])
        completed = music_backend.music_job_status(started["job_id"])
    assert completed["status"] == "complete"
    assert completed["result"]["path"] == str(output)


def test_music_job_id_cannot_escape_job_directory():
    with pytest.raises(ValueError, match="Invalid local music job ID"):
        music_backend.music_job_status("../outside")


def test_stop_music_job_marks_only_the_selected_job_and_skips_later_worker(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    chosen = "a" * 32
    other = "b" * 32
    for job_id in (chosen, other):
        (jobs / (job_id + ".json")).write_text(json.dumps({
            "job_id": job_id, "status": "queued", "current": "Starting",
            "output_path": str(tmp_path / (job_id + ".mp3")),
        }))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "generate_music") as generate:
        assert music_backend.stop_music_job(chosen)["status"] == "stopped"
        music_backend._run_job(chosen)
        generate.assert_not_called()
        assert music_backend.music_job_status(other)["status"] == "queued"


def test_audio_validation_decodes_song_and_rejects_silence_or_damage(tmp_path):
    audio = tmp_path / "song.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"".join(
            struct.pack("<h", round(5000 * math.sin(2 * math.pi * 440 * n / 16000)))
            for n in range(16000)
        ))
    assert music_backend.audio_file_ok(audio)
    mp3 = tmp_path / "song.mp3"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(audio), str(mp3)],
                   capture_output=True, check=True)
    assert music_backend.audio_file_ok(mp3)
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000)
    assert not music_backend.audio_file_ok(audio)
    audio.write_bytes(b"bad" * 4096)
    assert not music_backend.audio_file_ok(audio)


def test_paused_music_job_resumes_same_server_task(tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    job_id = "c" * 32
    (jobs / (job_id + ".json")).write_text(json.dumps({
        "job_id": job_id, "status": "working", "current": "Rendering",
        "server_task_id": "ace-123", "worker_pid": 123,
        "output_path": str(tmp_path / "song.mp3"),
    }))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "_launch_job") as launch:
        assert music_backend.pause_music_job(job_id)["status"] == "paused"
        music_backend.recover_music_jobs()
        launch.assert_not_called()
        resumed = music_backend.resume_music_job(job_id)
        assert resumed["server_task_id"] == "ace-123"
        launch.assert_called_once_with(job_id)


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


def test_recovery_only_marks_playable_saved_song_complete(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    job_id = "d" * 32
    output = tmp_path / "song.mp3"
    output.write_bytes(b"incomplete audio")
    (jobs / (job_id + ".json")).write_text(json.dumps({
        "job_id": job_id, "status": "working", "current": "Generating",
        "output_path": str(output), "created": 1,
    }))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "audio_file_ok", return_value=False), patch.object(
        music_backend, "_launch_job"
    ) as launch:
        music_backend.recover_music_jobs()
        assert music_backend.music_job_status(job_id)["status"] == "error"
        launch.assert_not_called()
        assert output.read_bytes() == b"incomplete audio"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "audio_file_ok", return_value=True), patch.object(
        music_backend, "_launch_job"
    ) as launch, patch("song_video.render", return_value={
        "path": "song-video.mp4", "captions_path": "song.captions.srt"
    }):
        record = music_backend.music_job_status(job_id)
        music_backend._write_job(job_id, {**record, "status": "working"})
        music_backend.recover_music_jobs()
        assert music_backend.music_job_status(job_id)["status"] == "complete"
        launch.assert_not_called()


def test_failed_video_retries_saved_audio_without_generating_song_again(tmp_path):
    jobs = tmp_path / "music-jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ):
        started = music_backend.start_music_job(
            "instrumental beach guitar", str(output), instrumental=True
        )
        def generate(*_args):
            output.write_bytes(b"preserved audio")
            return {"path": str(output), "model": "ACE-Step 1.5"}

        with patch.object(music_backend, "generate_music", side_effect=generate) as engine, patch(
            "song_video.render", side_effect=[RuntimeError("encoder busy"),
                                               RuntimeError("encoder busy"),
                                               {"path": "song-video.mp4",
                                                "captions_path": "song.captions.srt"}]
        ) as render, patch.object(music_backend, "audio_file_ok", return_value=True):
            music_backend._run_job(started["job_id"])
            first = music_backend.music_job_status(started["job_id"])
            assert first["status"] == "queued"
            assert first["video_retries"] == 1
            for expected in (2, 2):
                music_backend._write_job(started["job_id"], {**music_backend.music_job_status(
                    started["job_id"]), "created": 0})
                music_backend.recover_music_jobs()
                assert music_backend.music_job_status(started["job_id"])["video_retries"] == expected
        finished = music_backend.music_job_status(started["job_id"])
    assert finished["status"] == "complete"
    assert finished["result"]["video"]["path"] == "song-video.mp4"
    assert output.read_bytes() == b"preserved audio"
    engine.assert_called_once()
    assert render.call_count == 3


def test_video_retry_stops_after_three_failures_without_losing_audio(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    output = tmp_path / "song.mp3"
    output.write_bytes(b"preserved audio")
    job_id = "e" * 32
    (jobs / (job_id + ".json")).write_text(json.dumps({
        "job_id": job_id, "status": "working", "created": 0,
        "output_path": str(output), "instrumental": True,
        "duration_seconds": 30, "prompt": "beach music", "lyrics": "",
    }))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "audio_file_ok", return_value=True), patch(
        "song_video.render", side_effect=RuntimeError("encoder unavailable")
    ) as render:
        for attempt in range(1, 4):
            music_backend.recover_music_jobs()
            record = music_backend.music_job_status(job_id)
            assert record["video_retries"] == attempt
            assert record["status"] == ("error" if attempt == 3 else "queued")
            if attempt < 3:
                music_backend._write_job(job_id, {**record, "created": 0})
        music_backend.recover_music_jobs()
    assert render.call_count == 3
    assert output.read_bytes() == b"preserved audio"
