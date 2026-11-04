"""Protect existing audio and report actual separated files."""

import errno
import os
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

import music_stems


@pytest.mark.skipif(os.name != "nt", reason="Windows publication without hard links")
def test_separation_exports_when_hard_links_are_unavailable(tmp_path):
    source, output = tmp_path / "song.mp3", tmp_path / "stems"
    source.write_bytes(b"original music")

    def render(args, **kwargs):
        staging = Path(args[-1])
        (staging / "vocals.wav").write_bytes(b"vocals")
        (staging / "instrumental.wav").write_bytes(b"instrumental")
        return type("Result", (), {"returncode": 0})()

    with patch.object(music_stems.music_backend, "_setup_lock", side_effect=nullcontext), \
         patch.object(music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext), \
         patch.object(music_stems, "install", return_value=Path("python.exe")), \
         patch.object(music_stems.subprocess, "run", side_effect=render), \
         patch.object(music_stems.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "No hard links")):
        result = music_stems.separate_stems(str(source), str(output))
    assert {Path(path).read_bytes() for path in result["files"]} == {b"vocals", b"instrumental"}
    assert source.read_bytes() == b"original music"


def test_failed_runtime_rebuild_is_retried(tmp_path):
    engine = tmp_path / "audio-separator"
    engine.mkdir()
    marker = engine / ".cale-installed"
    marker.write_text(music_stems.VERSION, encoding="ascii")
    python = engine / ".venv" / "Scripts" / "python.exe"
    package_attempts = 0

    def rebuild(command, **_kwargs):
        nonlocal package_attempts
        if command[1] == "venv":
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"python")
        else:
            package_attempts += 1
            if package_attempts == 1:
                raise music_stems.subprocess.CalledProcessError(1, command)

    with patch.object(music_stems, "ENGINE", engine), patch.object(
        music_stems.shutil, "disk_usage", return_value=type("Usage", (), {"free": 10 * 1024**3})()
    ), patch.object(music_stems.music_backend, "_tool", return_value="uv"), patch.object(
        music_stems.subprocess, "run", side_effect=rebuild
    ) as run:
        with pytest.raises(music_stems.subprocess.CalledProcessError):
            music_stems.install()
        assert python.is_file()
        assert not marker.exists()

        assert music_stems.install() == python
        assert package_attempts == 2
        assert run.call_count == 4
        assert marker.read_text(encoding="ascii") == music_stems.VERSION


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
        staging = Path(_args[0][-1])
        (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        (staging / "song_(Instrumental).wav").write_bytes(b"instrumental")
        (staging / "empty.wav").write_bytes(b"")
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


def test_successful_separation_publishes_only_validated_stems(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"

    def render(args, **_kwargs):
        staging = Path(args[-1])
        (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        (staging / "song_(Instrumental).wav").write_bytes(b"instrumental")
        (staging / "empty.wav").write_bytes(b"")
        (staging / "separator.log").write_text("completed")
        scratch = staging / "scratch"
        scratch.mkdir()
        (scratch / "partial.wav").write_bytes(b"temporary audio")
        return SimpleNamespace(returncode=0)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(music_stems.subprocess, "run", side_effect=render):
        result = music_stems.separate_stems(str(source), str(output))

    expected = {output / "song_(Vocals).wav", output / "song_(Instrumental).wav"}
    assert {Path(path) for path in result["files"]} == expected
    assert set(output.iterdir()) == expected
    assert {path.read_bytes() for path in expected} == {b"vocals", b"instrumental"}
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


def test_failed_worker_leaves_output_empty_for_retry(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    attempts = 0

    def render(args, **_kwargs):
        nonlocal attempts
        attempts += 1
        staging = Path(args[-1])
        (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        if attempts == 1:
            return SimpleNamespace(returncode=1, stderr="inference failed", stdout="")
        (staging / "song_(Instrumental).wav").write_bytes(b"instrumental")
        return SimpleNamespace(returncode=0)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(music_stems.subprocess, "run", side_effect=render):
        with pytest.raises(RuntimeError, match="inference failed"):
            music_stems.separate_stems(str(source), str(output))
        assert list(output.iterdir()) == []
        result = music_stems.separate_stems(str(source), str(output))

    assert attempts == 2
    assert len(result["files"]) == 2
    assert all(Path(path).parent == output and Path(path).is_file() for path in result["files"])
    assert source.read_bytes() == b"music"


def test_failed_publication_leaves_output_empty_for_retry(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    publication = "rename" if os.name == "nt" else "link"
    original_publish = getattr(music_stems.os, publication)
    move_attempts = 0

    def render(args, **_kwargs):
        staging = Path(args[-1])
        (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        (staging / "song_(Instrumental).wav").write_bytes(b"instrumental")
        return SimpleNamespace(returncode=0)

    def fail_second_move(path, destination):
        nonlocal move_attempts
        move_attempts += 1
        if move_attempts == 2:
            raise OSError("simulated publication failure")
        return original_publish(path, destination)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ), patch.object(music_stems.os, publication, side_effect=fail_second_move):
        with pytest.raises(OSError, match="simulated publication failure"):
            music_stems.separate_stems(str(source), str(output))
        assert list(output.iterdir()) == []
        result = music_stems.separate_stems(str(source), str(output))

    assert len(result["files"]) == 2
    assert {Path(path).read_bytes() for path in result["files"]} == {b"vocals", b"instrumental"}
    assert source.read_bytes() == b"music"


def test_publication_preserves_file_created_during_inference(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    collision = output / "song_(Vocals).wav"
    attempts = 0

    def render(args, **_kwargs):
        nonlocal attempts
        attempts += 1
        staging = Path(args[-1])
        (staging / "song_(Instrumental).wav").write_bytes(b"instrumental")
        (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        if attempts == 1:
            # Arrives after the empty-folder check; sorts after Instrumental.
            collision.write_bytes(b"external audio")
        return SimpleNamespace(returncode=0)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(music_stems.subprocess, "run", side_effect=render):
        with pytest.raises(FileExistsError):
            music_stems.separate_stems(str(source), str(output))
        assert set(output.iterdir()) == {collision}
        assert collision.read_bytes() == b"external audio"
        collision.unlink()
        result = music_stems.separate_stems(str(source), str(output))

    assert attempts == 2
    assert {Path(path).read_bytes() for path in result["files"]} == {b"vocals", b"instrumental"}
    assert source.read_bytes() == b"music"


def test_relative_symlinks_do_not_count_as_stems(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"

    # Check platform support before invoking the separation code.
    probe = tmp_path / "probe.wav"
    try:
        probe.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable")
    probe.unlink()

    def render(args, **_kwargs):
        staging = Path(args[-1])
        for name in ("song_(Vocals).wav", "song_(Instrumental).wav"):
            link = staging / name
            # Valid in staging, but dangling if moved one directory upward.
            link.symlink_to("../../song.mp3")
            assert link.is_file()
            assert link.stat().st_size > 0
        return SimpleNamespace(returncode=0)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(music_stems.subprocess, "run", side_effect=render):
        with pytest.raises(RuntimeError, match="did not produce both"):
            music_stems.separate_stems(str(source), str(output))

    assert list(output.iterdir()) == []
    assert source.read_bytes() == b"music"


def test_worker_retries_after_partial_ffmpeg_copy(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    engine = tmp_path / "audio-separator"
    engine.mkdir()
    binary = tmp_path / "bundled-ffmpeg.exe"
    binary.write_bytes(b"complete executable")
    ffmpeg = engine / "bin" / "ffmpeg.exe"
    original_copy = music_stems.shutil.copy2
    attempts = 0
    separator_class = Mock()

    def interrupted_copy(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            Path(destination).write_bytes(b"partial")
            raise OSError("interrupted copy")
        return original_copy(source, destination)

    modules = {
        "imageio_ffmpeg": SimpleNamespace(get_ffmpeg_exe=lambda: str(binary)),
        "audio_separator": SimpleNamespace(),
        "audio_separator.separator": SimpleNamespace(Separator=separator_class),
    }
    with patch.dict(music_stems.sys.modules, modules), patch.dict(
        music_stems.os.environ
    ), patch.object(music_stems, "ENGINE", engine), patch.object(
        music_stems.shutil, "copy2", side_effect=interrupted_copy
    ):
        with pytest.raises(OSError, match="interrupted copy"):
            music_stems._worker("song.mp3", "stems")
        assert not ffmpeg.exists()
        assert list(ffmpeg.parent.iterdir()) == []
        separator_class.assert_not_called()

        music_stems._worker("song.mp3", "stems")

    assert attempts == 2
    assert ffmpeg.read_bytes() == binary.read_bytes()
    assert set(ffmpeg.parent.iterdir()) == {ffmpeg}
    separator_class.return_value.load_model.assert_called_once_with()
    separator_class.return_value.separate.assert_called_once_with("song.mp3")


@pytest.mark.parametrize("returncode", [0, 1])
def test_worker_diagnostic_decoding_does_not_mask_result(tmp_path, returncode):
    from contextlib import nullcontext

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    original_run = music_stems.subprocess.run

    def render(args, **kwargs):
        staging = Path(args[-1])
        (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        (staging / "song_(Instrumental).wav").write_bytes(b"instrumental")
        # Exercise actual subprocess decoding, not a pre-decoded mock result.
        kwargs["encoding"] = "utf-8"  # Reproduce the worker's invalid UTF-8 on every host locale.
        return original_run(
            [music_stems.sys.executable, "-c",
             "import os, sys; os.write(2, b'diagnostic: \\xff'); "
             f"sys.exit({returncode})"],
            **kwargs,
        )

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(music_stems.subprocess, "run", side_effect=render):
        if returncode:
            with pytest.raises(RuntimeError, match="diagnostic: \ufffd"):
                music_stems.separate_stems(str(source), str(output))
            assert list(output.iterdir()) == []
        else:
            result = music_stems.separate_stems(str(source), str(output))
            assert {Path(path).read_bytes() for path in result["files"]} == {
                b"vocals", b"instrumental"
            }

    assert source.read_bytes() == b"music"


@pytest.mark.parametrize("wav_count", [0, 1])
def test_auxiliary_files_do_not_count_as_stems(tmp_path, wav_count):
    from contextlib import nullcontext

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"

    def render(*_args, **_kwargs):
        staging = Path(_args[0][-1])
        (staging / "separator.log").write_text("completed")
        (staging / "metadata.json").write_text("{}")
        if wav_count:
            (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        return type("Result", (), {"returncode": 0})()

    with patch.object(music_stems.music_backend, "_setup_lock", return_value=nullcontext()), patch.object(
        music_stems.music_backend, "_music_gpu_lease", return_value=nullcontext()
    ), patch.object(music_stems, "install", return_value=Path("python.exe")), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ):
        with pytest.raises(RuntimeError, match="did not produce both"):
            music_stems.separate_stems(str(source), str(output))
    assert source.read_bytes() == b"music"
