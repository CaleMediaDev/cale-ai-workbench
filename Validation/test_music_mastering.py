"""Mastering writes a new file and refuses paths that could replace input audio."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import music_mastering
import toolbox_mcp


def test_install_repairs_non_ascii_marker(tmp_path):
    engine = tmp_path / "matchering"
    python = engine / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"existing runtime")
    marker = engine / ".cale-installed"
    marker.write_bytes(b"\xffcorrupted")

    with patch.object(music_mastering, "ENGINE", engine), patch.object(
        music_mastering.shutil, "disk_usage", return_value=Mock(free=3 * 1024**3)
    ), patch.object(
        music_mastering.music_backend, "_tool", return_value="uv"
    ), patch.object(music_mastering.subprocess, "run") as run:
        assert music_mastering.install() == python

    run.assert_called_once()
    assert run.call_args.args[0] == [
        "uv", "pip", "install", "--python", str(python),
        f"matchering=={music_mastering.VERSION}", "imageio-ffmpeg==0.6.0",
    ]
    assert run.call_args.kwargs["check"] is True
    assert marker.read_bytes() == music_mastering.VERSION.encode("ascii")
    assert python.read_bytes() == b"existing runtime"


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


def test_mastering_publishes_result_with_non_utf8_worker_diagnostics(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    real_run = music_mastering.subprocess.run
    worker_results = []

    def render(command, **kwargs):
        staged = command[-1]
        # Exercise the UTF-8 decoding case independently of the host locale.
        kwargs.setdefault("encoding", "utf-8")
        result = real_run(
            [music_mastering.sys.executable, "-c",
             "import pathlib, sys; "
             "pathlib.Path(sys.argv[1]).write_bytes(b'mastered'); "
             "sys.stderr.buffer.write(bytes([255]))", staged],
            **kwargs,
        )
        worker_results.append(result)
        return result

    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render):
        result = music_mastering.master_music(str(source), str(reference), str(output))

    assert Path(result["path"]).read_bytes() == b"mastered"
    assert worker_results[0].stderr == "\ufffd"
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


@pytest.mark.parametrize("destination_created", [False, True])
@pytest.mark.parametrize("winerror", [None, 1, 50])
def test_mastering_without_filesystem_hard_link_support(tmp_path, destination_created, winerror):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    staged_paths = []

    def render(*args, **kwargs):
        staged = Path(args[0][-1])
        staged_paths.append(staged)
        staged.write_bytes(b"mastered")
        if destination_created:
            output.write_bytes(b"another operation's audio")
        return type("Result", (), {"returncode": 0})()

    unsupported = OSError(
        music_mastering.errno.EOPNOTSUPP if winerror is None else music_mastering.errno.EINVAL,
        "Hard links unsupported",
    )
    if winerror is not None:
        unsupported.winerror = winerror
    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render), patch.object(
        music_mastering.os, "link", side_effect=unsupported
    ):
        if destination_created:
            with pytest.raises(ValueError, match="new output"):
                music_mastering.master_music(str(source), str(reference), str(output))
        else:
            result = music_mastering.master_music(str(source), str(reference), str(output))
            assert Path(result["path"]) == output

    assert output.read_bytes() == (
        b"another operation's audio" if destination_created else b"mastered"
    )
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"
    assert len(staged_paths) == 1
    assert not staged_paths[0].parent.exists()


def test_mastering_closes_fallback_output_when_fstat_fails(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    metadata_error = OSError(music_mastering.errno.EIO, "Metadata lookup failed")
    opened = []
    real_open = Path.open

    def track_open(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        if path == output and args == ("xb",):
            opened.append(stream)
        return stream

    def render(*args, **kwargs):
        Path(args[0][-1]).write_bytes(b"mastered")
        return type("Result", (), {"returncode": 0})()

    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render), patch.object(
        music_mastering.os, "link",
        side_effect=OSError(music_mastering.errno.EOPNOTSUPP, "Hard links unsupported"),
    ), patch.object(Path, "open", new=track_open), patch.object(
        music_mastering.os, "fstat", side_effect=metadata_error
    ):
        with pytest.raises(OSError) as caught:
            music_mastering.master_music(str(source), str(reference), str(output))

    assert caught.value is metadata_error
    assert len(opened) == 1
    assert opened[0].closed
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"


def test_mastering_preserves_copy_error_when_destination_close_fails(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    copy_error = OSError(music_mastering.errno.ENOSPC, "Copy failed")
    close_error = OSError(music_mastering.errno.EIO, "Flush failed")
    real_open = Path.open
    opened = []

    class FailingClose:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def write(self, data):
            return self.stream.write(data)

        def close(self):
            self.stream.close()
            raise close_error

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def open_destination(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        if path == output and args == ("xb",):
            opened.append(stream)
            return FailingClose(stream)
        return stream

    def render(*args, **kwargs):
        Path(args[0][-1]).write_bytes(b"mastered")
        return type("Result", (), {"returncode": 0})()

    def fail_copy(rendered, destination):
        destination.write(b"partial")
        raise copy_error

    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render), patch.object(
        music_mastering.os, "link",
        side_effect=OSError(music_mastering.errno.EOPNOTSUPP, "Hard links unsupported"),
    ), patch.object(Path, "open", new=open_destination), patch.object(
        music_mastering.shutil, "copyfileobj", side_effect=fail_copy
    ):
        with pytest.raises(OSError) as caught:
            music_mastering.master_music(str(source), str(reference), str(output))

    assert caught.value is copy_error
    assert caught.value.__cause__ is close_error
    assert len(opened) == 1
    assert opened[0].closed
    assert not output.exists()
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"


def test_mastering_preserves_copy_error_when_partial_output_cleanup_fails(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    staged_paths = []
    copy_error = OSError(music_mastering.errno.ENOSPC, "No space left on device")
    cleanup_error = PermissionError("Cannot remove partial output")

    def render(*args, **kwargs):
        staged = Path(args[0][-1])
        staged_paths.append(staged)
        staged.write_bytes(b"mastered")
        return type("Result", (), {"returncode": 0})()

    def fail_copy(rendered, destination):
        destination.write(b"partial")
        raise copy_error

    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render), patch.object(
        music_mastering.os, "link",
        side_effect=OSError(music_mastering.errno.EOPNOTSUPP, "Hard links unsupported"),
    ), patch.object(
        music_mastering.shutil, "copyfileobj", side_effect=fail_copy
    ), patch.object(Path, "unlink", side_effect=cleanup_error) as unlink:
        with pytest.raises(OSError) as caught:
            music_mastering.master_music(str(source), str(reference), str(output))
        unlink.assert_called_once_with()

    assert caught.value is copy_error
    assert caught.value.__cause__ is cleanup_error
    assert output.read_bytes() == b"partial"
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"
    assert len(staged_paths) == 1
    assert not staged_paths[0].parent.exists()


def test_mastering_copy_failure_preserves_replacement_destination(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    moved_partial = tmp_path / "moved-partial.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    copy_error = OSError(music_mastering.errno.ENOSPC, "No space left on device")
    staged_paths = []

    def render(*args, **kwargs):
        staged = Path(args[0][-1])
        staged_paths.append(staged)
        staged.write_bytes(b"mastered")
        return type("Result", (), {"returncode": 0})()

    def replace_then_fail(rendered, destination):
        destination.write(b"partial")
        # Close first so the replacement can also be simulated on Windows.
        destination.close()
        output.rename(moved_partial)
        output.write_bytes(b"another operation's audio")
        raise copy_error

    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render), patch.object(
        music_mastering.os, "link",
        side_effect=OSError(music_mastering.errno.EOPNOTSUPP, "Hard links unsupported"),
    ), patch.object(
        music_mastering.shutil, "copyfileobj", side_effect=replace_then_fail
    ):
        with pytest.raises(OSError) as caught:
            music_mastering.master_music(str(source), str(reference), str(output))

    assert caught.value is copy_error
    assert output.read_bytes() == b"another operation's audio"
    assert moved_partial.read_bytes() == b"partial"
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"
    assert len(staged_paths) == 1
    assert not staged_paths[0].parent.exists()


def test_mastering_copy_success_detects_replacement_destination(tmp_path):
    source = tmp_path / "song.wav"
    reference = tmp_path / "reference.wav"
    output = tmp_path / "master.wav"
    moved_output = tmp_path / "moved-master.wav"
    source.write_bytes(b"source")
    reference.write_bytes(b"reference")
    staged_paths = []
    real_copy = music_mastering.shutil.copyfileobj

    def render(*args, **kwargs):
        staged = Path(args[0][-1])
        staged_paths.append(staged)
        staged.write_bytes(b"mastered")
        return type("Result", (), {"returncode": 0})()

    def copy_then_replace(rendered, destination):
        real_copy(rendered, destination)
        # Close before moving so this also works on Windows.
        destination.close()
        output.rename(moved_output)
        output.write_bytes(b"another operation's audio")

    with patch.object(music_mastering.music_backend, "_setup_lock"), patch.object(
        music_mastering, "install", return_value=Path("python.exe")
    ), patch.object(music_mastering.subprocess, "run", side_effect=render), patch.object(
        music_mastering.os, "link",
        side_effect=OSError(music_mastering.errno.EOPNOTSUPP, "Hard links unsupported"),
    ), patch.object(
        music_mastering.shutil, "copyfileobj", side_effect=copy_then_replace
    ):
        with pytest.raises(RuntimeError, match="changed during publication"):
            music_mastering.master_music(str(source), str(reference), str(output))

    assert output.read_bytes() == b"another operation's audio"
    assert moved_output.read_bytes() == b"mastered"
    assert source.read_bytes() == b"source"
    assert reference.read_bytes() == b"reference"
    assert len(staged_paths) == 1
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
        pytest.skip("Creating symlinks is unavailable on this system")

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
