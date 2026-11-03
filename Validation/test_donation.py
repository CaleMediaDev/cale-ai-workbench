"""Donation consent, idle preemption, accounting and draft-only GitHub writes.

All inference and GitHub boundaries are faked: validation must never consume a
real account's allowance, create a fork, or publish a pull request.
"""

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from unittest.mock import patch

import pytest

import chatgpt_auth
import donation
import routing_mode

SOURCE = "def calculate():\n    return 1\n"
TEST = '"""Contribution regressions."""\n'
SOURCES = {"example.py": SOURCE, "Validation/test_example.py": TEST}
PROPOSAL = {"title": "Correct the return value", "summary": "Fixes a demonstrated return-value bug.", "changes": [
    {"path": "example.py", "before": "return 1", "after": "return 2"},
    {"path": "Validation/test_example.py", "before": TEST,
     "after": TEST + "def test_value():\n    from example import calculate\n    assert calculate() == 2\n"},
]}
RESULT = {"model": "plan-test", "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
          "choices": [{"message": {"content": json.dumps(PROPOSAL)}}]}
COMMIT = {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}}
JOB = {"id": "example-fix", "title": "Review correctness", "paths": list(SOURCES)}
FINGERPRINT = hashlib.sha256(b"donor-subject").hexdigest()


def enable(root):
    donation.set_enabled(True, root, chatgpt_account=FINGERPRINT, github_account="donor")


def test_opt_in_is_off_until_saved_and_damage_fails_closed(tmp_path):
    assert not donation.settings(tmp_path)["enabled"]
    with pytest.raises(ValueError):
        donation.set_enabled(True, tmp_path)
    enable(tmp_path)
    assert donation.settings(tmp_path)["enabled"]
    (tmp_path / "LongTasks/donation-settings.json").write_text("bad JSON")
    assert not donation.settings(tmp_path)["enabled"]
    donation.set_enabled(False, tmp_path)
    assert not donation.settings(tmp_path)["enabled"]


def test_ledger_counts_reported_usage_once_and_survives_restart(tmp_path):
    run = donation.claim_run(tmp_path, "first")
    assert run
    assert not donation.claim_run(tmp_path, "duplicate")
    donation.record_result(tmp_path, run, RESULT)
    donation.record_result(tmp_path, run, RESULT)
    donation.finish(tmp_path, run, "failed")
    assert donation.total_tokens(tmp_path) == 50
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute("SELECT status,model FROM runs").fetchone() == ("failed", "plan-test")


def test_missing_usage_does_not_invent_donated_tokens(tmp_path):
    run = donation.claim_run(tmp_path, "missing-usage")
    donation.record_result(tmp_path, run, {"model": "test"})
    assert donation.total_tokens(tmp_path) == 0


@pytest.mark.parametrize("path", ["../secrets.py", "Cloud/token.py", ".github/workflows/run.yml",
                                  "/absolute.py", "C:/outside.py", "Validation/../secret.py", "file\\name.py"])
def test_proposals_reject_unapproved_paths(path):
    value = {**PROPOSAL, "changes": [{"path": path, "before": "x", "after": "y"}]}
    with pytest.raises(ValueError):
        donation.validate_proposal(json.dumps(value), {path: "x"})


def test_proposals_require_real_exact_changes_regression_tests_and_valid_python():
    proposal, files = donation.validate_proposal(json.dumps(PROPOSAL), SOURCES)
    assert files["example.py"] == SOURCE.replace("return 1", "return 2")
    assert proposal["title"] == PROPOSAL["title"]
    for changes in (
        [{"path": "example.py", "before": "missing", "after": "anything"}],
        PROPOSAL["changes"][:1],
        [*PROPOSAL["changes"][:1], {"path": "Validation/test_example.py", "before": TEST, "after": "def ("}],
    ):
        with pytest.raises((ValueError, SyntaxError)):
            donation.validate_proposal(json.dumps({**PROPOSAL, "changes": changes}), SOURCES)


def test_empty_review_is_not_a_fake_code_contribution():
    assert donation.validate_proposal('{"changes":[]}', SOURCES)[1] == {}


def test_donation_permission_is_separate_from_normal_paid_routing(tmp_path):
    credentials = {"enabled": False, "subject": "donor-subject", "scopes": ["chatgpt.tokens.use.direct"],
                   "expires_at": time.time() + 3600, "access_token": "private-test-token"}
    with patch.object(chatgpt_auth, "ROOT", tmp_path), patch.object(chatgpt_auth, "load", return_value=credentials):
        with pytest.raises(ValueError):
            chatgpt_auth.access_token(purpose="donation")
        enable(tmp_path)
        assert chatgpt_auth.access_token(purpose="donation") == "private-test-token"
        with pytest.raises(ValueError):
            chatgpt_auth.access_token()  # Donations do not enable normal paid calls.
        credentials["subject"] = "a-different-person"
        with pytest.raises(ValueError):
            chatgpt_auth.access_token(purpose="donation")
        credentials["subject"] = "donor-subject"
        credentials["scopes"] = []
        with pytest.raises(ValueError):
            chatgpt_auth.access_token(purpose="donation")


def test_cancel_before_start_never_reads_credentials_or_calls_inference():
    with patch.object(chatgpt_auth, "access_token") as token, patch.object(chatgpt_auth, "urlopen") as upstream:
        with pytest.raises(InterruptedError):
            chatgpt_auth.complete({}, cancel=lambda: True)
    token.assert_not_called()
    upstream.assert_not_called()


@pytest.mark.parametrize("login,fingerprint", [("different-donor", FINGERPRINT), ("donor", "b" * 64)])
def test_account_changes_disable_donations_before_reading_source_or_spending(tmp_path, login, fingerprint):
    enable(tmp_path)
    service = donation.Service(tmp_path)
    service.last_activity -= 121
    with (patch("pc_offline.offline", return_value=False),
          patch("github_account.account", return_value=login),
          patch("chatgpt_auth.status", return_value={"sharing": True}),
          patch("chatgpt_auth.account_fingerprint", return_value=fingerprint),
          patch("chatgpt_auth.complete") as inference,
          patch.object(donation, "gh_api") as github):
        service._run(threading.Event())
    assert not donation.settings(tmp_path)["enabled"]
    assert "Accounts changed" in service.message
    inference.assert_not_called()
    github.assert_not_called()


def test_idle_service_respects_foreground_privacy_modes_and_offline(tmp_path):
    service = donation.Service(tmp_path)
    enable(tmp_path)
    service.last_activity -= 121
    with patch("pc_offline.offline", return_value=False), patch.object(service, "_run") as work:
        for mode in ("local_only", "free_cloud_only"):
            routing_mode.save_scope(tmp_path / "Cloud", mode)
            assert not service.allowed()
        routing_mode.save_scope(tmp_path / "Cloud", "both")
        assert service.allowed()
        assert not service.allowed(busy=True)
        with patch("pc_offline.offline", return_value=True):
            assert not service.allowed()
        service.tick(busy=True)
        work.assert_not_called()
        service.tick()
        service.thread.join(2)
        work.assert_called_once()
        service.activity()
        assert service.cancel.is_set()
        assert not service.allowed()


def test_draft_submission_writes_only_new_branch_and_never_main_or_merge():
    calls = []
    def api(endpoint, data=None):
        calls.append((endpoint, data))
        if endpoint.endswith("/forks"):
            return {"full_name": "donor/cale-ai-workbench", "fork": True}
        if endpoint == "repos/donor/cale-ai-workbench":
            return {"parent": {"full_name": donation.UPSTREAM}}
        if endpoint.endswith("/git/trees") or endpoint.endswith("/git/commits"):
            return {"sha": "c" * 40}
        if endpoint.endswith("/pulls"):
            return {"html_url": "https://github.com/" + donation.UPSTREAM + "/pull/1"}
        return {}
    with patch.object(donation, "gh_api", side_effect=api):
        url = donation.open_proposal("donor", "1" * 32, JOB, COMMIT, PROPOSAL, SOURCES, lambda: None)
    assert url.endswith("/pull/1")
    refs = [data["ref"] for endpoint, data in calls if endpoint.endswith("/git/refs")]
    assert refs == ["refs/heads/cale-donation-" + "1" * 16]
    request = calls[-1][1]
    assert request["draft"] is True and request["base"] == "main"
    assert "Behavior tests were NOT run" in request["body"]
    assert all("/merges" not in endpoint and "/merge" not in endpoint for endpoint, _ in calls)


@pytest.mark.parametrize("cancel_after", [True, False])
def test_background_flow_uses_only_public_source_and_keeps_usage_on_cancellation(tmp_path, cancel_after):
    enable(tmp_path)
    (tmp_path / "personal-document.txt").write_text("PRIVATE DOCUMENT MUST NOT BE SENT")
    service = donation.Service(tmp_path)
    service.last_activity -= 121
    cancel = threading.Event()
    payloads = []
    def public(path, revision):
        assert revision == COMMIT["sha"]
        return json.dumps([JOB]) if path == "donation-tasks.json" else SOURCES[path]
    def complete(payload, **kwargs):
        assert kwargs["purpose"] == "donation"
        payloads.append(payload)
        if cancel_after:
            cancel.set()  # User returns just after inference completes.
        return RESULT
    with (patch("pc_offline.offline", return_value=False),
          patch("github_account.account", return_value="donor"),
          patch("chatgpt_auth.status", return_value={"sharing": True}),
          patch("chatgpt_auth.account_fingerprint", return_value=FINGERPRINT),
          patch("chatgpt_auth.complete", side_effect=complete),
          patch("usage_history.record_completion"),
          patch.object(donation, "public_file", side_effect=public),
          patch.object(donation, "gh_api", side_effect=lambda endpoint: {"total_count": 0} if "search/" in endpoint else COMMIT),
          patch.object(donation, "open_proposal", return_value="https://github.com/test/pull/1") as publish):
        service._run(cancel)
    assert donation.total_tokens(tmp_path) == 50
    assert "PRIVATE DOCUMENT" not in json.dumps(payloads)
    if cancel_after:
        assert "Paused" in service.message
        publish.assert_not_called()
    else:
        assert "maintainer review" in service.message
        publish.assert_called_once()
        with closing(donation.connection(tmp_path)) as conn:
            assert conn.execute("SELECT status,url FROM runs").fetchone() == (
                "submitted", "https://github.com/test/pull/1")


@pytest.mark.parametrize("empty", [False, True])
def test_completed_stream_keeps_reported_usage_when_user_returns_on_final_event(empty):
    cancel = threading.Event()
    accounted = []
    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def __iter__(self):
            cancel.set()
            event = {"type": "response.completed", "response": {"id": "x", "model": "test",
                "output": [] if empty else [{"type": "message", "content": [{"type": "output_text", "text": "done"}]}],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}}}
            yield ("data: " + json.dumps(event) + "\n").encode()
    with (patch.object(chatgpt_auth, "access_token", return_value="test-token"),
          patch.object(chatgpt_auth, "available_models", return_value=[]),
          patch.object(chatgpt_auth, "choose_model", return_value="test"),
          patch.object(chatgpt_auth, "urlopen", return_value=Stream())):
        if empty:
            with pytest.raises(RuntimeError, match="empty response"):
                chatgpt_auth.complete({}, cancel=cancel.is_set, on_usage=accounted.append)
        else:
            result = chatgpt_auth.complete({}, cancel=cancel.is_set, on_usage=accounted.append)
            assert result["usage"]["total_tokens"] == 15
    assert accounted[0]["usage"]["total_tokens"] == 15
