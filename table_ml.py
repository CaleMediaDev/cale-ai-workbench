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
import tempfile
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
                normalized = (number - spec["median"]) / spec["scale"]
                if not math.isfinite(normalized):
                    raise ValueError(f"Numeric feature {column!r} overflowed during normalization.")
                features[column] = normalized
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
        # Repeated numeric codes (for example 1..12) are labels, even when
        # there are more than ten of them. Distinct or rarely repeated numbers
        # are better treated as continuous values by default.
        counts = Counter(targets)
        repeated_codes = 2 <= len(counts) <= 20 and min(counts.values()) >= 5
        problem_type = (
            "classification" if len(numeric_targets) != len(targets) or repeated_codes
            else "regression"
        )
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
        majority_label = Counter(y_train).most_common(1)[0][0]
        baseline = float(accuracy_score(y_test, [majority_label] * len(y_test)))
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
    with output.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    better = score > baseline if classes else score < baseline
    return {"model_path": str(output), "problem_type": problem_type,
            metric: round(score, 4), "baseline": round(baseline, 4),
            "better_than_baseline": better, "trained_rows": len(rows),
            "label_counts": dict(Counter(targets)) if classes else None,
            "note": (
                "This model did not beat the holdout baseline; do not rely on its predictions."
                if not better else
                "Use the holdout score before relying on predictions; small or biased data can mislead."
            )}


def predict_table_model(model_path: str, csv_path: str, output_path: str) -> dict:
    """Apply only Cale's safe JSON coefficients; never execute saved code."""
    path = Path(model_path).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".json" or path.stat().st_size > 10_000_000:
        raise ValueError("Choose a Cale table-model JSON file under 10 MB.")
    model = json.loads(path.read_text(encoding="utf-8"))
    names, coefficients, intercepts, classes = _checked_model(model)
    columns, rows = _csv(csv_path)
    if not rows or any(column not in columns for column in model["schema"]):
        raise ValueError("Prediction CSV needs rows and the model's feature columns.")
    output = _new_file(output_path, ".csv")
    import numpy as np
    from sklearn.feature_extraction import DictVectorizer

    vectorizer = DictVectorizer(sparse=True)
    vectorizer.vocabulary_ = {name: index for index, name in enumerate(names)}
    vectorizer.feature_names_ = names
    weights = np.asarray(coefficients, dtype=np.float64)
    biases = np.asarray(intercepts, dtype=np.float64)
    output_column = "prediction"
    suffix = 2
    while output_column in columns:
        output_column = f"prediction_{suffix}"
        suffix += 1
    # Keep partial batches private. Publish on the same filesystem without
    # replacing any destination created while prediction was running.
    with tempfile.TemporaryDirectory(prefix=".table-prediction-", dir=output.parent) as temporary:
        staged = Path(temporary) / output.name
        with staged.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[*columns, output_column])
            writer.writeheader()
            for start in range(0, len(rows), 4096):
                batch = rows[start:start + 4096]
                matrix = vectorizer.transform(_features(batch, model["schema"]))
                with np.errstate(over="ignore", invalid="ignore"):
                    scores = matrix @ weights.T + biases
                if not np.isfinite(scores).all():
                    raise ValueError("Prediction scores overflowed; cannot produce finite predictions.")
                if model["problem_type"] == "regression":
                    predictions = [f"{number:.6g}" for number in scores[:, 0]]
                elif len(classes) == 2:
                    predictions = [classes[int(number > 0)] for number in scores[:, 0]]
                else:
                    predictions = [classes[index] for index in np.argmax(scores, axis=1)]
                for row, prediction in zip(batch, predictions):
                    writer.writerow({**row, output_column: prediction})
        output.hardlink_to(staged)
    better = (
        model["holdout_score"] > model["baseline_score"]
        if model["problem_type"] == "classification"
        else model["holdout_score"] < model["baseline_score"]
    )
    return {"path": str(output), "rows": len(rows),
            "target_column": model["target_column"], "output_column": output_column,
            "holdout_metric": model["metric"],
            "holdout_score": round(model["holdout_score"], 4),
            "baseline_score": round(model["baseline_score"], 4),
            "better_than_baseline": better,
            "quality_warning": None if better else
            "This model did not beat its holdout baseline; review predictions before use."}


def _checked_model(model: dict) -> tuple[list[str], list[list[float]], list[float], list[str]]:
    """Reject malformed JSON before allocating a matrix or writing an output."""
    def finite_number(value):
        if not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    if not isinstance(model, dict) or model.get("format") != "cale-table-model-v1":
        raise ValueError("This is not a Cale table model.")
    schema = model.get("schema")
    names = model.get("feature_names")
    coefficients = model.get("coefficients")
    intercepts = model.get("intercepts")
    classes = model.get("classes")
    kind = model.get("problem_type")
    if not isinstance(schema, dict) or not 1 <= len(schema) <= 100 or not all(
        isinstance(column, str) and isinstance(spec, dict)
        and spec.get("kind") in ("number", "category")
        and (spec.get("kind") != "number" or (
            finite_number(spec.get("median"))
            and finite_number(spec.get("scale"))
            and spec["scale"] > 0))
        for column, spec in schema.items()
    ):
        raise ValueError("Saved model has an invalid feature schema.")
    if not isinstance(names, list) or not 1 <= len(names) <= 10_000 or not all(
        isinstance(name, str) for name in names
    ) or len(set(names)) != len(names):
        raise ValueError("Saved model has invalid feature names.")
    class_count = len(classes) if isinstance(classes, list) else 0
    rows_expected = 1 if kind == "regression" or class_count == 2 else class_count
    if (kind not in ("classification", "regression")
            or not isinstance(classes, list)
            or (kind == "classification" and not 2 <= class_count <= 20)
            or (kind == "regression" and classes)
            or not isinstance(coefficients, list)
            or len(coefficients) != rows_expected
            or not isinstance(intercepts, list)
            or len(intercepts) != rows_expected):
        raise ValueError("Saved model has invalid output classes or coefficients.")
    if not isinstance(model.get("target_column"), str) or not all(
        isinstance(label, str) for label in classes
    ) or len(set(classes)) != len(classes):
        raise ValueError("Saved model has invalid class labels.")
    expected_metric = ("holdout_accuracy" if kind == "classification"
                       else "holdout_mean_absolute_error")
    if model.get("metric") != expected_metric or not all(
        finite_number(model.get(key))
        for key in ("holdout_score", "baseline_score")
    ):
        raise ValueError("Saved model has an invalid holdout score.")
    if not all(
        isinstance(row, list) and len(row) == len(names)
        and all(finite_number(value) for value in row)
        for row in coefficients
    ) or not all(
        finite_number(value) for value in intercepts
    ):
        raise ValueError("Saved model contains invalid or nonfinite weights.")
    return names, coefficients, intercepts, classes
