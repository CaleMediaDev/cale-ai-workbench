"""A missing FFmpeg command is repaired from the pinned app dependency."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ffmpeg_runtime


@pytest.mark.parametrize("second_fails", [False, True])
def test_alternating_roots_keep_each_failed_repairs_cooldown(tmp_path, second_fails):
    first, second = tmp_path / "first", tmp_path / "second"
    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
         patch.object(ffmpeg_runtime, "_thread", None), \
         patch.object(ffmpeg_runtime, "working", return_value=False), \
         patch.object(ffmpeg_runtime, "ensure") as ensure, \
         patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
         patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        ensure.side_effect = [RuntimeError("first failed"),
                              RuntimeError("second failed") if second_fails else second / "ffmpeg.exe",
                              RuntimeError("first failed again")]
        thread.return_value.is_alive.return_value = False
        thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
        ffmpeg_runtime.start_background(first)
        ffmpeg_runtime.start_background(second)
        ffmpeg_runtime.start_background(first)
        assert ensure.call_count == 2
        clock.return_value = 311.0
        ffmpeg_runtime.start_background(first)
        assert ensure.call_count == 3


def test_root_failure_cache_is_bounded_and_keeps_recent_roots_throttled(tmp_path):
    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
         patch.object(ffmpeg_runtime, "_thread", None), \
         patch.object(ffmpeg_runtime, "working", return_value=False), \
         patch.object(ffmpeg_runtime, "ensure", side_effect=RuntimeError("offline")) as ensure, \
         patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0), \
         patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        thread.return_value.is_alive.return_value = False
        thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
        count = ffmpeg_runtime.MAX_REPAIR_ROOTS + 10
        for number in range(count):
            ffmpeg_runtime.start_background(tmp_path / str(number))
        assert ensure.call_count == count
        assert len(ffmpeg_runtime._last_attempts) <= ffmpeg_runtime.MAX_REPAIR_ROOTS
        for number in range(count - 10, count):
            ffmpeg_runtime.start_background(tmp_path / str(number))
        assert ensure.call_count == count


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


def test_ensure_reinstalls_existing_but_unusable_package_binary(tmp_path):
    from unittest.mock import MagicMock

    python = tmp_path / "Toolbox/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"mock Python runtime")
    source = tmp_path / "package-ffmpeg.exe"
    source.write_bytes(b"corrupted executable")
    discovered = subprocess.CompletedProcess([], 0, str(source) + "\n", "")

    def run(args, **kwargs):
        if args[1] == "-c":
            return discovered
        if args[1:4] == ["-m", "pip", "install"]:
            assert "--force-reinstall" in args
            assert "imageio-ffmpeg==0.6.0" in args
            source.write_bytes(b"repaired executable")
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args == [str(source), "-version"]
        assert kwargs["text"] is False
        return subprocess.CompletedProcess(args, 1, b"", b"corrupted executable")

    with patch.dict(sys.modules, {"msvcrt": MagicMock()}), \
            patch.object(ffmpeg_runtime, "working", side_effect=[False, False, True]), \
            patch.object(ffmpeg_runtime.shutil, "which", return_value=str(source)), \
            patch.object(ffmpeg_runtime.subprocess, "run", side_effect=run) as mocked_run:
        target = ffmpeg_runtime.ensure(tmp_path)

    assert target.read_bytes() == b"repaired executable"
    calls = [call.args[0] for call in mocked_run.call_args_list]
    assert len(calls) == 4
    assert calls[1] == [str(source), "-version"]
    assert calls[2][1:4] == ["-m", "pip", "install"]
    assert calls[0] == calls[3]


def test_ensure_resolves_discovered_ffmpeg_command_on_path(tmp_path):
    from unittest.mock import MagicMock

    python = tmp_path / "Toolbox/python/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"mock Python runtime")
    source = tmp_path / "system-bin/ffmpeg.exe"
    source.parent.mkdir()
    source.write_bytes(b"mock FFmpeg executable")
    discovered = subprocess.CompletedProcess([], 0, "ffmpeg\n", "")

    with patch.dict(sys.modules, {"msvcrt": MagicMock()}), \
            patch.object(ffmpeg_runtime, "working", side_effect=[False, False, True]), \
            patch.object(ffmpeg_runtime.shutil, "which", return_value=str(source)) as which, \
            patch.object(ffmpeg_runtime, "_working_executable", return_value=True), \
            patch.object(ffmpeg_runtime.subprocess, "run", return_value=discovered) as run:
        target = ffmpeg_runtime.ensure(tmp_path)

    assert target == ffmpeg_runtime.executable(tmp_path)
    assert target.read_bytes() == source.read_bytes()
    assert which.call_count == 2
    which.assert_called_with("ffmpeg")
    # Only discovery is needed: do not invoke pip for an executable on PATH.
    run.assert_called_once_with(
        [str(python), "-c",
         "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
        capture_output=True, text=True, timeout=30,
        creationflags=ffmpeg_runtime.HIDE,
    )


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


@pytest.mark.parametrize("error", [
    PermissionError("installation directory is inaccessible"),
    OSError("installation filesystem is unavailable"),
])
def test_version_health_check_handles_executable_stat_errors(tmp_path, error):
    with patch.object(Path, "is_file", side_effect=error), \
            patch.object(ffmpeg_runtime.subprocess, "run") as run:
        assert ffmpeg_runtime.working(tmp_path) is False

    run.assert_not_called()


@pytest.mark.parametrize("repair_fails", [False, True])
def test_background_repair_only_throttles_failures(tmp_path, repair_fails):
    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
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
        assert ffmpeg_runtime._last_attempts.get(tmp_path.resolve()) == (10.0 if repair_fails else None)

        # Model another missing executable shortly after the first attempt.
        clock.return_value = 20.0
        ffmpeg_runtime.start_background(tmp_path)

        expected_attempts = 1 if repair_fails else 2
        assert ensure.call_count == expected_attempts
        assert thread.return_value.start.call_count == expected_attempts


def test_slow_failed_repair_gets_full_cooldown_after_completion(tmp_path):
    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
            patch.object(ffmpeg_runtime, "_repairing_roots", set()), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime, "ensure") as ensure, \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        def slow_failure(root):
            clock.return_value += 300.0
            raise RuntimeError("repair failed after five minutes")

        ensure.side_effect = slow_failure
        thread.return_value.start.side_effect = (
            lambda: thread.call_args.kwargs["target"]()
        )

        ffmpeg_runtime.start_background(tmp_path)
        ensure.assert_called_once_with(tmp_path.resolve())
        assert ffmpeg_runtime._last_attempts[tmp_path.resolve()] == 310.0

        for now in (311.0, 609.0):
            clock.return_value = now
            ffmpeg_runtime.start_background(tmp_path)
            assert ensure.call_count == 1

        clock.return_value = 610.0
        ffmpeg_runtime.start_background(tmp_path)
        assert ensure.call_count == 2
        assert not ffmpeg_runtime._repairing_roots


def test_observed_external_recovery_clears_failed_repair_cooldown(tmp_path):
    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
            patch.object(ffmpeg_runtime, "_repairing_roots", set()), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", side_effect=[False, True, False]), \
            patch.object(ffmpeg_runtime, "ensure") as ensure, \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        ensure.side_effect = [RuntimeError("repair failed"),
                              ffmpeg_runtime.executable(tmp_path)]
        thread.return_value.start.side_effect = (
            lambda: thread.call_args.kwargs["target"]()
        )

        ffmpeg_runtime.start_background(tmp_path)
        assert ffmpeg_runtime._last_attempts[tmp_path.resolve()] == 10.0
        ensure.assert_called_once_with(tmp_path.resolve())

        # An external repair makes the previous failure obsolete.
        clock.return_value = 20.0
        ffmpeg_runtime.start_background(tmp_path)
        assert tmp_path.resolve() not in ffmpeg_runtime._last_attempts
        assert ensure.call_count == 1

        # A new deletion must be repaired, even inside the old cooldown.
        clock.return_value = 30.0
        ffmpeg_runtime.start_background(tmp_path)
        assert ensure.call_count == 2
        assert thread.return_value.start.call_count == 2
        assert not ffmpeg_runtime._last_attempts
        assert not ffmpeg_runtime._repairing_roots


def test_active_background_repair_does_not_block_another_root(tmp_path):
    from unittest.mock import MagicMock

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    workers = []

    def make_thread(**kwargs):
        worker = MagicMock()
        worker.is_alive.return_value = True
        workers.append((worker, kwargs["target"]))
        return worker

    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
            patch.object(ffmpeg_runtime, "_repairing_roots", set()), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime, "ensure") as ensure, \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
            patch.object(ffmpeg_runtime.threading, "Thread", side_effect=make_thread) as thread:
        ffmpeg_runtime.start_background(first_root)
        # Even after the cooldown, an in-flight repair must not be duplicated.
        clock.return_value = 311.0
        ffmpeg_runtime.start_background(first_root)
        assert thread.call_count == 1

        # Leave the first worker pending while another installation needs repair.
        ffmpeg_runtime.start_background(second_root)
        assert thread.call_count == 2
        ensure.assert_not_called()
        for worker, repair in workers:
            worker.start.assert_called_once_with()
            repair()

        assert [call.args[0] for call in ensure.call_args_list] == [
            first_root.resolve(), second_root.resolve(),
        ]
        assert not ffmpeg_runtime._repairing_roots
        assert not ffmpeg_runtime._last_attempts


def test_failed_background_repair_does_not_throttle_another_root(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime, "ensure", side_effect=RuntimeError("repair failed")) as ensure, \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0), \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        thread.return_value.is_alive.return_value = False
        thread.return_value.start.side_effect = (
            lambda: thread.call_args.kwargs["target"]()
        )

        ffmpeg_runtime.start_background(first_root)
        ensure.assert_called_once_with(first_root)

        # The same runtime remains subject to the failure cooldown.
        ffmpeg_runtime.start_background(first_root)
        assert ensure.call_count == 1

        # A different runtime must receive its own repair attempt immediately.
        ffmpeg_runtime.start_background(second_root)
        assert ensure.call_count == 2
        ensure.assert_called_with(second_root)
        assert thread.return_value.start.call_count == 2


def test_background_repair_preserves_relative_root_after_cwd_change(tmp_path, monkeypatch):
    initial_cwd = tmp_path / "initial"
    later_cwd = tmp_path / "later"
    initial_cwd.mkdir()
    later_cwd.mkdir()
    monkeypatch.chdir(initial_cwd)
    relative_root = Path("installation")
    expected_root = relative_root.resolve()

    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime, "ensure") as ensure, \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        ffmpeg_runtime.start_background(relative_root)
        thread.return_value.start.assert_called_once_with()
        ensure.assert_not_called()
        worker = thread.call_args.kwargs["target"]

        # The background worker must not reinterpret the caller's relative path.
        monkeypatch.chdir(later_cwd)
        worker()

        ensure.assert_called_once_with(expected_root)
        assert expected_root not in ffmpeg_runtime._last_attempts


def test_failed_thread_start_allows_immediate_repair_retry(tmp_path):
    with patch.object(ffmpeg_runtime, "_last_attempts", {}), \
            patch.object(ffmpeg_runtime, "_repairing_roots", set()), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime, "ensure") as ensure, \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        thread.return_value.start.side_effect = RuntimeError("cannot start thread")

        with pytest.raises(RuntimeError, match="cannot start thread"):
            ffmpeg_runtime.start_background(tmp_path)

        ensure.assert_not_called()
        assert tmp_path.resolve() not in ffmpeg_runtime._repairing_roots
        assert tmp_path.resolve() not in ffmpeg_runtime._last_attempts

        clock.return_value = 20.0
        thread.return_value.start.side_effect = (
            lambda: thread.call_args.kwargs["target"]()
        )
        ffmpeg_runtime.start_background(tmp_path)

        assert thread.call_count == 2
        ensure.assert_called_once_with(tmp_path.resolve())
        assert not ffmpeg_runtime._repairing_roots
        assert not ffmpeg_runtime._last_attempts


def test_first_background_repair_is_not_throttled_at_low_uptime(tmp_path):
    # Preserve the module's initial timestamp while restoring it after this test.
    with patch.object(ffmpeg_runtime, "_last_attempts", dict(ffmpeg_runtime._last_attempts)), \
            patch.object(ffmpeg_runtime, "_thread", None), \
            patch.object(ffmpeg_runtime, "working", return_value=False), \
            patch.object(ffmpeg_runtime.time, "monotonic", return_value=10.0) as clock, \
            patch.object(ffmpeg_runtime.threading, "Thread") as thread:
        thread.return_value.is_alive.return_value = False

        ffmpeg_runtime.start_background(tmp_path)

        thread.assert_called_once()
        thread.return_value.start.assert_called_once_with()
        assert ffmpeg_runtime._last_attempts[tmp_path.resolve()] == 10.0

        clock.return_value = 20.0
        ffmpeg_runtime.start_background(tmp_path)

        thread.assert_called_once()
        thread.return_value.start.assert_called_once_with()
