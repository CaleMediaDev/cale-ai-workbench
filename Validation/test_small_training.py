"""Exercise local text classifier training and reuse with real small datasets."""

import csv
import json

import pytest

import small_training


@pytest.mark.parametrize("columns,short_row", [
    (["text", "label"], ["Unlabeled draft"]),
    (["label", "text"], ["positive"]),
    (["text", "label"], ["Unlabeled draft", ""]),
])
def test_classifier_skips_unlabeled_short_rows(tmp_path, columns, short_row):
    dataset = tmp_path / "training.csv"
    with dataset.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerow(short_row)
        for label, subject in (("positive", "wonderful"), ("negative", "terrible")):
            for index in range(12):
                row = {"text": f"{subject} review example {index}", "label": label}
                writer.writerow([row[column] for column in columns])
    output = tmp_path / "model"
    report = small_training.train_classifier(str(dataset), str(output))
    assert report["training_examples"] + report["held_out_examples"] == 24
    assert report["classes"] == {"positive": 12, "negative": 12}
    assert json.loads((output / "model.json").read_text(encoding="utf-8"))["kind"] == "text_classifier"
    result = small_training.predict_classifier(output, "wonderful review")
    assert result["label"] == "positive"
    assert sum(result["probabilities"].values()) == pytest.approx(1)


def test_classifier_still_rejects_conflicting_labels(tmp_path):
    dataset = tmp_path / "training.csv"
    dataset.write_text("text,label\n\nUnlabeled\nSame input,yes\nsame input,no\n", encoding="utf-8")
    output = tmp_path / "model"
    with pytest.raises(ValueError, match="conflicting labels"):
        small_training.train_classifier(dataset, output)
    assert not output.exists()
