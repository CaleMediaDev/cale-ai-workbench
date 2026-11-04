"""Bounded local training with held-out evaluation and separate model artifacts."""

import csv
import math
import threading
from array import array
from collections import Counter, OrderedDict
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
CLASSIFIER_MAX_TEXT_CHARS = 16384


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



def _classifier_rows(dataset_path,text_column,label_column):
    raw = _bounded_bytes(dataset_path,MAX_DATASET_BYTES)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"),newline=""))
    if not reader.fieldnames or not {text_column,label_column}.issubset(reader.fieldnames):
        raise ValueError("Dataset needs the chosen text and label columns")
    explicit = "split" in reader.fieldnames
    partitions = {"train":[],"validation":[],"test":[]}
    all_rows = []
    for number,row in enumerate(reader,1):
        if number>50000:
            raise ValueError("Classifier dataset exceeds 50000 rows")
        text,label = row.get(text_column),row.get(label_column)
        if not isinstance(text,str) or not isinstance(label,str):
            raise ValueError("Malformed classifier CSV row")
        text,label=text.strip(),label.strip()
        if not text or not label or len(text)>CLASSIFIER_MAX_TEXT_CHARS or len(label)>128:
            raise ValueError("Classifier rows need bounded nonempty text and labels")
        split=row.get("split") if explicit else "train"
        if split not in partitions:
            raise ValueError("Use train, validation or test split labels")
        partitions[split].append((text,label));all_rows.append((text,label))
    if explicit:
        keys={name:{_text_key(text) for text,_ in values} for name,values in partitions.items()}
        if (keys["train"] & (keys["validation"]|keys["test"]) or keys["validation"] & keys["test"]):
            raise ValueError("Exact text overlaps held-out splits; normalize before training")
        if not partitions["test"]:
            raise ValueError("Explicit split data needs an untouched test partition")
    unique = {}
    for text,label in partitions["train"]:
        key=_text_key(text)
        if key in unique and unique[key][1]!=label:
            raise ValueError("Identical training input has conflicting labels")
        unique.setdefault(key,(text,label))
    partitions["train"]=list(unique.values())
    return raw,partitions,explicit


def _classifier_provenance(path,raw):
    """Hash-bound normalization metadata is provenance, not task suitability."""
    result={"kind":"local_csv","dataset_sha256":hashlib.sha256(raw).hexdigest()}
    manifest=Path(path).parent/"normalization.json"
    if not manifest.exists():return result
    data=_bounded_bytes(manifest,64000);value=json.loads(data)
    if (not isinstance(value,dict) or value.get("format_version")!=1
            or value.get("dataset_id") not in GITHUB_DATASETS
            or not isinstance(value.get("files"),dict)
            or value["files"].get(Path(path).name)!=result["dataset_sha256"]):
        raise ValueError("Normalization metadata does not match the training CSV")
    entry=GITHUB_DATASETS[value["dataset_id"]]
    source=value.get("source",{})
    if (not isinstance(source,dict)
            or value.get("raw_sha256")!=entry["files"].get("data/data_full.json")
            or any(source.get(key)!=entry[key] for key in ("repository","commit","files","license"))):
        raise ValueError("Normalization source differs from the reviewed dataset")
    return {**result,"kind":"normalized_public_benchmark","dataset_id":value["dataset_id"],
            "repository":entry["repository"],"commit":entry["commit"],"license":entry["license"],
            "attribution":entry["attribution"],"source_files":dict(entry["files"]),
            "raw_sha256":value["raw_sha256"],"normalization_sha256":hashlib.sha256(data).hexdigest()}


def train_classifier(dataset_path,output_folder,text_column="text",label_column="label",labels=None):
    """Fit only training rows; report held-out/baseline/latency, never activate.

    Split-tagged normalized benchmarks preserve their official test partition.
    Legacy plain CSVs keep the existing stratified random holdout behavior.
    Public intent accuracy does not qualify a model to route actual Cale tasks.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score,balanced_accuracy_score,classification_report,f1_score
    from sklearn.model_selection import train_test_split

    raw,partitions,explicit=_classifier_rows(dataset_path,text_column,label_column)
    provenance=_classifier_provenance(dataset_path,raw)
    before_counts={name:len(values) for name,values in partitions.items()}
    if labels is not None:
        if (not isinstance(labels,list) or not 2<=len(labels)<=256
                or any(not isinstance(label,str) or not 0<len(label)<=128 for label in labels)
                or len(set(labels))!=len(labels)
                or not set(labels).issubset({label for _,label in partitions["train"]})):
            raise ValueError("Choose two to 256 distinct labels present in the training split")
        selected=set(labels)
        partitions={name:[row for row in values if row[1] in selected] for name,values in partitions.items()}
        if not partitions["test"] and explicit:
            raise ValueError("Selected labels have no untouched test rows")
    policy={"kind":"exact_label_subset" if labels is not None else "all_labels",
            "selected_labels":sorted(labels) if labels is not None else None,"applied_before_fit":True,
            "source_split_counts":before_counts,"retained_split_counts":{name:len(values) for name,values in partitions.items()},
            "limitation":"Official OOS examples do not establish coverage of omitted intents or general out-of-distribution inputs."}
    policy_hash=hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()
    counts=Counter(label for _,label in partitions["train"])
    if len(partitions["train"])<12 or not 2<=len(counts)<=256 or min(counts.values())<3:
        raise ValueError("Need at least 12 distinct training examples, two classes, and three per class")
    if not explicit:
        texts,labels=zip(*partitions["train"])
        train_x,test_x,train_y,test_y=train_test_split(texts,labels,
            test_size=max(len(counts),round(len(texts)*.2)),random_state=42,stratify=labels)
        partitions["train"]=list(zip(train_x,train_y));partitions["test"]=list(zip(test_x,test_y))
    train_x,train_y=zip(*partitions["train"])
    test_x,test_y=zip(*partitions["test"])
    counts=Counter(train_y)
    destination=Path(output_folder).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Choose a new, empty output folder")
    started=time.perf_counter()
    vectorizer=TfidfVectorizer(ngram_range=(1,2),max_features=20000,sublinear_tf=True)
    model=LogisticRegression(max_iter=1000,class_weight="balanced",random_state=42)
    model.fit(vectorizer.fit_transform(train_x),train_y)
    fitted=time.perf_counter()-started
    predicted=model.predict(vectorizer.transform(test_x))
    majority=max(sorted(counts),key=counts.__getitem__)
    report={"kind":"text_classifier","training_examples":len(train_x),"held_out_examples":len(test_x),
        "validation_examples":len(partitions["validation"]),"accuracy":accuracy_score(test_y,predicted),
        "macro_f1":f1_score(test_y,predicted,average="macro",zero_division=0),
        "balanced_accuracy":balanced_accuracy_score(test_y,predicted),
        "majority_baseline_accuracy":accuracy_score(test_y,[majority]*len(test_y)),
        "classification_report":classification_report(test_y,predicted,zero_division=0,output_dict=True),
        "classes":dict(counts),"fit_seconds":fitted,"data":provenance,
        "subset_policy":policy,"subset_policy_sha256":policy_hash,
        "evaluation_split":"provided_test" if explicit else "random_stratified_holdout",
        "activated":False,"limitation":"Intent benchmark performance only; not proof of Cale routing/tool-use or user task success."}
    if "oos" in counts:
        report["always_oos_baseline_accuracy"]=accuracy_score(test_y,["oos"]*len(test_y))
    if partitions["validation"]:
        val_x,val_y=zip(*partitions["validation"])
        report["validation_accuracy"]=accuracy_score(val_y,model.predict(vectorizer.transform(val_x)))
    artifact={"kind":"text_classifier","format_version":1,
        "feature_config":{"ngram_range":[1,2],"sublinear_tf":True,"lowercase":True,"norm":"l2"},
        "vocabulary":{k:int(v) for k,v in vectorizer.vocabulary_.items()},"idf":vectorizer.idf_.tolist(),
        "coef":model.coef_.tolist(),"intercept":model.intercept_.tolist(),"classes":model.classes_.tolist()}
    model_bytes=json.dumps(artifact,allow_nan=False).encode("utf-8")
    if len(model_bytes)>CLASSIFIER_MAX_BYTES:
        raise ValueError("Trained classifier exceeds the portable artifact budget")
    folder=fresh_folder(output_folder)
    with (folder/"model.json").open("xb") as stream:stream.write(model_bytes)
    # Benchmark the actual portable path, not sklearn reconstruction. Test text
    # is read only for evaluation; it never fits vocabulary/IDF/coefficients.
    probe=list(test_x[:min(20,len(test_x))])
    started=time.perf_counter();predict_classifier(folder,probe[0]);cold=time.perf_counter()-started
    timings=[]
    for text in probe:
        started=time.perf_counter();predict_classifier(folder,text);timings.append(time.perf_counter()-started)
    report["portable_latency"]={"cold_seconds":cold,"warm_median_seconds":sorted(timings)[len(timings)//2],
                                "samples":len(timings),"device":"cpu"}
    report["model_sha256"]=hashlib.sha256(model_bytes).hexdigest()
    evaluation=json.dumps(report,indent=2,allow_nan=False).encode("utf-8")
    with (folder/"evaluation.json").open("xb") as stream:stream.write(evaluation)
    registry={"format_version":1,"kind":"intent_classifier_candidate","status":"candidate","activated":False,
        "model_sha256":report["model_sha256"],"evaluation_sha256":hashlib.sha256(evaluation).hexdigest(),
        "dataset_sha256":provenance["dataset_sha256"],"data":provenance,
        "subset_policy":policy,"subset_policy_sha256":policy_hash,
        "purpose":"public_intent_benchmark" if provenance["kind"]=="normalized_public_benchmark" else "local_text_classification",
        "reason":"Evaluation is recorded; independent Cale suitability and held-out task tests are still required."}
    with (folder/"candidate.json").open("x",encoding="utf-8") as stream:json.dump(registry,stream,indent=2,allow_nan=False)
    return dict(report,model_folder=str(folder),candidate_registry=str(folder/"candidate.json"),status="candidate")


CLASSIFIER_MAX_BYTES = 96 * 1024 * 1024
CLASSIFIER_CACHE_LIMIT = 2
_CLASSIFIER_CACHE = OrderedDict()
_CLASSIFIER_LOCK = threading.RLock()


def _classifier_signature(path):
    info = path.stat()
    return (info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_ino)


def _classifier_number(value, *, idf=False):
    if type(value) not in (int,float) or not math.isfinite(value):
        raise ValueError("Classifier weights must be finite numbers")
    if (idf and not 1 <= value <= 1_000_000) or (not idf and abs(value) > 1e100):
        raise ValueError("Classifier weights exceed supported numerical bounds")
    return float(value)


def _load_classifier(model_folder):
    """Load safe JSON once per unchanged artifact, with a bounded process cache.

    Predictions report the hash actually loaded. No pickle, generated code,
    numpy, sklearn reconstruction or fitting is involved in inference.
    """
    path = (Path(model_folder)/"model.json").resolve()
    signature = _classifier_signature(path)
    if signature[0] > CLASSIFIER_MAX_BYTES:
        raise ValueError("Classifier artifact exceeds its read budget")
    key = str(path)
    with _CLASSIFIER_LOCK:
        cached = _CLASSIFIER_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            _CLASSIFIER_CACHE.move_to_end(key)
            return cached[1]
        raw = _bounded_bytes(path,CLASSIFIER_MAX_BYTES)
        if _classifier_signature(path) != signature:
            raise ValueError("Classifier artifact changed while loading")
        try:
            artifact = json.loads(raw)
            if not isinstance(artifact,dict) or artifact.get("kind") != "text_classifier":
                raise ValueError("Not a supported text classifier")
            vocabulary = artifact["vocabulary"]
            classes = artifact["classes"]
            if (not isinstance(vocabulary,dict) or not 1 <= len(vocabulary) <= 20000
                    or any(not isinstance(term,str) or not 0 < len(term) <= CLASSIFIER_MAX_TEXT_CHARS
                           or type(index) is not int for term,index in vocabulary.items())
                    or set(vocabulary.values()) != set(range(len(vocabulary)))):
                raise ValueError("Classifier vocabulary must use contiguous bounded indices")
            if (not isinstance(classes,list) or not 2 <= len(classes) <= 256
                    or any(not isinstance(label,str) or not 0 < len(label) <= 128 for label in classes)
                    or len(set(classes)) != len(classes)):
                raise ValueError("Classifier needs distinct bounded class labels")
            config = {"ngram_range":[1,2],"sublinear_tf":True,"lowercase":True,"norm":"l2"}
            if (type(artifact.get("format_version",1)) is not int or artifact.get("format_version",1) != 1
                    or json.dumps(artifact.get("feature_config",config),sort_keys=True) != json.dumps(config,sort_keys=True)):
                raise ValueError("Unsupported classifier feature configuration")
            idf,coef,intercept = artifact["idf"],artifact["coef"],artifact["intercept"]
            row_count = 1 if len(classes)==2 else len(classes)
            if (not isinstance(idf,list) or len(idf)!=len(vocabulary)
                    or not isinstance(coef,list) or len(coef)!=row_count
                    or not isinstance(intercept,list) or len(intercept)!=row_count
                    or any(not isinstance(row,list) or len(row)!=len(vocabulary) for row in coef)):
                raise ValueError("Classifier weight dimensions do not match its features/classes")
            loaded = {"vocabulary":vocabulary,"classes":tuple(classes),
                      "idf":array('d',(_classifier_number(v,idf=True) for v in idf)),
                      "coef":tuple(array('d',(_classifier_number(v) for v in row)) for row in coef),
                      "intercept":tuple(_classifier_number(v) for v in intercept),
                      "model_sha256":hashlib.sha256(raw).hexdigest()}
        except (KeyError,TypeError,OverflowError,RecursionError) as error:
            raise ValueError("Malformed classifier artifact") from error
        _CLASSIFIER_CACHE[key] = (signature,loaded)
        _CLASSIFIER_CACHE.move_to_end(key)
        while len(_CLASSIFIER_CACHE) > CLASSIFIER_CACHE_LIMIT:
            _CLASSIFIER_CACHE.popitem(last=False)
        return loaded


def predict_classifier(model_folder, text):
    """CPU-only portable TF-IDF/logistic inference; probabilities match training.

    Empty/OOV text uses intercept probabilities, as sklearn does. This predicts
    an intent label, not authorization to execute a tool or spend an allowance.
    """
    if not isinstance(text,str) or len(text)>CLASSIFIER_MAX_TEXT_CHARS:
        raise ValueError("Classifier text must be a string of at most 16384 characters")
    artifact = _load_classifier(model_folder)
    words = re.findall(r"(?u)\b\w\w+\b",text.lower())
    terms = words + [words[i]+" "+words[i+1] for i in range(len(words)-1)]
    vocabulary = artifact["vocabulary"]
    counts = Counter(vocabulary[term] for term in terms if term in vocabulary)
    values = {index:(1+math.log(count))*artifact["idf"][index] for index,count in counts.items()}
    norm = math.sqrt(sum(value*value for value in values.values()))
    if norm:
        values = {index:value/norm for index,value in values.items()}
    decisions = [bias+sum(row[index]*value for index,value in values.items())
                 for row,bias in zip(artifact["coef"],artifact["intercept"])]
    if len(artifact["classes"])==2:
        decision = decisions[0]
        if decision>=0:
            probability=1/(1+math.exp(-decision))
        else:
            exp=math.exp(decision);probability=exp/(1+exp)
        scores=[1-probability,probability]
    else:
        maximum=max(decisions)
        exp=[math.exp(value-maximum) for value in decisions]
        total=sum(exp);scores=[value/total for value in exp]
    best=max(range(len(scores)),key=scores.__getitem__)
    return {"label":artifact["classes"][best],
            "probabilities":dict(zip(artifact["classes"],scores)),
            "model_sha256":artifact["model_sha256"]}


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


# Opt-in public-data donation jobs: only this installed CPU trainer runs.
# Candidates stay inactive; experiment novelty is local to one PC.

import os
import sqlite3
import subprocess
import sys
import uuid
import importlib.metadata
from contextlib import closing
from urllib.parse import urlencode

JOB_ID = "model-clinc-utility-v1"
LABELS = ("calculator", "calendar", "calendar_update", "date", "definition",
          "reminder", "reminder_update", "time", "timer", "translate", "weather", "oos")
TIMEOUT = 300
THREADS = 2
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
ARTIFACTS = ("model.json", "evaluation.json", "candidate.json", "contribution.json")


def identity():
    """Bind data, label/gate policy and installed trainer bytes, not global Git HEAD."""
    import small_training
    entry = small_training.GITHUB_DATASETS["clinc150"]
    policy = {"job": JOB_ID, "dataset_id": "clinc150", "dataset": entry,
              "labels": list(LABELS), "timeout": TIMEOUT, "threads": THREADS,
              "gate": "accuracy>both-baselines+.05;macro_f1>=.70;not-routing-qualified"}
    policy["trainer_sha256"] = hashlib.sha256(Path(small_training.__file__).read_bytes()).hexdigest()
    policy["worker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    policy["python"] = sys.version.split()[0]
    policy["dependencies"] = {name: importlib.metadata.version(name) for name in ("scikit-learn", "numpy", "scipy")}
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return digest, policy


def consent(root):
    import donation
    value = donation.settings(root)
    return (value["enabled"] and value.get("model_training") is True
            and "local" in donation.permitted_sources(value, root))


def _connection(root):
    import donation
    conn = donation.connection(root)
    conn.execute("""CREATE TABLE IF NOT EXISTS model_experiments (
        fingerprint TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, started REAL NOT NULL,
        state TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0, folder TEXT NOT NULL DEFAULT '',
        qualified INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 1,
        publication_checked REAL NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS model_compute (
        run_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
        state TEXT NOT NULL, qualified INTEGER NOT NULL DEFAULT 0)""")
    conn.commit()
    return conn


def claim(root):
    """One CPU experiment/hour, durable exact-input dedup; code lanes stay available."""
    import donation
    if not consent(root):
        return None
    fingerprint, policy = identity()
    with closing(_connection(root)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        config = donation.settings(root)
        conn.execute("UPDATE model_experiments SET state='superseded' WHERE fingerprint<>? AND state='ready'", (fingerprint,))
        previous = conn.execute("SELECT state,started,attempts,run_id,folder,seconds,publication_checked FROM model_experiments WHERE fingerprint=?",
                                (fingerprint,)).fetchone()
        if consent(root) and previous and previous[0] == "ready" and previous[6] < time.time()-3600:
            conn.execute("UPDATE model_experiments SET publication_checked=? WHERE fingerprint=?", (time.time(), fingerprint))
            return {"fingerprint": fingerprint, "run_id": previous[3], "policy": policy,
                    "resume": {"folder": previous[4], "seconds": previous[5], "qualified": True}}
        if not consent(root) or conn.execute(
                "SELECT 1 FROM model_experiments WHERE started>?", (time.time() - 3600,)).fetchone():
            return None
        # Completed results are never repeated. Transient/cancelled/crashed
        # attempts may retry twice, only after the hour cooldown. A self-killing
        # child deadline is shorter than this lease, including parent crashes.
        if previous and (previous[0] not in {"running", "failed", "interrupted"} or previous[2] >= 3):
            return None
        if config["intensity"] == "slow" and conn.execute(
                "SELECT 1 FROM runs WHERE started>?", (time.time()-86400,)).fetchone():
            return None
        if config["intensity"] == "slow" and conn.execute("SELECT 1 FROM runs WHERE status='submitted'").fetchone():
            return None
        run_id = uuid.uuid4().hex
        conn.execute("INSERT INTO runs(id,started,status,job,resource,intensity,scope_sha) VALUES(?,?,?,?,?,?,?)",
                     (run_id, time.time(), "requested", JOB_ID, "local", config["intensity"], fingerprint))
        if previous and previous[0] == "running":
            conn.execute("UPDATE runs SET status='interrupted',updated=? WHERE id=? AND status='requested'",
                         (time.time(), previous[3]))
        conn.execute("""INSERT INTO model_experiments(fingerprint,run_id,started,state) VALUES(?,?,?,?)
            ON CONFLICT(fingerprint) DO UPDATE SET run_id=excluded.run_id,started=excluded.started,
            state='running',seconds=0,folder='',qualified=0,attempts=model_experiments.attempts+1""",
                     (fingerprint, run_id, time.time(), "running"))
        conn.execute("INSERT INTO model_compute(run_id,fingerprint,state) VALUES(?,?,'running')", (run_id, fingerprint))
    return {"fingerprint": fingerprint, "run_id": run_id, "policy": policy}


def qualified(report):
    """Benchmark-only screening; even a pass cannot authorize Cale routing/activation."""
    fields = ("accuracy", "macro_f1", "majority_baseline_accuracy", "always_oos_baseline_accuracy")
    if any(type(report.get(k)) not in (int, float) or not math.isfinite(report[k])
           or not 0 <= report[k] <= 1 for k in fields):
        return False
    return (type(report.get("held_out_examples")) is int and report["held_out_examples"] >= 100
            and report["accuracy"] > max(report["majority_baseline_accuracy"],
                                         report["always_oos_baseline_accuracy"]) + .05
            and report["macro_f1"] >= .70 and report.get("activated") is False)


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)


def _train(folder, dataset_folder):
    """Child entry point: pinned acquisition, official splits, known CPU trainer."""
    import small_training
    fingerprint, policy = identity()
    source = small_training.download_training_dataset("clinc150", dataset_folder)
    normalized = small_training.normalize_training_dataset("clinc150", source["dataset_folder"], folder / "data")
    report = small_training.train_classifier(normalized["dataset_path"], folder / "candidate", labels=list(LABELS))
    model_folder = folder / "candidate"
    hashes = {name: hashlib.sha256((model_folder / name).read_bytes()).hexdigest()
              for name in ARTIFACTS if name != "contribution.json"}
    # This receipt contains public provenance and policy, never local paths/text.
    _write_json(model_folder / "contribution.json", {
        "format_version": 1, "experiment_sha256": fingerprint, "policy": policy,
        "artifacts": hashes, "benchmark_qualified": qualified(report),
        "routing_qualified": False, "activated": False, "inference_tokens": 0,
        "limitation": "Public intent benchmark; no evidence of Cale tool-use or task success."})


def run_job(root, *, output_folder=None, dataset_folder=None, check_cancel=lambda: None):
    """Construct a local candidate only. No GitHub calls, playback or activation.

    The optional dataset folder is still verified against reviewed public hashes;
    it is not an arbitrary training-data argument. Parent owns the returned child.
    """
    check_cancel()
    if not consent(root):
        raise PermissionError("Model training requires explicit Local donation consent")
    fingerprint, _ = identity()
    folder = Path(output_folder) if output_folder else Path(root) / "LongTasks/ModelDonations" / (fingerprint + "-" + uuid.uuid4().hex[:12])
    if folder.exists():
        raise ValueError("Preserve existing experiment; choose a new empty output folder")
    folder.mkdir(parents=True)
    dataset = Path(dataset_folder) if dataset_folder else folder / "public-dataset"
    environment = dict(os.environ)
    environment.update({key: str(THREADS) for key in
                        ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")})
    start = time.monotonic()
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(folder.resolve()),
                                str(dataset.resolve())], cwd=ROOT, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        while process.poll() is None:
            check_cancel()
            if not consent(root):
                raise InterruptedError("Model donation consent was withdrawn")
            if time.monotonic() - start >= TIMEOUT:
                raise TimeoutError("CPU experiment exceeded its 300 second budget")
            time.sleep(.1)
        if process.returncode:
            raise RuntimeError("Known CPU trainer failed; candidate was not submitted")
        check_cancel()
        if not consent(root):
            raise InterruptedError("Model donation consent was withdrawn")
        artifacts = review_artifacts(folder / "candidate", fingerprint)
        receipt = json.loads(artifacts["contribution.json"])
        return {"folder": str(folder / "candidate"), "seconds": time.monotonic()-start,
                "qualified": receipt["benchmark_qualified"], "fingerprint": fingerprint}
    except BaseException as error:
        error.worker_seconds = max(0, time.monotonic()-start)
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def review_artifacts(folder, fingerprint):
    """Whitelist bounded JSON bytes, binding exact public provenance before upload."""
    folder = Path(folder)
    result, total = {}, 0
    for name in ARTIFACTS:
        path = folder / name
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Model artifact left its owned folder")
        with path.open("rb") as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        total += len(raw)
        if total > MAX_ARTIFACT_BYTES:
            raise ValueError("Model contribution exceeds its upload budget")
        def finite_float(text):
            value = float(text)
            if not math.isfinite(value):
                raise ValueError("Nonfinite JSON")
            return value
        value = json.loads(raw, parse_float=finite_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        if not isinstance(value, dict):
            raise ValueError("Model artifacts must be JSON objects")
        result[name] = raw.decode("utf-8")
    receipt = json.loads(result["contribution.json"])
    current, policy = identity()
    if (fingerprint != current or receipt.get("experiment_sha256") != current
            or receipt.get("policy") != policy or receipt.get("activated") is not False
            or receipt.get("routing_qualified") is not False):
        raise ValueError("Candidate provenance does not match the installed experiment")
    for name in ARTIFACTS[:-1]:
        if receipt.get("artifacts", {}).get(name) != hashlib.sha256(result[name].encode()).hexdigest():
            raise ValueError("Candidate artifacts changed after evaluation")
    report = json.loads(result["evaluation.json"])
    candidate = json.loads(result["candidate.json"])
    model = json.loads(result["model.json"])
    import small_training
    # Reuse the actual portable weight/shape validator without inference. Its
    # loaded digest must bind the exact captured upload bytes, including races.
    loaded = small_training._load_classifier(folder)
    if loaded["model_sha256"] != receipt["artifacts"]["model.json"]:
        raise ValueError("Model changed during portable validation")
    data = report.get("data", {})
    subset = report.get("subset_policy", {})
    expected_source = policy["dataset"]
    if (report.get("model_sha256") != receipt["artifacts"]["model.json"]
            or candidate.get("model_sha256") != receipt["artifacts"]["model.json"]
            or candidate.get("evaluation_sha256") != receipt["artifacts"]["evaluation.json"]
            or candidate.get("activated") is not False or model.get("kind") != "text_classifier"
            or receipt.get("benchmark_qualified") is not qualified(report)):
        raise ValueError("Model candidate/evaluation bindings are invalid")
    if (report.get("evaluation_split") != "provided_test"
            or data.get("kind") != "normalized_public_benchmark" or data.get("dataset_id") != "clinc150"
            or any(data.get(key) != expected_source[key] for key in ("repository", "commit", "license"))
            or data.get("source_files") != expected_source["files"]
            or candidate.get("data") != data or candidate.get("subset_policy") != subset
            or subset.get("kind") != "exact_label_subset" or subset.get("selected_labels") != sorted(LABELS)
            or subset.get("applied_before_fit") is not True
            or set(model.get("classes", [])) != set(LABELS)):
        raise ValueError("Candidate needs reviewed public source and unchanged official held-out policy")
    return result


def complete(root, claim_value, result=None, state="failed"):
    """Record compute independently: never manufacture inference token receipts."""
    import donation
    seconds = (result or {}).get("seconds", 0)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Worker time must be a finite non-negative measurement")
    with closing(_connection(root)) as conn, conn:
        conn.execute("""INSERT INTO model_compute(run_id,fingerprint,seconds,state,qualified) VALUES(?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET seconds=MAX(seconds,excluded.seconds),
            state=excluded.state,qualified=excluded.qualified""",
                     (claim_value["run_id"], claim_value["fingerprint"], seconds, state,
                      int((result or {}).get("qualified", False))))
        conn.execute("UPDATE model_experiments SET state=?,seconds=?,folder=?,qualified=?,publication_checked=? WHERE fingerprint=? AND run_id=?",
                     (state, seconds, (result or {}).get("folder", ""),
                      int((result or {}).get("qualified", False)), time.time(), claim_value["fingerprint"], claim_value["run_id"]))
    donation.finish(root, claim_value["run_id"], state)


def summary(root):
    """CPU seconds are wall time in the bounded worker, never inferred LLM tokens."""
    # The UI must never migrate a ledger, wait on a writer or stop its poll loop.
    path = Path(root) / "LongTasks/donations.db"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.01)) as conn:
            row = conn.execute("SELECT COUNT(*),COALESCE(SUM(qualified),0) FROM model_experiments").fetchone()
            seconds = conn.execute("SELECT COALESCE(SUM(seconds),0) FROM model_compute").fetchone()[0]
    except (OSError, sqlite3.Error):
        return None  # Caller keeps the last visible snapshot while unavailable.
    return {"experiments": row[0], "worker_seconds": seconds, "screened_candidates": row[1]}


def publish_candidate(root, experiment, trained, login, check_cancel):
    """Retry a draft without retraining or duplicating an uncertain prior POST.

    Only four captured, validated JSON files can enter a new branch. An existing
    branch is reused only after its single-commit delta matches their blob SHAs.
    """
    import donation
    import github_account
    def guard():
        check_cancel()
        if not consent(root) or (github_account.account() or "").casefold() != login.casefold():
            raise InterruptedError("Model contribution consent/account changed")
    guard()
    artifacts = review_artifacts(trained["folder"], experiment["fingerprint"])
    if json.loads(artifacts["contribution.json"])["benchmark_qualified"] is not True:
        raise ValueError("Only screened candidates may be proposed")
    branch = "cale-donation-" + experiment["run_id"][:16]
    marker = "<!-- cale-model-experiment:" + experiment["fingerprint"] + " -->"
    pulls = donation.gh_api(f"repos/{donation.UPSTREAM}/pulls?" + urlencode(
        {"state": "all", "head": login + ":" + branch, "base": "main", "per_page": 2}))
    if not isinstance(pulls, list) or len(pulls) > 1:
        raise ValueError("Ambiguous model contribution publication")
    if pulls:
        pull = pulls[0]
        if (pull.get("head", {}).get("ref") != branch
                or pull.get("head", {}).get("repo", {}).get("owner", {}).get("login", "").casefold() != login.casefold()
                or pull.get("base", {}).get("repo", {}).get("full_name") != donation.UPSTREAM
                or marker not in (pull.get("body") or "")
                or not str(pull.get("html_url", "")).startswith("https://github.com/" + donation.UPSTREAM + "/pull/")):
            raise ValueError("Existing model PR does not match this experiment")
        guard()
        return pull["html_url"]
    paths = {f"ModelContributions/{experiment['fingerprint']}/{name}": body for name, body in artifacts.items()}
    target = donation.UPSTREAM if login.casefold() == donation.UPSTREAM.split("/")[0].casefold() else login + "/cale-ai-workbench"
    # Matching refs returns [] for an absent branch (unlike a 404 whose reason
    # the CLI intentionally redacts). The contributor fork may not exist yet;
    # create/verify it first, as in code donations.
    if target != donation.UPSTREAM:
        fork = donation.gh_api(f"repos/{donation.UPSTREAM}/forks", {"default_branch_only": True})
        if fork.get("full_name", "").casefold() != target.casefold() or not fork.get("fork"):
            raise ValueError("Model contribution fork does not match donor")
        metadata = donation.gh_api(f"repos/{target}")
        if metadata.get("parent", {}).get("full_name", "").casefold() != donation.UPSTREAM.casefold():
            raise ValueError("Model fork is not ready or has a different parent")
    guard()
    refs = donation.gh_api(f"repos/{target}/git/matching-refs/heads/{branch}")
    summary_text = (marker + "\nCPU-trained public-data intent classifier. Inactive and not routing qualified. "
                    "Untouched official held-out evaluation and public provenance are included. Independent owner review required.")
    validation = ("Known bounded CPU trainer; public pinned data only. Held-out benchmark screening passed. "
                  "No inference tokens, generated scripts or automatic activation.")
    if not isinstance(refs, list) or len(refs) > 1:
        raise ValueError("Ambiguous model contribution branch")
    if refs:
        ref = refs[0]
        if ref.get("ref") != "refs/heads/" + branch:
            raise ValueError("Different branch matched model contribution")
        sha = ref.get("object", {}).get("sha", "")
        import re
        if not re.fullmatch("[a-f0-9]{40}", sha):
            raise ValueError("Invalid model branch revision")
        commit = donation.gh_api(f"repos/{target}/git/commits/{sha}")
        parents = commit.get("parents", [])
        if len(parents) != 1 or not re.fullmatch("[a-f0-9]{40}", parents[0].get("sha", "")):
            raise ValueError("Unexpected model contribution history")
        comparison = donation.gh_api(f"repos/{target}/compare/{parents[0]['sha']}...{sha}")
        files = comparison.get("files", [])
        expected = {path: hashlib.sha1(b"blob " + str(len(body.encode())).encode() + b"\0" + body.encode()).hexdigest()
                    for path, body in paths.items()}
        if (comparison.get("total_commits") != 1 or len(files) != len(expected)
                or any(file.get("status") != "added" or expected.get(file.get("filename")) != file.get("sha") for file in files)):
            raise ValueError("Existing model branch differs from the validated candidate")
        guard()
        result = donation.gh_api(f"repos/{donation.UPSTREAM}/pulls", {
            "title": "[Donation] Evaluate a public-data intent classifier",
            "body": "<!-- cale-donation:" + JOB_ID + " -->\n" + summary_text + "\n\n" + validation,
            "head": login + ":" + branch, "base": "main", "draft": True, "maintainer_can_modify": False})
        return result["html_url"]
    commit = donation.gh_api(f"repos/{donation.UPSTREAM}/commits/main")
    return donation.open_proposal(login, experiment["run_id"], {"id": JOB_ID}, commit,
                                 {"title": "Evaluate a public-data intent classifier", "summary": summary_text},
                                 paths, guard, validation_summary=validation)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        raise SystemExit("Use the installed bounded experiment worker")
    # Parent loss must not leave synchronous sklearn work running indefinitely.
    watchdog = threading.Timer(TIMEOUT, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        _train(Path(sys.argv[2]), Path(sys.argv[3]))
    finally:
        watchdog.cancel()
