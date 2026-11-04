"""The optional voice model must release GPU memory after speech ends."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest

import cale_voice


def test_gpu_release_still_stops_parent_when_children_are_inaccessible(tmp_path):
    import psutil
    own = Mock(pid=100, info={"cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    own.children.side_effect = psutil.AccessDenied(pid=100)
    with patch.object(cale_voice, "ROOT", tmp_path), patch("psutil.process_iter", return_value=[own]), \
         patch("os.getpid", return_value=500), patch("psutil.wait_procs", return_value=([], [])):
        cale_voice.release_for_gpu_task()
    own.terminate.assert_called_once_with()
    own.wait.assert_called_once_with(timeout=5)


def test_gpu_release_waits_for_killed_descendants_before_returning(tmp_path):
    own = Mock(pid=100, info={"cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    child = Mock()
    own.children.return_value = [child]
    with patch.object(cale_voice, "ROOT", tmp_path), patch("psutil.process_iter", return_value=[own]), \
         patch("os.getpid", return_value=500), \
         patch("psutil.wait_procs", side_effect=[([], [child]), ([child], [])]) as wait:
        cale_voice.release_for_gpu_task()
    child.kill.assert_called_once_with()
    assert len(wait.call_args_list) == 2
    assert all(call.args == ([child],) and call.kwargs == {"timeout": 3} for call in wait.call_args_list)


def test_gpu_release_waits_for_parent_after_forced_kill(tmp_path):
    import psutil
    own = Mock(pid=100, info={"cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    own.children.return_value = []
    own.wait.side_effect = [psutil.TimeoutExpired(5, pid=100), 0]
    with patch.object(cale_voice, "ROOT", tmp_path), patch("psutil.process_iter", return_value=[own]), \
         patch("os.getpid", return_value=500), patch("psutil.wait_procs", return_value=([], [])):
        cale_voice.release_for_gpu_task()
    own.kill.assert_called_once_with()
    assert own.wait.call_count == 2
    own.wait.assert_called_with(timeout=5)


def test_voice_ready_rejects_a_stale_marker_with_wrong_dependencies():
    with TemporaryDirectory() as folder:
        root = Path(folder)
        python = root / "Media/python/Scripts/python.exe"
        reference = root / "assets/reference.wav"
        marker = root / "LongTasks/cale-voice-ready.json"
        for path in (python, reference, marker):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        packages = root / "Media/python/Lib/site-packages"
        for name, version in cale_voice.VOICE_PACKAGES.items():
            (packages / f"{name}-{version}.dist-info").mkdir(parents=True)
        with (patch.object(cale_voice, "ROOT", root),
              patch.object(cale_voice, "MEDIA_PYTHON", python),
              patch.object(cale_voice, "REFERENCE", reference),
              patch.object(cale_voice, "MARKER", marker)):
            assert cale_voice.ready()
            (packages / "transformers-4.57.3.dist-info").rename(
                packages / "transformers-4.57.1.dist-info"
            )
            assert not cale_voice.ready()


@pytest.mark.parametrize("error", [
    OSError("worker pipe failed"),
    ValueError("I/O operation on closed file"),
])
def test_worker_stream_errors_reach_the_caller(error):
    stream = Mock()
    stream.readline.side_effect = error
    with pytest.raises(type(error)) as raised:
        cale_voice._line_with_timeout(stream, 1)
    assert raised.value is error
    stream.readline.assert_called_once_with()


def test_idle_release_ignores_newer_speech():
    with patch.object(cale_voice, "_stop_worker") as stop, patch.object(
        cale_voice, "_generation", 12
    ):
        cale_voice._release_after_idle(11)
        stop.assert_not_called()
        cale_voice._release_after_idle(12)
        stop.assert_called_once()


def test_stopping_launcher_also_stops_gpu_child():
    launcher = Mock(pid=100)
    launcher.poll.return_value = None
    child = Mock()
    with patch.object(cale_voice, "_worker", launcher), patch(
        "psutil.Process"
    ) as process, patch("psutil.wait_procs", return_value=([], [])):
        process.return_value.children.return_value = [child]
        cale_voice._stop_worker()
    child.terminate.assert_called_once()
    launcher.terminate.assert_called_once()
    assert cale_voice._worker is None


def test_stopping_worker_kills_it_if_termination_times_out():
    worker = Mock(pid=100)
    worker.poll.return_value = None
    worker.wait.side_effect = [
        cale_voice.subprocess.TimeoutExpired("voice-worker", 3),
        0,
    ]
    with (patch.object(cale_voice, "_worker", worker),
          patch("psutil.Process") as process,
          patch("psutil.wait_procs", return_value=([], []))):
        process.return_value.children.return_value = []
        cale_voice._stop_worker()
        assert cale_voice._worker is None
    worker.terminate.assert_called_once_with()
    worker.kill.assert_called_once_with()
    assert worker.wait.call_count == 2
    worker.wait.assert_called_with(timeout=3)


@pytest.mark.parametrize("child_survives", [False, True])
def test_music_releases_only_this_installations_voice_worker(tmp_path, child_survives):
    own = Mock(pid=100, info={"cmdline": ["python.exe", str(tmp_path / "cale_voice_worker.py")]})
    other = Mock(pid=101, info={"cmdline": ["python.exe", str(tmp_path / "other.py")]})
    child = Mock()
    own.children.return_value = [child]
    alive = [child] if child_survives else []
    with (patch.object(cale_voice, "ROOT", tmp_path),
          patch("psutil.process_iter", return_value=[own, other]),
          patch("os.getpid", return_value=500),
          patch("psutil.wait_procs", return_value=([], alive)) as wait_procs):
        cale_voice.release_for_gpu_task()
    own.terminate.assert_called_once()
    own.wait.assert_called_once_with(timeout=5)
    own.children.assert_called_once_with(recursive=True)
    child.terminate.assert_called_once_with()
    assert wait_procs.call_count == (2 if child_survives else 1)
    wait_procs.assert_called_with([child], timeout=3)
    if child_survives:
        child.kill.assert_called_once_with()
    else:
        child.kill.assert_not_called()
    other.terminate.assert_not_called()
    other.children.assert_not_called()


@pytest.mark.parametrize("lines", [
    ["null"],
    ["[]"],
    ['{"ready": true}', "null"],
    ['{"ready": true}', "[]"],
])
def test_non_object_worker_messages_trigger_cleanup(tmp_path, lines):
    with (patch.object(cale_voice, "ROOT", tmp_path),
          patch.object(cale_voice, "ready", return_value=True),
          patch.object(cale_voice, "_worker", None),
          patch.object(cale_voice, "_idle_timer", None),
          patch.object(cale_voice, "_generation", 0),
          patch.object(cale_voice.subprocess, "Popen"),
          patch.object(cale_voice, "_line_with_timeout", side_effect=lines),
          patch.object(cale_voice, "_stop_worker") as stop,
          patch.object(cale_voice.threading, "Timer") as timer):
        with pytest.raises(RuntimeError, match="could not finish this update"):
            cale_voice.speak("Hello", tmp_path / "speech.wav", play=False)
        stop.assert_called_once_with()
        timer.assert_not_called()


@pytest.mark.parametrize("answer", [
    '{}',
    '{"path": null}',
    '{"path": ""}',
    '{"path": 123}',
])
@pytest.mark.parametrize("play", [False, True])
def test_invalid_audio_path_triggers_cleanup(tmp_path, answer, play):
    with (patch.object(cale_voice, "ROOT", tmp_path),
          patch.object(cale_voice, "ready", return_value=True),
          patch.object(cale_voice, "_worker", None),
          patch.object(cale_voice, "_idle_timer", None),
          patch.object(cale_voice, "_generation", 0),
          patch.object(cale_voice.subprocess, "Popen"),
          patch.object(cale_voice, "_line_with_timeout",
                       side_effect=['{"ready": true}', answer]),
          patch.object(cale_voice, "_stop_worker") as stop,
          patch.object(cale_voice.threading, "Timer") as timer):
        with pytest.raises(RuntimeError, match="could not finish this update"):
            cale_voice.speak("Hello", tmp_path / "speech.wav", play=play)
        stop.assert_called_once_with()
        timer.assert_not_called()
