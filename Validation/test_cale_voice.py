"""The optional voice model must release GPU memory after speech ends."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import cale_voice


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
