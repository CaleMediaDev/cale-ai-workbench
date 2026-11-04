"""Mastering writes a new file and refuses paths that could replace input audio."""

from pathlib import Path
from unittest.mock import patch

import pytest

import music_mastering
import toolbox_mcp


def test_mastering_discovery():
    names = [item["name"] for item in toolbox_mcp.toolbox_search("master song reference")["tools"]]
    assert "local.master_music" in names
    assert toolbox_mcp.toolbox_schema("local.master_music")["inputSchema"]["required"] == [
        "source_path", "reference_path", "output_path"
    ]


def test_mastering_preserves_inputs_and_writes_new_result(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")

    def render(*args, **kwargs):
        Path(args[0][-1]).write_bytes(b"mastered")
        return type("Result", (), {"returncode": 0})()

    with patch.object(music_mastering.music_backend, "_setup_lock") as lock, patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render):
        lock.return_value.__enter__.return_value = None
        result = music_mastering.master_music(str(source), str(reference), str(output))
    assert Path(result["path"]).read_bytes() == b"mastered"
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"


def test_mastering_timeout_removes_partial_output_and_preserves_inputs(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    timeout = music_mastering.subprocess.TimeoutExpired("mastering worker", 3600)

    def time_out(*args, **kwargs):
        Path(args[0][-1]).write_bytes(b"partial WAV")
        raise timeout

    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=time_out):
        with pytest.raises(music_mastering.subprocess.TimeoutExpired) as caught:
            music_mastering.master_music(str(source), str(reference), str(output))

    assert caught.value is timeout
    assert not output.exists()
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"


def test_mastering_rejects_overwrite_and_same_reference(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    with pytest.raises(ValueError, match="different reference"):
        music_mastering.master_music(str(source), str(source), str(tmp_path / "new.wav"))
    with pytest.raises(ValueError, match="new output"):
        music_mastering.master_music(str(source), str(reference), str(source))
