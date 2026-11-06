"""End-to-end local CSV training and prediction checks."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from table_ml import predict_table_model, train_table_model


class TableMLTests(unittest.TestCase):
    def test_training_rejects_unnamed_feature_column(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["", "label"])
                for index in range(40):
                    writer.writerow([index, "hot" if index >= 20 else "cold"])
            model = root / "model.json"
            with self.assertRaisesRegex(ValueError, "unique named columns"):
                train_table_model(str(training), "label", str(model),
                                  "classification")
            self.assertFalse(model.exists())
            self.assertFalse(list(root.glob(".table-model-*")))

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

    def test_failed_model_save_cleans_up_and_allows_retry(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["signal", "label"])
                for index in range(40):
                    writer.writerow([index, "hot" if index >= 20 else "cold"])
            model = root / "model.json"

            def fail_after_partial_write(payload, stream, **kwargs):
                stream.write('{"format":')
                raise OSError("simulated model write failure")

            with patch("table_ml.json.dump", side_effect=fail_after_partial_write):
                with self.assertRaisesRegex(OSError, "simulated model write failure"):
                    train_table_model(str(training), "label", str(model),
                                      "classification")
            self.assertFalse(model.exists())
            self.assertFalse(list(root.glob(".table-model-*")))

            train_table_model(str(training), "label", str(model), "classification")
            saved = json.loads(model.read_text(encoding="utf-8"))
            self.assertEqual(saved["format"], "cale-table-model-v1")
            self.assertFalse(list(root.glob(".table-model-*")))
            prediction = root / "new.csv"
            prediction.write_text("signal\n5\n", encoding="utf-8")
            result = predict_table_model(str(model), str(prediction),
                                         str(root / "predicted.csv"))
            self.assertEqual(result["rows"], 1)

    def test_oversized_trained_model_is_not_published_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            model = root / "model.json"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["category", "label"])
                for index in range(40):
                    writer.writerow(["x" * 100_000 + str(index),
                                     "hot" if index % 2 else "cold"])
            self.assertLess(training.stat().st_size, 25_000_000)
            # Only 32 training coordinates, but their names and ownership
            # metadata together exceed the prediction loader's size limit.
            with self.assertRaisesRegex(ValueError, "Trained model exceeds 10 MB"):
                train_table_model(str(training), "label", str(model),
                                  "classification")
            self.assertFalse(model.exists())
            self.assertFalse(list(root.glob(".table-model-*")))

            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["category", "label"])
                for index in range(40):
                    writer.writerow(["red" if index % 2 else "blue",
                                     "hot" if index % 2 else "cold"])
            train_table_model(str(training), "label", str(model), "classification")
            prediction = root / "new.csv"
            prediction.write_text("category\nred\n", encoding="utf-8")
            result = predict_table_model(str(model), str(prediction),
                                         str(root / "predicted.csv"))
            self.assertEqual(result["rows"], 1)
            self.assertFalse(list(root.glob(".table-model-*")))

    def test_training_rejects_colliding_encoded_feature_names(self):
        cases = (
            (["color", "color=red", "label"],
             lambda index: ["red", index, "hot" if index % 2 else "cold"]),
            (["color", "color=red", "label"],
             lambda index: ["red=dark", "dark", "hot" if index % 2 else "cold"]),
        )
        for header, make_row in cases:
            with self.subTest(example=make_row(0)):
                with tempfile.TemporaryDirectory() as folder:
                    root = Path(folder)
                    training = root / "training.csv"
                    with training.open("w", newline="", encoding="utf-8") as stream:
                        writer = csv.writer(stream)
                        writer.writerow(header)
                        for index in range(40):
                            writer.writerow(make_row(index))
                    model = root / "model.json"
                    with self.assertRaisesRegex(ValueError, "Feature names collide"):
                        train_table_model(str(training), "label", str(model),
                                          "classification")
                    self.assertFalse(model.exists())

    def test_prediction_rejects_unseen_category_colliding_with_numeric_feature(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            # Training saw only color=blue, so its encoded names were distinct.
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "regression",
                "schema": {
                    "color": {"kind": "category"},
                    "color=red": {"kind": "number", "median": 0, "scale": 1},
                },
                "feature_names": ["color=blue", "color=red"],
                "coefficients": [[0, 1]], "intercepts": [0], "classes": [],
                "target_column": "value", "metric": "holdout_mean_absolute_error",
                "holdout_score": 1, "baseline_score": 2,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            prediction.write_text("color,color=red\nblue,2\n", encoding="utf-8")
            valid_output = root / "valid.csv"
            predict_table_model(str(model), str(prediction), str(valid_output))
            with valid_output.open(newline="", encoding="utf-8") as stream:
                self.assertEqual(next(csv.DictReader(stream))["prediction"], "2")

            prediction.write_text("color,color=red\nred,2\n", encoding="utf-8")
            output = root / "colliding.csv"
            with self.assertRaisesRegex(ValueError, "Feature names collide"):
                predict_table_model(str(model), str(prediction), str(output))
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".table-prediction-*")))

    def test_prediction_rejects_collision_with_training_only_category(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["color", "color=red", "label"])
                for index in range(40):
                    writer.writerow([
                        "red=dark" if index % 2 else "blue",
                        "light",
                        "hot" if index % 2 else "cold",
                    ])
            model = root / "model.json"
            train_table_model(str(training), "label", str(model), "classification")
            saved = json.loads(model.read_text(encoding="utf-8"))
            self.assertEqual(saved["feature_owners"]["color=red=dark"],
                             ["color", "red=dark"])
            prediction = root / "new.csv"
            prediction.write_text("color,color=red\nblue,light\n", encoding="utf-8")
            predict_table_model(str(model), str(prediction), str(root / "valid.csv"))

            # This row has no internal collision, but color=red='dark' would
            # reuse the coefficient trained for color='red=dark'.
            prediction.write_text("color,color=red\nblue,dark\n", encoding="utf-8")
            output = root / "colliding.csv"
            with self.assertRaisesRegex(ValueError, "Feature names collide"):
                predict_table_model(str(model), str(prediction), str(output))
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".table-prediction-*")))

    def test_prediction_rejects_category_collisions_across_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "regression",
                "schema": {
                    "color": {"kind": "category"},
                    "color=red": {"kind": "category"},
                },
                "feature_names": ["color=blue", "color=red=dark"],
                "coefficients": [[0, 1]], "intercepts": [0], "classes": [],
                "target_column": "value", "metric": "holdout_mean_absolute_error",
                "holdout_score": 1, "baseline_score": 2,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            output = root / "predicted.csv"
            # Neither row collides internally. Together, color='red=dark'
            # and color=red='dark' encode to the same coordinate.
            for repetitions in (1, 4096):
                with self.subTest(repetitions=repetitions):
                    prediction.write_text(
                        "color,color=red\n" + "red=dark,light\n" * repetitions
                        + "blue,dark\n", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Feature names collide"):
                        predict_table_model(str(model), str(prediction), str(output))
                    self.assertFalse(output.exists())
                    self.assertFalse(list(root.glob(".table-prediction-*")))

    def test_large_finite_feature_median_can_be_trained_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            training = root / "training.csv"
            with training.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["signal", "label"])
                for index in range(40):
                    writer.writerow(["1e308", "hot" if index % 2 else "cold"])
            model = root / "model.json"
            train_table_model(str(training), "label", str(model), "classification")
            saved = json.loads(model.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema"]["signal"],
                             {"kind": "number", "median": 1e308, "scale": 1.0})
            prediction = root / "new.csv"
            prediction.write_text("signal\n1e308\n", encoding="utf-8")
            output = root / "predicted.csv"
            result = predict_table_model(str(model), str(prediction), str(output))
            self.assertEqual(result["rows"], 1)
            with output.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertIn(rows[0]["prediction"], ["cold", "hot"])

    def test_large_finite_deviations_preserve_numeric_scale(self):
        from table_ml import _features, _schema

        rows = [{"signal": "-1e308"}, {"signal": "1e308"}]
        schema = _schema(rows, ["signal"])
        self.assertEqual(schema["signal"],
                         {"kind": "number", "median": 0.0, "scale": 1e308})
        # An infinite scale would silently turn both distinct values into zero
        # and would fail saved-model validation when reused for prediction.
        self.assertEqual(_features(rows, schema),
                         [{"signal": -1.0}, {"signal": 1.0}])

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

    def test_prediction_scales_before_overflowing_subtraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model.json"
            model.write_text(json.dumps({
                "format": "cale-table-model-v1", "problem_type": "regression",
                "schema": {"size": {"kind": "number", "median": -1e308,
                                    "scale": 1e308}},
                "feature_names": ["size"], "coefficients": [[1]],
                "intercepts": [0], "classes": [], "target_column": "value",
                "metric": "holdout_mean_absolute_error", "holdout_score": 1,
                "baseline_score": 2,
            }), encoding="utf-8")
            prediction = root / "new.csv"
            prediction.write_text("size\n-1e308\n0\n1e308\n", encoding="utf-8")
            output = root / "predicted.csv"
            result = predict_table_model(str(model), str(prediction), str(output))
            self.assertEqual(result["rows"], 3)
            with output.open(newline="", encoding="utf-8") as stream:
                values = [float(row["prediction"]) for row in csv.DictReader(stream)]
            self.assertEqual(values, [0.0, 1.0, 2.0])

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


def test_prediction_error_and_publication_preserve_concurrent_destination(tmp_path):
    from contextlib import contextmanager
    from unittest.mock import patch
    import pytest
    from sklearn.feature_extraction import DictVectorizer
    for outcome in ("failure", "success"):
        root = tmp_path / outcome
        root.mkdir()
        model, source, output = root / "model.json", root / "input.csv", root / "predicted.csv"
        model.write_text(json.dumps({
            "format": "cale-table-model-v1", "problem_type": "regression",
            "schema": {"size": {"kind": "number", "median": 0, "scale": 1}},
            "feature_names": ["size"], "coefficients": [[1]], "intercepts": [0], "classes": [],
            "target_column": "value", "metric": "holdout_mean_absolute_error", "holdout_score": 1, "baseline_score": 2}))
        source.write_text("size\n2\n")
        original_open, transform = Path.open, DictVectorizer.transform
        staged_paths = []
        @contextmanager
        def closed_writer_replacement(path, *args, **kwargs):
            staged_paths.append(path)
            try:
                with original_open(path, *args, **kwargs) as stream:
                    yield stream
            finally:
                # The original writer is closed; another process creates or
                # replaces the public destination before recovery/publication.
                with original_open(output, "wb") as stream:
                    stream.write(b"concurrent user replacement")
        def opening(path, *args, **kwargs):
            if path.name == output.name and args and args[0] == "x":
                return closed_writer_replacement(path, *args, **kwargs)
            return original_open(path, *args, **kwargs)
        def transforming(self, *args, **kwargs):
            if outcome == "failure":
                raise RuntimeError("prediction failed")
            return transform(self, *args, **kwargs)
        with patch.object(Path, "open", opening), patch.object(DictVectorizer, "transform", transforming):
            with pytest.raises(RuntimeError if outcome == "failure" else FileExistsError):
                predict_table_model(str(model), str(source), str(output))
        assert output.read_bytes() == b"concurrent user replacement"
        assert source.read_text() == "size\n2\n"
        assert staged_paths and all(path != output and not path.exists() for path in staged_paths)
        assert not list(root.glob(".table-prediction-*"))


def _ownership_collision_model(root):
    training = root / "training.csv"
    with training.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["color", "color=red", "label"])
        for index in range(40):
            writer.writerow(["red=dark" if index % 2 else "blue", "light",
                             "hot" if index % 2 else "cold"])
    model = root / "model.json"
    train_table_model(str(training), "label", str(model), "classification")
    prediction = root / "prediction.csv"
    prediction.write_text("color,color=red\nblue,dark\n", encoding="utf-8")
    return model, prediction


def test_training_coordinate_collision_rejected_without_metadata_assertions(tmp_path):
    model, prediction = _ownership_collision_model(tmp_path)
    output = tmp_path / "result.csv"
    with unittest.TestCase().assertRaisesRegex(ValueError, "Feature names collide"):
        predict_table_model(str(model), str(prediction), str(output))
    assert not output.exists()
    assert not list(tmp_path.glob(".table-prediction-*"))


def test_legacy_ambiguous_feature_ownership_fails_before_output_setup(tmp_path):
    model, prediction = _ownership_collision_model(tmp_path)
    saved = json.loads(model.read_text(encoding="utf-8"))
    saved.pop("feature_owners", None)
    model.write_text(json.dumps(saved), encoding="utf-8")
    before = model.read_bytes()
    output = tmp_path / "new-output-folder" / "result.csv"
    with unittest.TestCase().assertRaisesRegex(ValueError, "ambiguous.*retrain"):
        predict_table_model(str(model), str(prediction), str(output))
    assert not output.parent.exists()
    assert model.read_bytes() == before


def test_legacy_unambiguous_feature_ownership_remains_usable(tmp_path):
    training = tmp_path / "train.csv"
    with training.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["color", "size", "label"])
        for index in range(40):
            writer.writerow(["red" if index % 2 else "blue", index,
                             "hot" if index % 2 else "cold"])
    model = tmp_path / "model.json"
    train_table_model(str(training), "label", str(model), "classification")
    saved = json.loads(model.read_text(encoding="utf-8"))
    saved.pop("feature_owners")
    model.write_text(json.dumps(saved), encoding="utf-8")
    before = model.read_bytes()
    prediction = tmp_path / "predict.csv"
    prediction.write_text("color,size\nred,7\nblue,12\n", encoding="utf-8")
    output = tmp_path / "result.csv"
    assert predict_table_model(str(model), str(prediction), str(output))["rows"] == 2
    assert model.read_bytes() == before
    with output.open(encoding="utf-8") as stream:
        assert len(list(csv.DictReader(stream))) == 2


def test_malformed_feature_ownership_rejected_before_output_setup(tmp_path):
    model, prediction = _ownership_collision_model(tmp_path)
    saved = json.loads(model.read_text(encoding="utf-8"))
    invalid_maps = (None, [], {}, {"color=red=dark": ["absent", "dark"]})
    for index, owners in enumerate(invalid_maps):
        malformed = dict(saved, feature_owners=owners)
        model.write_text(json.dumps(malformed), encoding="utf-8")
        output = tmp_path / f"invalid-output-{index}" / "result.csv"
        with unittest.TestCase().assertRaisesRegex(ValueError, "invalid feature ownership"):
            predict_table_model(str(model), str(prediction), str(output))
        assert not output.parent.exists()
