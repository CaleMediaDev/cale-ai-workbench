"""Saved classifier predictions must use finite numeric model data."""

import json

import pytest

import small_training


@pytest.mark.parametrize("field,value", [
    ("coef", [[float("nan"), 1]]),
    ("coef", [[float("inf"), 1]]),
    ("intercept", [float("nan")]),
    ("intercept", [float("-inf")]),
])
def test_nonfinite_saved_weights_do_not_return_a_label(tmp_path, field, value):
    artifact = {"kind": "text_classifier", "vocabulary": {"good": 0, "bad": 1},
                "idf": [1, 1], "coef": [[1, -1]], "intercept": [0],
                "classes": ["negative", "positive"]}
    artifact[field] = value
    (tmp_path / "model.json").write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError, match="finite"):
        small_training.predict_classifier(tmp_path, "good")


def test_valid_saved_classifier_retains_probability_distribution(tmp_path):
    artifact = {"kind": "text_classifier", "vocabulary": {"good": 0, "bad": 1},
                "idf": [1, 1], "coef": [[1, -1]], "intercept": [0],
                "classes": ["negative", "positive"]}
    (tmp_path / "model.json").write_text(json.dumps(artifact), encoding="utf-8")
    result = small_training.predict_classifier(tmp_path, "good")
    assert result["label"] == "positive"
    assert sum(result["probabilities"].values()) == pytest.approx(1)


def test_finite_weights_that_overflow_do_not_return_nan_probabilities(tmp_path):
    artifact = {"kind": "text_classifier", "vocabulary": {"good": 0, "bad": 1},
                "idf": [1, 1], "coef": [[1e308, 0], [1e308, 0], [-1e308, 0]],
                "intercept": [1e308, 1e308, -1e308], "classes": ["a", "b", "c"]}
    (tmp_path / "model.json").write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError, match="probabilities must be finite"):
        small_training.predict_classifier(tmp_path, "good")
