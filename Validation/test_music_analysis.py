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
