"""The music adapter must preserve local paths and never fetch remote results."""

import io
from contextlib import nullcontext
from unittest.mock import patch

import pytest

import music_backend


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


def test_cover_uses_local_source_path(tmp_path):
    source = tmp_path / "original.wav"
    source.write_bytes(b"source audio")
    responses = iter([
        {"code": 200, "data": {"task_id": "task-2"}},
        {"code": 200, "data": [{"status": 1, "result": '[{"file":"/v1/audio?path=cover"}]'}]},
    ])
    with patch.object(music_backend, "start_server"), patch.object(
        music_backend, "_request", side_effect=lambda *_args, **_kwargs: next(responses)
    ) as request, patch.object(
        music_backend, "urlopen", return_value=io.BytesIO(b"cover audio")
    ), patch.object(music_backend.time, "sleep"):
        result = music_backend.generate_music(
            "jazzy cover", str(tmp_path / "cover.mp3"), source_path=str(source)
        )
    assert result["source_path"] == str(source)
    assert request.call_args_list[0].args[1]["task_type"] == "cover"
    assert request.call_args_list[0].args[1]["src_audio_path"] == str(source)


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
