"""Branded exports must never delete another writer's destination."""

import subprocess
from pathlib import Path

import pytest

import song_video


@pytest.mark.parametrize("outcome", ["error", "timeout", "success"])
def test_branded_export_preserves_concurrent_destination(tmp_path, monkeypatch, outcome):
    source = tmp_path / "song.wav"
    source.write_bytes(b"original audio")
    video = tmp_path / "song-facebook.mp4"

    def render(command, **kwargs):
        video.write_bytes(b"another operation's video")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if outcome == "error":
            return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"encoder error")
        Path(command[-1]).write_bytes(b"completed render")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(song_video.subprocess, "run", render)
    monkeypatch.setattr(song_video, "_video_playable", lambda path: True)
    expected = {"error": RuntimeError, "timeout": subprocess.TimeoutExpired, "success": FileExistsError}[outcome]
    with pytest.raises(expected):
        song_video._render_branded(str(source))
    assert video.read_bytes() == b"another operation's video"
    assert source.read_bytes() == b"original audio"
    assert not song_video._render_record(video).exists()
    assert set(tmp_path.iterdir()) == {source, video}
