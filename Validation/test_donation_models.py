"""CPU contributions are explicit, bounded, independently accounted candidates."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import threading

import pytest

import donation
import donation_models as models


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(donation, "permitted_sources", lambda config, root: config["sources"])
    donation.set_enabled(True, tmp_path, sources=["local"], github_account="TestDonor",
                         intensity="full_blast", model_training=True)
    return tmp_path


def artifacts(folder, *, good=True):
    folder.mkdir(parents=True)
    fingerprint, policy = models.identity()
    model = {"kind": "text_classifier", "classes": list(models.LABELS), "format_version": 1,
             "vocabulary": {"term": 0}, "idf": [1], "coef": [[0] for _ in models.LABELS],
             "intercept": [0 for _ in models.LABELS]}
    raw = json.dumps(model)
    data = {"kind": "normalized_public_benchmark", "dataset_id": "clinc150",
            **{key: policy["dataset"][key] for key in ("repository", "commit", "license")},
            "source_files": policy["dataset"]["files"]}
    subset = {"kind": "exact_label_subset", "selected_labels": sorted(models.LABELS), "applied_before_fit": True}
    report = {"accuracy": .96 if good else .1, "macro_f1": .9, "held_out_examples": 1000,
              "majority_baseline_accuracy": .2, "always_oos_baseline_accuracy": .7,
              "activated": False, "data": data, "subset_policy": subset, "evaluation_split": "provided_test",
              "model_sha256": hashlib.sha256(raw.encode()).hexdigest()}
    evaluation = json.dumps(report)
    candidate = {"activated": False, "data": data, "subset_policy": subset, "model_sha256": report["model_sha256"],
                 "evaluation_sha256": hashlib.sha256(evaluation.encode()).hexdigest()}
    bodies = {"model.json": raw, "evaluation.json": evaluation, "candidate.json": json.dumps(candidate)}
    receipt = {"experiment_sha256": fingerprint, "policy": policy, "activated": False,
               "routing_qualified": False, "benchmark_qualified": good,
               "artifacts": {name: hashlib.sha256(body.encode()).hexdigest() for name, body in bodies.items()}}
    bodies["contribution.json"] = json.dumps(receipt)
    for name, body in bodies.items():
        (folder/name).write_text(body, encoding="utf-8")
    return fingerprint


def test_existing_local_consent_does_not_authorize_training(tmp_path, monkeypatch):
    donation.set_enabled(True, tmp_path, sources=["local"], github_account="Donor")
    assert donation.settings(tmp_path)["model_training"] is False
    assert models.claim(tmp_path) is None
    with pytest.raises(PermissionError):
        models.run_job(tmp_path)


def test_training_requires_local_source(tmp_path):
    with pytest.raises(ValueError, match="Local"):
        donation.set_enabled(True, tmp_path, sources=["chatgpt"], github_account="Donor",
                             chatgpt_account="a"*64, model_training=True)


@pytest.mark.parametrize("sources", [42, None, "local", {"local": True}])
def test_malformed_sources_never_authorize_training_or_crash_settings(tmp_path, sources):
    path = tmp_path/"LongTasks/donation-settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"enabled": True, "model_training": True, "sources": sources}))
    config = donation.settings(tmp_path)
    assert config["enabled"] is False
    assert config["model_training"] is False


def test_claim_is_atomic_and_persists_across_restarts_without_tokens(enabled, monkeypatch):
    monkeypatch.setattr(donation, "local_available", lambda: pytest.fail("CPU training must not probe Ollama"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: models.claim(enabled), range(4)))
    claimed = [result for result in results if result]
    assert len(claimed) == 1
    models.complete(enabled, claimed[0], {"seconds": 2.5}, "not_qualified")
    assert models.claim(enabled) is None
    assert donation.total_tokens(enabled) == 0
    with models._connection(enabled) as conn:
        assert conn.execute("SELECT state,seconds FROM model_experiments").fetchone() == ("not_qualified", 2.5)


def test_training_does_not_reserve_code_jobs(enabled):
    assert models.claim(enabled)
    assert donation.claim_run(enabled, "unrelated-code", "local", "full_blast")


def test_changed_trainer_identity_can_run_after_hour_but_not_before(enabled, monkeypatch):
    first = models.claim(enabled)
    original = models.identity()
    monkeypatch.setattr(models, "identity", lambda: ("b"*64, original[1]))
    assert models.claim(enabled) is None
    with models._connection(enabled) as conn:
        conn.execute("UPDATE model_experiments SET started=0")
    assert models.claim(enabled)["fingerprint"] != first["fingerprint"]


@pytest.mark.parametrize("state", ["failed", "interrupted", "running"])
def test_transient_or_crashed_attempt_recovers_with_bounded_retry(enabled, state):
    first = models.claim(enabled)
    with models._connection(enabled) as conn:
        conn.execute("UPDATE model_experiments SET state=?,started=0", (state,))
    second = models.claim(enabled)
    assert second and second["run_id"] != first["run_id"]
    with models._connection(enabled) as conn:
        conn.execute("UPDATE model_experiments SET state='failed',started=0,attempts=3")
    assert models.claim(enabled) is None


def test_completed_result_is_not_retrained_if_upload_fails(enabled, monkeypatch):
    import github_account
    monkeypatch.setattr(github_account, "account", lambda: "TestDonor")
    monkeypatch.setattr(donation.Service, "allowed", lambda *a, **k: True)
    monkeypatch.setattr(models, "run_job", lambda *a, **k: {"qualified": True, "seconds": 2, "folder": "candidate"})
    monkeypatch.setattr(models, "review_artifacts", lambda *a: (_ for _ in ()).throw(OSError("disk changed")))
    service = donation.Service(enabled)
    service._run(threading.Event())
    with models._connection(enabled) as conn:
        assert conn.execute("SELECT state,seconds FROM model_experiments").fetchone() == ("ready", 2)
        conn.execute("UPDATE model_experiments SET started=0")
    assert models.claim(enabled) is None


def test_screen_rejects_always_oos_and_missing_nonfinite_metrics():
    report = {"accuracy": .71, "macro_f1": .8, "held_out_examples": 1000,
              "majority_baseline_accuracy": .1, "always_oos_baseline_accuracy": .7, "activated": False}
    assert not models.qualified(report)
    report["accuracy"] = .95
    assert models.qualified(report)
    report["macro_f1"] = float("nan")
    assert not models.qualified(report)


def test_artifact_hash_budget_and_activation_guards(tmp_path, monkeypatch):
    fingerprint = artifacts(tmp_path/"candidate")
    assert set(models.review_artifacts(tmp_path/"candidate", fingerprint)) == set(models.ARTIFACTS)
    (tmp_path/"candidate/model.json").write_text('{"kind":"text_classifier","activated":true}')
    with pytest.raises(ValueError, match="changed"):
        models.review_artifacts(tmp_path/"candidate", fingerprint)
    monkeypatch.setattr(models, "MAX_ARTIFACT_BYTES", 10)
    with pytest.raises(ValueError, match="budget"):
        models.review_artifacts(tmp_path/"candidate", fingerprint)


@pytest.mark.parametrize("mutation", ["missing_weights", "overflow"])
def test_rebound_hashes_cannot_make_invalid_weights_uploadable(tmp_path, mutation):
    folder = tmp_path/"candidate"
    fingerprint = artifacts(folder)
    raw = (folder/"model.json").read_text()
    if mutation == "missing_weights":
        raw = json.dumps({"kind": "text_classifier", "classes": list(models.LABELS)})
    else:
        raw = raw.replace('"coef": [[0]', '"coef": [[1e999]', 1)
    (folder/"model.json").write_text(raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    report = json.loads((folder/"evaluation.json").read_text())
    report["model_sha256"] = digest
    evaluation = json.dumps(report)
    (folder/"evaluation.json").write_text(evaluation)
    candidate = json.loads((folder/"candidate.json").read_text())
    candidate.update(model_sha256=digest, evaluation_sha256=hashlib.sha256(evaluation.encode()).hexdigest())
    (folder/"candidate.json").write_text(json.dumps(candidate))
    receipt = json.loads((folder/"contribution.json").read_text())
    receipt["artifacts"] = {name: hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in models.ARTIFACTS[:-1]}
    (folder/"contribution.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        models.review_artifacts(folder, fingerprint)


class Process:
    returncode = None
    def poll(self):
        return self.returncode
    def terminate(self):
        self.terminated = True
        self.returncode = -1
    def wait(self, timeout):
        return self.returncode
    def kill(self):
        self.returncode = -9


def test_cancel_terminates_owned_child_with_thread_caps(enabled, tmp_path, monkeypatch):
    process = Process()
    captured = {}
    def popen(args, **kwargs):
        captured.update(args=args, **kwargs)
        return process
    monkeypatch.setattr(models.subprocess, "Popen", popen)
    checks = [0]
    def cancel():
        checks[0] += 1
        if checks[0] > 1:
            raise InterruptedError("stop")
    with pytest.raises(InterruptedError):
        models.run_job(enabled, output_folder=tmp_path/"run", check_cancel=cancel)
    assert process.terminated
    assert captured["args"][1] == str(Path(models.__file__).resolve())
    assert captured["args"][2] == "--worker"
    assert all(captured["env"][key] == "2" for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"))


def test_timeout_kills_owned_child_and_does_not_upload(enabled, tmp_path, monkeypatch):
    process = Process()
    monkeypatch.setattr(models.subprocess, "Popen", lambda *a, **k: process)
    clock = iter([0, 301])
    monkeypatch.setattr(models.time, "monotonic", lambda: next(clock, 301))
    with pytest.raises(TimeoutError):
        models.run_job(enabled, output_folder=tmp_path/"run")
    assert process.terminated


def test_compute_receipts_accumulate_retries_without_double_counting(enabled):
    first = models.claim(enabled)
    models.complete(enabled, first, {"seconds": 4}, "failed")
    models.complete(enabled, first, {"seconds": 4}, "failed")
    with models._connection(enabled) as conn:
        conn.execute("UPDATE model_experiments SET started=0")
    second = models.claim(enabled)
    models.complete(enabled, second, {"seconds": 2}, "not_qualified")
    assert models.summary(enabled) == {"experiments": 1, "worker_seconds": 6, "screened_candidates": 0}
    assert donation.total_tokens(enabled) == 0


def test_scheduler_records_weak_candidate_and_resumes_without_cloud(enabled, monkeypatch):
    import chatgpt_auth
    import github_account
    monkeypatch.setattr(github_account, "account", lambda: "TestDonor")
    monkeypatch.setattr(donation.Service, "allowed", lambda *a, **k: True)
    monkeypatch.setattr(models, "run_job", lambda *a, **k: {"qualified": False, "seconds": 2.5, "folder": "public-candidate"})
    monkeypatch.setattr(donation, "open_proposal", lambda *a, **k: pytest.fail("Weak model must stay local"))
    monkeypatch.setattr(donation, "local_available", lambda: pytest.fail("Not needed for CPU training"))
    service = donation.Service(enabled)
    service._run(threading.Event())
    assert "baseline screening failed" in service.message
    assert service.next_attempt == 0
    assert donation.total_tokens(enabled) == 0


def test_screened_candidate_uses_reviewed_artifacts_and_draft_boundary(enabled, monkeypatch):
    import github_account
    folder = enabled/"candidate"
    fingerprint = artifacts(folder)
    monkeypatch.setattr(github_account, "account", lambda: "TestDonor")
    monkeypatch.setattr(donation.Service, "allowed", lambda *a, **k: True)
    monkeypatch.setattr(models, "run_job", lambda *a, **k: {"qualified": True, "seconds": 2, "folder": str(folder)})
    monkeypatch.setattr(donation, "gh_api", lambda path, *a, **k:
                        [] if "pulls?" in path or "matching-refs" in path else
                        {"full_name": "TestDonor/cale-ai-workbench", "fork": True} if path.endswith("forks") else
                        {"parent": {"full_name": donation.UPSTREAM}} if path == "repos/TestDonor/cale-ai-workbench" else {"sha": "a"*40})
    submitted = []
    def proposal(login, run, job, commit, description, paths, guard, **kwargs):
        guard()
        assert job["id"] == models.JOB_ID
        assert set(paths) == {f"ModelContributions/{fingerprint}/{name}" for name in models.ARTIFACTS}
        assert "No inference tokens" in kwargs["validation_summary"]
        submitted.append(run)
        return "https://github.com/CaleMediaDev/cale-ai-workbench/pull/1"
    monkeypatch.setattr(donation, "open_proposal", proposal)
    service = donation.Service(enabled)
    service._run(threading.Event())
    assert len(submitted) == 1
    assert donation.total_tokens(enabled) == 0
    with models._connection(enabled) as conn:
        assert conn.execute("SELECT state FROM model_experiments").fetchone()[0] == "submitted"


def test_ui_allows_explicit_cpu_training_without_ollama(tmp_path):
    import donation_ui
    from types import SimpleNamespace
    ui = object.__new__(donation_ui.Controls)
    ui.login, ui.local_ready, ui.chat_ready, ui.free_fingerprint = "Donor", False, False, ""
    ui.source_vars = {key: SimpleNamespace(get=lambda key=key: key == "local") for key in donation.SOURCES}
    ui.model_training = SimpleNamespace(get=lambda: True)
    assert ui.ready_to_start()


def test_summary_uses_nonblocking_read_only_snapshot(enabled):
    import sqlite3
    import time
    models.claim(enabled)
    conn = sqlite3.connect(enabled/"LongTasks/donations.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        assert models.summary(enabled)["experiments"] == 1
        assert time.monotonic()-start < 1
    finally:
        conn.close()


def test_uninitialized_summary_does_not_create_database(tmp_path):
    assert models.summary(tmp_path) is None
    assert not (tmp_path/"LongTasks").exists()


def test_pending_publication_resumes_same_run_without_training(enabled, monkeypatch):
    import github_account
    folder = enabled/"candidate"
    fingerprint = artifacts(folder)
    claimed = models.claim(enabled)
    models.complete(enabled, claimed, {"folder": str(folder), "seconds": 2, "qualified": True}, "ready")
    with models._connection(enabled) as conn:
        conn.execute("UPDATE model_experiments SET publication_checked=0")
    monkeypatch.setattr(github_account, "account", lambda: "TestDonor")
    monkeypatch.setattr(donation.Service, "allowed", lambda *a, **k: True)
    monkeypatch.setattr(models, "run_job", lambda *a, **k: pytest.fail("Never retrain pending publication"))
    branch = "cale-donation-" + claimed["run_id"][:16]
    writes = []
    def gh(path, data=None):
        if data:
            writes.append(data)
        return [{"head": {"ref": branch, "repo": {"owner": {"login": "TestDonor"}}},
                 "base": {"repo": {"full_name": donation.UPSTREAM}},
                 "body": "<!-- cale-model-experiment:" + fingerprint + " -->",
                 "html_url": "https://github.com/" + donation.UPSTREAM + "/pull/1"}]
    monkeypatch.setattr(donation, "gh_api", gh)
    service = donation.Service(enabled)
    service._run(threading.Event())
    assert service.message == "Model candidate submitted for owner review."
    assert not writes
    assert models.summary(enabled)["worker_seconds"] == 2
    assert donation.total_tokens(enabled) == 0


def test_old_ready_candidate_is_preserved_but_does_not_block_new_policy(enabled, monkeypatch):
    claimed = models.claim(enabled)
    models.complete(enabled, claimed, {"folder": "old-public-artifact", "seconds": 2, "qualified": True}, "ready")
    _, policy = models.identity()
    monkeypatch.setattr(models, "identity", lambda: ("b"*64, policy))
    with models._connection(enabled) as conn:
        conn.execute("UPDATE model_experiments SET started=0")
    assert models.claim(enabled)["fingerprint"] == "b"*64
    with models._connection(enabled) as conn:
        assert conn.execute("SELECT state,folder FROM model_experiments WHERE fingerprint=?", (claimed["fingerprint"],)).fetchone() == ("superseded", "old-public-artifact")


@pytest.mark.parametrize("wrong_blob", [False, True])
def test_partial_publication_reuses_only_exact_artifact_branch(enabled, monkeypatch, wrong_blob):
    import github_account
    folder = enabled/"candidate"
    fingerprint = artifacts(folder)
    claimed = models.claim(enabled)
    branch = "cale-donation-" + claimed["run_id"][:16]
    monkeypatch.setattr(github_account, "account", lambda: "TestDonor")
    bodies = models.review_artifacts(folder, fingerprint)
    files = [{"filename": f"ModelContributions/{fingerprint}/{name}", "status": "added",
              "sha": hashlib.sha1(b"blob " + str(len(body.encode())).encode() + b"\0" + body.encode()).hexdigest()}
             for name, body in bodies.items()]
    if wrong_blob:
        files[0]["sha"] = "b"*40
    posts = []
    def gh(path, data=None):
        if data is not None:
            posts.append((path, data))
        if "pulls?" in path:
            return []
        if path.endswith("forks"):
            return {"full_name": "TestDonor/cale-ai-workbench", "fork": True}
        if path == "repos/TestDonor/cale-ai-workbench":
            return {"parent": {"full_name": donation.UPSTREAM}}
        if "matching-refs" in path:
            return [{"ref": "refs/heads/"+branch, "object": {"sha": "a"*40}}]
        if "git/commits" in path:
            return {"parents": [{"sha": "c"*40}]}
        if "compare/" in path:
            return {"total_commits": 1, "files": files}
        if path.endswith("pulls"):
            return {"html_url": "https://github.com/"+donation.UPSTREAM+"/pull/2"}
        pytest.fail(path)
    monkeypatch.setattr(donation, "gh_api", gh)
    if wrong_blob:
        with pytest.raises(ValueError, match="differs"):
            models.publish_candidate(enabled, claimed, {"folder": str(folder)}, "TestDonor", lambda: None)
        assert not any(path.endswith("pulls") for path, _ in posts)
    else:
        assert models.publish_candidate(enabled, claimed, {"folder": str(folder)}, "TestDonor", lambda: None).endswith("/pull/2")
        assert [data["draft"] for path, data in posts if path.endswith("pulls")] == [True]
