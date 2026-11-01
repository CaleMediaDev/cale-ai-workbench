"""The optional voice model must release GPU memory after speech ends."""

from unittest.mock import Mock, patch

import cale_voice


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
