"""End-to-end local CSV training and prediction checks."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from table_ml import predict_table_model, train_table_model


class TableMLTests(unittest.TestCase):
    def test_classification_and_prediction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["temperature", "weather", "label"])
                for value in range(20, 100):
                    writer.writerow([value, "sun" if value % 2 else "cloud", "hot" if value >= 60 else "cold"])
            model = root / "model.json"
            result = train_table_model(str(training), "label", str(model))
            self.assertEqual(result["problem_type"], "classification")
            self.assertTrue(result["better_than_baseline"])
            self.assertGreater(result["holdout_accuracy"], .8)
            data = json.loads(model.read_text(encoding="utf-8"))
            self.assertEqual(data["format"], "cale-table-model-v1")
            prediction = root / "new.csv"
            prediction.write_text("temperature,weather\n30,sun\n85,cloud\n", encoding="utf-8")
            output = root / "prediction.csv"
            response = predict_table_model(str(model), str(prediction), str(output))
            self.assertTrue(response["better_than_baseline"])
            self.assertIsNone(response["quality_warning"])
            with output.open(newline="", encoding="utf-8") as stream:
                self.assertEqual([row["prediction"] for row in csv.DictReader(stream)],
                                 ["cold", "hot"])

    def test_regression_and_prediction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["size", "price"])
                for value in range(25, 125):
                    writer.writerow([value, value * 12 + 50])
            model = root / "model.json"
            result = train_table_model(str(training), "price", str(model))
            self.assertEqual(result["problem_type"], "regression")
            self.assertTrue(result["better_than_baseline"])
            prediction = root / "new.csv"
            prediction.write_text("size\n75\n", encoding="utf-8")
            output = root / "prediction.csv"
            predict_table_model(str(model), str(prediction), str(output))
            with output.open(newline="", encoding="utf-8") as stream:
                estimated = float(next(csv.DictReader(stream))["prediction"])
            self.assertAlmostEqual(estimated, 950, delta=25)

    def test_multiclass_and_existing_prediction_column(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["signal", "label"])
                for repeat in range(30):
                    for label, signal in (("low", 1), ("middle", 10), ("high", 20)):
                        writer.writerow([signal + repeat / 100, label])
            model = root / "model.json"
            result = train_table_model(str(training), "label", str(model), "classification")
            self.assertGreater(result["holdout_accuracy"], .8)
            prediction = root / "new.csv"
            prediction.write_text("signal,prediction\n1,old\n10,old\n20,old\n", encoding="utf-8")
            output = root / "predicted.csv"
            response = predict_table_model(str(model), str(prediction), str(output))
            self.assertEqual(response["output_column"], "prediction_2")
            with output.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["prediction"] for row in rows], ["old"] * 3)
            self.assertEqual([row["prediction_2"] for row in rows],
                             ["low", "middle", "high"])

    def test_binary_prediction_at_zero_matches_logistic_regression(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "classification",
                "schema": {"signal": {"kind": "number", "median": 0, "scale": 1}},
                "feature_names": ["signal"], "coefficients": [[1]],
                "intercepts": [0], "classes": ["cold", "hot"],
                "target_column": "label", "metric": "holdout_accuracy",
                "holdout_score": 1, "baseline_score": .5,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            prediction.write_text("signal\n-1\n0\n1\n", encoding="utf-8")
            output = root / "predicted.csv"
            predict_table_model(str(model), str(prediction), str(output))
            with output.open(newline="", encoding="utf-8") as stream:
                labels = [row["prediction"] for row in csv.DictReader(stream)]
            # LogisticRegression chooses its first class for a zero score.
            self.assertEqual(labels, ["cold", "cold", "hot"])

    def test_prediction_rejects_numeric_normalization_overflow(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "regression",
                "schema": {"size": {"kind": "number", "median": -1e308, "scale": 1}},
                "feature_names": ["size"], "coefficients": [[1]],
                "intercepts": [0], "classes": [], "target_column": "value",
                "metric": "holdout_mean_absolute_error", "holdout_score": 1,
                "baseline_score": 2,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            prediction.write_text("size\n1e308\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overflowed during normalization"):
                predict_table_model(str(model), str(prediction),
                                    str(root / "predicted.csv"))

    def test_prediction_rejects_score_overflow(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            prediction = root / "new.csv"
            prediction.write_text("size\n2\n", encoding="utf-8")
            for kind, classes, metric in (
                ("regression", [], "holdout_mean_absolute_error"),
                ("classification", ["cold", "hot"], "holdout_accuracy"),
            ):
                with self.subTest(problem_type=kind):
                    model.write_text(json.dumps({
                        "format": "cale-table-model-v1", "problem_type": kind,
                        "schema": {"size": {"kind": "number", "median": 0, "scale": 1}},
                        "feature_names": ["size"], "coefficients": [[1e308]],
                        "intercepts": [0], "classes": classes, "target_column": "value",
                        "metric": metric, "holdout_score": .5, "baseline_score": .5,
                    }), encoding="utf-8")
                    output = root / f"{kind}.csv"
                    with self.assertRaisesRegex(ValueError, "Prediction scores overflowed"):
                        predict_table_model(str(model), str(prediction), str(output))
                    self.assertFalse(output.exists())

    def test_late_prediction_failure_removes_partial_output_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "regression",
                "schema": {"size": {"kind": "number", "median": 0, "scale": 1}},
                "feature_names": ["size"], "coefficients": [[1e308]],
                "intercepts": [0], "classes": [], "target_column": "value",
                "metric": "holdout_mean_absolute_error", "holdout_score": 1,
                "baseline_score": 2,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            output = root / "predicted.csv"
            # The first batch is written successfully; the next batch overflows.
            prediction.write_text("size\n" + "0\n" * 4096 + "2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Prediction scores overflowed"):
                predict_table_model(str(model), str(prediction), str(output))
            self.assertFalse(output.exists())

            prediction.write_text("size\n0\n", encoding="utf-8")
            result = predict_table_model(str(model), str(prediction), str(output))
            self.assertEqual(result["rows"], 1)
            with output.open(newline="", encoding="utf-8") as stream:
                self.assertEqual([row["prediction"] for row in csv.DictReader(stream)], ["0"])

    def test_malformed_model_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "regression",
                "schema": {"size": {"kind": "number", "median": 0, "scale": 1}},
                "feature_names": ["size"], "coefficients": [[float("inf")]],
                "intercepts": [0], "classes": [], "target_column": "value",
                "metric": "holdout_mean_absolute_error", "holdout_score": 1,
                "baseline_score": 2,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            prediction.write_text("size\n2\n", encoding="utf-8")
            output = root / "predicted.csv"
            with self.assertRaisesRegex(ValueError, "nonfinite"):
                predict_table_model(str(model), str(prediction), str(output))
            self.assertFalse(output.exists())

    def test_oversized_model_numbers_are_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            prediction = root / "new.csv"
            prediction.write_text("size\n2\n", encoding="utf-8")
            output = root / "predicted.csv"
            for field in ("median", "scale", "coefficients", "intercepts",
                          "holdout_score", "baseline_score"):
                with self.subTest(field=field):
                    data = {
                        "format": "cale-table-model-v1", "problem_type": "regression",
                        "schema": {"size": {"kind": "number", "median": 0, "scale": 1}},
                        "feature_names": ["size"], "coefficients": [[1]],
                        "intercepts": [0], "classes": [], "target_column": "value",
                        "metric": "holdout_mean_absolute_error", "holdout_score": 1,
                        "baseline_score": 2,
                    }
                    oversized = 10 ** 400
                    if field in ("median", "scale"):
                        data["schema"]["size"][field] = oversized
                    elif field == "coefficients":
                        data[field] = [[oversized]]
                    elif field == "intercepts":
                        data[field] = [oversized]
                    else:
                        data[field] = oversized
                    model.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "invalid|nonfinite"):
                        predict_table_model(str(model), str(prediction), str(output))
                    self.assertFalse(output.exists())

    def test_reused_weak_model_carries_quality_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "regression",
                "schema": {"size": {"kind": "number", "median": 0, "scale": 1}},
                "feature_names": ["size"], "coefficients": [[1]],
                "intercepts": [0], "classes": [], "target_column": "value",
                "metric": "holdout_mean_absolute_error", "holdout_score": 10,
                "baseline_score": 2,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            prediction.write_text("size\n2\n", encoding="utf-8")
            result = predict_table_model(str(model), str(prediction),
                                         str(root / "prediction.csv"))
            self.assertFalse(result["better_than_baseline"])
            self.assertIn("did not beat", result["quality_warning"])

    def test_classification_baseline_uses_holdout_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["feature", "label"])
                for index in range(31):
                    writer.writerow([index, "A"])
                for index in range(20):
                    writer.writerow([index + 100, "B"])
            result = train_table_model(str(training), "label", str(root / "model.json"))
            self.assertEqual(result["baseline"], .6364)

    def test_auto_detects_repeated_numeric_codes_as_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["feature", "code"])
                for code in range(1, 13):
                    for repeat in range(10):
                        writer.writerow([code * 100 + repeat, code])
            result = train_table_model(str(training), "code", str(root / "model.json"))
            self.assertEqual(result["problem_type"], "classification")
            self.assertEqual(result["label_counts"]["12"], 10)


if __name__ == "__main__":
    unittest.main()
