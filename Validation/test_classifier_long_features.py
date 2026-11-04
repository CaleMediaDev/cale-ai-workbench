"""Portable training must support features produced from accepted text rows."""

import csv
import json
import math

import pytest

import small_training


@pytest.mark.parametrize("long_feature", ["a" * 9000, "a" * 4500 + " " + "b" * 4500])
def test_accepted_long_text_can_train_and_predict(tmp_path, long_feature):
    dataset = tmp_path / "examples.csv"
    with dataset.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["text", "label"])
        for label, theme in [("billing", "invoice payment"), ("travel", "flight airport")]:
            for index in range(8):
                writer.writerow([f"{theme} {long_feature} sample{index}", label])
    folder = tmp_path / "candidate"
    report = small_training.train_classifier(dataset, folder)
    artifact = json.loads((folder / "model.json").read_text(encoding="utf-8"))
    assert max(map(len, artifact["vocabulary"])) > 8192
    result = small_training.predict_classifier(folder, f"invoice payment {long_feature}")
    assert result["label"] == "billing"
    assert all(math.isfinite(value) for value in result["probabilities"].values())
    assert sum(result["probabilities"].values()) == pytest.approx(1)
    assert report["status"] == "candidate"
    assert (folder / "evaluation.json").is_file()
    assert (folder / "candidate.json").is_file()
