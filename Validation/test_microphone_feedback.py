"""A muted input should be reported distinctly from an unrecognized phrase."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import media_worker
import voice_assistant


def _audio_modules(level):
    device = {"name": "Fifine Microphone", "max_input_channels": 1,
              "default_samplerate": 44100}
    sounddevice = ModuleType("sounddevice")
    sounddevice.default = SimpleNamespace(device=(0, 0))
    sounddevice.query_devices = lambda: [device]
    sounddevice.check_input_settings = Mock()
    sounddevice.PortAudioError = RuntimeError
    sounddevice.rec = Mock(return_value=np.full((100, 1), level, dtype=np.float32))
    sounddevice.wait = Mock()
    soundfile = ModuleType("soundfile")
    soundfile.write = Mock()
    return sounddevice, soundfile


def test_quiet_microphone_skips_transcription_and_names_device(tmp_path):
    sd, sf = _audio_modules(0.0001)
    with patch.dict(sys.modules, {"sounddevice": sd, "soundfile": sf}), patch.object(
        media_worker, "ROOT", tmp_path
    ), patch.object(media_worker, "transcribe_audio") as transcribe:
        result = media_worker.record_audio(seconds=1)
    assert result["quiet"] is True
    assert result["input_device"] == "Fifine Microphone"
    assert result["text"] == ""
    transcribe.assert_not_called()
    sf.write.assert_not_called()


def test_audible_microphone_transcribes_and_reports_input_level(tmp_path):
    sd, sf = _audio_modules(0.05)
    transcript = {"text": "Hey, Kale, can you hear me?", "segments": [], "language": "en"}
    with patch.dict(sys.modules, {"sounddevice": sd, "soundfile": sf}), patch.object(
        media_worker, "ROOT", tmp_path
    ), patch.object(media_worker, "transcribe_audio", return_value=transcript):
        result = media_worker.record_audio(seconds=1)
    assert result["quiet"] is False
    assert result["input_peak"] == 0.05
    assert result["text"] == transcript["text"]
    sf.write.assert_called_once()


def test_listen_result_preserves_mic_diagnostics():
    with patch.object(voice_assistant, "media_call", return_value={
        "text": "  Hey, Kale  ", "quiet": False,
        "input_device": "Fifine Microphone", "input_peak": 0.05,
    }):
        result = voice_assistant.listen_result(8)
    assert result == {"text": "Hey, Kale", "quiet": False,
                      "input_device": "Fifine Microphone", "input_peak": 0.05}
