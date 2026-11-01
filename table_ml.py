"""Train and reuse a small, local CSV prediction model with safe JSON storage.

No pickle/joblib loading: saved models are numbers, categories and column names.
The holdout score is reported against a simple baseline before anyone relies on
predictions from new rows.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path


def _csv(path: str, target: str = "") -> tuple[list[str], list[dict]]:
    source = Path(path).expanduser().resolve(strict=True)
    if source.suffix.lower() != ".csv" or not source.is_file() or source.stat().st_size > 25_000_000:
        raise ValueError("Choose an existing CSV file under 25 MB.")
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        if not columns or len(columns) != len(set(columns)) or len(columns) > 100:
            raise ValueError("CSV needs 1 to 100 unique named columns.")
        if target and target not in columns:
            raise ValueError("Target column was not found in the CSV.")
        rows = []
        for row in reader:
            if len(rows) >= 100_000:
                raise ValueError("Use at most 100,000 CSV rows for local training.")
            if None in row:
                raise ValueError("A CSV row has more fields than the header.")
            rows.append(row)
    return columns, rows


def _new_file(path: str, suffix: str) -> Path:
    output = Path(path).expanduser().resolve()
    if output.suffix.lower() != suffix or output.exists():
        raise ValueError(f"Choose a new {suffix} output filename.")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _schema(rows: list[dict], columns: list[str]) -> dict:
    schema = {}
    for column in columns:
        values = [str(row.get(column) or "").strip() for row in rows]
        nonempty = [value for value in values if value]
        numeric = []
        for value in nonempty:
            try:
                number = float(value)
                if math.isfinite(number):
                    numeric.append(number)
            except ValueError:
                pass
        if nonempty and len(numeric) / len(nonempty) >= .95:
            median = statistics.median(numeric)
            scale = max(1.0, statistics.median(abs(value - median) for value in numeric))
            schema[column] = {"kind": "number", "median": median, "scale": scale}
        else:
            schema[column] = {"kind": "category"}
    return schema


def _features(rows: list[dict], schema: dict) -> list[dict]:
    output = []
    for row in rows:
        features = {}
        for column, spec in schema.items():
            value = str(row.get(column) or "").strip()
            if spec["kind"] == "number":
                try:
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError
                except ValueError:
                    number = spec["median"]
                features[column] = (number - spec["median"]) / spec["scale"]
            else:
                features[column] = value or "(missing)"
        output.append(features)
    return output


def train_table_model(csv_path: str, target_column: str, model_path: str,
                      problem_type: str = "auto") -> dict:
    """Fit a linear classifier or regressor and save portable numeric weights."""
    if problem_type not in ("auto", "classification", "regression"):
        raise ValueError("Problem type must be auto, classification or regression.")
    columns, rows = _csv(csv_path, target_column)
    rows = [row for row in rows if str(row.get(target_column) or "").strip()]
    if len(rows) < 20 or len(columns) < 2:
        raise ValueError("Training needs at least 20 labeled rows and one feature column.")
    output = _new_file(model_path, ".json")
    targets = [str(row[target_column]).strip() for row in rows]
    numeric_targets = []
    for value in targets:
        try:
            number = float(value)
            if math.isfinite(number):
                numeric_targets.append(number)
        except ValueError:
            pass
    if problem_type == "auto":
        problem_type = ("regression" if len(numeric_targets) == len(targets)
                        and len(set(targets)) > min(10, len(targets) // 4)
                        else "classification")
    if problem_type == "regression" and len(numeric_targets) != len(targets):
        raise ValueError("Regression target column must contain finite numbers only.")
    classes = sorted(set(targets)) if problem_type == "classification" else []
    if problem_type == "classification" and not 2 <= len(classes) <= 20:
        raise ValueError("Classification needs 2 to 20 distinct labels.")
    if classes and min(Counter(targets).values()) < 5:
        raise ValueError("Classification needs at least five examples of each label.")
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.metrics import accuracy_score, mean_absolute_error
    from sklearn.model_selection import train_test_split

    labels = targets if classes else numeric_targets
    stratify = (targets if classes and len(rows) // 5 >= len(classes) else None)
    train_ids, test_ids = train_test_split(
        list(range(len(rows))), test_size=.2, random_state=42, stratify=stratify)
    train_rows = [rows[index] for index in train_ids]
    test_rows = [rows[index] for index in test_ids]
    y_train = [labels[index] for index in train_ids]
    y_test = [labels[index] for index in test_ids]
    # Fit preprocessing on training rows only, so the reported holdout score
    # does not benefit from categories or numeric statistics in test rows.
    schema = _schema(train_rows, [column for column in columns if column != target_column])
    vectorizer = DictVectorizer(sparse=True)
    x_train = vectorizer.fit_transform(_features(train_rows, schema))
    x_test = vectorizer.transform(_features(test_rows, schema))
    if x_train.shape[1] > 10_000:
        raise ValueError("Too many distinct categories; remove ID/text columns first.")
    if classes:
        model = LogisticRegression(max_iter=1000, class_weight="balanced")
        model.fit(x_train, y_train)
        score = float(accuracy_score(y_test, model.predict(x_test)))
        baseline = max(Counter(y_train).values()) / len(y_train)
        metric = "holdout_accuracy"
    else:
        model = Ridge(alpha=1.0)
        model.fit(x_train, y_train)
        score = float(mean_absolute_error(y_test, model.predict(x_test)))
        median = statistics.median(y_train)
        baseline = float(mean_absolute_error(y_test, [median] * len(y_test)))
        metric = "holdout_mean_absolute_error"
    payload = {"format": "cale-table-model-v1", "problem_type": problem_type,
               "target_column": target_column, "schema": schema,
               "feature_names": vectorizer.get_feature_names_out().tolist(),
               "classes": model.classes_.tolist() if classes else [],
               "coefficients": model.coef_.tolist() if classes else [model.coef_.tolist()],
               "intercepts": model.intercept_.tolist() if classes else [float(model.intercept_)],
               "metric": metric, "holdout_score": score, "baseline_score": baseline,
               "trained_rows": len(rows), "holdout_rows": len(y_test)}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    better = score > baseline if classes else score < baseline
    return {"model_path": str(output), "problem_type": problem_type,
            metric: round(score, 4), "baseline": round(baseline, 4),
            "better_than_baseline": better, "trained_rows": len(rows),
            "note": "Use the holdout score before relying on predictions; small or biased data can mislead."}


def predict_table_model(model_path: str, csv_path: str, output_path: str) -> dict:
    """Apply only Cale's safe JSON coefficients; never execute saved code."""
    path = Path(model_path).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".json" or path.stat().st_size > 10_000_000:
        raise ValueError("Choose a Cale table-model JSON file under 10 MB.")
    model = json.loads(path.read_text(encoding="utf-8"))
    if model.get("format") != "cale-table-model-v1":
        raise ValueError("This is not a Cale table model.")
    columns, rows = _csv(csv_path)
    if not rows or any(column not in columns for column in model["schema"]):
        raise ValueError("Prediction CSV needs rows and the model's feature columns.")
    output = _new_file(output_path, ".csv")
    names = model["feature_names"]
    if len(names) > 10_000 or len(model["coefficients"]) > 20:
        raise ValueError("Saved model exceeds local prediction limits.")
    values = _features(rows, model["schema"])
    predictions = []
    for row in values:
        vector = {key: float(value) for key, value in row.items() if isinstance(value, float)}
        vector.update({f"{key}={value}": 1.0 for key, value in row.items() if isinstance(value, str)})
        logits = []
        for weights, bias in zip(model["coefficients"], model["intercepts"]):
            if len(weights) != len(names):
                raise ValueError("Saved model coefficients do not match its features.")
            logits.append(float(bias) + sum(float(weight) * vector.get(name, 0)
                                            for name, weight in zip(names, weights)))
        if model["problem_type"] == "regression":
            predictions.append(f"{logits[0]:.6g}")
        elif len(model["classes"]) == 2:
            predictions.append(model["classes"][int(logits[0] >= 0)])
        else:
            predictions.append(model["classes"][max(range(len(logits)), key=logits.__getitem__)])
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[*columns, "prediction"])
        writer.writeheader()
        for row, prediction in zip(rows, predictions):
            writer.writerow({**row, "prediction": prediction})
    return {"path": str(output), "rows": len(predictions), "target_column": model["target_column"]}
