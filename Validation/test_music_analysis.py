"""Fast checks for approximate harmony helpers; runtime audio is tested separately."""

import numpy as np

import music_analysis


def test_chord_detection_uses_pitch_classes():
    chroma = np.zeros(12)
    chroma[[0, 4, 7]] = [1, 0.8, 0.7]
    assert music_analysis._best_chord(chroma) == "C major"
    assert music_analysis._best_chord(np.zeros(12)) is None


def test_key_estimate_declines_empty_input():
    assert music_analysis._best_key(np.zeros(12)) is None


def test_analysis_records_same_chord_returning_after_silence(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    source = tmp_path / "chord-gap.wav"
    source.touch()
    sample_rate = 22050
    frames_per_segment = round(4 * sample_rate / 512)
    chroma = np.zeros((12, 3 * frames_per_segment))
    for start in (0, 2 * frames_per_segment):
        chroma[[0, 4, 7], start:start + frames_per_segment] = 1.0

    librosa_stub = SimpleNamespace(
        get_duration=lambda *, path: 12.0,
        load=lambda *args, **kwargs: (np.full(12 * sample_rate, 0.1), sample_rate),
        effects=SimpleNamespace(harmonic=lambda signal: signal),
        feature=SimpleNamespace(chroma_stft=lambda **kwargs: chroma),
        beat=SimpleNamespace(beat_track=lambda **kwargs: (0.0, np.array([]))),
    )
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)

    result = music_analysis.analyze_music(str(source))

    assert result["chord_changes"] == [
        {"at_seconds": 0.0, "chord": "C major"},
        {"at_seconds": 8.0, "chord": "C major"},
    ]


def test_analysis_rejects_nonfinite_duration_before_loading(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    import pytest

    source = tmp_path / "invalid-duration.wav"
    source.touch()

    def unexpected_load(*args, **kwargs):
        pytest.fail("Audio must not be loaded when its duration is invalid")

    librosa_stub = SimpleNamespace(load=unexpected_load)
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)
    for duration in (float("nan"), float("inf"), float("-inf")):
        librosa_stub.get_duration = lambda *, path: duration
        with pytest.raises(ValueError, match="no playable duration"):
            music_analysis.analyze_music(str(source))


def test_analysis_rejects_nonfinite_samples_before_feature_extraction(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    import pytest

    source = tmp_path / "invalid-samples.wav"
    source.touch()

    def unexpected_harmonic(*args, **kwargs):
        pytest.fail("Invalid samples must not reach harmonic processing")

    librosa_stub = SimpleNamespace(
        get_duration=lambda *, path: 1.0,
        effects=SimpleNamespace(harmonic=unexpected_harmonic),
    )
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)
    for invalid_sample in (float("nan"), float("inf"), float("-inf")):
        signal = np.array([0.1, invalid_sample, -0.1])
        librosa_stub.load = lambda *args, **kwargs: (signal, 22050)
        with pytest.raises(ValueError, match="nonfinite samples"):
            music_analysis.analyze_music(str(source))
