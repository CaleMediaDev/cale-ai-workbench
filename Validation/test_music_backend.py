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
from pathlib import Path
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


@pytest.mark.parametrize("response", [
    None, [], "unavailable", 42,
    {"data": None}, {"data": []}, {"data": "unavailable"}, {"data": 42},
])
@pytest.mark.parametrize("probe", [music_backend.healthy, music_backend.music_lm_ready])
def test_health_probes_reject_non_object_response_shapes(response, probe):
    with patch.object(music_backend, "_request", return_value=response) as request:
        assert probe() is False
    request.assert_called_once_with("/health", timeout=2)


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
    executable = engine / ".venv" / "Scripts" / "python.exe"
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
        progress_updates = []

        def generate_with_progress(*args):
            args[8](1.0)
            return {"path": str(output)}

        original_write = music_backend._write_job

        def capture_write(job_id, info):
            progress_updates.append((info.get("progress"), info.get("current")))
            original_write(job_id, info)

        with patch.object(music_backend, "compose_lyrics", return_value=lyrics) as compose, patch.object(
            music_backend, "generate_music", side_effect=generate_with_progress
        ) as generate, patch.object(
            music_backend, "vocal_evidence", return_value={"passed": True,
                "recognized_words": 50, "last_vocal_second": 58}
        ) as check, patch.object(
            music_backend, "_write_job", side_effect=capture_write
        ), patch("song_video.render_branded", return_value={
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
    assert (70, "Generating music locally · audio 100%") in progress_updates
    assert (75, "Checking the vocals") in progress_updates
    assert job["result"]["video"]["path"] == "song-video.mp4"
    render.assert_called_once()


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
              patch("song_video.render_branded", return_value={
                  "path": "song-video.mp4", "captions_path": "song.captions.srt"
              }) as render):
            music_backend._run_job(started["job_id"])
        job = music_backend.music_job_status(started["job_id"])
    compose.assert_not_called()
    check.assert_not_called()
    assert generate.call_args.args[2] == ""
    render.assert_called_once()
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
              patch("song_video.render_branded", return_value={
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


def test_sparse_vocal_hooks_get_short_lyrics_with_room_for_drops():
    brief = ("Female singing voice, main vocal hook: waapy waaap. "
             "Very little conventional singing, with massive instrumental drops.")
    answer = {"choices": [{"finish_reason": "stop", "message": {
        "content": "[Hook]\n" + "Waapy waaap, dance through the night " * 10}}]}
    with patch("routing_mode.load_scope", return_value="local_only"), patch(
        "cloud_ai.post", return_value=answer
    ) as post:
        lyrics = music_backend.compose_lyrics(brief, 180)
    assert lyrics.startswith("[Hook]")
    instruction = post.call_args.args[1]["messages"][0]["content"]
    assert "[Instrumental Drop]" in instruction
    assert "long verses" in instruction
    assert "Aim for about 81 words" in instruction


def test_local_only_song_writes_lyrics_locally_when_global_scope_is_cloud_only():
    import cloud_ai
    import usage_history

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


def test_sparse_vocal_check_does_not_count_instrumental_drop_directions():
    lyrics = ("[Hook]\nI want waapy waaap, make your head go wet\n"
              "[Instrumental Drop]\nMetallic alien growl, rubbery bass rumble\n"
              "[Hook]\nI want waapy waaap, make your head go wet")
    transcript = "Metallic alien growl rubbery bass rumble and make your head"
    assert music_backend.lyric_match(transcript, lyrics)[0] >= 4
    matched, required = music_backend.lyric_match(
        transcript, lyrics, vocal_sections_only=True,
    )
    assert matched < required
    assert "Metallic" not in music_backend.vocal_lines(lyrics)


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


def test_longer_reference_song_uses_style_audio_and_requested_duration(tmp_path):
    source = tmp_path / "short-reference.wav"
    source.write_bytes(b"source audio")
    response = {"data": [{"status": 1,
                         "result": '[{"file":"/v1/audio?path=long-song"}]'}]}
    with (patch.object(music_backend, "start_server", return_value=False),
          patch.object(music_backend, "audio_duration_seconds", return_value=29),
          patch.object(music_backend, "_request", return_value=response),
          patch.object(music_backend, "_upload_cover",
                       return_value={"code": 200, "data": {"task_id": "task-3"}}) as upload,
          patch.object(music_backend, "urlopen", return_value=io.BytesIO(b"three-minute song")),
          patch.object(music_backend.time, "sleep")):
        music_backend.generate_music(
            "Male singer, late-night pop", str(tmp_path / "song.mp3"),
            lyrics="A sung verse and chorus", duration_seconds=180,
            source_path=str(source),
        )
    request = upload.call_args.args[0]
    assert upload.call_args.args[2] == "reference_audio"
    assert request["task_type"] == "text2music"
    assert request["audio_duration"] == 180
    assert request["lyrics"] == "A sung verse and chorus"


def test_video_reference_extracts_audio_for_local_upload(tmp_path):
    pcm = tmp_path / "tone.wav"
    with wave.open(str(pcm), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b"".join(struct.pack("<h", round(12000 * math.sin(
            2 * math.pi * 440 * i / 44100))) for i in range(44100)))
    video = tmp_path / "reference.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner",
                    "-loglevel", "error", "-i", str(pcm), "-c:a", "aac", str(video)],
                   check=True, timeout=30)
    assert music_backend.effective_reference_mode(video, 180, "cover") == "style"
    uploaded = []

    def inspect_upload(request, source, field):
        assert request["task_type"] == "text2music"
        assert field == "reference_audio"
        assert source.suffix == ".wav"
        with wave.open(str(source), "rb") as converted:
            assert converted.getnframes() >= 44000
        uploaded.append(source)
        return {"code": 200, "data": {"task_id": "local-only"}}

    with (patch.object(music_backend, "ROOT", tmp_path),
          patch.object(music_backend, "_upload_cover", side_effect=inspect_upload)):
        result = music_backend._upload_reference_media(
            {"task_type": "text2music"}, video, "reference_audio")
    assert result["data"]["task_id"] == "local-only"
    assert uploaded and not uploaded[0].exists()
    assert video.is_file()


def test_phone_m4a_reference_converts_to_wav_for_local_upload(tmp_path):
    pcm = tmp_path / "tone.wav"
    with wave.open(str(pcm), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b"\0\0" * 44100)
    phone_audio = tmp_path / "phone-recording.m4a"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner",
                    "-loglevel", "error", "-i", str(pcm), "-c:a", "aac", str(phone_audio)],
                   check=True, timeout=30)
    uploaded = []

    def inspect_upload(request, source, field):
        assert field == "reference_audio"
        assert source.suffix == ".wav"
        with wave.open(str(source), "rb") as converted:
            assert converted.getnframes() >= 44000
        uploaded.append(source)
        return {"code": 200, "data": {"task_id": "local-only"}}

    with (patch.object(music_backend, "ROOT", tmp_path),
          patch.object(music_backend, "_upload_cover", side_effect=inspect_upload)):
        result = music_backend._upload_reference_media(
            {"task_type": "text2music"}, phone_audio, "reference_audio")
    assert result["data"]["task_id"] == "local-only"
    assert uploaded and not uploaded[0].exists()
    assert phone_audio.is_file()


def test_durable_long_reference_checks_actual_duration_and_vocals(tmp_path):
    source = tmp_path / "reference.wav"
    source.write_bytes(b"source audio")
    output = tmp_path / "song.mp3"
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "_launch_job"),
          patch.object(music_backend, "audio_duration_seconds", return_value=29)):
        started = music_backend.start_music_job(
            "Male singer with verses and chorus", str(output),
            lyrics="[Verse]\nA late night song", duration_seconds=180,
            source_path=str(source),
        )
        assert music_backend.music_job_status(started["job_id"])["reference_mode"] == "style"
        with (patch.object(music_backend, "audio_duration_seconds", return_value=29),
              patch.object(music_backend, "generate_music", return_value={"path": str(output)}),
              patch.object(music_backend, "vocal_evidence") as vocals):
            music_backend._run_job(started["job_id"])
        failed = music_backend.music_job_status(started["job_id"])
        assert failed["status"] == "error"
        assert "source clip's length" in failed["current"]
        vocals.assert_not_called()

        failed["status"] = "queued"
        music_backend._write_job(started["job_id"], failed)
        with (patch.object(music_backend, "audio_duration_seconds", return_value=180),
              patch.object(music_backend, "generate_music", return_value={"path": str(output)}),
              patch.object(music_backend, "vocal_evidence", return_value={"passed": True,
                  "segments": []}) as vocals,
              patch("song_video.render_branded", return_value={"path": "song-video.mp4"})):
            music_backend._run_job(started["job_id"])
        assert music_backend.music_job_status(started["job_id"])["status"] == "complete"
        vocals.assert_called_once()


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


def test_setup_status_interleaved_writes_use_distinct_temporary_files(tmp_path):
    original_replace = music_backend.os.replace
    sources = []

    def interleave_write(source, destination):
        sources.append(Path(source))
        if len(sources) == 1:
            music_backend._write_setup_status("working", "Second writer", 35)
        return original_replace(source, destination)

    with (patch.object(music_backend, "ROOT", tmp_path),
          patch.object(music_backend.os, "replace", side_effect=interleave_write)):
        music_backend._write_setup_status("working", "First writer", 5)

    directory = tmp_path / "LongTasks"
    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert json.loads((directory / "music-install-progress.json").read_text(
        encoding="utf-8"
    )) == {"status": "working", "current": "First writer", "progress": 5}
    assert not list(directory.glob("*.tmp"))


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
            "song_video.render_branded", return_value={
                "path": "song-video.mp4", "captions_path": "song.captions.srt"
            }
        ):
            music_backend._run_job(started["job_id"])
        completed = music_backend.music_job_status(started["job_id"])
    assert completed["status"] == "complete"
    assert completed["progress"] == 100
    assert completed["result"]["path"] == str(output)


def test_explicit_singer_overrides_wrong_instrumental_tool_argument(tmp_path):
    jobs = tmp_path / "music-jobs"
    output = tmp_path / "song.mp3"
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_launch_job"
    ):
        started = music_backend.start_music_job(
            "A gritty male rapper performs verses about Bobby's mobile detailing",
            str(output), instrumental=True)
        saved = music_backend.music_job_status(started["job_id"])
        assert saved["instrumental"] is False
        # A plainly instrumental request still keeps its explicit setting.
        other = music_backend.start_music_job(
            "An instrumental track with no vocals, inspired by G-funk",
            str(tmp_path / "beat.mp3"), instrumental=True)
        assert music_backend.music_job_status(other["job_id"])["instrumental"] is True

        # Queued jobs from an older release receive the same correction when
        # their worker starts, before the music engine sees the request.
        saved["instrumental"] = True
        music_backend._write_job(started["job_id"], saved)
        with (patch.object(music_backend, "compose_lyrics", return_value="Bobby shines every ride"),
              patch.object(music_backend, "generate_music", return_value={"path": str(output)}),
              patch.object(music_backend, "vocal_evidence", return_value={"passed": True,
                  "segments": []}), patch("song_video.render_branded", return_value={"path": "video.mp4"})):
            music_backend._run_job(started["job_id"])
        assert music_backend.music_job_status(started["job_id"])["status"] == "complete"
        assert music_backend.music_job_status(started["job_id"])["instrumental"] is False


def test_durable_reference_path_survives_worker_directory_change(tmp_path, monkeypatch):
    submission_dir = tmp_path / "submission"
    submission_dir.mkdir()
    reference = submission_dir / "reference.wav"
    reference.write_bytes(b"reference audio")
    worker_dir = tmp_path / "worker"
    worker_dir.mkdir()
    output = tmp_path / "cover.mp3"
    monkeypatch.chdir(submission_dir)
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "_launch_job"),
          patch.object(music_backend, "audio_duration_seconds", return_value=30)):
        started = music_backend.start_music_job(
            "instrumental jazz cover", str(output), instrumental=True,
            source_path="reference.wav", make_video=False,
        )
        saved = music_backend.music_job_status(started["job_id"])
        assert saved["source_path"] == str(reference)
        monkeypatch.chdir(worker_dir)
        with patch.object(music_backend, "generate_music",
                          return_value={"path": str(output)}) as generate:
            music_backend._run_job(started["job_id"])
        assert generate.call_args.args[5] == str(reference)
        assert music_backend.music_job_status(started["job_id"])["status"] == "complete"


def test_missing_worker_runtime_marks_submission_error(tmp_path):
    jobs = tmp_path / "jobs"
    output = tmp_path / "song.mp3"
    with (patch.object(music_backend, "ROOT", tmp_path),
          patch.object(music_backend, "JOBS", jobs),
          patch.object(music_backend.subprocess, "Popen") as launch):
        with pytest.raises(RuntimeError, match="Toolbox runtime is missing"):
            music_backend.start_music_job(
                "instrumental piano", str(output), instrumental=True,
                make_video=False,
            )
        records = list(jobs.glob("*.json"))
        assert len(records) == 1
        job = music_backend.music_job_status(records[0].stem)
    launch.assert_not_called()
    assert job["status"] == "error"
    assert job["current"] == "The local Toolbox runtime is missing."
    assert job["output_path"] == str(output)
    assert not output.exists()


def test_music_job_id_cannot_escape_job_directory():
    with pytest.raises(ValueError, match="Invalid local music job ID"):
        music_backend.music_job_status("../outside")


def test_job_status_write_retries_windows_file_lock_with_unique_temp_files(tmp_path):
    job_id = "f" * 32
    original_replace = music_backend.os.replace
    sources = []
    failures = 0

    def briefly_locked(source, destination):
        nonlocal failures
        sources.append(Path(source))
        if failures < 2:
            failures += 1
            raise PermissionError(5, "Access is denied", str(destination))
        return original_replace(source, destination)

    with (patch.object(music_backend, "JOBS", tmp_path),
          patch.object(music_backend.os, "replace", side_effect=briefly_locked)):
        music_backend._write_job(job_id, {"job_id": job_id, "status": "working"})
        music_backend._write_job(job_id, {"job_id": job_id, "status": "complete"})
        assert music_backend.music_job_status(job_id)["status"] == "complete"
    assert len(sources) == 4
    assert sources[0] == sources[1] == sources[2]
    assert sources[3] != sources[0]
    assert not list(tmp_path.glob("*.tmp"))


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


def test_stopping_paused_music_job_prevents_resume(tmp_path):
    job_id = "c" * 32
    record = {
        "job_id": job_id, "status": "paused", "current": "Paused",
        "server_task_id": "ace-123", "worker_pid": None,
        "output_path": str(tmp_path / "song.mp3"),
    }
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "_worker_running", return_value=False),
          patch.object(music_backend, "_launch_job") as launch):
        music_backend._write_job(job_id, record)
        assert music_backend.pause_music_job(job_id) == record
        stopped = music_backend.stop_music_job(job_id)
        assert stopped["status"] == "stopped"
        assert stopped["current"] == "Stopped by user"
        assert stopped["server_task_id"] == "ace-123"
        assert music_backend.music_job_status(job_id) == stopped
        with pytest.raises(ValueError, match="Only a paused music job"):
            music_backend.resume_music_job(job_id)
        launch.assert_not_called()


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


@pytest.mark.parametrize("bad_record", [None, [], "damaged", 42, True])
def test_recovery_skips_non_object_record_and_recovers_next_job(tmp_path, bad_record):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    bad_id = "a" * 32
    good_id = "b" * 32
    bad_path = jobs / (bad_id + ".json")
    good_path = jobs / (good_id + ".json")
    bad_path.write_text(json.dumps(bad_record), encoding="utf-8")
    good_path.write_text(json.dumps({
        "job_id": good_id, "status": "working", "created": 0,
        "output_path": str(tmp_path / "good.mp3"),
    }), encoding="utf-8")
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(Path, "glob", return_value=[bad_path, good_path]),
          patch.object(music_backend, "_worker_running", return_value=False),
          patch.object(music_backend, "_launch_job") as launch):
        music_backend.recover_music_jobs()
        launch.assert_called_once_with(good_id)
        recovered = music_backend.music_job_status(good_id)
        assert recovered["status"] == "queued"
        assert recovered["recoveries"] == 1
        assert json.loads(bad_path.read_text(encoding="utf-8")) == bad_record


def test_recovery_skips_overflowing_timestamp_and_recovers_next_job(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    bad_id = "a" * 32
    good_id = "b" * 32
    bad_path = jobs / (bad_id + ".json")
    good_path = jobs / (good_id + ".json")
    bad_record = {
        "job_id": bad_id, "status": "queued", "created": 10**400,
        "output_path": str(tmp_path / "bad.mp3"),
    }
    bad_path.write_text(json.dumps(bad_record), encoding="utf-8")
    good_path.write_text(json.dumps({
        "job_id": good_id, "status": "working", "created": 0,
        "output_path": str(tmp_path / "good.mp3"),
    }), encoding="utf-8")
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(Path, "glob", return_value=[bad_path, good_path]),
          patch.object(music_backend, "_worker_running", return_value=False),
          patch.object(music_backend, "_launch_job") as launch):
        music_backend.recover_music_jobs()
        launch.assert_called_once_with(good_id)
        recovered = music_backend.music_job_status(good_id)
        assert recovered["status"] == "queued"
        assert recovered["recoveries"] == 1
        assert music_backend.music_job_status(bad_id) == bad_record


@pytest.mark.parametrize("created", [float("inf"), float("-inf"), float("nan")])
def test_recovery_skips_nonfinite_timestamp_and_recovers_next_job(tmp_path, created):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    bad_id = "a" * 32
    good_id = "b" * 32
    bad_path = jobs / (bad_id + ".json")
    good_path = jobs / (good_id + ".json")
    bad_path.write_text(json.dumps({
        "job_id": bad_id, "status": "queued", "created": created,
        "output_path": str(tmp_path / "bad.mp3"),
    }), encoding="utf-8")
    original = bad_path.read_bytes()
    good_path.write_text(json.dumps({
        "job_id": good_id, "status": "queued", "created": 0,
        "output_path": str(tmp_path / "good.mp3"),
    }), encoding="utf-8")
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(Path, "glob", return_value=[bad_path, good_path]),
          patch.object(music_backend.time, "time", return_value=100),
          patch.object(music_backend, "_worker_running", return_value=False),
          patch.object(music_backend, "_launch_job") as launch):
        music_backend.recover_music_jobs()
        launch.assert_called_once_with(good_id)
        assert music_backend.music_job_status(good_id)["recoveries"] == 1
    assert bad_path.read_bytes() == original


def test_recovery_skips_null_timestamp_and_recovers_next_job(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    bad_id = "a" * 32
    good_id = "b" * 32
    bad_path = jobs / (bad_id + ".json")
    good_path = jobs / (good_id + ".json")
    bad_record = {
        "job_id": bad_id, "status": "queued", "created": None,
        "output_path": str(tmp_path / "bad.mp3"),
    }
    bad_path.write_text(json.dumps(bad_record), encoding="utf-8")
    good_path.write_text(json.dumps({
        "job_id": good_id, "status": "working", "created": 0,
        "output_path": str(tmp_path / "good.mp3"),
    }), encoding="utf-8")
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(Path, "glob", return_value=[bad_path, good_path]),
          patch.object(music_backend, "_worker_running", return_value=False),
          patch.object(music_backend, "_launch_job") as launch):
        music_backend.recover_music_jobs()
        launch.assert_called_once_with(good_id)
        recovered = music_backend.music_job_status(good_id)
        assert recovered["status"] == "queued"
        assert recovered["recoveries"] == 1
        assert music_backend.music_job_status(bad_id) == bad_record


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


@pytest.mark.parametrize("failure", [
    OSError("Worker launch failed"),
    RuntimeError("The local Toolbox runtime is missing."),
])
def test_sdk_retry_launch_failure_is_saved_as_error(tmp_path, failure):
    job_id = "e" * 32
    record = {
        "job_id": job_id, "status": "error",
        "current": "No module named 'openai.types.shared_params.response_format_json_object'",
        "output_path": str(tmp_path / "song.mp3"),
        "server_task_id": "saved-server-task",
    }
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "_launch_job", side_effect=failure) as launch):
        music_backend._write_job(job_id, record)
        with pytest.raises(type(failure), match=str(failure)):
            music_backend.resume_failed_sdk_job(job_id)
        saved = music_backend.music_job_status(job_id)
    launch.assert_called_once_with(job_id)
    assert saved["status"] == "error"
    assert saved["current"] == str(failure)
    assert saved["server_task_id"] == "saved-server-task"
    assert saved["sdk_retries"] == 1


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
    ) as launch, patch("song_video.render_branded", return_value={
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
            "instrumental beach guitar", str(output), instrumental=True,
            make_video=True,
        )
        def generate(*_args):
            output.write_bytes(b"preserved audio")
            return {"path": str(output), "model": "ACE-Step 1.5"}

        with patch.object(music_backend, "generate_music", side_effect=generate) as engine, patch(
            "song_video.render_branded", side_effect=[RuntimeError("encoder busy"),
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
        "source_path": "", "make_video": True,
    }))
    with patch.object(music_backend, "JOBS", jobs), patch.object(
        music_backend, "_worker_running", return_value=False
    ), patch.object(music_backend, "audio_file_ok", return_value=True), patch(
        "song_video.render_branded", side_effect=RuntimeError("encoder unavailable")
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


@pytest.mark.parametrize("bad_record", [None, [], "damaged", 42, True])
def test_project_jobs_skips_non_object_records(tmp_path, bad_record):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    task_id = "a" * 12
    bad_path = jobs / ("1" * 32 + ".json")
    good_path = jobs / ("2" * 32 + ".json")
    good_record = {
        "job_id": "2" * 32, "status": "working", "created": 1,
        "output_path": str(project / "song.mp3"),
    }
    bad_path.write_text(json.dumps(bad_record), encoding="utf-8")
    good_path.write_text(json.dumps(good_record), encoding="utf-8")
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(Path, "glob", return_value=[bad_path, good_path])):
        assert music_backend.project_jobs(task_id, project) == [good_record]
    assert json.loads(bad_path.read_text(encoding="utf-8")) == bad_record


@pytest.mark.parametrize("bad_created", ["not-a-timestamp", [1], {"time": 1}, 10**400])
def test_project_jobs_skips_invalid_timestamp_and_sorts_valid_jobs(tmp_path, bad_created):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    task_id = "a" * 12
    records = [
        {"job_id": "1" * 32, "task_id": task_id, "created": bad_created},
        {"job_id": "2" * 32, "task_id": task_id, "created": 20},
        {"job_id": "3" * 32, "task_id": task_id, "created": 10},
    ]
    paths = []
    for record in records:
        path = jobs / (record["job_id"] + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(path)
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(Path, "glob", return_value=paths)):
        assert music_backend.project_jobs(task_id, project) == [records[2], records[1]]
    assert json.loads(paths[0].read_text(encoding="utf-8")) == records[0]


@pytest.mark.parametrize("created", [float("nan"), float("inf"), float("-inf")])
def test_project_jobs_skips_nonfinite_timestamps_and_preserves_valid_order(tmp_path, created):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    task_id = "a" * 12
    records = [
        {"job_id": "1" * 32, "task_id": task_id, "created": 20},
        {"job_id": "2" * 32, "task_id": task_id, "created": created},
        {"job_id": "3" * 32, "task_id": task_id, "created": 10},
    ]
    paths = []
    for record in records:
        path = jobs / (record["job_id"] + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(path)
    original = paths[1].read_bytes()
    with (patch.object(music_backend, "JOBS", jobs),
          patch.object(Path, "glob", return_value=paths)):
        assert music_backend.project_jobs(task_id, tmp_path) == [records[2], records[0]]
    assert paths[1].read_bytes() == original


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



def test_video_repair_pins_audio_and_never_regenerates_after_it_changes(tmp_path):
    import hashlib

    job_id = "d" * 32
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"original audio")
    info = {"job_id": job_id, "status": "complete", "output_path": str(audio),
            "prompt": "instrumental", "instrumental": True, "lyrics": "", "source_path": "",
            "duration_seconds": 10, "make_video": True,
            "result": {"path": str(audio), "video": {"kind": "branded_song_video",
                "path": str(tmp_path / "song-facebook.mp4"),
                "source_sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}}}
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "_launch_job") as launch,
          patch.object(music_backend, "generate_music", side_effect=AssertionError("No new audio")),
          patch.object(music_backend, "compose_lyrics", side_effect=AssertionError("No new lyrics"))):
        music_backend._write_job(job_id, info)
        assert music_backend.repair_saved_music_video(job_id)
        assert not music_backend.repair_saved_music_video(job_id)
        launch.assert_called_once()
        audio.write_bytes(b"owner edited the audio")
        music_backend._run_job(job_id)
        failed = music_backend.music_job_status(job_id)
    assert failed["status"] == "error" and "audio changed" in failed["current"]
    assert audio.read_bytes() == b"owner edited the audio"


def test_stop_during_video_repair_request_cannot_be_overwritten(tmp_path):
    import song_video

    job_id = "e" * 32
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"saved audio")
    info = {"job_id": job_id, "status": "complete", "output_path": str(audio),
            "result": {"path": str(audio), "video": {"kind": "branded_song_video",
                "path": str(tmp_path / "missing.mp4")}}}
    def stopped_during_hash(_):
        music_backend._write_job(job_id, {**info, "status": "stopped"})
        return "a" * 64
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "_launch_job") as launch,
          patch.object(song_video, "_digest", side_effect=stopped_during_hash)):
        music_backend._write_job(job_id, info)
        assert not music_backend.repair_saved_music_video(job_id)
        assert music_backend.music_job_status(job_id)["status"] == "stopped"
    launch.assert_not_called()



def test_video_repair_budget_can_be_explicitly_retried_without_new_song(tmp_path):
    job_id = "f" * 32
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"preserved audio")
    info = {"job_id": job_id, "status": "complete", "output_path": str(audio),
            "completed_video_repairs": 3,
            "result": {"path": str(audio), "video": {"kind": "branded_song_video",
                "path": str(tmp_path / "song-facebook.mp4")}}}
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "_launch_job") as launch):
        music_backend._write_job(job_id, info)
        assert not music_backend.repair_saved_music_video(job_id)
        assert music_backend.repair_saved_music_video(job_id, retry=True)
        resumed = music_backend.music_job_status(job_id)
    launch.assert_called_once()
    assert resumed["status"] == "queued" and resumed["video_repair_only"]
    assert resumed["completed_video_repairs"] == 1 and audio.read_bytes() == b"preserved audio"



def test_vocal_song_video_repair_does_not_recompose_or_retranscribe(tmp_path):
    import hashlib

    job_id = "1" * 32
    audio = tmp_path / "vocal.mp3"
    audio.write_bytes(b"approved vocal audio")
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    info = {"job_id": job_id, "status": "queued", "output_path": str(audio),
            "prompt": "Rap with clear lead vocals", "instrumental": False, "lyrics": "", "source_path": "",
            "duration_seconds": 180, "make_video": True, "video_repair_only": True,
            "video_repair_source_sha256": digest, "vocal_check": {"passed": True},
            "result": {"path": str(audio), "model": "ACE-Step 1.5"}}
    with (patch.object(music_backend, "JOBS", tmp_path / "jobs"),
          patch.object(music_backend, "audio_file_ok", return_value=True),
          patch.object(music_backend, "generate_music", side_effect=AssertionError("No new music")),
          patch.object(music_backend, "compose_lyrics", side_effect=AssertionError("No new lyrics")),
          patch.object(music_backend, "vocal_evidence", side_effect=AssertionError("No new ASR")),
          patch("song_video.render_branded", return_value={"path": str(tmp_path / "video.mp4")}) as render):
        music_backend._write_job(job_id, info)
        music_backend._run_job(job_id)
        done = music_backend.music_job_status(job_id)
    assert done["status"] == "complete" and done["vocal_check"]["passed"]
    assert render.call_args.kwargs["expected_source_sha256"] == digest
    assert audio.read_bytes() == b"approved vocal audio"


# Exact MIT upstream source fixture at ca1e85fe; no installed engine is required.
_ACE_CPU_DECODE_FIXTURE = 'c-q}sTW{RD5q{ra!RkrMS<7~kQxw~704H&R1H_5p<D@NIL#8EamyK>EDchSMKz~GkVSh<yDC$n_ZrrpFCl9fgmzm)=_gOoR^O}Wu#18Lh!Xv79o{=?63MM3pb3txyRxD$JDi)-r<l)T?$wjzkQVD(7cN}Ln<7tr#MOI<`TQ2M06i?YK7I{k4rr_C%nDvuvGbgWksOIEDfs|-6CzoZBu!fvMPSxEcUzMV)@4}}Q6SHcIh9C?p{8do~VV=djVX<N=U~$Ys#qU@U3&w&wLDOJ$JDbhIgi1-?n#Y~t+bSf^_yf-tGXnqMZJ#Pw7^z{1NCN+7N)nnyB+^%iTC<e!Oy%UB-Z8)pUdu9>3t8cV#3B}xpb)H}f(3n0gNjTb6)d^>H2#w;iRaCyyi`T0<U)gX1^CYq!|V0{&tfJ5#6kuj0`SULf=RdmlC{TZs7jgyw^W7eK=LnaxL-y*52CWt@1oJpRh;Bhb@){{1ai$|wIF(Tv<dt}ny}usM-E<8n!eH+_AfP5^TETe7Y#AM@Kv59pvW3<o*@=m1$VWE1idE_k7Ga-jM!+&pEXoY#7ed_dywATNG9%h*eV9?T<?;CZb0^kfNd$tGc;jE1=v>45Wvh~2iSG#Piy3~Hp`C0cn-LfnbMvIR?So3FraBsUrWqLkJR)5f(HioC)C@$TkpaHzzE^t(Ljj4>QADGYxdPLDRQX};H}`865-OvtOOeKI4cvyi-d=CD<M4G(dY$JrN{<K)t1%OR&5{=s2Th(3XI-B#i+ToRP+{Z+}vQOf)FBctILIQ$p%a=Kud7?RES(G$S-kY3x+RwPv0N@lEuipWD;T~>?8>-Bm|3`&?F&Wn8-V@Vw`Bu(ecd(^T}Q3svj$^$r(66PhDZ2!js{FKG7izA6=Wj?>k;YxBGgstj+(*sqDGlaju&SEh4?+&8qe2m6ONl?orUJIoEI>!<iqJLZIgBBe>f_T^M(P(=9z8WOJRyPlEbThkW})9up3>0+BQenOh&76BlD#6@@%c>%HOif*kdgs|5u#qzWut!686AO{t;pS?-qw8eQY_LG90O?`-$gNP0OSJRhCz1eN7ffEnLv<Hd3mLB^-+0H7u6M+`)Gu!3j~mH<=25-|4nc{ZT$&AQ6cSVio%T<PpLa5LiwdVh>tJob&Qg)hk0?nhs)X~DkDNd$!mTlV+#+Bf1@c=QiKly|))P#G-K2kSDU`uHA%rHb&RGr+3;4Z+-k%@2p7<5IgAwVn!W+7C#gtLZA&bD04oU51`dvyJOvh^&u*S=)~E-w$+xDqAkM(3WNDdOlYSehhQ-==zxN<n=YLV(67cv2WYZ3=SEUA*3G22lzrv33ULr*@UY#v;3NHsob&3ysbK$TS-O?3!C8uy$Ck|vE|nWN>~64mR}oizInbg4i8X`r70#JT0&;j{vY!P$dR_QgdF?iU4C(LNF!P(25J_NApsK|I!K;Gu$Jnib_Z<-!uIcf{)-5v&mn46D8(w97#hBv(^H?kTC?zTk%M{;O*fEond)K)sR9QcZw$MzIV(wZM6J05&IzNEP4IZ;laHA!3v4mq5&tMDlmcJ0x({z@_?ZD3$VrI}5oM-JkbADy<lvy*F4P3==OF+2M`DsANmz{aC-f|k-Ju}^WbQ={^eXikm>_OKvx7<ytoF?b`ak>R4d$2Q-;*4W0511LTPZs@Ng?Mj@)p=5Sqb&+$*a>}FHg^d*Oz}iKdlnev!_GK*S${yYo%Uw<T`)L9RKeCva5Z@>*ea6QD$Yz5{LwtJamTcZqi%n8h<3q<uGMi_>SLi^GmG4nqtY~Kh7muL@6O-CL3^$2uoD7`Hu073=K`TV<LcMS@=_toyWa$n*0tP=N}=BevynD$NrHu3Fu03pZs`I6v?Ko4~G@@qN>#){&Xaoh$}{{=B)WVnZfo-_`8$-#KYcI?|FIp=EKEl^@_u$yMUy<w50Kc|2(!nuHpeXeQC+^<`$;*r7hd<pbB|@Y03Qh*7!~zzCr!o`}kyNE<g3m>i?^TlQ);A7r#X<2;Fh^ke_!7pw6VnjC2Nrp6<n|K~KMM>y1b{XX;4;&e57>NXO6>C2+rDzaY1Hp3E&xnthBkIhxsjP*V-j^dsQZU|ePrc|A4LGZWCE5<J{Ksp(^KJ|J#}ZA8~d*ydm9<_7vbOr4;ih^N=s)Avc%5H=%_dI<`fB*A3ezcEJl&ua!n2~;$=N<N*O)=!h`yi6i;%kWavC(~8JnHfId+5xUL<0d`ZG*hWgv%lZ;Bae+BQ8SPP6tJ{tjJ8!rH0Ev}OXz%UK(j*W9{GO&u)*P%PBV$none0tv3<TVoMBYe6M)+%qRSpV12EG9AOGu=>H3-}?Xgd0SqMA;lwl{K51OVQXA-(N#96Xp56X4!D094VeC(QJ5EkViAVc6(G+aZ?0(*nzVRRjPcVjsNDo_Jw60$UNkLTnm5Cf_Bs?1BQhE(oqu|dMB1sJozGPg@`bf9`SOAECLLJ$}cd_*o7tMMh89qTy7Vlo<ox8+z?K}@d1Nm-hUP0*~n-r@1l)8|i~JX`or<8N<Y?#LI03Vqm3X;;pz93RXvNRx*Ekgg|L7!B$rbSAx6FF?(h<w{RKM$};6*vlIVUA=YR6!=R!1#m>9-V<wK#iQkwOP^(T9NLF!o>mw4pr7Ew`v78t1E3x`o)1j{6HsMC@8vqjxT-|S@aKj}$FuS=5*c=OS^AYn^}Z!>(n31Z7j_o8yAAQ|{8!U3L3#U`7y4VMPZuX=RS7>~Nj;iwnemuIq^!)zo0HFsRdm3-p`QRA+sSy7yW`2u=tS>3EYRij7hA6PzeKYAqCpFyF}&T^%?HD;zWmx5iOn1L1JT&l0I5GgC-w7Ur!)qYdn&M5-wli%oDUf^h|XS@C!gQq`bWr{DnDQulm}>Mn7dnyceC+!o@hhECAjyLSMT)bUEfvQ)n1}<{RsSO%cMGY9D|m}UO&rk4UQS+=OzQTgA&*COoW-FQ{SqiITElZ>hDX~ot+)@Y>9VxMv2X0N;9Ks^pPt1sJ~hpRj{Bgcvcx2H!me!vF)nSFhIE}YfQV+xf3AS$+1K~aA$hkiqWK8HO)tfrp0>*S)s_O{-M7Ck+`;37TY_@Si0-8cYlFaMs&UhkEzz1`xV%KgR~Swp@Ztg<^@%FzPZE#_`eCO6(<pxufse3@>uwWO6ha(CB?5ZcgU0sZq2?wwf^{rJ^oR9JSgTF&L9^%BDMLOh$c#QD$&PAhzlm6jq1d4_^?80>NzxxajcKg-I!%T)p~odu*F|!XIjDSSN92;k7-s;^b39qy~?oR>lm)Bk7!J`Y9MjQT(!(*+;6v;uA#RSs;{mstuCXVdk3poKE**^#M9*!wzk(*O^u(i9`S6#R+8a&ftutCoQHegAt5=`viM;F-ss{y5(&({$H-QpA69>g)LGnifo0LQAw~DkCfbxV_{)ig#zAN5VI`q*MU%np?fXCNA;J&&BM;jM6_qj{O;UOj)&Bq>-N>i'

def ace_cpu_source_fixture():
    import base64,zlib
    return zlib.decompress(base64.b85decode(_ACE_CPU_DECODE_FIXTURE))


def cpu_decode_method(source):
    """Execute the actual trusted upstream method with typed CPU/GPU doubles."""
    import ast,gc,types,time
    tree=ast.parse(source.decode())
    node=next(n for c in tree.body if isinstance(c,ast.ClassDef) for n in c.body
              if isinstance(n,ast.FunctionDef) and n.name=='_decode_generate_music_pred_latents')
    code=ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[]))
    class Tensor:
        def __init__(self,device='cuda:0',dtype='bfloat16'):self.device=device;self.dtype=dtype
        def detach(self):return self
        def cpu(self):return Tensor('cpu',self.dtype)
        def transpose(self,*a):return self
        def contiguous(self):return self
        def to(self,*args,device=None,dtype=None):
            if args:
                if args[0] in ('bfloat16','float32'):dtype=args[0]
                else:device=args[0]
            return Tensor(device or self.device,dtype or self.dtype)
        def float(self):return Tensor(self.device,'float32')
        def abs(self):return self
        def amax(self,**kw):return self
        def __gt__(self,value):return False
    torch=types.SimpleNamespace(Tensor=Tensor,inference_mode=nullcontext,float32='float32',any=lambda value:False)
    context={'torch':torch,'gc':gc,'time':time,'os':types.SimpleNamespace(environ={}),
             'logger':types.SimpleNamespace(info=lambda *a:None,debug=lambda *a:None,warning=lambda *a:None),
             'get_effective_free_vram_gb':lambda:.1}
    # The method annotations reference Optional/Callable/Dict/Any/Tuple.
    import typing
    context.update(vars(typing));exec(compile(code,'pinned_ace_decode_fixture.py','exec'),context)
    return context['_decode_generate_music_pred_latents'],context,Tensor


@pytest.mark.parametrize('manual',[False,True])
@pytest.mark.parametrize('decode_error',[False,True])
def test_cpu_vae_patch_uses_cpu_float32_then_restores_original_device_and_dtype(manual,decode_error):
    import types
    source=music_backend._cpu_vae_compatibility_source(ace_cpu_source_fixture())
    method,context,Tensor=cpu_decode_method(source)
    if manual:context['os'].environ['ACESTEP_VAE_ON_CPU']='1'
    observed=[]
    class Vae:
        def __init__(self):self.parameter=Tensor()
        @property
        def dtype(self):return self.parameter.dtype
        def parameters(self):return iter([self.parameter])
        def cpu(self):self.parameter=self.parameter.cpu();return self
        def to(self,*args,**kw):self.parameter=self.parameter.to(*args,**kw);return self
        def decode(self,latents):
            observed.append((self.parameter.device,self.parameter.dtype,latents.device,latents.dtype))
            assert observed[-1]==('cpu','float32','cpu','float32')
            if decode_error:raise RuntimeError('actual decode failure')
            return types.SimpleNamespace(sample=Tensor('cpu','float32'))
    vae=Vae();host=types.SimpleNamespace(vae=vae,use_mlx_vae=False,mlx_vae=None,device='cuda',
        current_offload_cost=0,_empty_cache=lambda:None,_memory_allocated=lambda:0,_max_memory_allocated=lambda:0,
        _load_model_context=lambda *a:nullcontext(),_get_vae_dtype=lambda device:'float32' if device=='cpu' else 'bfloat16')
    arguments=dict(pred_latents=Tensor(),progress=None,use_tiled_decode=False,time_costs={'total_time_cost':0})
    if decode_error:
        with pytest.raises(RuntimeError,match='actual decode failure'):method(host,**arguments)
    else:
        wave,latents,cost=method(host,**arguments)
        assert wave.dtype=='float32' and latents.device=='cpu'
    assert observed and vae.parameter.device=='cuda:0' and vae.parameter.dtype=='bfloat16'


def test_cpu_vae_patch_leaves_gpu_decode_and_dtype_policy_unchanged():
    import types
    method,context,Tensor=cpu_decode_method(music_backend._cpu_vae_compatibility_source(ace_cpu_source_fixture()))
    context['get_effective_free_vram_gb']=lambda:4.0
    class Vae:
        dtype='bfloat16'
        def decode(self,latents):
            assert latents.device=='cuda:0' and latents.dtype=='bfloat16'
            return types.SimpleNamespace(sample=Tensor('cuda:0','float32'))
    host=types.SimpleNamespace(vae=Vae(),use_mlx_vae=False,mlx_vae=None,device='cuda',current_offload_cost=0,
        _empty_cache=lambda:None,_memory_allocated=lambda:0,_max_memory_allocated=lambda:0,
        _load_model_context=lambda *a:nullcontext(),_get_vae_dtype=lambda *a:(_ for _ in ()).throw(AssertionError('No CPU transition')))
    wave,_,_=method(host,pred_latents=Tensor(),progress=None,use_tiled_decode=False,time_costs={'total_time_cost':0})
    assert wave.device=='cuda:0'


@pytest.mark.parametrize('newline',[b'\n',b'\r\n'])
def test_cpu_vae_patch_exact_pin_guard_preserves_supported_checkout_newlines(newline):
    import hashlib
    source=ace_cpu_source_fixture().replace(b'\n',newline)
    result=music_backend._cpu_vae_compatibility_source(source)
    assert result!=source and (result.count(b'\r\n')>0)==(newline==b'\r\n')
    with pytest.raises(ValueError,match='differs'):music_backend._cpu_vae_compatibility_source(source+b'# user change')


def test_cpu_vae_preparation_preserves_engine_and_publishes_exclusive_backup_candidate(tmp_path,monkeypatch):
    root=tmp_path/'app';engine=root/'Media/music/ACE-Step-1.5';engine.mkdir(parents=True);(root/'Recovery').mkdir()
    source=engine/music_backend._ACE_VAE_DECODE_RELATIVE;source.parent.mkdir(parents=True);original=ace_cpu_source_fixture();source.write_bytes(original)
    monkeypatch.setattr(music_backend,'ROOT',root);monkeypatch.setattr(music_backend,'ENGINE',engine)
    monkeypatch.setattr(music_backend,'_tool',lambda name:'git')
    with patch.object(music_backend.subprocess,'run',return_value=subprocess.CompletedProcess([],0,music_backend.REVISION+'\n','')):
        target=root/'Recovery/candidate';result=music_backend.prepare_cpu_vae_compatibility_patch(target)
        assert result['activated'] is False and source.read_bytes()==original
        assert (target/'original.py').read_bytes()==original
        assert (target/'candidate.py').read_bytes()==music_backend._cpu_vae_compatibility_source(original)
        assert json.loads((target/'receipt.json').read_text())['candidate_sha256']==result['candidate_sha256']
        with pytest.raises(FileExistsError):music_backend.prepare_cpu_vae_compatibility_patch(target)
        assert source.read_bytes()==original


@pytest.mark.parametrize('failure',['revision','source'])
def test_cpu_vae_preparation_declines_unknown_source_without_overwrite(tmp_path,monkeypatch,failure):
    root=tmp_path/'app';engine=root/'Media/music/ACE-Step-1.5';engine.mkdir(parents=True);(root/'Recovery').mkdir()
    source=engine/music_backend._ACE_VAE_DECODE_RELATIVE;source.parent.mkdir(parents=True)
    raw=ace_cpu_source_fixture()+(b'# local change' if failure=='source' else b'');source.write_bytes(raw)
    monkeypatch.setattr(music_backend,'ROOT',root);monkeypatch.setattr(music_backend,'ENGINE',engine);monkeypatch.setattr(music_backend,'_tool',lambda name:'git')
    revision='unknown' if failure=='revision' else music_backend.REVISION
    with patch.object(music_backend.subprocess,'run',return_value=subprocess.CompletedProcess([],0,revision+'\n','')):
        with pytest.raises(ValueError):music_backend.prepare_cpu_vae_compatibility_patch(root/'Recovery/candidate')
    assert source.read_bytes()==raw and not (root/'Recovery/candidate').exists()

@pytest.mark.parametrize('failure',['latent_conversion','partial_model_conversion'])
def test_cpu_vae_patch_restores_after_handoff_conversion_failure(monkeypatch,failure):
    import types
    method,context,Tensor=cpu_decode_method(music_backend._cpu_vae_compatibility_source(ace_cpu_source_fixture()))
    actual_to=Tensor.to
    def latent_to(self,*args,**kwargs):
        if kwargs.get('device')=='cpu' and kwargs.get('dtype')=='float32':
            raise RuntimeError('latent conversion failed')
        return actual_to(self,*args,**kwargs)
    monkeypatch.setattr(Tensor,'to',latent_to)
    transitions=[]
    class Vae:
        def __init__(self):self.parameter=Tensor()
        @property
        def dtype(self):return self.parameter.dtype
        def parameters(self):return iter([self.parameter])
        def to(self,*args,device=None,dtype=None):
            device=device or args[0];dtype=dtype or self.parameter.dtype
            self.parameter=Tensor(device,dtype);transitions.append((device,dtype))
            if device=='cpu' and failure=='partial_model_conversion':
                raise RuntimeError('model conversion partly mutated then failed')
            return self
        def decode(self,*args):raise AssertionError('Decode must not run after handoff failure')
    vae=Vae();host=types.SimpleNamespace(vae=vae,use_mlx_vae=False,mlx_vae=None,device='cuda',
        current_offload_cost=0,_empty_cache=lambda:None,_memory_allocated=lambda:0,_max_memory_allocated=lambda:0,
        _load_model_context=lambda *a:nullcontext(),_get_vae_dtype=lambda device:'float32' if device=='cpu' else 'bfloat16')
    with pytest.raises(RuntimeError,match='conversion'):
        method(host,pred_latents=Tensor(),progress=None,use_tiled_decode=False,time_costs={'total_time_cost':0})
    assert transitions==[('cpu','float32'),('cuda:0','bfloat16')]
    assert (vae.parameter.device,vae.parameter.dtype)==('cuda:0','bfloat16')


# Exact pinned source dependencies for process-local bootstrap, no live engine.
_ACE_PROCESS_SOURCE_FIXTURES = {'acestep/core/generation/handler/memory_utils.py': 'c-qZbZExE+68`RAL1?jnlylVBd1+xZ1=7UH9&WP<lC;>pqEKmxwt3ZyPo$h=i|%jV8B#BlL_5hYaJYPNByxtsXP$W><v7kIOS1ft&@>_+Z%!`BlEo{QlZ44-76~%Xa<YWM7~VxJ%#u|mxa3*tJB~9LaCpuo$;4ouX9<yyE1oV$`Sxms6KPyelUW`v^~^Y16nRn2gr^0|2YQHxOh~r!7po!&vvkgDh3<gBUsyym5XA~#iC{L5Gb%?%Lp8QwGKjbgzz`OowqQl&vKeFZISVCU;~-<fI;Tmnm^I^zIGfQpXqMJPTm*R*CIMYj9@AOOh68Uf7({GN0vJ;kMQqJO=IWmb$!7n7l{~Whj?A(wo+v$l*$tCLo|5||01TK?q6G+#SStCP!@Hc#SuOxA@X8}O{cv_tPEwdFd*-#6AircO)9Udr*ZR+CESP#J^T*z(a)e)6EM#6<?Rh=|M~;t)gL~48v4@bY0Q@@`n&x?y56MjdwkFlnM;aIUf3LR^7d#b`rZqA{0_%B-Z{?=MD(2F4CeDzIyia?djpa4M2D|}JBlchenWwUgF#v3$N0cmbDdsF9vqu~FCd3xoP2l>Q-D@1<my2ol5C-08jr3i!qPYOGjyF4}o(ikz*&TN^%DP6C(=A4sBY(r)D=Nem+<HEj6oU7j%cZMKpE!oD7Az*0Qt4VbGy2?MQ<~oxX$NJ{dKe7C7&bt3GW;NUEH3#2Pisc0Cju!!Ql4UpOLW>4{C9k+v*)6qc~s_5;SZGf8C$UwQfZcwWhNww&R|sQ-X$ke=6qI2p!1h6dV62K_|?K%1%?6S8Np+Qs0tR(hlCap&n6{Z?pP|oA?<Y24u`wEV3>DOEr~R!(_xHT0p+shzkv@bboDYcj$I|~)0uER@|KL)?h#P4HZ#C9Djp$&CxHeD60EXV8CTVWPbzJHw%I8&Nn@f_5Cj#Fzj$9-UM1j`ojt;==z;-Sze8+5ZSp}EW^>oMJq@l;E~dflKmR>-yuL`70n;rqKmB&A1aCV+Dzd7jkOYmEwm}A(ZX>Z{%=gGE;*R#l2Rl3aJvO@yormP=w*04NPB$2h)iBTpws&oZHFYsriI@8AJ=E)6(10cS^7^)l4S35FDj9KeY#b5@0?2O(eIU?NH173Ut^`DvR8ZKNX55fCV3DJfM{C4~_b_Vc4nTKlyVF6V$B<qz1Z}=umuq*8;hw4b0et#v)OivA%CK0>fMSTOZx5DMqHkn+rTz&wH<CMK&wqWWUH<W88k|i}ug<2y<;kzX=}#Yi`Im|Zu<39#?(l*ZG7GTpi-4xfBK@j}+t7lWL<c{fF0%}Z3dK|@SiYvnDO{6iB}6G9iv2T+^)54lx@1m^5ZRjLIn-wzl`LKJ9GXwC&2>&rr?+?0>%iDjvmK{}Vx{F7tXN~H!D?7-DlxA&3v}v<@5o+Xhv5Y^%SM__6{==U{T8XHaVu%F(T9T;b8u?e;9!e@anxQa>639&BIxD4q$XvWNeeOC)7~AEgEw1oN_gsy4)>3S&33)*3daXeD}<oksL+@P#BWVAVeB^e2CUCXGU}Y6K;C@*a4jvaxy(zGPP67-YfK-rK<lhnxO3;zmJt6;rj@Bmooa6;<XfqF;*aOwp(^+5VYRk$kiWMv>)K%7tZH=hgsS~F2hUYCenQpw@aQ?58I9jqr)|;H<W{lFiZ}|gIhNBXxTos?yb!L6Cbjn3xHA@F9;{A|a6Bx<%iJ0fP@|5Gr`I3U!M$oy>72<IXH}@=Rg$?nKmYma<m@>qwcLF*=*QVT%b|io59c}~XxAMHZ7BXeGSOD}zs^^xrD)R0{&*uRn!|Kw09)$+>s(=t`i<oKYeE0qz~2h!3p*(j|0b$5M0f-XauyYU&y31&={5}*9-QhYHK?-P1gm{BpiVeJROD2d(rtdhKggeEej!*8CleL2FsOE?N3BiL31r+|ZJ=R;KH$`_BU5FT5y=-z*=>;xske~PtRdLUY06m@U1qGAdX4A<Iyl&<Zw<&TOH~0~u}m|pEpFWK9ow(p`f+UD3U2E?BxgA`@u;~L@i7@U)&_V10iLtbxealp&Ccppgjr{(=ypCecLbkF8<=CH^3YiI_*?V;cQnD-q8QC1F;BQ8-zrnTlU*Xd@|C)gjUH2)@K7@=T(a=1U7fcrYHe=r<*~n-!+Pm!dq&ts@-l7%!9p-Y#jM*7X%9Wzex<5`eJ-^4`mXF~&u<#46~HWLvWlx#%ReOlz-sTptRXy(4&mVCwyfDzD)2n1er=~-eYfKzP|)M@*sJ15z1U27&2M$v4YoK=FKn1J)yGsw0G>;b0G|jb$f(on^SiBBnbH;3eS<L(%X_CtRG0E4FZy@%t|8wR{ek@YGjR{zlQVwj5ru;QxS&S;@p|t^c#%UXN0KR=1v-Xk$8<rcR&8BBKE#1z*>~H$JCJ4l*B!Io!vXfy?fG406O;jRdVWFN@dLJB_jnbERf`F4qa}DVsO<>wJPaPkoLhqhu2ip|VIQ=$O$_Lk57I!s;Hl>4$UkffTy^3Oz)qbIF;dFV-R+5+M77C6dg>JXG9>~AcwO30ik)P5mBx=opD|K>hAxJ?M3qX+Vn$AeE_c)>xwTOrwNKHwuX?)}s-vMg=iB!5sts}b`n{v<t=UWb9URW<^4|j^q^a@?05yj^|Hrqs4c|-73rnGxJ8sVYTi|E4h?sT7^T~}J+^%;28DO_X22Z5w`i^N$Sr8%J%j8sj+^M@RA@i!|QvFo<fdoZbeboh%`l?CAPJO)uu@1{rjSME#3pgK1^J9;yWNKPaw@Bqbcvop}mb%S;OF<{2sIMZOZ-}lX>-+$bKyI#16NF)|PkDNIwWs?f2ev^GpmEM<1V=k2)ptV042w2>N9C%zwm-&iUZ9p1u^i&Ys-qq(cp04C{dPUA4{n~n=`hr(B+4+S`Z887)XR7FZ??8RppByU#I0?mX|QodcLEsG@igSv2WWODgXgbeRkE=?et{w3c0)>ddpEr`Lk52Wu1!mA', 'acestep/core/generation/handler/init_service_offload_context.py': 'c-p;IO^@3)5WVYH43vw7^{P4aWMG3d3p5Cj%?34kDhh#?Mm7<N6iC`wW3m6eLsH+CZMi9`gJDs_dGqxi8HVAb6b4@mluWQKD#)b*gJmTZQ%5Leh6NOoWn653k$GPKjl(bu0&dL=7+zwKD_KJ7ofUio^|yMZ_8xe%LT>kJ-<Z&w`xK-F(;B`AZq}#{JjG{^SBLn-U$|HWKu_4leeXSIy-1rdVS_t%;L_*x`2A04K3H3yfpzwA6r)|sFo(q2CeCkCn=CXI`O;%0f|a-eZB*ogsssCK5m+6Mk>tvz;_JG@rXavX*2c48=?`n_VibTc+_>->uRI4K4OsBF-jf)aq}9Gdw$G7ckquT9n!OT1-%}SnJUl??UPBko2}Z`vB#zJR0k&wEG3ul$OZX-QMuR;KGH~sHti{l)eT+JuF50JJij@Q@Y!{rP*e#(!r58~&xrs2HQS5sVn`7mZri{XA9Q*izjJe3=A`kzXn1jFJNgjv>@Y*H)W;^)m>gVC!U{O(UTpJW73Zt`lZcm~x-IMhJZOOd{pXd^LPMa5-%5{$gGL4BoQV3;IVky;eaefz-YsYl2qoL*niWHNA*&M>?e9c<a;TG!H#A<BhqL)+Dy-%lwFDX_A)<#xesNc+RDb9_F);Ch()?w}s^63y?$5UD-8zecvrZ@>fCw95q^tv-=3cq?azBYz`tk)}e?SNPD`}_@_KJ|xE8>Wcu+{%Je^w|C^q6^Y-U^rqCtt6ReSueGAbu@do&3)^obAp8)mH*#UX`V#h#E<`s551YN%74-$xbL+u7{F5@6d@#yq^D9FxO?b4b<W?x?+OuQk-3A20@HM6X+t-GEdtAlc~luEdo*I7$z)-LJe7Nz#tgXVoJPTIN@PdV1I(txpwgH4L7`oy2L=AxM!t%F&EGVHBll-0bpOGxx!4<4E$PTF3|sX+QG<G0?^^n`D7_w^9Dzeg$+=)fF*2E}DHn@)sgTd@&F`d`c2+JZ%m8A<|7(&nS9WGG-yQnPNm+8AMt6u)ZJCeK>!y9$E89%sUL|ke;SlN_{I=aH)Hb-{NM{e-sd)*Wa4SdJl%;eHy@T&Zv!l!A=STYgyoMdJie`_OBY$I#50atw|3Iv4N_I3@N)eNyP0RfC%g9U__}M9*Py{=~RtNgoO-jN4BL|)<{{V<y5)=', 'acestep/api/server_cli.py': 'c-o~`Yi}wy5dF@tm~KB@kl1ZfsShD-CGu#4F0klAR8<JA957&IvA4FDlvMY>-x=?N1rkKsM1t(YGv}O{Id(kHyY2VjNhxcrW+|+Bl5%ZesWc=klk>HZE3j)0-BAw=*ITY3)oRX-2|UkhG?rRzAdZ)r%`}f=5SvtK3rtF7nH5T!Mx(r^S1Hqmm)FYVtL-kOEXCbhmL%*c;cbXgTF;UyK(-ZgrDdbhSnwt2OvWq~agHk9Fd>@_z#lHRF;Yr8Rjlb0>9vVxGAqB_s|ab^sS-^SVJy8mn?VReC3(Bi0{=JnXU8o-&LiRgq#Ld_ZB)flN}-k9aEY`KjTQ@zkC<G5v1|`LXt*d{T$sl@#CwCEoJqO^PAUzV;R{y|q|CC!nxI;D2p;%9X(Hyr=?ugKnr`va4lFm;v;w#*Vt`d;6<w5|<d(xLQioN=0tB29ImcZ$k=$GhmpJsgxA*?o^0d|>UaJ--V2ee}iU7?Dz32ODWvth(?i36|Wr7vAs6mrrk0<w|7(>So(Rkvu!MpzPm*5J2uf5iOEj4cF-3Sz%qC&F>2Cl=7!vN}8g=rNKF2hWXSi_9H%R?dUDGR*We22*>`r?@UdUb^)-e%z%{(Tk{f>=<9j}gF;@t^tbm;(*SrIq-6{};lip*X=#J%G_&$FdBKLs~I+K5&K%#`_qDg?f>RVhi7>OwajQF7ve#*y!QZTW6~k8qJc?3h*{+!4orAsk=h3%9~vz!Cmy{upf2r;&Jr&<@UZ>eImw4^Cfx#&Y;T$C2YuHcSs>s>C$RL4Pl$ATZi}od4v?!Pm)bd7dFp_)mVi#ZFR=q7`)Pm_EdYr-X!k#2gvbvmYCCe_b`=UiFoCJuL0k331SI<j2>abH%jk77!cBA%O!df*<*9Vb}(mXO%O!`G$n5j{T96s(<pf2&Q`aA!7-GOz#kaymT5sQAACrpWaB$|{gm1Df1QT@ARa___x*U(ojgz@oHhZfl2;!~?TX*z!76COCoHl-+w^~WcqK4b!Ou6J_8xsWB?oyv##1WS5C6XxhLNkmyR9&O7HL71c+!^6Y`*<vDVE*!GQ5Z+q4Fl|b?4K2I2~vXOQF%VHUB$9Q7&(a0@I~EK^r!1YlUR)i$pU``Uq(!Y~?#3wlLia=zM{8w%@C<2Z}V)6HA=sJ7%@&FjP7GaX$R+Z1CoT@4~`Q&SyP#p(^`PyedelX-isqkA_N<R^D{tO8>qU=Bsxk4K-DaCUYKTWgrC4bG>#1$eYrhF?Q#MnKKbldP=@;a7l$+xVqQ8?Fde?iCu@qG|uNS9S&`!+d+gA)ecbR5|)!)Ds~XyL@%jg!hRlFIb#>9RZVvX2DR}TMiVz;`E=EXkfttop_7>_@qQ1z0i7<o!$}8A;TJZJFJ=4(XBl)Neo^|F{!th_j$N*6x3)ax;?cb3L50x;`@?#NrD@G`=)^{r>&7sktD3pcLC77Z+(WvmJ=C1YgwBxmcMEXgbvgUr0DLIy;{', 'acestep/api_server.py': 'c-plaYjfK+@;iS8M)?q_BQti~v^~x6?o?Yz)%bOm-1IJ!!O$dRGoeV858Jv-{`>7N01_lYIj_@9Y#G>H0E>MuG8hbA`gt+En6q4DcOqk}G-K1F+1{cM8}`S6`@(gcSrTs2C@J!Zv&SyZ7fbdm6S44f!3#hCz^cztCj3IMb(u#2yAg@V{31#dhENsiU!};lJQH~t7d7gyei7U<nj>%~0rCnhAlf|hlQ3QL++S~E!PqOgh+uMDExqwGe=Q1;<?Jr<*~!UiMF!5d_6r^*o6=bQC5<wm!9;TFCxKw`Hj9G%S>SKz2sNEovn6BC?hc-<(@?}YGe0L$4pi>^DE6;mdlJIL@yjqu8z}5Jy-(uQ51E`>MX{(bZiOEgx2*N~O^yb)BKY7qXXncqUd%k%TZ?rH`~xnQf)4XOy=HllW`gbgon>Xh%DX5?vxMD8K;S;jJ^+ur1J@Z029C4JfP<W`%A(8!=PX)p(hRwhqy=4M?l@}fCQh&Q_xE|4=<jK+zvbKNYZ0x5{$6Hr99_F`L%EO(25C}=hXO{c1@mnZsNuDr_%|Yxpv86*B{v#unrz4HI0}j}@qNtB>8ku#0>|l1nUxwC{z5WNk!>dqGLc}yv=GT%4daJg4msi>5SxO{X=avXX*OZ(NwowE#YQBdNP;a<Gf<hw&eB96H0)PQSag%X4Jh`mK;8UJq?c6MC+GY?<e+vg%)3SAM+t{-92GMHfm9enS}#hXBJ$(tzo3E$32fm&xDaD!)FlRDkh@Y26czq^dcDAbE--M4qtur*bd&(?IOfJw7cWao(9c{H<woPd9j>Aq6ec_f0pwjoX?b`MS6l%}t&R>eYdD}~pV3O>8$aOyjS^OGwqi{i#L5|d9jCzuWT0WOjE%IXd#H#95tNAA-Vf3MdEf}Nb01_a;LNcI%st2fV{;q~R2XZWXj4&3NNH#aO#n7KDL1r}Laa9&ZA}j9#<+b-ixeV@B*%-CCXLrj4hg?^Rw9?15RLaHOK-rg=d?`Vg8?Jt-q{8+nDcc6s%J;;&RR7A4wQj_DqiD0En=fi0d7Kpcj-ve*}iH5=V}hUSUyJcyJ-ydDga0ixRb_>fjQyVX&NgY%ReMnZbYF@k$|rwfaZ_{i|{t`A}iM-0Uai|)|dob`y0CMQVK-<r<^8(%|np~S+tQ-h!Fw7_RN`x$R#|liBOGMZ|b%v;7bW^s=*PkFJu}%h^?d#oQqal>sXO~5D7=h5N?1|1Rv46k{Qr&^2D&q;<j}kxql}>;>g+;DQOBF$TF^W0un{?S(79L%lK#RO+_7QTRQ$Z#kl8E9`bXjP=qA!A!{B{5xJl#c?dkx3eHy0))gzO!5RiGy$u0F)^lkqbt~cxhG~s0_#5=C9ODWpuo~K&kjwf-x{d;VpGCk+9B;z6XC;zUQ}ES!nq8-U7B-MNvl<EXxF4rEDN#@i(#(fwH;&ix;u=_%c`LJ|PKP>9Y*~of@KzueqioFK8qGzNL#YC*Kw$X)o6XJOPNQTcGLh6V2?WMgVDSIIO*9jYJs;0rPOnau{CK|P^RvbB^6F?gKR=U9TTU0R&Cx}NC7tDm${<Qr;FJSMVk&8n*@7HW34k6NIZsrOCnLfF+)5}`6j5RPUP2f@g!q>xR8FEDa1PO20302$Jf(Sz<eunE3QoS&2d8WRV{Qn6*iGhRps#`|5xULem60nUTyY2XId|2nt`PwM3(_i-yDp<R<gHo9fm2AA0Dkp7Mi5z=7Q+!{0$}@P5FC^laIzq84{>XnyJ(4A|2oItLrxQ%j|ez}&<;`u4HQNhx|w17Z<FQ-Bn>#?ea{cjJJNFX-Dx|QvjvLg>)F5g#pUef{7+!Z0Ir7P-S9g|i1|PR&HlWYU(Wbqc65Grya2$T*$?dbkMQHCefa}&fS*|ia^R3yFXyMT^Q)zSaro0dK^-s<jm-J|{8zqMo&(XNliBp_>H;)jIlKI0dV;VL^aX=J2P0{=2Qmb45BL&F+bM=av4sFUjVbxd4T`7JKl${Px!qrW`caaEJ>&97F-TD_3L(*v4v2CA0uMi;9Ar3-*|m^siO_LQPPta+<?Qrgb~#;MUCw~`zWd7ybN1-vE510Ip5WYbw=uOmzu*^y@;67x?R^IE2MGQKZdwe%Q~DE`?*(AYCU|Znv%f!PLgr>VF?$QL{Z6G|B<q+U;@}wZ-$}8DZ1)W;!1MwP7XYHDvq^}?U0Mkj(_%yPKRP-EA0WnHLWtvf6n6)s_{$ijEy6JUHpXQD1cqopCmQ14F8%;5{ed0q@9&RHDi4t90VJMG4uAT;DA(*k)>e*@Cn#5!vb3_G;qR|;i}~rr35e(U@eDL-ihnG?HC!6@=zSU`LyIjNBUfl!`2fD>5q>3qHyAlbS0MMN--9mJA)b?08H}`mQmBVHu?8GUk3y?Y(88kOXzU235epS>n<81u<Ox-QK*1DEDV8!xtDKWwkAb~~9jDp0lG19*V{hLzMmPY&XfLay6*IOEH{9GhgNReGK8C6=zW`H|w3%yepyn$7!Nq6{xv5+-osyzs&_XOOx%gwKsE#I;8dUWg{h>-$3UT33lNht7*;6nK38*2ML+@!>toD9+I>MM})kMY*G1;RkmW#JKT#Y&~`WUW^K*=E0P^wlmqhFDXK}B<+O@<1-8ob3)>w*G=ckHDPI}TZqQu(O5b#lfZ^=Up$*hle6vq~8vy&p0F7n#^=5TFx5w64MznxnFVx_@<f{CK*YPOKsc{Maqwo?Y3#3+14MaC)sNJ5HsXm;kHr)&u5&Og6O90Fz-3TFRz!xmceg&g2D4F@%Kd11=|p*el12@bA(H7`aJ<+r8_wL}##N6sP5EDBK&Dy@|jY-skKjy__;#dmB|ch++%=*s^Dtj<0M}3>3huAcCW<_g-JS(@_A^Y$0d6rmPlHSdA2{rJ*J`Xee@MP-DS<HMDqu>&%Cm&ZsHi#`Y_aF-t+tvj{GgZmTA1ztUw^3dg2U^-@L!_8NTWo>+nLC?-@yhNuY4BgD+OYr1Kqi~-RK6fJjauK77?jzwx<6l<K<<qA@aG350Hgg+U=JZxcumOAKA7M90Y?{+2l$f>{MrI<vNz^c3itcjFVRR*5uOf$o1ysFU-mGVEq1cSX6TZVNJh_RzURx5|(9pD5}mL^!#VRwEOVbU>D2{jc*<kf?W;8_RLquF9PyWl_*!(DK66f?D}EJO8M?crZnvnz@v7xVwjh$Y3)LbL&fJ``fL!AKH+bAI`HcFB*<ug;c_QIN0;q;LTE$b$vkC|I>u7gwMb5U#GV4j;<h&Clk``SgTO|9HhI@~vWBDplClX@TNOFWhRcIQwRPc6|P(f2TDJArmeklW9Ulz)FZL^pshQPVJT=gk~<L>=fiSpfuQw2r#ZkUqEUL#!08}luc$LhReSb?CSDF8y}^yn6A<y4GmSU11C&i(rv1F<mgI=11uIzo;`zj1+0>FHmB*wsgnm5p*A~a>&N}81Y0qbRwDOTVD=5Gqlm2rRm<!j2<d$^DSjGAF8|ZkE+VPNak>QE2LM^#Oti8Y2he3)Z&&kR*f=jx^5EaNF-V&0(a+`tb>#Tkf5?Hd*HDfopnrG6@5#e>wOBPl)D}5{VzE01B}l^D>yiB?a@#?89hYHUtlMYetHj&Ugle1#vS(E>;X#~x&GJ$c7RyT}Y+YHZ;c7Y2bt_Nbpsr*&7J3GhG1{i}yi4qP8|JVJb6CO1o)<}|Ny{9z!5-RS4~-xb`%9^L3_avjK()woqwO?AF^NL5Ib%?+zb4kYaVusrSU52%e6T*qy-WApU}OV{wjc{rRsT%Zv4)-CS~j>qs}-dQ+a)0ljJn$)57&U~P5&)+%i8GcZdGZ%lya})!`KQ4jP~H4eNtYDCx6*Ndaa2?xIOsZL#4%CuRZVxTFqk5GG<4vn@3)!k<{f9N(Dw3V1~phdhj|WxSfdNVFQV9Wd*IJIXi(bwS^|0!4+%Ut&>37OYvTRjowa0b^yIrFR=aTX~=oIJ9Ty{RhQ1|w(V?Kt>&U<?>4qSjY2*87E9&6{b^M45vNt!x8I%FBtRrv+n`e-5Gx;LEsP3v7V6M@I~y785B1#)?A)`oI&6s6`aW*mUit64%vKmm1UjQVh6eH-0H-2G7h=>j{r<flSzkSL&FN>E(w%1T5i{Crf_vX95L+I3&Cs=zme(0bsCTkU=Sz)>)wdc#(VFQJ=zVlSL>_sPVIkCgfMF7{J<s@BuklqL<D0xizx<D^u~*M)8n938aI7chk~*~WRnD+h8KnirUewFJarNYfo>Y342P&PcX17B2Dx;zqq@e4bnc+9V471>wA!-xKes1p(zS?Q%7Fl)Y{XI5Yim~fi&}U?XI_SE6B-!zd?^X)WHHqie=|eqz*y4hbc6jC=y<mqX*x|Qjk6&WczAZx96&a2Cn+MxS#9=kELJJkQKsxWYx^7Irqt#m10Wh`DGykwSCY959`is@xfSgeYoC+DkA}V6x4XS6`Fg6(DBd8pMl)-_!4}a=%hNr(MQnojm^$|NlZ8ZR&Ro?jOx<&JamuGwMwe!)Jx8pfjb%kcX*=x3+Yp5@g|Id~Eub2B>GOxNby5EK!A<xqNsS#AurGa2+>eL90V7o8$mM_4(&WkW(o(i*G&#N*O!_ka<tVUHK%NsQlQnCyIZDkHaV!Ds;Jp0SMimlyueqR6Mzi&z%z#nK_q=?4=aq#T#Ev#t4>W1fmA2*PyXBhUCz}9WN_S1|S^sx}_i$XI>h%hk#n^Rc>+dCO!t5tKxGx{{`I&Sv~pZD17z9x3_F`xIyGd~m8@}$q({ob!lq4Q?Zd+a%51KW5Z>Fszo*~z(W33s#S3`88qk)FrwU#XTBKGL_eU5`%YV5q?H(*Gcm|GlXG-$p8sqVB1q<HOd=|6$k8!J61eB}>XVJy+la%lTlUUNhrv9QFAheT>g8'}


def loaded_ace_fixture_function(source,module,name,class_name=None):
    """Model a real import's FULL-module code without executing its imports."""
    import types
    code=compile(source,str(module.__file__),'exec',dont_inherit=True)
    if class_name:
        code=next(item for item in code.co_consts if isinstance(item,types.CodeType) and item.co_name==class_name)
    code=next(item for item in code.co_consts if isinstance(item,types.CodeType) and item.co_name==name)
    defaults=(None,) if name in ('_get_vae_dtype','run_api_server_main') else None
    return types.FunctionType(code,module.__dict__,name,defaults)


def process_engine_fixture(tmp_path,monkeypatch,*,already_patched=False):
    import base64,zlib,types,typing,argparse,importlib
    root=tmp_path/'app';engine=root/'Media/music/ACE-Step-1.5';engine.mkdir(parents=True)
    source=ace_cpu_source_fixture()
    if already_patched:source=music_backend._cpu_vae_compatibility_source(source)
    sources={music_backend._ACE_VAE_DECODE_RELATIVE:source,
             **{k:zlib.decompress(base64.b85decode(v)) for k,v in _ACE_PROCESS_SOURCE_FIXTURES.items()}}
    for relative,raw in sources.items():
        path=engine/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    monkeypatch.setattr(music_backend,'ROOT',root);monkeypatch.setattr(music_backend,'ENGINE',engine)
    monkeypatch.setattr(music_backend,'_tool',lambda name:'git')
    monkeypatch.setattr(music_backend.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess([],0,music_backend.REVISION+'\n',''))
    _,context,Tensor=cpu_decode_method(source);context['torch'].dtype=str
    modules={};observed=[]
    def module(name,relative,globals=None):
        result=types.ModuleType(name);result.__file__=str(engine/relative)
        if globals:result.__dict__.update(globals)
        result.__dict__['__name__']=name;result.__dict__['__file__']=str(engine/relative)
        modules[name]=result;return result
    decode=module('acestep.core.generation.handler.generate_music_decode',music_backend._ACE_VAE_DECODE_RELATIVE,context)
    decode.GenerateMusicDecodeMixin=type('GenerateMusicDecodeMixin',(),{})
    method=loaded_ace_fixture_function(source,decode,'_decode_generate_music_pred_latents','GenerateMusicDecodeMixin')
    decode.GenerateMusicDecodeMixin._decode_generate_music_pred_latents=method
    policy_relative='acestep/core/generation/handler/memory_utils.py'
    policy=module('acestep.core.generation.handler.memory_utils',policy_relative,context)
    policy._is_cuda_device=lambda value:str(value).startswith('cuda')
    policy.MemoryUtilsMixin=type('MemoryUtilsMixin',(),{})
    policy.MemoryUtilsMixin._get_vae_dtype=loaded_ace_fixture_function(sources[policy_relative],policy,'_get_vae_dtype','MemoryUtilsMixin')
    def uvicorn_run(*args,**kw):
        assert args==('acestep.api_server:app',) and kw['workers']==1 and kw['reload'] is False
        assert decode.GenerateMusicDecodeMixin._decode_generate_music_pred_latents.__globals__ is decode.__dict__
        class Vae:
            def __init__(self):self.parameter=Tensor()
            @property
            def dtype(self):return self.parameter.dtype
            def parameters(self):return iter([self.parameter])
            def to(self,*a,**k):self.parameter=self.parameter.to(*a,**k);return self
            def cpu(self):self.parameter=self.parameter.cpu();return self
            def decode(self,latents):
                assert self.parameter.dtype==latents.dtype=='float32'
                return types.SimpleNamespace(sample=Tensor('cpu','float32'))
        class Host(decode.GenerateMusicDecodeMixin,policy.MemoryUtilsMixin):pass
        host=Host();host.vae=Vae();host.use_mlx_vae=False;host.mlx_vae=None;host.device='cuda';host.current_offload_cost=0
        host._empty_cache=lambda:None;host._memory_allocated=lambda:0;host._max_memory_allocated=lambda:0
        host._load_model_context=lambda *a:nullcontext()
        host._decode_generate_music_pred_latents(pred_latents=Tensor(),progress=None,use_tiled_decode=False,time_costs={'total_time_cost':0})
        assert host.vae.parameter.dtype=='bfloat16' and host.vae.parameter.device=='cuda:0'
        observed.append({'argv':list(music_backend.sys.argv),'uvicorn':kw})
    cli_relative='acestep/api/server_cli.py'
    cli=module('acestep.api.server_cli',cli_relative,{'argparse':argparse,'os':types.SimpleNamespace(environ={},getenv=lambda key,default=None:default),
        'uvicorn':types.SimpleNamespace(run=uvicorn_run),**vars(typing)})
    cli.run_api_server_main=loaded_ace_fixture_function(sources[cli_relative],cli,'run_api_server_main')
    entry_relative='acestep/api_server.py'
    entry=module('acestep.api_server',entry_relative,{'run_api_server_main':cli.run_api_server_main,'_env_bool':lambda name,default:default})
    entry.main=loaded_ace_fixture_function(sources[entry_relative],entry,'main')
    calls=[]
    def imported(name):calls.append(name);return modules[name]
    monkeypatch.setattr(importlib,'import_module',imported)
    return engine,sources,modules,calls,observed


@pytest.mark.parametrize('already_patched',[False,True])
def test_process_cpu_wrapper_calls_exact_pinned_entrypoint_with_method_in_original_globals(tmp_path,monkeypatch,already_patched):
    engine,sources,modules,calls,observed=process_engine_fixture(tmp_path,monkeypatch,already_patched=already_patched)
    method=modules['acestep.core.generation.handler.generate_music_decode'].GenerateMusicDecodeMixin._decode_generate_music_pred_latents
    argv_before=list(music_backend.sys.argv)
    music_backend._run_engine(['--host','127.0.0.1','--port','8001'])
    assert len(observed)==1 and '--run-engine' not in observed[0]['argv']
    assert observed[0]['argv'][1:]==['--host','127.0.0.1','--port','8001']
    assert music_backend.sys.argv==argv_before
    assert all((engine/key).read_bytes()==raw for key,raw in sources.items())
    current=modules['acestep.core.generation.handler.generate_music_decode'].GenerateMusicDecodeMixin._decode_generate_music_pred_latents
    assert (current is method)==already_patched
    # A second invocation verifies and reuses the already-qualified in-process method.
    music_backend._run_engine([])
    assert len(observed)==2


@pytest.mark.parametrize('failure',['source','policy','origin','loaded_decode','loaded_policy','entry_binding','entry_defaults','revision'])
def test_process_cpu_wrapper_refuses_unknown_before_upstream_main(tmp_path,monkeypatch,failure):
    import types
    engine,sources,modules,calls,observed=process_engine_fixture(tmp_path,monkeypatch)
    decode=modules['acestep.core.generation.handler.generate_music_decode'];policy=modules['acestep.core.generation.handler.memory_utils']
    if failure=='source':(engine/music_backend._ACE_VAE_DECODE_RELATIVE).write_bytes(b'unknown source')
    if failure=='policy':(engine/'acestep/core/generation/handler/memory_utils.py').write_bytes(b'unknown policy')
    if failure=='origin':decode.__file__=str(tmp_path/'foreign.py')
    if failure=='loaded_decode':decode.GenerateMusicDecodeMixin._decode_generate_music_pred_latents=lambda *a,**k:None
    if failure=='loaded_policy':policy.MemoryUtilsMixin._get_vae_dtype=lambda *a:None
    if failure=='entry_binding':modules['acestep.api_server'].run_api_server_main=lambda *a,**k:None
    if failure=='entry_defaults':modules['acestep.api.server_cli'].run_api_server_main.__defaults__=(['--host','0.0.0.0'],)
    if failure=='revision':monkeypatch.setattr(music_backend.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess([],0,'unknown\n',''))
    with pytest.raises(ValueError):music_backend._run_engine([])
    assert observed==[]
    if failure in ('source','policy','revision'):assert calls==[]


def test_healthy_existing_music_api_never_imports_patches_installs_or_launches():
    with (patch.object(music_backend,'healthy',return_value=True),
          patch.object(music_backend,'music_lm_ready',return_value=False),
          patch.object(music_backend,'install',side_effect=AssertionError('No install')),
          patch.object(music_backend,'_run_engine',side_effect=AssertionError('No in-parent patch')),
          patch.object(music_backend,'_setup_lock',side_effect=AssertionError('No launch lock')),
          patch.object(music_backend.subprocess,'Popen',side_effect=AssertionError('No child'))):
        assert music_backend.start_server() is False


def test_music_start_lock_covers_wrapper_launch_and_http_readiness_without_forcing_eager(tmp_path,monkeypatch):
    from contextlib import contextmanager
    root=tmp_path/'app';engine=root/'Media/music/ACE-Step-1.5';python=engine/'.venv/Scripts/python.exe';python.parent.mkdir(parents=True);python.touch();(python.parent/'acestep-api.exe').touch()
    monkeypatch.setattr(music_backend,'ROOT',root);monkeypatch.setattr(music_backend,'ENGINE',engine)
    monkeypatch.setenv('ACESTEP_NO_INIT','true')
    monkeypatch.setenv('MIMALLOC_PURGE_DELAY','-1')
    held=[];polls=[];launched=[]
    @contextmanager
    def locked():
        held.append(True)
        try:yield
        finally:held.clear()
    def healthy():
        polls.append(bool(held))
        if len(polls)>=3:assert held
        return len(polls)>=4
    def launch(command,**kwargs):
        assert held and command==[str(python),'-B',str(Path(music_backend.__file__).resolve()),'--run-engine']
        assert kwargs['env']['ACESTEP_INIT_LLM']=='auto' and kwargs['env']['ACESTEP_NO_INIT']=='true'
        if os.name == 'nt':
            assert kwargs['env']['MIMALLOC_PURGE_DELAY']=='0'
            assert kwargs['env']['MIMALLOC_PURGE_DECOMMITS']=='1'
            assert kwargs['env']['MIMALLOC_ARENA_EAGER_COMMIT']=='0'
            assert kwargs['env']['MIMALLOC_EAGER_COMMIT']=='0'
        launched.append(command);return types.SimpleNamespace(poll=lambda:None)
    import types
    with (patch.object(music_backend,'_setup_lock',locked),patch.object(music_backend,'healthy',healthy),
          patch.object(music_backend,'music_lm_ready',return_value=False),patch.object(music_backend,'install'),
          patch.object(music_backend.subprocess,'Popen',launch),patch.object(music_backend.time,'sleep')):
        assert music_backend.start_server() is False
    assert polls==[False,True,True,True] and len(launched)==1 and not held


def test_music_allocator_policy_precedes_any_ace_import(tmp_path,monkeypatch):
    """Applying options after an ACE import would silently miss torch init."""
    import importlib
    monkeypatch.setattr(music_backend, '_music_memory_environment', lambda: {
        'MIMALLOC_PURGE_DELAY': '0', 'MIMALLOC_PURGE_DECOMMITS': '1'})
    monkeypatch.setenv('MIMALLOC_PURGE_DELAY', '-1')
    def before_import():
        assert os.environ['MIMALLOC_PURGE_DELAY'] == '0'
        assert os.environ['MIMALLOC_PURGE_DECOMMITS'] == '1'
        raise ValueError('stop before runtime import')
    monkeypatch.setattr(music_backend, '_read_engine_cpu_sources', before_import)
    with patch.object(importlib, 'import_module') as imported:
        with pytest.raises(ValueError, match='stop before runtime import'):
            music_backend._run_engine([])
    imported.assert_not_called()


def test_music_allocator_policy_does_not_change_other_platforms(monkeypatch):
    # Avoid patching os.name, which would affect pytest's own path handling.
    import types
    monkeypatch.setattr(music_backend, 'os', types.SimpleNamespace(name='posix'))
    assert music_backend._music_memory_environment() == {}


def test_music_start_actual_windows_setup_lock_prevents_duplicate_launch_while_first_readies(tmp_path):
    import sys,time
    root=tmp_path/'app';engine=root/'Media/music/ACE-Step-1.5';scripts=engine/'.venv/Scripts';scripts.mkdir(parents=True)
    (scripts/'python.exe').touch();(scripts/'acestep-api.exe').touch()
    driver=tmp_path/'startup.py'
    driver.write_text('''import sys,json,time
from pathlib import Path
sys.path.insert(0,sys.argv[1]);import music_backend as m
root=Path(sys.argv[2]);m.ROOT=root;m.ENGINE=root/'Media/music/ACE-Step-1.5'
m.healthy=lambda:(root/'ready').is_file();m.music_lm_ready=lambda:False;m.install=lambda:None
class Process:
 def poll(self):return None
def launched(*args,**kwargs):
 with (root/'launches').open('a') as f:f.write('launch\\n')
 (root/'launched').touch();return Process()
m.subprocess.Popen=launched
print(json.dumps({'result':m.start_server()}))
''',encoding='utf-8')
    args=[sys.executable,'-B',str(driver),str(Path(music_backend.__file__).parent),str(root)]
    first=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    second=None
    try:
        deadline=time.monotonic()+8
        while not (root/'launched').exists() and time.monotonic()<deadline:time.sleep(.05)
        assert (root/'launched').exists()
        second=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        time.sleep(.4)
        assert (root/'launches').read_text()=='launch\n'
        (root/'ready').touch()
        one=first.communicate(timeout=12);two=second.communicate(timeout=12)
        assert first.returncode==second.returncode==0,(one,two)
        assert (root/'launches').read_text()=='launch\n'
    finally:
        for child in (first,second):
            if child is not None and child.poll() is None:
                child.terminate();child.communicate(timeout=10)


def test_process_wrapper_compiles_real_module_context_on_python311(tmp_path):
    import sys
    base=Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Python/Python311/python.exe'
    if not base.is_file():pytest.skip('This host has no optional Python3.11 base for cross-version compiler proof')
    music_site=Path(music_backend.ROOT)/'Media/music/ACE-Step-1.5/.venv/Lib/site-packages'
    if not music_site.is_dir():
        music_site=Path(music_backend.__file__).parent.parent/'Hermes-Agent/Media/music/ACE-Step-1.5/.venv/Lib/site-packages'
    if not music_site.is_dir():pytest.skip('This host has no optional music package overlay for Python3.11 compiler proof')
    source=tmp_path/'pinned_decode.py';source.write_bytes(ace_cpu_source_fixture())
    script='''import sys,site,types,typing,ast,json,hashlib
from pathlib import Path
sys.path[:]=[p for p in sys.path if 'site-packages' not in p.lower()];site.addsitedir(sys.argv[1]);sys.path.insert(0,sys.argv[2])
import music_backend as m
path=Path(sys.argv[3]);source=path.read_bytes();tree=ast.parse(source)
compiled=compile(source,str(path),'exec',dont_inherit=True)
cls=next(c for c in compiled.co_consts if isinstance(c,types.CodeType) and c.co_name=='GenerateMusicDecodeMixin')
code=next(c for c in cls.co_consts if isinstance(c,types.CodeType) and c.co_name=='_decode_generate_music_pred_latents')
module=types.ModuleType('pinned_decode');module.__file__=str(path);module.__dict__.update({'torch':types.SimpleNamespace(Tensor=object),**vars(typing)})
module.__dict__['__file__']=str(path)
actual=types.FunctionType(code,module.__dict__)
expected=m._ace_function_from_source(source,module,code.co_name,'GenerateMusicDecodeMixin')
node=next(n for c in tree.body if isinstance(c,ast.ClassDef) for n in c.body if isinstance(n,ast.FunctionDef) and n.name==code.co_name)
standalone=compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(path),'exec',dont_inherit=True)
isolated=next(c for c in standalone.co_consts if isinstance(c,types.CodeType))
print(json.dumps({'python':sys.version,'full_module_code_sha256':hashlib.sha256(code.co_code).hexdigest(),'isolated_ast_code_sha256':hashlib.sha256(isolated.co_code).hexdigest(),'isolated_ast_bytecode_differs':code.co_code!=isolated.co_code,'strict_expected_matches_actual':m._ace_code_matches(actual,expected)}))
assert code.co_code!=isolated.co_code
assert m._ace_code_matches(actual,expected)
'''
    child=subprocess.run([str(base),'-I','-B','-c',script,str(music_site),str(Path(music_backend.__file__).parent),str(source)],capture_output=True,text=True,timeout=15)
    assert child.returncode==0,(child.stdout,child.stderr)
    evidence=json.loads(child.stdout)
    assert evidence['strict_expected_matches_actual'] and evidence['isolated_ast_bytecode_differs']
