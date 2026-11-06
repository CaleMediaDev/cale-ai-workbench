"""Fast checks for approximate harmony helpers; runtime audio is tested separately."""

import numpy as np

import music_analysis


def test_chord_detection_uses_pitch_classes():
    chroma = np.zeros(12)
    chroma[[0, 4, 7]] = [1, 0.8, 0.7]
    assert music_analysis._best_chord(chroma) == "C major"
    assert music_analysis._best_chord(np.zeros(12)) is None


def test_chord_detection_declines_equally_supported_triads():
    for pitch in range(12):
        chroma = np.zeros(12)
        chroma[pitch] = 1.0
        assert music_analysis._best_chord(chroma) is None

    # C and E alone support both C major and A minor equally.
    chroma = np.zeros(12)
    chroma[[0, 4]] = 1.0
    assert music_analysis._best_chord(chroma) is None

    # Complete triads still have a unique best match.
    for pitches, expected in (
        ([0, 4, 7], "C major"),
        ([0, 3, 7], "C minor"),
        ([0, 4, 7], "C major"), # Re-test this case to ensure it passes after logic change
    ):
        chroma = np.zeros(12)
        chroma[pitches] = 1.0
        assert music_analysis._best_chord(chroma) == expected


def test_chord_detection_declines_ties_with_floating_point_roundoff():
    chroma = np.zeros(12)
    # C major and A minor have equal support, but their sums use
    # different operand orders: 0.3 + 0.2 + 0.1 versus 0.1 + 0.3 + 0.2.
    chroma[[0, 4, 7, 9]] = [0.3, 0.2, 0.1, 0.1]
    for transposition in range(12):
        assert music_analysis._best_chord(np.roll(chroma, transposition)) is None


def test_key_estimate_declines_empty_input():
    assert music_analysis._best_key(np.zeros(12)) is None


def test_analysis_does_not_start_chord_segment_at_audio_endpoint(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    source = tmp_path / "four-seconds.wav"
    source.touch()
    sample_rate = 22050
    sample_count = 4 * sample_rate
    # Centered STFT includes one more frame than the number of full hops.
    chroma = np.zeros((12, 1 + sample_count // 512))
    chroma[[0, 4, 7], :-1] = 1.0
    chroma[[2, 7, 11], -1] = 1.0
    librosa_stub = SimpleNamespace(
        get_duration=lambda *, path: 4.0,
        load=lambda *args, **kwargs: (np.full(sample_count, 0.1), sample_rate),
        effects=SimpleNamespace(harmonic=lambda signal: signal),
        feature=SimpleNamespace(chroma_stft=lambda **kwargs: chroma),
        beat=SimpleNamespace(beat_track=lambda **kwargs: (0.0, np.array([]))),
    )
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)

    result = music_analysis.analyze_music(str(source))

    assert result["analyzed_seconds"] == 4.0
    assert result["chord_changes"] == [
        {"at_seconds": 0.0, "chord": "C major"},
    ]


def test_analysis_does_not_move_pre_boundary_frame_into_short_tail(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    source = tmp_path / "four-seconds-plus-one-sample.wav"
    source.touch()
    sample_rate = 22050
    sample_count = 4 * sample_rate + 1
    chroma = np.zeros((12, 1 + sample_count // 512))
    chroma[[0, 4, 7], :-1] = 1.0
    chroma[[2, 7, 11], -1] = 1.0
    # The last frame belongs before four seconds, not to the tiny tail.
    assert (chroma.shape[1] - 1) * 512 < 4 * sample_rate
    librosa_stub = SimpleNamespace(
        get_duration=lambda *, path: sample_count / sample_rate,
        load=lambda *args, **kwargs: (np.full(sample_count, 0.1), sample_rate),
        effects=SimpleNamespace(harmonic=lambda signal: signal),
        feature=SimpleNamespace(chroma_stft=lambda **kwargs: chroma),
        beat=SimpleNamespace(beat_track=lambda **kwargs: (0.0, np.array([]))),
    )
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)

    result = music_analysis.analyze_music(str(source))

    assert result["chord_changes"] == [
        {"at_seconds": 0.0, "chord": "C major"},
    ]


def test_analysis_includes_pitch_evidence_in_final_chroma_frame(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    source = tmp_path / "trailing-chord.wav"
    source.touch()
    sample_rate = 22050
    sample_count = 4 * sample_rate
    chroma = np.zeros((12, 1 + sample_count // 512))
    # Frame 172 is centered before four seconds, but round(4 * sr / 512)
    # is also 172: using it as an exclusive slice end loses this evidence.
    assert (chroma.shape[1] - 1) * 512 < sample_count
    chroma[[0, 4, 7], -1] = 1.0
    librosa_stub = SimpleNamespace(
        get_duration=lambda *, path: 4.0,
        load=lambda *args, **kwargs: (np.full(sample_count, 0.1), sample_rate),
        effects=SimpleNamespace(harmonic=lambda signal: signal),
        feature=SimpleNamespace(chroma_stft=lambda **kwargs: chroma),
        beat=SimpleNamespace(beat_track=lambda **kwargs: (0.0, np.array([]))),
    )
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)

    result = music_analysis.analyze_music(str(source))

    assert result["dominant_chord"] == "C major"
    assert result["chord_changes"] == [
        {"at_seconds": 0.0, "chord": "C major"},
    ]


def test_analysis_reports_only_finite_positive_tempos(tmp_path, monkeypatch):
    import json
    import sys
    from types import SimpleNamespace

    source = tmp_path / "tempo.wav"
    source.touch()
    librosa_stub = SimpleNamespace(
        get_duration=lambda *, path: 1.0,
        load=lambda *args, **kwargs: (np.full(22050, 0.1), 22050),
        effects=SimpleNamespace(harmonic=lambda signal: signal),
        feature=SimpleNamespace(chroma_stft=lambda **kwargs: np.zeros((12, 44))),
        beat=SimpleNamespace(),
    )
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)

    for tempo, expected in (
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (-120.0, None),
        (0.0, None),
        (120.0, 120.0),
    ):
        for estimate in (tempo, np.array([tempo])):
            librosa_stub.beat.beat_track = lambda **kwargs: (estimate, np.array([0, 20]))
            result = music_analysis.analyze_music(str(source))
            assert result["tempo_bpm"] == expected
            json.dumps(result, allow_nan=False)


def test_analysis_records_same_chord_returning_after_silence(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    source = tmp_path / "chord-gap.wav"
    source.touch()
    sample_rate = 22050
    # The silent interval spans actual four/eight-second frame boundaries.
    # Multiplying a rounded four-second interval puts one returning chord
    # frame inside the intended silent interval at this sample rate.
    def boundary(seconds):
        return round(seconds * sample_rate / 512)
    chroma = np.zeros((12, boundary(12)))
    for start, end in ((boundary(0), boundary(4)), (boundary(8), boundary(12))):
        chroma[[0, 4, 7], start:end] = 1.0

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


def test_key_estimate_declines_flat_pitch_evidence_without_invalid_arithmetic():
    with np.errstate(divide="raise", invalid="raise"):
        for strength in (0.25, 1.0):
            assert music_analysis._best_key(np.full(12, strength)) is None



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


def test_analysis_chord_segment_boundaries_do_not_accumulate_rounding_drift(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    source = tmp_path / "long-chord-change.wav"
    source.touch()
    sample_rate = 22050
    duration = 180
    change_frame = round(176 * sample_rate / 512)
    chroma = np.zeros((12, 1 + duration * sample_rate // 512))
    chroma[[0, 4, 7], :change_frame] = 1.0
    chroma[[2, 7, 11], change_frame:] = 1.0

    librosa_stub = SimpleNamespace(
        get_duration=lambda *, path: float(duration),
        load=lambda *args, **kwargs: (np.full(duration * sample_rate, 0.1), sample_rate),
        effects=SimpleNamespace(harmonic=lambda signal: signal),
        feature=SimpleNamespace(chroma_stft=lambda **kwargs: chroma),
        beat=SimpleNamespace(beat_track=lambda **kwargs: (0.0, np.array([]))),
    )
    monkeypatch.setitem(sys.modules, "librosa", librosa_stub)

    result = music_analysis.analyze_music(str(source))

    assert result["chord_changes"] == [
        {"at_seconds": 0.0, "chord": "C major"},
        {"at_seconds": 176.0, "chord": "G major"},
    ]
