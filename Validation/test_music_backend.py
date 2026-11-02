"""The music adapter must preserve local paths and never fetch remote results."""

import io
import json
import math
import os
import sqlite3
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


def test_sends_explicit_tempo_as_numeric_music_setting(tmp_path):
    responses = iter([
        {"code": 200, "data": {"task_id": "tempo"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=tempo"}]'}]},
    ])
    with (patch.object(music_backend, "start_server"),
          patch.object(music_backend, "_request",
                       side_effect=lambda *_args, **_kwargs: next(responses)) as request,
          patch.object(music_backend, "urlopen", return_value=io.BytesIO(b"audio")),
          patch.object(music_backend.time, "sleep")):
        music_backend.generate_music("Techno at 150 BPM", str(tmp_path / "tempo.mp3"))
    assert request.call_args_list[0].args[1]["bpm"] == 150


def test_vocal_song_uses_loaded_music_planner(tmp_path):
    responses = iter([
        {"code": 200, "data": {"task_id": "planned"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=planned"}]'}]},
    ])
    with (patch.object(music_backend, "start_server", return_value=True),
          patch.object(music_backend, "_request",
                       side_effect=lambda *_args, **_kwargs: next(responses)) as request,
          patch.object(music_backend, "urlopen", return_value=io.BytesIO(b"audio")),
          patch.object(music_backend.time, "sleep")):
        music_backend.generate_music(
            "Female pop at 150 BPM", str(tmp_path / "planned.mp3"),
            lyrics="[Chorus]\nSing it again",
        )
    assert request.call_args_list[0].args[1]["thinking"] is True


def test_music_planner_status_falls_back_when_unavailable():
    with patch.object(music_backend, "_request", return_value={
        "data": {"service": "ACE-Step API", "llm_initialized": False}
    }):
        assert music_backend.music_lm_ready() is False
    with patch.object(music_backend, "_request", side_effect=OSError("offline")):
        assert music_backend.music_lm_ready() is False


def test_music_install_repairs_broken_environment_without_refetching_source(tmp_path):
    engine = tmp_path / "ACE-Step-1.5"
    python = engine / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (engine / ".git").mkdir()
    (engine / "pyproject.toml").touch()
    (engine / ".cale-installed").write_text(music_backend.REVISION)
    with (patch.object(music_backend, "ROOT", tmp_path),
          patch.object(music_backend, "ENGINE", engine),
          patch.object(music_backend, "_tool", return_value="uv") as tool,
          patch.object(music_backend, "_runtime_healthy", side_effect=[False, True]) as health,
          patch.object(music_backend.subprocess, "run") as run):
        assert music_backend.install() == engine
    tool.assert_called_once_with("uv")
    assert health.call_count == 2
    assert len(run.call_args_list) == 1
    assert run.call_args.args[0] == ["uv", "sync", "--no-dev", "--python", str(python)]


def test_music_server_allows_gpu_tier_to_load_planner(tmp_path):
    engine = tmp_path / "ACE-Step-1.5"
    executable = engine / ".venv" / "Scripts" / "acestep-api.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    (tmp_path / "Media" / "music").mkdir(parents=True)
    with (patch.object(music_backend, "ROOT", tmp_path),
          patch.object(music_backend, "ENGINE", engine),
          patch.object(music_backend, "healthy", side_effect=[False, False, True]),
          patch.object(music_backend, "music_lm_ready", return_value=True),
          patch.object(music_backend, "_setup_lock", return_value=nullcontext()),
          patch.object(music_backend, "install"),
          patch.object(music_backend.subprocess, "Popen") as launch):
        assert music_backend.start_server() is True
    assert launch.call_args.kwargs["env"]["ACESTEP_INIT_LLM"] == "auto"


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
    check.assert_called_once_with(str(output), 60, lyrics)
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
            check.assert_not_called()
            music_backend._run_job(started["job_id"])
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
    from cloud_ai import FAST_MODEL, LOCAL_MODEL

    good = {"choices": [{"finish_reason": "stop", "message": {
        "content": "[Verse]\n" + "Michigan lights and water rolling " * 10}}]}
    with patch("routing_mode.load_scope", return_value="local_only"), patch("cloud_ai.post", return_value=good) as post:
        lyrics = music_backend.compose_lyrics("Michigan rap", 60)
    assert "Michigan lights" in lyrics
    assert post.call_args.args[1]["model"] == FAST_MODEL
    bad = {"choices": [{"finish_reason": "length", "message": {
        "content": good["choices"][0]["message"]["content"]}}]}
    with patch("routing_mode.load_scope", return_value="local_only"), patch("cloud_ai.post", side_effect=[bad, good]) as retry:
        assert music_backend.compose_lyrics("Michigan rap", 60).startswith("[Verse]")
    assert retry.call_count == 2
    with patch("routing_mode.load_scope", return_value="local_only"), patch("cloud_ai.post", side_effect=[TimeoutError("model busy"), good]) as retry:
        assert music_backend.compose_lyrics("Michigan rap", 60).startswith("[Verse]")
    assert retry.call_count == 2
    with patch("routing_mode.load_scope", return_value="local_only"), patch("cloud_ai.post", return_value=bad):
        with pytest.raises(RuntimeError, match="could not finish"):
            music_backend.compose_lyrics("Michigan rap", 60)
    with patch("routing_mode.load_scope", return_value="local_only"), patch(
        "cloud_ai.post", side_effect=[bad, bad, good]
    ) as rescue:
        assert music_backend.compose_lyrics("Michigan rap", 60).startswith("[Verse]")
    assert rescue.call_count == 3
    assert [call.args[1]["model"] for call in rescue.call_args_list] == [
        FAST_MODEL, FAST_MODEL, LOCAL_MODEL,
    ]


def test_local_only_song_writes_lyrics_locally_when_global_scope_is_cloud_only():
    import usage_history
    import cloud_ai

    good = {"choices": [{"finish_reason": "stop", "message": {
        "content": "[Verse]\n" + "Tahoe water and summer mountains " * 10}}]}
    token = usage_history.CURRENT_TASK.set("a" * 12)
    try:
        with (patch("routing_mode.load_scope", return_value="cloud_only"),
              patch.object(cloud_ai, "task_route", return_value="local_only"),
              patch.object(cloud_ai, "post", return_value=good) as local,
              patch.object(cloud_ai.Router, "complete") as cloud):
            assert "Tahoe water" in music_backend.compose_lyrics("Tahoe song", 60)
        local.assert_called_once()
        cloud.assert_not_called()
    finally:
        usage_history.CURRENT_TASK.reset(token)


def test_cloud_only_music_writes_lyrics_in_cloud_then_falls_back_locally():
    from cloud_ai import FAST_MODEL, LOCAL_MODEL

    answer = {"choices": [{"finish_reason": "stop", "message": {
        "content": "[Verse]\n" + "Michigan lights and water rolling " * 10}}]}
    with (patch("routing_mode.load_scope", return_value="cloud_only"),
          patch("cloud_ai.Router.complete", return_value=answer) as cloud,
          patch("cloud_ai.post") as local):
        assert music_backend.compose_lyrics("Michigan rap", 60).startswith("[Verse]")
    assert cloud.call_args.args[0]["model"] == LOCAL_MODEL
    local.assert_not_called()

    with (patch("routing_mode.load_scope", return_value="cloud_only"),
          patch("cloud_ai.Router.complete", side_effect=RuntimeError("quota exhausted")),
          patch("cloud_ai.post", return_value=answer) as local):
        assert music_backend.compose_lyrics("Michigan rap", 60).startswith("[Verse]")
    assert local.call_args.args[1]["model"] == FAST_MODEL


def test_vocal_evidence_requires_words_through_most_of_the_song(tmp_path):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"runtime")
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"audio")
    sparse = {"text": "yeah yeah", "segments": [{"end": 10.0}]}
    sustained = {"text": "Michigan " * 60, "segments": [{"end": 55.0}]}
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(
        music_backend.subprocess, "run", side_effect=[
            type("Result", (), {"returncode": 0, "stdout": json.dumps(sparse)})(),
            type("Result", (), {"returncode": 0, "stdout": json.dumps(sustained)})(),
        ],
    ) as transcribe:
        assert not music_backend.vocal_evidence(audio, 60)["passed"]
        assert music_backend.vocal_evidence(audio, 60)["passed"]
    assert json.loads(transcribe.call_args_list[0].kwargs["input"])["music"] is True


def test_vocal_evidence_rejects_sparsely_sung_full_song(tmp_path):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"runtime")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    sparse = {"text": "Tahoe blue water summer night " * 23 + "Tahoe blue water",
              "segments": [{"end": 154.5}]}
    prominent = {"text": "Tahoe blue water summer night " * 40 + "Tahoe blue water",
                 "segments": [{"end": 154.0}]}
    lyrics = "[Verse] Tahoe blue water summer night beneath the mountains"
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(
        music_backend.subprocess, "run", side_effect=[
            type("Result", (), {"returncode": 0, "stdout": json.dumps(sparse)})(),
            type("Result", (), {"returncode": 0, "stdout": json.dumps(prominent)})(),
        ],
    ):
        first = music_backend.vocal_evidence(audio, 160, lyrics)
        revised = music_backend.vocal_evidence(audio, 160, lyrics)
    assert first["recognized_words"] == 118
    assert first["required_words"] == 136
    assert not first["passed"]
    assert revised["recognized_words"] == 203
    assert revised["passed"]


def test_repetitive_techno_hook_accepts_sparse_but_real_vocals(tmp_path):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    audio = tmp_path / "song.mp3"
    audio.touch()
    brief = "1990s German techno, minimal lyrics, repetitive nonsense vocals and short vocal chants"
    assert music_backend.sparse_vocal_style(brief)
    lyrics = "[Chorus] Tanz mit mir die ganze nacht, bist du bereit fur diesen beat"
    transcript = {"text": ("Tanz mit mir die ganze nacht bist du bereit fur diesen beat " * 6),
                  "segments": [{"end": 165.0}]}
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(
        music_backend.subprocess, "run", return_value=type(
            "Result", (), {"returncode": 0, "stdout": json.dumps(transcript)}
        )(),
    ):
        default = music_backend.vocal_evidence(audio, 180, lyrics)
        sparse = music_backend.vocal_evidence(audio, 180, lyrics, sparse_vocals=True)
    assert not default["passed"]
    assert sparse["passed"]
    assert sparse["required_words"] == 54


def test_vocal_evidence_rejects_unrelated_words_even_when_sustained(tmp_path):
    python = tmp_path / "Media/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"runtime")
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio")
    unrelated = {"text": "Sunshine dancing on the beach " * 12,
                 "segments": [{"end": 55.0}]}
    lyrics = ("[Verse]\nMichigan forests and winter snow, "
              "Detroit lights across the river flow, "
              "Traverse City orchards bloom in spring")
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(
        music_backend.subprocess, "run", return_value=type(
            "Result", (), {"returncode": 0, "stdout": json.dumps(unrelated)}
        )(),
    ):
        evidence = music_backend.vocal_evidence(audio, 60, lyrics)
    assert evidence["recognized_words"] >= 21
    assert evidence["last_vocal_second"] >= 36
    assert evidence["matched_lyric_terms"] < evidence["required_lyric_terms"]
    assert not evidence["passed"]


def test_lyric_match_ignores_stage_directions():
    matched, required = music_backend.lyric_match(
        "Michigan forests and winter snow across the river",
        "[Verse - winter snow]\nMichigan forests and winter snow across the river",
    )
    assert matched >= required >= 4


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


def test_lost_fresh_music_task_is_resubmitted_once(tmp_path):
    submitted = iter(("first", "replacement"))
    polled = []

    def engine(path, data=None):
        if path == "/release_task":
            return {"code": 200, "data": {"task_id": next(submitted)}}
        polled.append(data["task_id_list"][0])
        if len(polled) <= 10:
            return {"data": [{"task_id": "first", "status": 0, "result": "[]"}]}
        return {"data": [{"status": 1,
                          "result": '[{"file":"/v1/audio?path=replacement"}]'}]}

    saved = []
    with (patch.object(music_backend, "start_server"),
          patch.object(music_backend, "_request", side_effect=engine),
          patch.object(music_backend, "urlopen", return_value=io.BytesIO(b"song")),
          patch.object(music_backend.time, "sleep")):
        music_backend.generate_music(
            "jazz", str(tmp_path / "song.mp3"),
            on_task_submitted=saved.append,
        )
    assert polled == ["first"] * 10 + ["replacement"]
    assert saved == ["first", "replacement"]


def test_running_music_task_is_not_resubmitted_during_long_generation(tmp_path):
    polls = 0
    submissions = 0

    def engine(path, data=None):
        nonlocal polls, submissions
        if path == "/release_task":
            submissions += 1
            return {"code": 200, "data": {"task_id": "working"}}
        polls += 1
        if polls <= 12:
            return {"data": [{"status": 0, "result": '[{"stage":"generating","progress":0.2}]'}]}
        return {"data": [{"status": 1, "result": '[{"file":"/v1/audio?path=working"}]'}]}

    with (patch.object(music_backend, "start_server"),
          patch.object(music_backend, "_request", side_effect=engine),
          patch.object(music_backend, "urlopen", return_value=io.BytesIO(b"song")),
          patch.object(music_backend.time, "sleep")):
        music_backend.generate_music("jazz", str(tmp_path / "song.mp3"))
    assert polls == 13
    assert submissions == 1


def test_music_engine_progress_reaches_the_job_callback(tmp_path):
    responses = iter([
        {"code": 200, "data": {"task_id": "working"}},
        {"data": [{"status": 0,
                   "result": '[{"stage":"generating","progress":0.8}]'}]},
        {"data": [{"status": 1,
                   "result": '[{"file":"/v1/audio?path=working"}]'}]},
    ])
    progress = []
    with (patch.object(music_backend, "start_server"),
          patch.object(music_backend, "_request", side_effect=lambda *_a, **_k: next(responses)),
          patch.object(music_backend, "urlopen", return_value=io.BytesIO(b"song")),
          patch.object(music_backend.time, "sleep")):
        music_backend.generate_music(
            "jazz", str(tmp_path / "song.mp3"), on_progress=progress.append,
        )
    assert progress == [0.8]


def test_repeatedly_lost_music_task_reports_error_without_two_hour_wait(tmp_path):
    submissions = 0

    def engine(path, data=None):
        nonlocal submissions
        if path == "/release_task":
            submissions += 1
            return {"code": 200, "data": {"task_id": str(submissions)}}
        return {"data": [{"status": 0, "result": "[]"}]}

    with (patch.object(music_backend, "start_server"),
          patch.object(music_backend, "_request", side_effect=engine),
          patch.object(music_backend.time, "sleep")):
        with pytest.raises(RuntimeError, match="repeatedly lost this song"):
            music_backend.generate_music("jazz", str(tmp_path / "song.mp3"))
    assert submissions == 3


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
        assert music_backend.music_job_status(started["job_id"])["progress"] == 5
        process.assert_called_once()
        def finish(*args):
            args[7]("server-task-1")
            args[8](0.8)
            current = music_backend.music_job_status(started["job_id"])
            assert current["server_task_id"] == "server-task-1"
            assert current["progress"] == 63
            assert "audio 80%" in current["current"]
            return {"path": str(output)}

        with patch.object(music_backend, "generate_music", side_effect=finish), patch(
            "song_video.render", return_value={
                "path": "song-video.mp4", "captions_path": "song.captions.srt"
            }
        ):
            music_backend._run_job(started["job_id"])
        completed = music_backend.music_job_status(started["job_id"])
    assert completed["status"] == "complete"
    assert completed["progress"] == 100
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
    ) as unload, patch("cale_voice.release_for_gpu_task") as release_voice:
        with REAL_GPU_LEASE():
            marker = tmp_path / "LongTasks" / f"music-gpu-active-{os.getpid()}.json"
            assert marker.is_file()
        assert not marker.exists()
    unload.assert_called_once()
    release_voice.assert_called_once()


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


def test_sdk_failure_retries_same_saved_music_job_once(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    job_id = "e" * 32
    record = {
        "job_id": job_id, "status": "error",
        "current": "No module named 'openai.types.shared_params.response_format_json_object'",
        "output_path": str(tmp_path / "song.mp3"), "server_task_id": "saved-server-task",
    }
    (jobs / (job_id + ".json")).write_text(json.dumps(record))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ) as launch:
        resumed = music_backend.resume_failed_sdk_job(job_id)
        launch.assert_called_once_with(job_id)
        assert resumed["status"] == "queued"
        assert resumed["server_task_id"] == "saved-server-task"
        assert resumed["sdk_retries"] == 1
        music_backend._write_job(job_id, {**resumed, "status": "error",
                                          "current": record["current"]})
        with pytest.raises(ValueError, match="not eligible"):
            music_backend.resume_failed_sdk_job(job_id)


def test_music_worker_saves_runtime_and_traceback_for_failed_render(tmp_path):
    jobs = tmp_path / "music-jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ):
        started = music_backend.start_music_job(
            "instrumental techno", str(output), instrumental=True)
        with patch.object(music_backend, "generate_music", side_effect=ModuleNotFoundError(
            "No module named 'openai.types.shared_params.response_format_json_object'")):
            music_backend._run_job(started["job_id"])
        failed = music_backend.music_job_status(started["job_id"])
    assert failed["status"] == "error"
    assert failed["error_stage"] == "Generating music locally"
    diagnostic = tmp_path / ".local-ai" / f"music-error-{started['job_id']}.log"
    assert failed["diagnostic_path"] == str(diagnostic)
    assert "Worker Python:" in diagnostic.read_text()
    assert "ModuleNotFoundError" in diagnostic.read_text()


def test_recovery_only_marks_playable_saved_song_complete(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    job_id = "d" * 32
    output = tmp_path / "song.mp3"
    output.write_bytes(b"incomplete audio")
    (jobs / (job_id + ".json")).write_text(json.dumps({
        "job_id": job_id, "status": "working", "current": "Generating",
        "output_path": str(output), "created": 1, "lyrics": "",
        "prompt": "instrumental song", "instrumental": True,
        "source_path": "", "duration_seconds": 30,
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
    }) as render:
        record = music_backend.music_job_status(job_id)
        music_backend._write_job(job_id, {**record, "status": "working"})
        music_backend.recover_music_jobs()
        assert music_backend.music_job_status(job_id)["status"] == "queued"
        launch.assert_called_once_with(job_id)
        render.assert_not_called()  # The supervisor stays free for other tasks.
        music_backend._run_job(job_id)
        assert music_backend.music_job_status(job_id)["status"] == "complete"


def test_saved_song_vocal_failure_uses_normal_retry_signal(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    job_id = "f" * 32
    output = tmp_path / "song.wav"
    output.write_bytes(b"saved audio")
    (jobs / (job_id + ".json")).write_text(json.dumps({
        "job_id": job_id, "status": "working", "created": 1,
        "output_path": str(output), "prompt": "female lead vocals",
        "lyrics": "Sing the bright summer chorus across the water and keep the melody alive " * 3,
        "duration_seconds": 60, "instrumental": False, "source_path": "",
    }))
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(music_backend, "_worker_running", return_value=False),
          patch.object(music_backend, "audio_file_ok", return_value=True),
          patch.object(music_backend, "_launch_job") as launch,
          patch.object(music_backend, "vocal_evidence", return_value={"passed": False}) as check):
        music_backend.recover_music_jobs()
        check.assert_not_called()
        launch.assert_called_once_with(job_id)
        music_backend._run_job(job_id)
        recovered = music_backend.music_job_status(job_id)
    assert recovered["status"] == "error"
    assert "sustained vocals were not detected" in recovered["current"]


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
                assert music_backend.music_job_status(started["job_id"])["status"] == "queued"
                music_backend._run_job(started["job_id"])
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
        "source_path": "",
    }))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "audio_file_ok", return_value=True), patch(
        "song_video.render", side_effect=RuntimeError("encoder unavailable")
    ) as render:
        for attempt in range(1, 4):
            music_backend.recover_music_jobs()
            assert music_backend.music_job_status(job_id)["status"] == "queued"
            music_backend._run_job(job_id)
            record = music_backend.music_job_status(job_id)
            assert record["video_retries"] == attempt
            assert record["status"] == ("error" if attempt == 3 else "queued")
            if attempt < 3:
                music_backend._write_job(job_id, {**record, "created": 0})
        music_backend.recover_music_jobs()
    assert render.call_count == 3
    assert output.read_bytes() == b"preserved audio"


def test_agent_music_job_is_attributed_and_task_controls_only_its_renders(tmp_path):
    task_id = "a" * 12
    project = tmp_path / "Projects" / task_id
    project.mkdir(parents=True)
    other = tmp_path / "Projects" / ("b" * 12)
    other.mkdir(parents=True)
    state = tmp_path / "LongTasks"
    state.mkdir()
    with sqlite3.connect(state / "projects.db") as connection:
        connection.execute("CREATE TABLE projects (id TEXT, folder TEXT)")
        connection.execute("INSERT INTO projects VALUES (?,?)", (task_id, str(project)))
    jobs = state / "music-jobs"
    jobs.mkdir()
    records = [
        ("1" * 32, "working", project / "song-v1.wav"),
        ("2" * 32, "paused", project / "song-v2.wav"),
        ("3" * 32, "working", other / "song.wav"),
    ]
    for job_id, status, output in records:
        (jobs / (job_id + ".json")).write_text(json.dumps({
            "job_id": job_id, "status": status, "output_path": str(output),
            "created": int(job_id[0]),
        }))
    with patch.object(music_backend, "ROOT", tmp_path), patch.object(music_backend, "JOBS", jobs):
        assert music_backend.project_for_output(str(project / "song-v3.wav")) == task_id
        assert music_backend.project_for_output(str(other / "song.wav")) == ""
        with patch.object(music_backend, "hold_music_job") as hold:
            assert music_backend.hold_project_jobs(task_id, project, paused=True) == 1
            hold.assert_called_once_with("1" * 32, paused=True)
        with patch.object(music_backend, "resume_music_job") as resume:
            assert music_backend.resume_project_jobs(task_id, project) == 1
            resume.assert_called_once_with("2" * 32)
