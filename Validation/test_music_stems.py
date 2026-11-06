"""Protect existing audio and report actual separated files."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import music_stems


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


def test_separation_requires_existing_audio_and_empty_output(tmp_path, monkeypatch):
    from contextlib import nullcontext

    # Invalid-output checks must not acquire the live GPU lease or unload a
    # user's models. Keep both folder assertions independent of running services.
    monkeypatch.setattr(music_stems.music_backend, "_setup_lock", nullcontext)
    monkeypatch.setattr(music_stems.music_backend, "_music_gpu_lease", nullcontext)
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


def test_hard_link_rejects_stem_changed_after_hashing(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    original_link = music_stems.os.link
    linked = []

    def render(args, **_kwargs):
        staging = Path(args[-1])
        (staging / "a.wav").write_bytes(b"first")
        (staging / "b.wav").write_bytes(b"second")
        return SimpleNamespace(returncode=0)

    def change_before_link(staged, destination):
        prior = staged.stat()
        staged.write_bytes(b"U" * prior.st_size)
        os.utime(staged, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        original_link(staged, destination)
        linked.append(destination)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ), patch.object(music_stems.os, "link", side_effect=change_before_link):
        with pytest.raises(OSError, match="changed during publication"):
            music_stems.separate_stems(str(source), str(output))

    assert linked == [output / "a.wav"]
    # Rollback must not delete content changed since the original digest.
    assert linked[0].read_bytes() == b"UUUUU"
    assert set(output.iterdir()) == set(linked)
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
    original_link = music_stems.os.link
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
        return original_link(path, destination)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ), patch.object(music_stems.os, "link", side_effect=fail_second_move):
        with pytest.raises(OSError, match="simulated publication failure"):
            music_stems.separate_stems(str(source), str(output))
        assert list(output.iterdir()) == []
        result = music_stems.separate_stems(str(source), str(output))

    assert len(result["files"]) == 2
    assert {Path(path).read_bytes() for path in result["files"]} == {b"vocals", b"instrumental"}
    assert source.read_bytes() == b"music"


@pytest.mark.parametrize("interrupt_copy", [False, True])
@pytest.mark.parametrize("link_errno", [
    music_stems.errno.ENOTSUP, music_stems.errno.EXDEV, music_stems.errno.EPERM,
    music_stems.errno.ENOSYS,
])
def test_publication_without_hard_links_is_retryable(tmp_path, interrupt_copy, link_errno):
    import errno
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    original_copy = music_stems.shutil.copyfileobj
    copy_attempts = 0

    def render(args, **_kwargs):
        staging = Path(args[-1])
        (staging / "song_(Vocals).wav").write_bytes(b"vocals")
        (staging / "song_(Instrumental).wav").write_bytes(b"instrumental")
        return SimpleNamespace(returncode=0)

    def copy_with_interruption(input_file, output_file):
        nonlocal copy_attempts
        copy_attempts += 1
        if interrupt_copy and copy_attempts == 2:
            output_file.write(b"partial")
            raise OSError("interrupted stem copy")
        return original_copy(input_file, output_file)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ), patch.object(
        music_stems.os, "link", side_effect=OSError(link_errno, "Hard links unsupported")
    ), patch.object(
        music_stems.shutil, "copyfileobj", side_effect=copy_with_interruption
    ):
        if interrupt_copy:
            with pytest.raises(OSError, match="interrupted stem copy"):
                music_stems.separate_stems(str(source), str(output))
            assert list(output.iterdir()) == []
        result = music_stems.separate_stems(str(source), str(output))

    assert {Path(path).read_bytes() for path in result["files"]} == {
        b"vocals", b"instrumental"
    }
    assert set(output.iterdir()) == {Path(path) for path in result["files"]}
    assert source.read_bytes() == b"music"


def test_fallback_rejects_destination_edited_during_copy(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    original_copy = music_stems.shutil.copyfileobj
    edited = output / "a.wav"

    def render(args, **_kwargs):
        staging = Path(args[-1])
        (staging / "a.wav").write_bytes(b"first")
        (staging / "b.wav").write_bytes(b"second")
        return SimpleNamespace(returncode=0)

    def copy_then_edit(input_file, output_file):
        original_copy(input_file, output_file)
        destination = Path(output_file.name)
        prior = destination.stat()
        destination.write_bytes(b"U" * prior.st_size)
        os.utime(destination, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        assert destination.stat().st_ino == prior.st_ino

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ), patch.object(
        music_stems.os, "link",
        side_effect=OSError(music_stems.errno.ENOTSUP, "unsupported")
    ), patch.object(
        music_stems.shutil, "copyfileobj", side_effect=copy_then_edit
    ):
        with pytest.raises(OSError, match="changed during publication"):
            music_stems.separate_stems(str(source), str(output))

    assert set(output.iterdir()) == {edited}
    assert edited.read_bytes() == b"UUUUU"
    assert source.read_bytes() == b"music"


def test_fallback_short_disk_write_removes_partial_output(tmp_path):
    import errno
    import io
    from contextlib import nullcontext
    from types import SimpleNamespace

    source = tmp_path / "song.mp3"
    source.write_bytes(b"music")
    output = tmp_path / "stems"
    original_open = Path.open

    class ShortWriteFile(io.FileIO):
        def write(self, data):
            if self.tell() == 0:
                return super().write(data[:2])
            raise OSError("simulated disk full")

    def open_with_short_writes(path, mode="r", buffering=-1, **kwargs):
        if mode == "xb" and path.parent == output:
            raw = ShortWriteFile(path, mode)
            # Reproduce normal buffering when the caller does not disable it.
            return io.BufferedWriter(raw) if buffering == -1 else raw
        return original_open(path, mode, buffering=buffering, **kwargs)

    def render(args, **_kwargs):
        staging = Path(args[-1])
        (staging / "a.wav").write_bytes(b"first stem")
        (staging / "b.wav").write_bytes(b"second stem")
        return SimpleNamespace(returncode=0)

    with patch.object(
        music_stems.music_backend, "_setup_lock", side_effect=nullcontext
    ), patch.object(
        music_stems.music_backend, "_music_gpu_lease", side_effect=nullcontext
    ), patch.object(
        music_stems, "install", return_value=Path("python.exe")
    ), patch.object(
        music_stems.subprocess, "run", side_effect=render
    ), patch.object(
        music_stems.os, "link", side_effect=OSError(errno.ENOTSUP, "unsupported")
    ), patch.object(Path, "open", new=open_with_short_writes):
        with pytest.raises(OSError, match="full buffer|simulated disk full"):
            music_stems.separate_stems(str(source), str(output))

    assert list(output.iterdir()) == []
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable lookup regression")
def test_worker_exposes_bundled_ffmpeg_on_posix_path(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    engine = tmp_path / "audio-separator"
    engine.mkdir()
    binary = tmp_path / "bundled-ffmpeg"
    binary.write_bytes(b"bundled executable")
    binary.chmod(0o755)
    expected = engine / "bin" / "ffmpeg"
    separator_class = Mock()

    def check_ffmpeg_lookup():
        assert music_stems.shutil.which("ffmpeg") == str(expected)
        assert expected.read_bytes() == binary.read_bytes()

    separator_class.return_value.load_model.side_effect = check_ffmpeg_lookup
    modules = {
        "imageio_ffmpeg": SimpleNamespace(get_ffmpeg_exe=lambda: str(binary)),
        "audio_separator": SimpleNamespace(),
        "audio_separator.separator": SimpleNamespace(Separator=separator_class),
    }
    with patch.dict(music_stems.sys.modules, modules), patch.dict(
        music_stems.os.environ, {"PATH": ""}
    ), patch.object(music_stems, "ENGINE", engine):
        music_stems._worker("song.mp3", "stems")

    separator_class.return_value.load_model.assert_called_once_with()
    separator_class.return_value.separate.assert_called_once_with("song.mp3")


def test_worker_retries_after_partial_ffmpeg_copy(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    engine = tmp_path / "audio-separator"
    engine.mkdir()
    binary = tmp_path / "bundled-ffmpeg.exe"
    binary.write_bytes(b"complete executable")
    ffmpeg = engine / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
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

@pytest.mark.parametrize('external_change',['replace','same_inode_edit'])
def test_fallback_rollback_preserves_real_user_replacement_or_edit(tmp_path,external_change):
    import errno
    from contextlib import nullcontext
    source=tmp_path/'song.mp3';source.write_bytes(b'original audio');target=tmp_path/'stems'
    original_copy=music_stems.shutil.copyfileobj;copies=[];original_error=OSError('later copy failed')
    user_bytes=b''
    def render(args,**kwargs):
        staging=Path(args[-1]);(staging/'a-Instrumental.wav').write_bytes(b'own instrumental');(staging/'b-Vocals.wav').write_bytes(b'own vocals')
        return type('Done',(),{'returncode':0})()
    def copy(input_file,output_file):
        nonlocal user_bytes
        copies.append(Path(output_file.name))
        if len(copies)==2:
            first=copies[0];prior=first.stat();user_bytes=b'U'*prior.st_size
            if external_change=='replace':
                replacement=target/'replacement.tmp';replacement.write_bytes(user_bytes);replacement.replace(first)
                assert first.stat().st_ino!=prior.st_ino
            else:
                first.write_bytes(user_bytes);os.utime(first,ns=(prior.st_atime_ns,prior.st_mtime_ns))
                assert first.stat().st_ino==prior.st_ino and first.stat().st_size==prior.st_size
            output_file.write(b'own partial');raise original_error
        return original_copy(input_file,output_file)
    with patch.object(music_stems.music_backend,'_setup_lock',side_effect=nullcontext),patch.object(music_stems.music_backend,'_music_gpu_lease',side_effect=nullcontext),patch.object(music_stems,'install',return_value=Path('python.exe')),patch.object(music_stems.subprocess,'run',side_effect=render),patch.object(music_stems.os,'link',side_effect=OSError(errno.ENOTSUP,'unsupported')),patch.object(music_stems.shutil,'copyfileobj',side_effect=copy):
        with pytest.raises(OSError) as caught:music_stems.separate_stems(str(source),str(target))
    assert caught.value is original_error
    assert copies[0].read_bytes()==user_bytes
    assert not copies[1].exists()
    assert set(target.iterdir())=={copies[0]}
    assert source.read_bytes()==b'original audio'


@pytest.mark.parametrize('error_type',[OSError,KeyboardInterrupt])
def test_fallback_owned_partial_rolls_back_without_masking_original_error(tmp_path,error_type):
    import errno
    from contextlib import nullcontext
    source=tmp_path/'song.mp3';source.write_bytes(b'original audio');target=tmp_path/'stems';failure=error_type('copy interrupted')
    def render(args,**kwargs):
        staging=Path(args[-1]);(staging/'a.wav').write_bytes(b'first');(staging/'b.wav').write_bytes(b'second');return type('Done',(),{'returncode':0})()
    def copy(input_file,output_file):output_file.write(b'own partial');raise failure
    with patch.object(music_stems.music_backend,'_setup_lock',side_effect=nullcontext),patch.object(music_stems.music_backend,'_music_gpu_lease',side_effect=nullcontext),patch.object(music_stems,'install',return_value=Path('python.exe')),patch.object(music_stems.subprocess,'run',side_effect=render),patch.object(music_stems.os,'link',side_effect=OSError(errno.ENOTSUP,'unsupported')),patch.object(music_stems.shutil,'copyfileobj',side_effect=copy):
        with pytest.raises(error_type) as caught:music_stems.separate_stems(str(source),str(target))
    assert caught.value is failure and list(target.iterdir())==[]
    assert source.read_bytes()==b'original audio'


def test_fallback_exclusive_creation_never_overwrites_new_user_file(tmp_path):
    import errno
    from contextlib import nullcontext
    source=tmp_path/'song.mp3';source.write_bytes(b'original audio');target=tmp_path/'stems'
    def render(args,**kwargs):
        staging=Path(args[-1]);(staging/'a.wav').write_bytes(b'first');(staging/'b.wav').write_bytes(b'second');return type('Done',(),{'returncode':0})()
    def unsupported(staged,destination):
        Path(destination).write_bytes(b'user-created audio');raise OSError(errno.ENOTSUP,'unsupported')
    with patch.object(music_stems.music_backend,'_setup_lock',side_effect=nullcontext),patch.object(music_stems.music_backend,'_music_gpu_lease',side_effect=nullcontext),patch.object(music_stems,'install',return_value=Path('python.exe')),patch.object(music_stems.subprocess,'run',side_effect=render),patch.object(music_stems.os,'link',side_effect=unsupported):
        with pytest.raises(FileExistsError):music_stems.separate_stems(str(source),str(target))
    assert (target/'a.wav').read_bytes()==b'user-created audio'
    assert list(target.iterdir())==[target/'a.wav']
    assert source.read_bytes()==b'original audio'


@pytest.mark.parametrize('change',['replace','same_inode_edit'])
def test_link_rollback_preserves_real_user_replacement_or_edit(tmp_path,change):
    from contextlib import nullcontext
    source=tmp_path/'song.mp3';source.write_bytes(b'original audio');target=tmp_path/'stems'
    original_link=music_stems.os.link;links=[];failure=OSError('second link failed');user_bytes=b''
    def render(args,**kwargs):
        staging=Path(args[-1]);(staging/'a.wav').write_bytes(b'first');(staging/'b.wav').write_bytes(b'second');return type('Done',(),{'returncode':0})()
    def link(staged,destination):
        nonlocal user_bytes
        links.append(Path(destination))
        if len(links)==2:
            first=links[0];prior=first.stat();user_bytes=b'U'*prior.st_size
            if change=='replace':
                replacement=target/'replacement.tmp';replacement.write_bytes(user_bytes);replacement.replace(first)
            else:
                first.write_bytes(user_bytes);os.utime(first,ns=(prior.st_atime_ns,prior.st_mtime_ns));assert first.stat().st_ino==prior.st_ino
            raise failure
        return original_link(staged,destination)
    with patch.object(music_stems.music_backend,'_setup_lock',side_effect=nullcontext),patch.object(music_stems.music_backend,'_music_gpu_lease',side_effect=nullcontext),patch.object(music_stems,'install',return_value=Path('python.exe')),patch.object(music_stems.subprocess,'run',side_effect=render),patch.object(music_stems.os,'link',side_effect=link):
        with pytest.raises(OSError) as caught:music_stems.separate_stems(str(source),str(target))
    assert caught.value is failure
    assert links[0].read_bytes()==user_bytes and set(target.iterdir())=={links[0]}
    assert source.read_bytes()==b'original audio'
