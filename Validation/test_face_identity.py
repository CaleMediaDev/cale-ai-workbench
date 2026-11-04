"""Match synthetic vectors without loading camera models or personal data."""

import pytest

import face_identity


@pytest.mark.parametrize("sample", [[float("nan"), 0], [float("inf"), 0], ["bad", 0], 42, [[1], [0]]])
def test_invalid_saved_embedding_does_not_identify_or_abort(monkeypatch, sample):
    monkeypatch.setattr(face_identity, "_known", lambda: {"Invalid": [sample], "Valid": [[1.0, 0.0]]})
    model = face_identity.FaceModel.__new__(face_identity.FaceModel)
    assert model.identify([1.0, 0.0]) == "Valid"


@pytest.mark.parametrize("feature", [[float("nan"), 0], [float("inf"), 0], ["bad", 0], [[1], [0]], []])
def test_invalid_candidate_is_not_identified(monkeypatch, feature):
    monkeypatch.setattr(face_identity, "_known", lambda: {"Someone": [[1.0, 0.0]]})
    model = face_identity.FaceModel.__new__(face_identity.FaceModel)
    assert model.identify(feature) is None


def test_valid_ambiguous_match_remains_unidentified(monkeypatch):
    monkeypatch.setattr(face_identity, "_known", lambda: {"First": [[1.0, 0.0]], "Second": [[0.99, 0.01]]})
    model = face_identity.FaceModel.__new__(face_identity.FaceModel)
    assert model.identify([1.0, 0.0]) is None


def test_overflowing_similarity_is_not_a_confident_match(monkeypatch):
    monkeypatch.setattr(face_identity, "_known", lambda: {"Someone": [[1e308, 1e308]]})
    model = face_identity.FaceModel.__new__(face_identity.FaceModel)
    assert model.identify([1e308, 1e308]) is None
