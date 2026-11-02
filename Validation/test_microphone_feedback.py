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


def test_microphone_preflight_identifies_input_without_recording():
    sd, sf = _audio_modules(0)
    with patch.dict(sys.modules, {"sounddevice": sd, "soundfile": sf}):
        result = media_worker.microphone_status()
    assert result == {"input_device": "Fifine Microphone", "device_index": 0,
                      "sample_rate": 44100}
    sd.check_input_settings.assert_called_once_with(device=0, samplerate=44100, channels=1)
    sd.rec.assert_not_called()
    sf.write.assert_not_called()


def test_near_silent_microphone_skips_transcription_and_names_device(tmp_path):
    sd, sf = _audio_modules(0.0001)
    with patch.dict(sys.modules, {"sounddevice": sd, "soundfile": sf}), patch.object(
        media_worker, "ROOT", tmp_path
    ), patch.object(media_worker, "transcribe_audio") as transcribe:
        result = media_worker.record_audio(seconds=1)
    assert result["quiet"] is True
    assert result["input_device"] == "Fifine Microphone"
    assert result["input_peak"] == 0.0001
    assert result["text"] == ""
    transcribe.assert_not_called()
    sf.write.assert_not_called()


def test_faint_speech_is_recognized_before_diagnosing_low_input(tmp_path):
    sd, sf = _audio_modules(0.006)
    with patch.dict(sys.modules, {"sounddevice": sd, "soundfile": sf}), patch.object(
        media_worker, "ROOT", tmp_path
    ), patch.object(media_worker, "transcribe_audio", return_value={
        "text": "Hey Kale, can you hear me?", "segments": [], "language": "en"
    }) as transcribe:
        result = media_worker.record_audio(seconds=1)
    transcribe.assert_called_once()
    assert result["quiet"] is False
    assert result["text"] == "Hey Kale, can you hear me?"


def test_faint_hiss_without_words_reports_low_input(tmp_path):
    # This low level matches a quiet-room capture from the default microphone.
    sd, sf = _audio_modules(0.0104)
    with patch.dict(sys.modules, {"sounddevice": sd, "soundfile": sf}), patch.object(
        media_worker, "ROOT", tmp_path
    ), patch.object(media_worker, "transcribe_audio", return_value={
        "text": "", "segments": [], "language": ""
    }) as transcribe:
        result = media_worker.record_audio(seconds=1)
    transcribe.assert_called_once()
    assert result["quiet"] is True
    assert result["input_peak"] == 0.0104


def test_idle_mic_noise_spikes_do_not_masquerade_as_speech(tmp_path):
    sd, sf = _audio_modules(0.001)
    samples = np.full((100, 1), 0.001, dtype=np.float32)
    samples[::10] = 0.014
    sd.rec.return_value = samples
    with patch.dict(sys.modules, {"sounddevice": sd, "soundfile": sf}), patch.object(
        media_worker, "ROOT", tmp_path
    ), patch.object(media_worker, "transcribe_audio", return_value={
        "text": "", "segments": [], "language": ""
    }):
        result = media_worker.record_audio(seconds=1)
    assert result["input_peak"] == 0.014
    assert result["quiet"] is True


def test_noise_spikes_do_not_end_a_clip_early():
    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, frames):
            block = np.full((frames, 1), 0.001, dtype=np.float32)
            block[0] = 0.014
            return block, False

    sd = SimpleNamespace(InputStream=lambda **_kwargs: Stream())
    clip = media_worker._capture_microphone(sd, device=1, rate=100, seconds=8)
    # A spike must not count as speech; quiet listening can restart at four
    # seconds instead of waiting through the full long-command window.
    assert len(clip) == 400


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


def test_spoken_clip_ends_after_pause_instead_of_full_eight_seconds():
    class Stream:
        def __init__(self, **_kwargs):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, frames):
            self.reads += 1
            # Four spoken blocks, then silence. The microphone captures the
            # pause after speech but need not wait through all eight seconds.
            level = 0.04 if self.reads <= 4 else 0.0
            return np.full((frames, 1), level, dtype=np.float32), False

    stream = Stream()
    sd = SimpleNamespace(InputStream=lambda **_kwargs: stream)
    clip = media_worker._capture_microphone(sd, device=1, rate=100, seconds=8)
    assert 100 < len(clip) < 800
    assert stream.reads < 40
    assert np.max(clip) == np.float32(0.04)


def test_quiet_clip_returns_early_to_restart_handsfree_listening():
    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, frames):
            return np.zeros((frames, 1), dtype=np.float32), False

    sd = SimpleNamespace(InputStream=lambda **_kwargs: Stream())
    clip = media_worker._capture_microphone(sd, device=1, rate=100, seconds=8)
    assert len(clip) == 400


def test_long_spoken_request_is_not_cut_off_after_eight_seconds():
    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, frames):
            return np.full((frames, 1), 0.04, dtype=np.float32), False

    sd = SimpleNamespace(InputStream=lambda **_kwargs: Stream())
    clip = media_worker._capture_microphone(sd, device=1, rate=100, seconds=20)
    assert len(clip) == 2000


def test_whisper_model_is_loaded_once_for_repeated_clips(tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"test")
    model = Mock()
    model.transcribe.return_value = ([], SimpleNamespace(language="en"))
    fake = ModuleType("faster_whisper")
    fake.WhisperModel = Mock(return_value=model)
    with patch.dict(sys.modules, {"faster_whisper": fake}), patch.object(
        media_worker, "_whisper_model", None
    ):
        media_worker.transcribe_audio(audio, fast=True)
        media_worker.transcribe_audio(audio, fast=True)
        media_worker.transcribe_audio(audio, music=True)
    fake.WhisperModel.assert_called_once()
    assert model.transcribe.call_count == 3
    assert model.transcribe.call_args_list[0].kwargs["vad_filter"] is True
    assert model.transcribe.call_args_list[2].kwargs["vad_filter"] is False
