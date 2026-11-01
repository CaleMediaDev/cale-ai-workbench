"""Bounded local training with held-out evaluation and separate model artifacts."""

import csv
import json
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def fresh_folder(path):
    folder = Path(path).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("Choose a new, empty output folder.")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def train_classifier(
    dataset_path, output_folder, text_column="text", label_column="label"
):
    """Train a TF-IDF classifier and save held-out evaluation beside it."""
    from collections import Counter

    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, classification_report
    from sklearn.model_selection import train_test_split

    rows = {}
    with Path(dataset_path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            text, label = row[text_column].strip(), row[label_column].strip()
            key = " ".join(text.lower().split())
            if not text or not label:
                continue
            if key in rows and rows[key][1] != label:
                raise ValueError("Identical input has conflicting labels.")
            rows[key] = (text, label)
    if len(rows) < 12:
        raise ValueError(
            "Provide at least 12 distinct labeled examples, with at least 3 per class."
        )
    texts, labels = zip(*rows.values())
    counts = Counter(labels)
    if len(counts) < 2 or min(counts.values()) < 3:
        raise ValueError(
            "Need at least two classes and three distinct examples per class."
        )
    train_x, test_x, train_y, test_y = train_test_split(
        texts,
        labels,
        test_size=max(len(counts), round(len(rows) * 0.2)),
        random_state=42,
        stratify=labels,
    )
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2), max_features=20000, sublinear_tf=True
    )
    model = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)
    model.fit(vectorizer.fit_transform(train_x), train_y)
    predicted = model.predict(vectorizer.transform(test_x))
    report = {
        "kind": "text_classifier",
        "training_examples": len(train_x),
        "held_out_examples": len(test_x),
        "accuracy": accuracy_score(test_y, predicted),
        "classes": dict(counts),
        "classification_report": classification_report(
            test_y, predicted, zero_division=0, output_dict=True
        ),
        "limitation": "A small held-out set estimates performance only on similar examples; it does not prove general reliability.",
    }
    folder = fresh_folder(output_folder)
    artifact = {
        "kind": "text_classifier",
        "vocabulary": {k: int(v) for k, v in vectorizer.vocabulary_.items()},
        "idf": vectorizer.idf_.tolist(),
        "coef": model.coef_.tolist(),
        "intercept": model.intercept_.tolist(),
        "classes": model.classes_.tolist(),
    }
    (folder / "model.json").write_text(json.dumps(artifact), encoding="utf-8")
    (folder / "evaluation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return dict(report, model_folder=str(folder))


def predict_classifier(model_folder, text):
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    artifact = json.loads((Path(model_folder) / "model.json").read_text())
    if artifact.get("kind") != "text_classifier":
        raise ValueError("Not a text classifier.")
    vocabulary = artifact["vocabulary"]
    if len(vocabulary) > 20000:
        raise ValueError("Model exceeds the supported vocabulary size.")
    vectorizer = TfidfVectorizer(
        vocabulary=vocabulary, ngram_range=(1, 2), sublinear_tf=True
    )
    vectorizer.fit([" ".join(vocabulary)])
    vectorizer.idf_ = np.array(artifact["idf"])
    model = LogisticRegression()
    model.classes_ = np.array(artifact["classes"])
    model.coef_ = np.array(artifact["coef"])
    model.intercept_ = np.array(artifact["intercept"])
    model.n_features_in_ = len(vocabulary)
    scores = model.predict_proba(vectorizer.transform([text]))[0]
    return {
        "label": str(model.classes_[scores.argmax()]),
        "probabilities": {str(k): float(v) for k, v in zip(model.classes_, scores)},
    }


def train_adapter(dataset_path, output_folder, max_steps=60):
    """Train a small LoRA adapter from examples without changing the main model."""
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from media_worker import unload_local_models

    max_steps = min(max(int(max_steps), 1), 200)
    examples = []
    seen = set()
    for line in Path(dataset_path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row.get("input"), str) or not isinstance(
            row.get("output"), str
        ):
            raise ValueError("Each JSONL row needs input and output strings.")
        key = " ".join(row["input"].lower().split())
        if key in seen:
            raise ValueError(
                "Use distinct training inputs to avoid duplicate leakage into evaluation."
            )
        seen.add(key)
        examples.append(row)
    if len(examples) < 10:
        raise ValueError(
            "Provide at least 10 distinct examples; 50 or more is preferable."
        )
    random.Random(42).shuffle(examples)
    split = max(2, round(len(examples) * 0.2))
    heldout, training = examples[:split], examples[split:]
    folder = fresh_folder(output_folder)
    unload_local_models()
    torch.manual_seed(42)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    base = ROOT / "Media/models/trainer"
    tokenizer = AutoTokenizer.from_pretrained(
        str(base), local_files_only=True, trust_remote_code=False
    )
    model = AutoModelForCausalLM.from_pretrained(
        str(base),
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
    ).to(device)
    model = get_peft_model(
        model,
        LoraConfig(
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "v_proj"],
            task_type="CAUSAL_LM",
        ),
    )

    def encode(row):
        prefix = tokenizer.apply_chat_template(
            [{"role": "user", "content": row["input"]}],
            tokenize=False,
            add_generation_prompt=True,
        )
        input_ids = tokenizer(
            prefix + row["output"] + tokenizer.eos_token,
            return_tensors="pt",
            truncation=True,
            max_length=512,
        )["input_ids"].to(device)
        prefix_length = len(tokenizer(prefix)["input_ids"])
        if prefix_length >= input_ids.shape[1]:
            raise ValueError(
                "An input is too long; shorten examples so their answers fit in 512 tokens."
            )
        labels = input_ids.clone()
        labels[:, :prefix_length] = -100
        return {"input_ids": input_ids, "labels": labels}

    train_rows = [encode(row) for row in training]
    test_rows = [encode(row) for row in heldout]

    def loss():
        model.eval()
        with torch.no_grad():
            return sum(float(model(**row).loss) for row in test_rows) / len(test_rows)

    before = loss()
    model.train()
    optimizer = torch.optim.AdamW(
        (p for p in model.parameters() if p.requires_grad), lr=2e-4
    )
    started = time.monotonic()
    actual = 0
    for step in range(max_steps):
        optimizer.zero_grad()
        value = model(**train_rows[step % len(train_rows)]).loss
        if not torch.isfinite(value):
            raise RuntimeError(
                "Training loss became non-finite; no usable adapter saved."
            )
        value.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        actual += 1
        if time.monotonic() - started > 900:
            break
    after = loss()
    model.save_pretrained(folder, safe_serialization=True)
    tokenizer.save_pretrained(folder)
    report = {
        "kind": "language_adapter",
        "base": "Qwen/Qwen2.5-0.5B-Instruct",
        "training_examples": len(training),
        "held_out_examples": len(heldout),
        "steps": actual,
        "held_out_loss_before": before,
        "held_out_loss_after": after,
        "held_out_loss_improved": after < before,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "limitation": "Loss is not an accuracy or safety guarantee. Test on independent real examples before use. The main coding model was not changed.",
    }
    (folder / "evaluation.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return dict(report, model_folder=str(folder))


def ask_adapter(model_folder, prompt):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from media_worker import unload_local_models

    folder = Path(model_folder).resolve()
    if not (folder / "adapter_model.safetensors").is_file():
        raise ValueError("A safetensors adapter is required.")
    unload_local_models()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    base = ROOT / "Media/models/trainer"
    tokenizer = AutoTokenizer.from_pretrained(
        str(base), local_files_only=True, trust_remote_code=False
    )
    model = AutoModelForCausalLM.from_pretrained(
        str(base),
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
    ).to(device)
    model = PeftModel.from_pretrained(
        model, str(folder), is_trainable=False, local_files_only=True
    )
    tokens = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}],
        add_generation_prompt=True,
        return_tensors="pt",
    ).to(device)
    if tokens.shape[1] > 2048:
        raise ValueError("Adapter prompt exceeds 2048 tokens.")
    with torch.no_grad():
        generated = model.generate(
            input_ids=tokens,
            attention_mask=torch.ones_like(tokens),
            max_new_tokens=160,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )
    return {
        "answer": tokenizer.decode(
            generated[0, tokens.shape[1] :], skip_special_tokens=True
        ),
        "model_folder": str(folder),
    }
