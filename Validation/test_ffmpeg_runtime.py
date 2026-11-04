"""A missing FFmpeg command is repaired from the pinned app dependency."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ffmpeg_runtime


@pytest.mark.skipif(os.name != "nt", reason="The app and executable are Windows-only")
def test_missing_ffmpeg_is_installed_and_validated(tmp_path):
    import imageio_ffmpeg

    python = tmp_path / "Toolbox/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake runtime")
    source = imageio_ffmpeg.get_ffmpeg_exe()
    actual_run = subprocess.run

    def run(args, **kwargs):
        if len(args) >= 3 and args[1] == "-c":
            return subprocess.CompletedProcess(args, 0, source + "\n", "")
        return actual_run(args, **kwargs)

    with patch.object(ffmpeg_runtime.subprocess, "run", side_effect=run):
        target = ffmpeg_runtime.ensure(tmp_path)
        assert target == tmp_path / "Toolbox/bin/ffmpeg.exe"
        assert ffmpeg_runtime.working(tmp_path)
        target.unlink()
        assert ffmpeg_runtime.working(tmp_path) is False
        assert ffmpeg_runtime.ensure(tmp_path) == target
        assert ffmpeg_runtime.working(tmp_path)


def test_repair_restores_missing_binary_from_already_installed_package(tmp_path):
    from types import SimpleNamespace

    python = tmp_path / "Toolbox/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake runtime")
    source = tmp_path / "installed-package/ffmpeg.exe"
    source.parent.mkdir()
    target = ffmpeg_runtime.executable(tmp_path)
    installs = []

    def run(args, **kwargs):
        if args[1] == "-c":
            if source.is_file():
                return subprocess.CompletedProcess(args, 0, str(source) + "\n", "")
            return subprocess.CompletedProcess(args, 1, "", "Bundled executable missing")
        if args[1:4] == ["-m", "pip", "install"]:
            installs.append(args)
            # Plain pip install leaves an already-satisfied package untouched.
            if "--force-reinstall" in args:
                source.write_bytes(b"restored executable")
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == [str(target), "-version"]:
            assert target.read_bytes() == b"restored executable"
            return subprocess.CompletedProcess(args, 0, "ffmpeg version test\n", "")
        raise AssertionError(f"Unexpected subprocess: {args}")

    fake_msvcrt = SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=lambda *args: None)
    with patch.dict(sys.modules, {"msvcrt": fake_msvcrt}), \
            patch.object(ffmpeg_runtime.subprocess, "run", side_effect=run):
        assert ffmpeg_runtime.ensure(tmp_path) == target
        assert ffmpeg_runtime.working(tmp_path)

    assert len(installs) == 1
    assert "--force-reinstall" in installs[0]
    assert "imageio-ffmpeg==0.6.0" in installs[0]
    assert target.read_bytes() == b"restored executable"


def test_undecodable_version_output_is_a_failed_health_check(tmp_path):
    target = ffmpeg_runtime.executable(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"invalid executable")
    decode_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    with patch.object(ffmpeg_runtime.subprocess, "run", side_effect=decode_error) as run:
        assert ffmpeg_runtime.working(tmp_path) is False

    run.assert_called_once_with(
        [str(target), "-version"], capture_output=True,
        text=True, timeout=10, creationflags=ffmpeg_runtime.HIDE,
    )


def test_first_background_repair_is_not_throttled_at_low_uptime(tmp_path):
    # Preserve the module's initial timestamp while restoring it after this test.
    with patch.object(ffmpeg_runtime, "_last_attempt", ffmpeg_runtime._last_attempt), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        thread.return_value.is_alive.return_value = False

        ffmpeg_runtime.start_background(tmp_path)

        thread.assert_called_once()
        thread.return_value.start.assert_called_once_with()
        assert ffmpeg_runtime._last_attempt == 10.0

        clock.return_value = 20.0
        ffmpeg_runtime.start_background(tmp_path)

        thread.assert_called_once()
        thread.return_value.start.assert_called_once_with()
