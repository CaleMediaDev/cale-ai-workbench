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
            predict_table_model(str(model), str(prediction), str(output))
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


if __name__ == "__main__":
    unittest.main()
