"""A broken neural voice must still produce audible offline speech."""

import os
import wave
from unittest.mock import patch

import pytest

import media_worker


@pytest.mark.skipif(os.name != "nt", reason="Windows SAPI is Windows-only")
def test_kokoro_failure_falls_back_to_real_windows_speech(tmp_path):
    model_dir = tmp_path / "models/natural_voice"
    model_dir.mkdir(parents=True)
    (model_dir / "kokoro-v1.0.int8.onnx").write_bytes(b"broken model")
    (model_dir / "voices-v1.0.bin").write_bytes(b"broken voices")
    output = tmp_path / "spoken.wav"
    with patch.object(media_worker, "MEDIA", tmp_path):
        result = media_worker.speak("Cale can still speak offline.", str(output), play=False)
    assert result["engine"] == "Windows offline speech"
    with wave.open(str(output), "rb") as audio:
        assert audio.getnframes() > 0
