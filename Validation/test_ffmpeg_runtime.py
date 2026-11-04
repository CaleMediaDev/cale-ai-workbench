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


@pytest.mark.parametrize("stdout,returncode,expected", [
    (b"ffmpeg version 7.0\n", 0, True),
    (b"ffmpeg version 7.0\nconfiguration: --extra-version=\xff\n", 0, True),
    (b"ffmpeg version 7.0\n", 1, False),
    (b"not ffmpeg\n", 0, False),
])
def test_version_health_check_uses_bytes(tmp_path, stdout, returncode, expected):
    target = ffmpeg_runtime.executable(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"mock executable")

    def probe(args, **kwargs):
        # Model subprocess decoding: the metadata is not valid UTF-8, but
        # that must not prevent recognition of the ASCII version banner.
        output = stdout.decode("utf-8") if kwargs.get("text") else stdout
        return subprocess.CompletedProcess(args, returncode, output, b"")

    with patch.object(ffmpeg_runtime.subprocess, "run", side_effect=probe) as run:
        assert ffmpeg_runtime.working(tmp_path) is expected

    run.assert_called_once_with(
        [str(target), "-version"], capture_output=True,
        text=False, timeout=10, creationflags=ffmpeg_runtime.HIDE,
    )


@pytest.mark.parametrize("repair_fails", [False, True])
def test_background_repair_only_throttles_failures(tmp_path, repair_fails):
    with patch.object(ffmpeg_runtime, "_last_attempt", None), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime, "ensure") as ensure, \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        ensure.return_value = ffmpeg_runtime.executable(tmp_path)
        if repair_fails:
            ensure.side_effect = RuntimeError("repair failed")
        thread.return_value.is_alive.return_value = False
        # Run the worker synchronously without installing anything.
        thread.return_value.start.side_effect = (
            lambda: thread.call_args.kwargs["target"]()
        )

        ffmpeg_runtime.start_background(tmp_path)
        ensure.assert_called_once_with(tmp_path)
        assert ffmpeg_runtime._last_attempt == (10.0 if repair_fails else None)

        # Model another missing executable shortly after the first attempt.
        clock.return_value = 20.0
        ffmpeg_runtime.start_background(tmp_path)

        expected_attempts = 1 if repair_fails else 2
        assert ensure.call_count == expected_attempts
        assert thread.return_value.start.call_count == expected_attempts


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
