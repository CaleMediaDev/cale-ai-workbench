"""Bounded local training with held-out evaluation and separate model artifacts."""

import csv
import hashlib
import io
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent

# Reviewed public sources are pinned to immutable bytes. A catalogue entry is
# evidence about a particular dataset, not permission to train on any repo file.
GITHUB_DATASETS = {
    "clinc150": {
        "repository": "clinc/oos-eval",
        "commit": "828f8093932c8fe6ca7936c3d2e52903b1c523de",
        "purpose": "intent classification and out-of-scope evaluation",
        "license": "CC-BY-3.0", "license_path": "LICENSE",
        "attribution": "Larson et al. (2019), An Evaluation Dataset for Intent Classification and Out-of-Scope Prediction",
        "files": {
            "LICENSE": "e6bc9e9c474700b708f568bac9e5a8a9bcb2b1dad53442f5ba449fcb848b8e76",
            "data/data_full.json": "36923c3705a59e08fe9c3883d8bc2dd966ef93e22cb78ac41171782a698d56e0"},
    },
}
MAX_DATASET_FILE_BYTES = 8 * 1024 * 1024
MAX_DATASET_BYTES = 32 * 1024 * 1024


class _GitHubDataRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if (parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com"
                or parsed.username is not None or parsed.port not in (None, 443)):
            raise ValueError("Dataset redirect left the public GitHub data host")
        return super().redirect_request(request, response, code, message, headers, new_url)


def _github_dataset_bytes(url, limit):
    """Read actual bytes with a cap; no account, archive extraction or script execution."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com"
            or parsed.username is not None or parsed.port not in (None, 443)):
        raise ValueError("Use an immutable public GitHub raw-file URL")
    request = Request(url, headers={"User-Agent": "Cale-AI-Workbench-datasets"})
    with build_opener(_GitHubDataRedirect()).open(request, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Training dataset file exceeds its download budget")
    return data


def training_dataset_catalog(purpose=""):
    """Return source metadata; catalogue lookup never downloads or trains."""
    words = set(re.findall(r"\w+", str(purpose).lower()))
    return [{"id": name, **json.loads(json.dumps(item))} for name, item in GITHUB_DATASETS.items()
            if not words or words.intersection(re.findall(r"\w+", name + " " + item["purpose"]))]


def _dataset_manifest(dataset_id, entry, size):
    return {"dataset_id": dataset_id, "repository": entry["repository"], "commit": entry["commit"],
            "source_url": "https://github.com/" + entry["repository"] + "/tree/" + entry["commit"],
            "license": entry["license"], "license_path": "files/" + entry["license_path"],
            "attribution": entry["attribution"], "files": dict(entry["files"]), "downloaded_bytes": size,
            "source_kind": "public_github", "code_executed": False, "trained": False}


def _cached_dataset(folder, dataset_id, entry):
    """Only exact catalogue bytes count as cache; never overwrite modified data."""
    def read(path, limit):
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError("Dataset cache points outside its folder")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Dataset cache exceeds its read budget")
        return data
    try:
        stored = json.loads(read(folder / "source.json", 64000))
        if any(stored.get(key) != value for key, value in (
                ("dataset_id", dataset_id), ("repository", entry["repository"]),
                ("commit", entry["commit"]), ("files", entry["files"]))):
            raise ValueError("Dataset cache does not match the reviewed source")
        size = 0
        for path, digest in entry["files"].items():
            data = read(folder / "files" / path, min(MAX_DATASET_FILE_BYTES, MAX_DATASET_BYTES - size))
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Dataset cache changed after download")
            size += len(data)
        return dict(_dataset_manifest(dataset_id, entry, size), dataset_folder=str(folder), cached=True)
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as error:
        raise ValueError("Existing dataset folder is modified or incomplete; preserve it and use a new empty folder") from error


def download_training_dataset(dataset_id, output_folder):
    """Automatically acquire a reviewed, pinned public dataset when a job needs it.

    Raw files and attribution remain beside the derived training files. This
    operation supplies data only: it does not train, activate, execute fetched
    code, or infer that a public benchmark matches the user's actual task.
    """
    import pc_offline

    if dataset_id not in GITHUB_DATASETS:
        raise ValueError("Choose a reviewed source from training_dataset_catalog")
    entry = GITHUB_DATASETS[dataset_id]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repository"]) or not re.fullmatch(
            r"[a-f0-9]{40}", entry["commit"]):
        raise ValueError("Dataset catalogue needs a repository and full commit hash")
    files = entry["files"]
    if not 1 <= len(files) <= 12 or entry["license_path"] not in files:
        raise ValueError("Dataset catalogue must retain its pinned license file")
    for path, digest in files.items():
        parts = path.split("/")
        if (not path or len(path) > 240 or any(part in {"", ".", ".."} or ":" in part or "\\" in part
                or part.endswith((" ", ".")) or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                for part in parts) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("Dataset catalogue contains an unsafe path or checksum")
    folder = Path(output_folder).resolve()
    if folder.exists() and any(folder.iterdir()):
        return _cached_dataset(folder, dataset_id, entry)
    if pc_offline.offline():
        raise RuntimeError("Dataset downloads wait until internet access is enabled")
    downloaded, size = {}, 0
    for path, digest in files.items():
        url = ("https://raw.githubusercontent.com/" + entry["repository"] + "/" + entry["commit"]
               + "/" + quote(path, safe="/"))
        data = _github_dataset_bytes(url, min(MAX_DATASET_FILE_BYTES, MAX_DATASET_BYTES - size))
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Dataset checksum differs from the reviewed source")
        downloaded[path] = data
        size += len(data)
    # Publish only after every download and checksum succeeds. An I/O failure
    # leaves inspectable partial data rather than deleting possibly edited files.
    folder.mkdir(parents=True, exist_ok=True)
    for path, data in downloaded.items():
        target = folder / "files" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(folder):
            raise ValueError("Dataset destination left its output folder")
        with target.open("xb") as stream:
            stream.write(data)
    manifest = _dataset_manifest(dataset_id, entry, size)
    with (folder / "source.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
    return dict(manifest, dataset_folder=str(folder), cached=False)



def _bounded_bytes(path, limit):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Training data exceeds its read budget")
    return data


def _text_key(text):
    return " ".join(text.casefold().split())


def normalize_training_dataset(dataset_id, dataset_folder, output_folder):
    """Prepare pinned CLINC splits without fitting or touching official test rows.

    Exact casefold/whitespace matches with test are removed from train/val;
    train also excludes original validation texts. This prevents exact-text
    leakage, not semantic/paraphrase overlap. Raw source files stay unchanged.
    """
    if dataset_id != "clinc150" or dataset_id not in GITHUB_DATASETS:
        raise ValueError("Only the reviewed CLINC split format is supported")
    source = Path(dataset_folder).resolve()
    entry = GITHUB_DATASETS[dataset_id]
    provenance = _cached_dataset(source, dataset_id, entry)
    raw = _bounded_bytes(source / "files/data/data_full.json", MAX_DATASET_FILE_BYTES)
    if hashlib.sha256(raw).hexdigest() != entry["files"]["data/data_full.json"]:
        raise ValueError("Dataset changed during normalization")
    official = json.loads(raw)
    names = ("train", "oos_train", "val", "oos_val", "test", "oos_test")
    if not isinstance(official, dict) or set(official) != set(names):
        raise ValueError("CLINC needs its six official train/val/test and OOS splits")
    rows = {}
    total = 0
    for split in names:
        values = official[split]
        if not isinstance(values, list):
            raise ValueError("Each CLINC split must be a list")
        total += len(values)
        if total > 50000:
            raise ValueError("CLINC row budget exceeded")
        rows[split] = []
        for index, item in enumerate(values):
            if (not isinstance(item, list) or len(item) != 2
                    or not all(isinstance(value,str) for value in item)
                    or not item[0].strip() or len(item[0]) > 4096
                    or not item[1].strip() or len(item[1]) > 128
                    or (split.startswith("oos_") and item[1] != "oos")):
                raise ValueError("Malformed CLINC text/label row")
            rows[split].append({"text":item[0], "label":item[1],
                                "source_split":split, "source_row":index})
    test = rows["test"] + rows["oos_test"]
    validation = rows["val"] + rows["oos_val"]
    training = rows["train"] + rows["oos_train"]
    test_keys = {_text_key(row["text"]) for row in test}
    val_keys = {_text_key(row["text"]) for row in validation}
    labels_by_text = {}
    for row in training:
        labels_by_text.setdefault(_text_key(row["text"]), set()).add(row["label"])
    filtered = {"train_overlap_test":0, "train_overlap_validation":0,
                "train_conflicting_labels":0, "train_duplicate":0, "validation_overlap_test":0}
    train, seen = [], set()
    for row in training:
        key = _text_key(row["text"])
        reason = ("train_overlap_test" if key in test_keys else
                  "train_overlap_validation" if key in val_keys else
                  "train_conflicting_labels" if len(labels_by_text[key]) > 1 else
                  "train_duplicate" if key in seen else "")
        if reason:
            filtered[reason] += 1
        else:
            train.append(row)
            seen.add(key)
    val = []
    for row in validation:
        if _text_key(row["text"]) in test_keys:
            filtered["validation_overlap_test"] += 1
        else:
            val.append(row)
    partitions = {"train":train, "validation":val, "test":test}
    payloads = {}
    columns = ["text","label","split","source_split","source_row"]
    combined = []
    for split, values in partitions.items():
        typed = [dict(row,split=split) for row in values]
        combined.extend(typed)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer,fieldnames=columns);writer.writeheader();writer.writerows(typed)
        payloads[split+".csv"] = buffer.getvalue().encode("utf-8")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer,fieldnames=columns);writer.writeheader();writer.writerows(combined)
    payloads["classifier.csv"] = buffer.getvalue().encode("utf-8")
    if sum(map(len,payloads.values())) > MAX_DATASET_BYTES:
        raise ValueError("Normalized dataset exceeds its write budget")
    manifest = {"format_version":1,"dataset_id":dataset_id,"source":{key:value for key,value in provenance.items() if key not in {"dataset_folder","cached"}},
                "raw_sha256":hashlib.sha256(raw).hexdigest(),
                "official_counts":{name:len(rows[name]) for name in names},
                "normalized_counts":{name:len(values) for name,values in partitions.items()},
                "filtered_rows":filtered,"official_test_untouched":True,
                "duplicate_policy":"casefold plus collapsed whitespace; train excludes original val and test; val excludes test",
                "files":{name:hashlib.sha256(data).hexdigest() for name,data in payloads.items()},
                "trained":False,"activated":False,
                "limitation":"Public intent benchmark only; not evidence of Cale tool-use or task success."}
    folder = Path(output_folder).resolve()
    report_bytes = json.dumps(manifest,sort_keys=True,indent=2).encode("utf-8")
    if folder.exists() and any(folder.iterdir()):
        if (folder/"normalization.json").is_symlink() or _bounded_bytes(folder/"normalization.json",64000) != report_bytes:
            raise ValueError("Normalized output changed; preserve it and choose a new folder")
        for name,digest in manifest["files"].items():
            path=folder/name
            if path.is_symlink() or hashlib.sha256(_bounded_bytes(path,MAX_DATASET_BYTES)).hexdigest()!=digest:
                raise ValueError("Normalized output changed; preserve it and choose a new folder")
        cached=True
    else:
        folder.mkdir(parents=True,exist_ok=True)
        for name,data in {**payloads,"normalization.json":report_bytes}.items():
            with (folder/name).open("xb") as stream:stream.write(data)
        cached=False
    return {**manifest,"dataset_path":str(folder/"classifier.csv"),
            "normalization_sha256":hashlib.sha256(report_bytes).hexdigest(),"cached":cached}

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
