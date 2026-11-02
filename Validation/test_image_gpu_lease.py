"""Image rendering reserves GPU memory before it loads local weights."""

import os
from unittest.mock import patch

import media_worker


def test_image_lease_releases_voice_and_cleans_marker(tmp_path):
    with patch.object(media_worker, "ROOT", tmp_path), patch(
        "cale_voice.release_for_gpu_task"
    ) as release:
        with media_worker._image_gpu_lease():
            marker = tmp_path / "LongTasks" / f"image-gpu-active-{os.getpid()}.json"
            assert marker.is_file()
        assert not marker.exists()
    release.assert_called_once()
