"""Protect existing audio and report actual separated files."""

from pathlib import Path
from unittest.mock import patch

import pytest

import music_stems


def test_separation_requires_existing_audio_and_empty_output(tmp_path):
    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    with pytest.raises(ValueError, match="separate output folder"):
        music_stems.separate_stems(str(source), str(tmp_path))
    output = tmp_path / "stems"
    output.mkdir()
    (output / "keep.txt").write_text("do not overwrite")
    with pytest.raises(ValueError, match="empty output folder"):
        music_stems.separate_stems(str(source), str(output))
    assert source.read_bytes() == b"music"
    assert (output / "keep.txt").read_text() == "do not overwrite"


def test_separation_returns_only_real_nonempty_stems(tmp_path):
    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"

    def render(*_args, **_kwargs):
        (output / "song_(Vocals).wav").write_bytes(b"vocals")
        (output / "song_(Instrumental).wav").write_bytes(b"instrumental")
        (output / "empty.wav").write_bytes(b"")
        return type("Result", (), {"returncode": 0})()

    from contextlib import nullcontext

    with patch.object(music_stems.music_backend, "_setup_lock", return_value=nullcontext()), patch.object(
        music_stems.music_backend, "_music_gpu_lease", return_value=nullcontext()
    ), patch.object(music_stems, "install", return_value=Path("python.exe")), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ):
        result = music_stems.separate_stems(str(source), str(output))
    assert len(result["files"]) == 2
    assert all(Path(path).stat().st_size for path in result["files"])
    assert source.read_bytes() == b"music"


def test_output_rechecked_after_waiting_for_setup_lock(tmp_path):
    from contextlib import contextmanager, nullcontext

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    output.mkdir()
    existing_stem = output / "song_(Vocals).wav"

    @contextmanager
    def acquire_after_previous_separation():
        # Another call finishes writing while this call waits for the lock.
        existing_stem.write_bytes(b"previous vocals")
        yield

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=acquire_after_previous_separation
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", return_value=nullcontext()
    ), patch.object(music_stems, "install") as install, patch.object(
        music_stems.subprocess, "run"
    ) as run:
        with pytest.raises(ValueError, match="empty output folder"):
            music_stems.separate_stems(str(source), str(output))
        install.assert_not_called()
        run.assert_not_called()

    assert existing_stem.read_bytes() == b"previous vocals"
    assert source.read_bytes() == b"music"


@pytest.mark.parametrize("wav_count", [0, 1])
def test_auxiliary_files_do_not_count_as_stems(tmp_path, wav_count):
    from contextlib import nullcontext

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"

    def render(*_args, **_kwargs):
        (output / "separator.log").write_text("completed")
        (output / "metadata.json").write_text("{}")
        if wav_count:
            (output / "song_(Vocals).wav").write_bytes(b"vocals")
        return type("Result", (), {"returncode": 0})()

    with patch.object(music_stems.music_backend, "_setup_lock", return_value=nullcontext()), patch.object(
        music_stems.music_backend, "_music_gpu_lease", return_value=nullcontext()
    ), patch.object(music_stems, "install", return_value=Path("python.exe")), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ):
        with pytest.raises(RuntimeError, match="did not produce both"):
            music_stems.separate_stems(str(source), str(output))
    assert source.read_bytes() == b"music"
