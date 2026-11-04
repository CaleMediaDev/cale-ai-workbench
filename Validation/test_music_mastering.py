"""Mastering writes a new file and refuses paths that could replace input audio."""

from pathlib import Path
from unittest.mock import Mock, patch

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


def test_worker_preserves_precision_in_both_intermediate_wavs(tmp_path):
    ffmpeg = Mock()
    ffmpeg.get_ffmpeg_exe.return_value = "ffmpeg"
    matchering = Mock()
    source = str(tmp_path / "source.flac")
    reference = str(tmp_path / "reference.wav")
    output = str(tmp_path / "master.wav")

    with patch.dict("sys.modules", {"imageio_ffmpeg": ffmpeg, "matchering": matchering}), patch.object(
        music_mastering.subprocess, "run"
    ) as run:
        music_mastering._worker(source, reference, output)

    assert run.call_count == 2
    converted = []
    for call, original in zip(run.call_args_list, (source, reference)):
        command = call.args[0]
        assert command[0] == "ffmpeg"
        assert command[command.index("-i") + 1] == original
        assert "-c:a" in command
        assert command[command.index("-c:a") + 1] == "pcm_f32le"
        assert call.kwargs["check"] is True
        converted.append(command[-1])
    assert converted[0] != converted[1]
    matchering.pcm24.assert_called_once_with(output)
    matchering.process.assert_called_once_with(
        target=converted[0], reference=converted[1], results=[matchering.pcm24.return_value]
    )


@pytest.mark.parametrize("worker_result", ["success", "failure", "timeout"])
def test_mastering_preserves_destination_created_during_render(tmp_path, worker_result):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    staged_paths = []

    def render(*args, **kwargs):
        staged = Path(args[0][-1])
        staged_paths.append(staged)
        staged.write_bytes(b"rendered audio")
        output.write_bytes(b"another operation's audio")
        if worker_result == "timeout":
            raise music_mastering.subprocess.TimeoutExpired("mastering worker", 3600)
        return type("Result", (), {
            "returncode": int(worker_result == "failure"),
            "stderr": "worker failed",
            "stdout": "",
        })()

    expected = {
        "success": ValueError,
        "failure": RuntimeError,
        "timeout": music_mastering.subprocess.TimeoutExpired,
    }[worker_result]
    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render):
        with pytest.raises(expected):
            music_mastering.master_music(str(source), str(reference), str(output))

    assert output.read_bytes() == b"another operation's audio"
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"
    assert len(staged_paths) == 1
    assert staged_paths[0] != output
    assert not staged_paths[0].parent.exists()


def test_mastering_rejects_hard_linked_reference(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.hardlink_to(source)

    with patch.object(music_mastering, "install") as install:
        with pytest.raises(ValueError, match="different reference"):
            music_mastering.master_music(str(source), str(reference), str(output))
        install.assert_not_called()

    assert not output.exists()
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"source"


def test_mastering_rejects_dangling_output_symlink(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    target = tmp_path / "missing-target.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    try:
        output.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symlinks is not supported in this environment")

    with patch.object(music_mastering, "install") as install:
        with pytest.raises(ValueError, match="new output"):
            music_mastering.master_music(str(source), str(reference), str(output))
        install.assert_not_called()

    assert output.is_symlink()
    assert output.readlink() == target
    assert not target.exists()
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
