"""Opt-in public-data CPU experiments; candidates never become active models.

Only this installed trainer runs, never donor/model-supplied Python. Novelty is
local to this installation, not a claim of distributed experiment coordination.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
import threading
import importlib.metadata
from contextlib import closing
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
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
