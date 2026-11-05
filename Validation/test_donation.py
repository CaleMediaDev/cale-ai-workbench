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
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import chatgpt_auth
import donation
import routing_mode

def open_pull(number, *, author="donor", title="[Donation] Correct fixture", body=""):
    """A real-shaped upstream REST listing item, never a search-index hit."""
    return {"number":number,"state":"open","title":title,"body":body,
        "html_url":f"https://github.com/{donation.UPSTREAM}/pull/{number}",
        "user":{"login":author},"base":{"repo":{"full_name":donation.UPSTREAM}}}


def is_open_pull_list(endpoint):
    return endpoint.startswith(f"repos/{donation.UPSTREAM}/pulls?") and "head=" not in endpoint


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
    donation.set_enabled(True, root, chatgpt_account=FINGERPRINT, github_account="donor", sources=["chatgpt"])


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
    service.last_activity -= 301
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
    service.last_activity -= 301
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
            return {"full_name": "donor/cale-ai-workbench", "fork": True, "parent": {"full_name": donation.UPSTREAM}}
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
    service.last_activity -= 301
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
          patch.object(donation, "gh_api", side_effect=lambda endpoint: [] if is_open_pull_list(endpoint) else COMMIT),
          patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation, "open_proposal", return_value="https://github.com/CaleMediaDev/cale-ai-workbench/pull/1") as publish):
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
                "submitted", "https://github.com/CaleMediaDev/cale-ai-workbench/pull/1")


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


def test_local_consent_needs_only_github_and_cannot_authorize_chatgpt(tmp_path):
    donation.set_enabled(True, tmp_path, sources=["local"], github_account="donor", chatgpt_account=FINGERPRINT)
    assert donation.settings(tmp_path)["sources"] == ["local"]
    assert donation.settings(tmp_path)["chatgpt_account"] == ""  # Older apps cannot reuse a paid grant either.
    credentials = {"enabled": False, "subject": "donor-subject", "scopes": ["chatgpt.tokens.use.direct"],
                   "expires_at": time.time() + 3600, "access_token": "test"}
    with patch.object(chatgpt_auth, "ROOT", tmp_path), patch.object(chatgpt_auth, "load", return_value=credentials):
        with pytest.raises(ValueError):
            chatgpt_auth.access_token(purpose="donation")


def test_local_only_setup_button_does_not_require_chatgpt(tmp_path):
    import donation_ui

    control = object.__new__(donation_ui.Controls)
    control.service = SimpleNamespace(root=tmp_path, activity=lambda: None, message="")
    control.login, control.chat_ready, control.local_ready = "donor", False, True
    control.fingerprint = control.free_fingerprint = ""
    control.source_vars = {key: SimpleNamespace(get=lambda source=key: source == "local") for key in donation.SOURCES}
    control.intensity = SimpleNamespace(get=lambda: "medium")
    control.model_training = SimpleNamespace(get=lambda: False)
    control.refresh = lambda: None
    assert control.ready_to_start()
    control.toggle()
    assert donation.settings(tmp_path)["enabled"]
    assert donation.settings(tmp_path)["sources"] == ["local"]
    assert donation.settings(tmp_path)["intensity"] == "medium"


def test_upgrade_preserves_existing_chatgpt_only_consent_and_usage(tmp_path):
    state = tmp_path / "LongTasks"
    state.mkdir()
    (state / "donation-settings.json").write_text(json.dumps({"enabled": True,
        "chatgpt_account": FINGERPRINT, "github_account": "donor"}))
    with closing(sqlite3.connect(state / "donations.db")) as conn, conn:
        conn.execute("CREATE TABLE runs(id TEXT PRIMARY KEY,started REAL,status TEXT,tokens INTEGER,model TEXT,url TEXT,job TEXT)")
        conn.execute("INSERT INTO runs VALUES('old',1,'submitted',75,'old-model','','old-job')")
    assert donation.settings(tmp_path)["sources"] == ["chatgpt"]
    assert donation.settings(tmp_path)["enabled"]
    assert donation.total_tokens(tmp_path) == 75
    assert donation.source_totals(tmp_path) == {"chatgpt": 75}


@pytest.mark.parametrize("intensity,limit", [("slow", 1)])
def test_intensity_caps_remain_durable_across_resources(tmp_path, intensity, limit):
    for number in range(limit):
        assert donation.claim_run(tmp_path, str(number), donation.SOURCES[number % 3], intensity)
    assert not donation.claim_run(tmp_path, "too-many", "local", intensity)


def test_token_receipts_add_free_fallbacks_without_double_counting(tmp_path):
    run = donation.claim_run(tmp_path, "fix", "free_cloud", "medium")
    donation.record_result(tmp_path, run, RESULT, "provider-1", "groq")
    second = {**RESULT, "usage": {"total_tokens": 30}}
    donation.record_result(tmp_path, run, second, "provider-2", "openrouter")
    donation.record_result(tmp_path, run, second, "provider-2", "openrouter")
    assert donation.total_tokens(tmp_path) == 80
    assert donation.source_totals(tmp_path) == {"free_cloud": 80}


def test_free_donation_observes_usage_without_leaking_context_or_double_recording(tmp_path):
    import usage_history
    received = []
    class Router:
        def complete(self, payload, **kwargs):
            assert kwargs["scope_override"] == "free_cloud_only"
            usage_history.record_completion(RESULT, "groq")
            return RESULT
    before = usage_history.OBSERVER.get()
    with patch("cloud_ai.Router", Router), patch.object(usage_history, "DATABASE", tmp_path / "usage.db"):
        result = donation.complete_donation({"messages": []}, "free_cloud", "medium", lambda: False,
                                            lambda *args: received.append(args))
    assert result == RESULT
    assert usage_history.OBSERVER.get() is before
    assert len(received) == 1 and received[0][1:] == ("groq", "free-1", True)


@pytest.mark.parametrize("intensity,threads", [("slow", 2), ("medium", 4), ("full_blast", 8)])
def test_local_stream_uses_only_loopback_and_reports_real_usage(intensity, threads):
    calls, receipts = [], []
    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def __iter__(self):
            yield b'{"message":{"content":"{\\"changes\\":[]}"},"done":false}\n'
            yield b'{"model":"local-test","message":{"content":""},"done":true,"prompt_eval_count":12,"eval_count":4}\n'
    def opened(request, **_kwargs):
        assert request.full_url == "http://127.0.0.1:11434/api/chat"
        calls.append(json.loads(request.data))
        return Stream()
    with (patch("cloud_ai.Router.local_candidates", return_value=["local-test"]),
          patch.object(donation, "urlopen", side_effect=opened), patch.object(donation.os, "cpu_count", return_value=8),
          patch("chatgpt_auth.complete") as paid):
        result = donation.local_completion({"messages": [{"role": "user", "content": "Public code"}]},
                                           intensity, lambda: False, lambda *args: receipts.append(args))
    assert calls[0]["options"]["num_thread"] == threads
    assert calls[0]["stream"] is True and calls[0]["think"] is False
    assert result["usage"]["total_tokens"] == 16
    assert json.loads(result["choices"][0]["message"]["content"]) == {"changes": []}
    assert receipts[0][1] == "ollama"
    paid.assert_not_called()


def test_local_stream_closes_when_foreground_activity_interrupts():
    cancel, closed = threading.Event(), []
    class Stream:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            closed.append(True)
        def __iter__(self):
            cancel.set()
            yield b'{"message":{"content":"partial"},"done":false}\n'
    with patch("cloud_ai.Router.local_candidates", return_value=["local-test"]), patch.object(donation, "urlopen", return_value=Stream()):
        with pytest.raises(InterruptedError):
            donation.local_completion({"messages": []}, "slow", cancel.is_set, lambda *_args: None)
    assert closed == [True]


def test_local_background_flow_does_not_require_a_chatgpt_account(tmp_path):
    donation.set_enabled(True, tmp_path, sources=["local"], github_account="donor")
    service = donation.Service(tmp_path)
    service.last_activity -= 301
    def public(path, _revision):
        return json.dumps([JOB]) if path == "donation-tasks.json" else SOURCES[path]
    def local(_payload, _intensity, _cancel, receipt):
        receipt(RESULT, "ollama", "main", False)
        return RESULT
    with (patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"),
          patch.object(donation, "local_available", return_value=True),
          patch.object(donation, "local_completion", side_effect=local),
          patch("chatgpt_auth.status", side_effect=AssertionError("ChatGPT should not be needed")),
          patch("usage_history.record_completion"), patch.object(donation, "public_file", side_effect=public),
          patch.object(donation, "gh_api", side_effect=lambda endpoint: [] if is_open_pull_list(endpoint) else COMMIT),
          patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation, "open_proposal", return_value="https://github.com/CaleMediaDev/cale-ai-workbench/pull/2") as publish):
        service._run(threading.Event())
    publish.assert_called_once()
    assert donation.source_totals(tmp_path) == {"local": 50}


def merged_fix(number, login="alice", author=1, **changes):
    return {"number": number, "state": "closed", "merged_at": "2026-11-03T00:00:00Z", "draft": False,
            "title": "[Donation] A real fix", "body": "<!-- cale-donation:example-fix -->",
            "head": {"ref": "cale-donation-" + "a" * 16},
            "base": {"ref": "main", "repo": {"full_name": donation.UPSTREAM}},
            "user": {"id": author, "login": login}, **changes}


def test_leaderboard_uses_merged_fixes_not_claimed_tokens_and_deduplicates(tmp_path):
    pulls = [merged_fix(1), merged_fix(2), merged_fix(2), merged_fix(3, "bob", 2),
             merged_fix(4, merged_at=None), merged_fix(5, state="open"),
             merged_fix(6, title="Not a donation"), merged_fix(7, body="I donated 99999999 tokens"),
             merged_fix(8, head={"ref": "ordinary-branch"}),
             merged_fix(9, base={"ref": "main", "repo": {"full_name": "other/repository"}})]
    with patch("pc_offline.offline", return_value=False):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda _endpoint: pulls)
    assert result["complete"]
    assert result["rows"] == [{"login": "alice", "accepted": 2}, {"login": "bob", "accepted": 1}]
    with patch("pc_offline.offline", return_value=True):
        cached = donation.refresh_leaderboard(tmp_path, fetch=lambda _endpoint: pytest.fail("Offline request"))
    assert cached["rows"] == result["rows"]


@pytest.mark.parametrize("include_integrated", [False, True])
def test_leaderboard_continues_partial_history_without_double_credit(tmp_path, include_integrated):
    def pages(endpoint):
        page = int(endpoint.rsplit("page=", 1)[1])
        return [merged_fix(n, **({"merged_at": None, "closed_at": "2026-11-03T00:00:00Z",
            "labels": [{"name": "cale-accepted-fix"}]} if include_integrated and n % 2 == 0 else {}))
            for n in range((page - 1) * 100 + 1, page * 100 + 1)] if page <= 10 else []
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1000):
        partial = donation.refresh_leaderboard(tmp_path, fetch=pages)
    assert not partial["complete"] and partial["rows"] == []
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1061):
        complete = donation.refresh_leaderboard(tmp_path, fetch=pages, force=True)
    assert complete["complete"] and complete["rows"][0]["accepted"] == 1000


def test_complete_leaderboard_snapshot_removes_stale_credits(tmp_path):
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1000):
        donation.refresh_leaderboard(tmp_path, fetch=lambda endpoint: [merged_fix(1), merged_fix(2)])
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1061):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda endpoint: [merged_fix(1, merged_at=None)], force=True)
    assert result["complete"] and result["rows"] == []


def test_partial_snapshot_and_failed_page_preserve_previous_verified_counts(tmp_path):
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1000):
        donation.refresh_leaderboard(tmp_path, fetch=lambda endpoint: [merged_fix(9999, "old", 9)])

    def pages(endpoint):
        page = int(endpoint.rsplit("page=", 1)[1])
        if page == 11:
            raise OSError("network interrupted")
        return [merged_fix(n) for n in range((page - 1) * 100 + 1, page * 100 + 1)]

    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1061):
        partial = donation.refresh_leaderboard(tmp_path, fetch=pages, force=True)
    assert not partial["complete"] and partial["rows"] == [{"login": "old", "accepted": 1}]
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1122):
        with pytest.raises(OSError, match="interrupted"):
            donation.refresh_leaderboard(tmp_path, fetch=pages, force=True)
    assert donation.leaderboard(tmp_path)["rows"] == partial["rows"]
    with donation.connection(tmp_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leaderboard_pending").fetchone()[0] == 1000


def test_changed_upstream_during_pagination_restarts_without_deleting_cache(tmp_path):
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1000):
        donation.refresh_leaderboard(tmp_path, fetch=lambda endpoint: [merged_fix(1)])
    anchors = iter([[merged_fix(1)], [merged_fix(2)]])
    calls = []

    def changed(endpoint):
        calls.append(endpoint)
        return next(anchors) if "sort=updated" in endpoint else []

    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1061):
        unstable = donation.refresh_leaderboard(tmp_path, fetch=changed, force=True)
    assert not unstable["complete"] and unstable["rows"] == [{"login": "alice", "accepted": 1}]
    assert len(calls) == 3
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1122):
        stable = donation.refresh_leaderboard(tmp_path, fetch=lambda endpoint: [], force=True)
    assert stable["complete"] and stable["rows"] == []


@pytest.mark.parametrize("failure", [OSError("final anchor unavailable"), {"message": "API error"}])
def test_final_snapshot_validation_failure_cannot_clear_cached_fixes(tmp_path, failure):
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1000):
        donation.refresh_leaderboard(tmp_path, fetch=lambda endpoint: [merged_fix(1)])
    calls = 0

    def failing(endpoint):
        nonlocal calls
        calls += 1
        if calls == 3:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return []

    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1061):
        with pytest.raises((OSError, ValueError)):
            donation.refresh_leaderboard(tmp_path, fetch=failing, force=True)
    assert donation.leaderboard(tmp_path)["rows"] == [{"login": "alice", "accepted": 1}]



def test_full_blast_has_no_daily_cutoff_even_after_many_resources(tmp_path):
    for number in range(30):
        assert donation.claim_run(tmp_path, str(number), donation.SOURCES[number % 3], "full_blast")
    assert donation.PROFILES["full_blast"]["open"] is None
    assert donation.PROFILES["full_blast"]["interval"] == 0


def test_full_blast_continues_during_foreground_but_stops_for_updates_and_opt_out(tmp_path):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="full_blast")
    service = donation.Service(tmp_path)
    with (patch("pc_offline.offline", return_value=False),
          patch.object(service, "_run") as run):
        assert service.allowed(busy=True)
        service.activity()
        assert not service.cancel.is_set()
        service.tick(busy=True)
        service.thread.join(2)
        run.assert_called_once()
        service.tick(busy=True)  # Next completed unit can start immediately.
        service.thread.join(2)
        assert run.call_count == 2
        service.tick(suspended=True)
        assert service.cancel.is_set()
        assert not service.allowed()
        service.tick(busy=True, suspended=False)
        service.thread.join(2)
        assert run.call_count == 3
        donation.set_enabled(False, tmp_path)
        service.tick(busy=True)
        assert service.cancel.is_set() and not service.allowed(busy=True)
        assert "Continuous" in donation.intensity_description("full_blast")


def test_full_blast_discovers_other_public_areas_without_sensitive_or_duplicate_jobs():
    tree = {"tree": [{"path": path, "type": "blob", "size": 300} for path in (
        "quick_calculator.py", "Validation/test_quick_calculator.py",
        "music_style.py", "Validation/test_music_style.py",
        "chatgpt_auth.py", "Validation/test_chatgpt_auth.py",
        "donation.py", "Validation/test_donation.py")], "truncated": False}
    existing = [{"id": "calculator", "paths": ["quick_calculator.py"]}]
    with patch.object(donation, "gh_api", return_value=tree):
        jobs = donation.continuous_jobs(existing, COMMIT["sha"])
    assert len(jobs) == 2 and jobs[0] == existing[0]
    assert jobs[1]["paths"] == ["music_style.py", "Validation/test_music_style.py"]
    assert len(jobs[1]["id"]) <= 60


def test_full_blast_gets_past_twelve_runs_and_three_pending_drafts_then_continues(tmp_path):
    donation.set_enabled(True, tmp_path, chatgpt_account=FINGERPRINT,
                         github_account="donor", sources=["chatgpt"], intensity="full_blast")
    for number in range(12):
        run = donation.claim_run(tmp_path, str(number), "chatgpt", "full_blast")
        donation.finish(tmp_path, run, "no_change")
    service = donation.Service(tmp_path)
    def public(path, revision):
        return json.dumps([JOB]) if path == "donation-tasks.json" else SOURCES[path]
    calls = []
    def api(endpoint):
        calls.append(endpoint)
        if is_open_pull_list(endpoint):
            numbers = [101] if "page=2" in endpoint else range(1,101)
            return [open_pull(n,body="<!-- cale-donation:other -->") for n in numbers]
        return COMMIT
    with (patch("pc_offline.offline", return_value=False),
          patch("github_account.account", return_value="donor"),
          patch("chatgpt_auth.status", return_value={"sharing": True}),
          patch("chatgpt_auth.account_fingerprint", return_value=FINGERPRINT),
          patch.object(donation, "continuous_jobs", side_effect=lambda jobs, revision, **kwargs: jobs),
          patch.object(donation, "public_file", side_effect=public),
          patch.object(donation, "gh_api", side_effect=api),
          patch.object(donation, "complete_donation", return_value={"choices": [{"message": {
              "content": '{"changes":[]}'}}]}) as inference):
        service._run(threading.Event())
    inference.assert_called_once()
    assert any("page=2" in endpoint for endpoint in calls)
    assert service.next_attempt == 0
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 13


def test_full_blast_retries_failures_without_busy_looping(tmp_path):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="full_blast")
    service = donation.Service(tmp_path)
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", side_effect=OSError("offline")):
        service._run(threading.Event())
        first = service.next_attempt
        assert first >= time.monotonic() + 55
        service._run(threading.Event())
        assert service.next_attempt > first and donation.settings(tmp_path)["enabled"]



def test_full_blast_keeps_inflight_work_on_activity_but_explicit_stop_cancels(tmp_path):
    donation.set_enabled(True, tmp_path, chatgpt_account=FINGERPRINT,
                         github_account="donor", sources=["chatgpt"], intensity="full_blast")
    service = donation.Service(tmp_path)
    payloads = []
    def public(path, revision):
        return json.dumps([JOB]) if path == "donation-tasks.json" else SOURCES[path]
    def complete(payload, resource, intensity, cancel, on_usage):
        service.activity()
        assert not cancel()
        payloads.append(payload)
        return RESULT
    with (patch("pc_offline.offline", return_value=False),
          patch("github_account.account", return_value="donor"),
          patch("chatgpt_auth.status", return_value={"sharing": True}),
          patch("chatgpt_auth.account_fingerprint", return_value=FINGERPRINT),
          patch.object(donation, "continuous_jobs", side_effect=lambda jobs, revision, **kwargs: jobs),
          patch.object(donation, "public_file", side_effect=public),
          patch.object(donation, "gh_api", side_effect=lambda endpoint: [] if is_open_pull_list(endpoint) else COMMIT),
          patch.object(donation, "complete_donation", side_effect=complete),
          patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation, "open_proposal", return_value="https://github.com/CaleMediaDev/cale-ai-workbench/pull/7") as publish):
        service._run(service.cancel)
    publish.assert_called_once()
    assert payloads and service.next_attempt == 0
    donation.set_enabled(False, tmp_path)
    service.tick(busy=True)
    assert service.cancel.is_set()


def test_medium_is_continuous_serial_without_old_daily_or_draft_caps(tmp_path):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="medium")
    service = donation.Service(tmp_path)
    with patch("pc_offline.offline", return_value=False), patch.object(service, "_run") as run:
        assert service.allowed(busy=True)
        for _ in range(3):
            service.tick(busy=True)
            service.thread.join(2)
        assert run.call_count == 3 and not service.children
    for number in range(20):
        assert donation.claim_run(tmp_path, str(number), "local", "medium")
    assert donation.PROFILES["medium"]["open"] is None


@pytest.mark.parametrize("stop", ["opt_out", "update"])
def test_full_blast_six_lanes_overlap_and_all_stop_on_opt_out_or_update(tmp_path, stop):
    donation.set_enabled(True, tmp_path, chatgpt_account=FINGERPRINT,
                         github_account="donor", sources=["chatgpt"], intensity="full_blast")
    service = donation.Service(tmp_path)
    entered = threading.Barrier(7)
    release = threading.Event()
    def work(worker, cancel):
        entered.wait(timeout=3)
        release.wait(timeout=3)
    with patch("pc_offline.offline", return_value=False), patch.object(donation.Service, "_run", autospec=True, side_effect=work):
        service.tick(busy=True)
        entered.wait(timeout=3)
        workers = [service, *service.children]
        assert len(workers) == 6 and all(worker.thread.is_alive() for worker in workers)
        assert all(worker.resource == "chatgpt" for worker in service.children)
        assert "6 reviews running" in service.status_message()
        if stop == "opt_out":
            donation.set_enabled(False, tmp_path)
        service.tick(busy=True, suspended=(stop == "update"))
        assert all(worker.cancel.is_set() for worker in workers)
        release.set()
        for worker in workers:
            worker.thread.join(2)


def test_claim_collision_moves_to_next_job_without_fetching_lost_source(tmp_path):
    donation.set_enabled(True, tmp_path, chatgpt_account=FINGERPRINT,
                         github_account="donor", sources=["chatgpt"], intensity="full_blast")
    service = donation.Service(tmp_path)
    jobs = [{**JOB, "id": "area-first"}, {**JOB, "id": "area-second"}]
    calls = []
    real_claim = donation.claim_run

    def claim(root, job, *args):
        calls.append(("claim", job))
        # Reproduce a different lane winning the first selection after this
        # lane took its pending snapshot. The next claim uses real SQLite.
        return "" if sum(kind == "claim" for kind, _ in calls) == 1 else real_claim(root, job, *args)

    def public(path, revision):
        calls.append(("read", path))
        return json.dumps(jobs) if path == "donation-tasks.json" else SOURCES[path]

    with (patch("pc_offline.offline", return_value=False),
          patch("github_account.account", return_value="donor"),
          patch("chatgpt_auth.status", return_value={"sharing": True}),
          patch("chatgpt_auth.account_fingerprint", return_value=FINGERPRINT),
          patch.object(donation, "continuous_jobs", side_effect=lambda jobs, revision, **kwargs: jobs),
          patch.object(donation, "public_file", side_effect=public),
          patch.object(donation, "claim_run", side_effect=claim),
          patch.object(donation, "gh_api", side_effect=lambda endpoint: [] if is_open_pull_list(endpoint) else COMMIT),
          patch.object(donation, "complete_donation", return_value={"choices": [{"message": {
              "content": '{"changes":[]}'}}]}) as inference):
        service._run(threading.Event())
    inference.assert_called_once()
    assert len([call for call in calls if call[0] == "claim"]) == 2
    source_reads = [index for index, call in enumerate(calls) if call[0] == "read" and call[1] != "donation-tasks.json"]
    assert len(source_reads) == len(SOURCES)
    assert min(source_reads) > max(index for index, call in enumerate(calls) if call[0] == "claim")
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='no_change'").fetchone()[0] == 1
    assert service.next_attempt == 0


def test_parallel_lanes_cannot_claim_same_job_across_public_revisions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    # Initialize schema before racing the actual transactional claim path.
    with closing(donation.connection(tmp_path)):
        pass
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda n: donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", str(n) * 40), range(3)))
    assert sum(bool(result) for result in results) == 1
    donation.finish(tmp_path, next(result for result in results if result), "no_change")
    assert not donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", COMMIT["sha"])
    assert donation.claim_run(tmp_path, "different-area", "chatgpt", "full_blast", COMMIT["sha"])
    assert not donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "c"*40)


def test_six_first_launch_lanes_migrate_old_ledger_without_duplicate_columns(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "LongTasks" / "donations.db"
    path.parent.mkdir()
    # Create a real pre-migration ledger, not connection()'s current schema.
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, started REAL NOT NULL, status TEXT NOT NULL, "
                     "tokens INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '', "
                     "url TEXT NOT NULL DEFAULT '', job TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO runs(id,started,status,tokens,job) VALUES('historic',1,'no_change',23,'old-area')")
    entered = threading.Barrier(6)
    def first_launch(number):
        entered.wait(timeout=5)
        return donation.claim_run(tmp_path, "new-area", "chatgpt", "full_blast", str(number) * 40)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(first_launch, range(6)))
    assert sum(bool(result) for result in results) == 1
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        assert {"resource", "intensity", "revision", "updated", "review_checked"} <= columns
        assert conn.execute("SELECT tokens,job FROM runs WHERE id='historic'").fetchone() == (23, "old-area")
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


@pytest.mark.parametrize("status", ["requested", "received", "no_change"])
def test_cross_revision_reservations_expire_from_recent_activity(tmp_path, status):
    with patch.object(donation.time, "time", return_value=1000):
        run = donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "a" * 40)
    with patch.object(donation.time, "time", return_value=2000):
        if status == "received":
            donation.record_result(tmp_path, run, RESULT)
        elif status == "no_change":
            donation.finish(tmp_path, run, status)
    with patch.object(donation.time, "time", return_value=2801):
        if status == "requested":
            assert donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "b" * 40)
        else:
            assert not donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "b" * 40)
    with patch.object(donation.time, "time", return_value=5000):
        assert donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "c" * 40)


def test_submitted_cross_revision_guard_waits_for_authoritative_review(tmp_path):
    with patch.object(donation.time, "time", return_value=1000):
        run = donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "a" * 40)
        donation.finish(tmp_path, run, "submitted", f"https://github.com/{donation.UPSTREAM}/pull/7")
    closed = {"number": 7, "state": "closed", "merged_at": None, "labels": [], "base": {"repo": {"full_name": donation.UPSTREAM}},
              "head": {"ref": "cale-donation-" + run[:16]}, "body": "<!-- cale-donation:same-area -->"}
    with patch.object(donation.time, "time", return_value=100000):
        assert not donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "b" * 40)
        donation.refresh_pending_reviews(tmp_path, fetch=lambda _: {**closed, "state": "open"})
        assert not donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "c" * 40)
        donation.refresh_pending_reviews(tmp_path, fetch=lambda _: pytest.fail("Shared sync lease must avoid another request"))
    with patch.object(donation.time, "time", return_value=100031):
        def unavailable(_):
            raise OSError("offline")
        donation.refresh_pending_reviews(tmp_path, fetch=unavailable)
        assert not donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "d" * 40)
    with patch.object(donation.time, "time", return_value=100062):
        donation.refresh_pending_reviews(tmp_path, fetch=lambda _: closed)
        assert donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "e" * 40)


def test_pending_review_refresh_is_bounded_and_rotates_oldest_drafts(tmp_path):
    for number in range(8):
        run = donation.claim_run(tmp_path, f"area-{number}", "chatgpt", "full_blast", "a" * 40)
        donation.finish(tmp_path, run, "submitted", f"https://github.com/{donation.UPSTREAM}/pull/{number+1}")
    calls = []
    with patch.object(donation.time, "time", return_value=100000):
        donation.refresh_pending_reviews(tmp_path, fetch=lambda endpoint: calls.append(endpoint) or {})
        donation.refresh_pending_reviews(tmp_path, fetch=lambda _: pytest.fail("Other lane must share lease"))
    assert len(calls) == 3
    with patch.object(donation.time, "time", return_value=100031):
        donation.refresh_pending_reviews(tmp_path, fetch=lambda endpoint: calls.append(endpoint) or {})
    assert len(calls) == 6 and len(set(calls)) == 6


def test_search_index_lag_after_new_main_commit_cannot_repeat_submitted_job(tmp_path):
    donation.set_enabled(True, tmp_path, chatgpt_account=FINGERPRINT,
                         github_account="donor", sources=["chatgpt"], intensity="full_blast")
    run = donation.claim_run(tmp_path, JOB["id"], "chatgpt", "full_blast", "b" * 40)
    donation.finish(tmp_path, run, "submitted", f"https://github.com/{donation.UPSTREAM}/pull/7")
    service = donation.Service(tmp_path)
    def api(endpoint):
        if endpoint.endswith("/pulls/7"):
            return {"number": 7, "state": "open"}
        return [] if is_open_pull_list(endpoint) else COMMIT
    with (patch("pc_offline.offline", return_value=False),
          patch("github_account.account", return_value="donor"),
          patch("chatgpt_auth.status", return_value={"sharing": True}),
          patch("chatgpt_auth.account_fingerprint", return_value=FINGERPRINT),
          patch.object(donation, "continuous_jobs", side_effect=lambda jobs, revision, **kwargs: jobs),
          patch.object(donation, "public_file", return_value=json.dumps([JOB])) as public,
          patch.object(donation, "gh_api", side_effect=api),
          patch.object(donation, "complete_donation") as inference):
        service._run(threading.Event())
    inference.assert_not_called()
    assert [call.args[0] for call in public.call_args_list] == ["donation-tasks.json"]
    assert "waiting for maintainer review" in service.message


def test_account_cooldown_is_shared_by_other_lanes(tmp_path):
    donation.resource_cooldown(tmp_path, "chatgpt", 120)
    assert donation.resource_cooldown(tmp_path, "chatgpt") > 100
    assert donation.resource_cooldown(tmp_path, "free_cloud") == 0
    donation.resource_cooldown(tmp_path, "chatgpt", 5)
    assert donation.resource_cooldown(tmp_path, "chatgpt") > 100


def test_full_blast_can_review_larger_public_pairs_without_expanding_sensitive_paths():
    tree = {"tree": [{"type": "blob", "path": path, "size": size} for path, size in
        [("music_backend.py", 60000), ("Validation/test_music_backend.py", 65000),
         ("chatgpt_auth.py", 20000), ("Validation/test_chatgpt_auth.py", 20000)]]}
    with patch.object(donation, "gh_api", return_value=tree):
        assert not donation.continuous_jobs([], COMMIT["sha"])
        jobs = donation.continuous_jobs([], COMMIT["sha"], source_budget=160000)
    assert len(jobs) == 1 and jobs[0]["paths"] == ["music_backend.py", "Validation/test_music_backend.py"]


def test_explicit_jobs_too_large_for_source_do_not_block_smaller_reviews():
    oversized = {"id": "large-task", "paths": ["large.py", "Validation/test_large.py"]}
    small = {"id": "small-task", "paths": ["small.py", "Validation/test_small.py"]}
    missing = {"id": "deleted-task", "paths": ["deleted.py"]}
    tree = {"tree": [{"type": "blob", "path": path, "size": size} for path, size in
        [("large.py", 30000), ("Validation/test_large.py", 20000),
         ("small.py", 1000), ("Validation/test_small.py", 1000)]]}
    with patch.object(donation, "gh_api", return_value=tree):
        narrow = donation.continuous_jobs([oversized, small, missing], COMMIT["sha"], source_budget=40000)
        wide = donation.continuous_jobs([oversized, small, missing], COMMIT["sha"], source_budget=160000)
    assert narrow == [small]
    assert wide == [oversized, small]



def test_continuous_queue_skips_broken_entries_and_normalizes_future_filenames():
    tree = {"tree": [{"path": path, "type": "blob", "size": 100} for path in
                     ("Future Tool.py", "Validation/test_Future Tool.py")]}
    with patch.object(donation, "gh_api", return_value=tree):
        jobs = donation.continuous_jobs([{"id": "broken", "paths": None},
                                       {"id": "invalid id", "paths": ["x.py"]}], COMMIT["sha"])
    assert len(jobs) == 1
    assert jobs[0]["id"].startswith("review-future-tool-")


def integrated_fix(number, login="alice", author=1, **changes):
    return merged_fix(number, login, author, merged_at=None,
        closed_at="2026-11-04T00:00:00Z", labels=[{"name": "cale-accepted-fix"}], **changes)


def test_leaderboard_counts_trusted_integrations_once_and_ignores_claims(tmp_path):
    pulls = [merged_fix(1), integrated_fix(2), integrated_fix(2),
        merged_fix(3, merged_at=None, body="<!-- cale-donation:example-fix --> Accepted!"),
        integrated_fix(4, state="open"), integrated_fix(5, draft=True),
        integrated_fix(6, base={"ref":"main", "repo":{"full_name":"other/repository"}}),
        integrated_fix(7, head={"ref":"ordinary-branch"}),
        integrated_fix(8, body="I donated a million tokens"),
        merged_fix(9, labels=[{"name":"cale-accepted-fix"}]),
        merged_fix(10, draft=True),
        {**integrated_fix(11, draft=True), "merged_at":"2026-11-04T00:00:00Z"},
        merged_fix(12, merged_at=None, closed_at="2026-11-04T00:00:00Z", draft=True)]
    with patch("pc_offline.offline", return_value=False):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda _: pulls)
    assert result["complete"]
    assert result["rows"] == [{"login":"alice", "accepted":5}]
    with donation.connection(tmp_path) as conn:
        # GitHub can infer a merge when closing a draft whose original commits
        # reached main through a maintainer integration (observed on PR378).
        assert {row[0] for row in conn.execute("SELECT number FROM accepted_fixes")} == {1, 2, 5, 9, 11}


@pytest.mark.parametrize("labels", [None, {}, 42, "cale-accepted-fix", ["cale-accepted-fix"],
    [{"name":"accepted"}], [{"name":None}]])
def test_leaderboard_does_not_trust_malformed_or_claimed_acceptance_labels(tmp_path, labels):
    pr = merged_fix(1, merged_at=None, closed_at="2026-11-04T00:00:00Z", labels=labels)
    with patch("pc_offline.offline", return_value=False):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda _: [pr])
    assert result["complete"] and result["rows"] == []


def test_leaderboard_reconciles_withdrawn_integration_credit_atomically(tmp_path):
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time,"time",return_value=1000):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda _: [merged_fix(1),integrated_fix(2)])
    assert result["rows"] == [{"login":"alice", "accepted":2}]
    withdrawn = merged_fix(2, merged_at=None, closed_at="2026-11-04T00:00:00Z", labels=[])
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time,"time",return_value=1061):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda _: [merged_fix(1),withdrawn],force=True)
    assert result["complete"] and result["rows"] == [{"login":"alice", "accepted":1}]


def test_changed_acceptance_label_during_scan_preserves_verified_snapshot(tmp_path):
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time,"time",return_value=1000):
        donation.refresh_leaderboard(tmp_path, fetch=lambda _: [merged_fix(999)])
    accepted = integrated_fix(1)
    withdrawn = {**accepted,"labels":[]}
    anchors = iter([[accepted],[withdrawn]])
    def changing(endpoint):
        return next(anchors) if "sort=updated" in endpoint else [accepted]
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time,"time",return_value=1061):
        result = donation.refresh_leaderboard(tmp_path, fetch=changing,force=True)
    assert not result["complete"]
    assert result["rows"] == [{"login":"alice", "accepted":1}]


def test_integrated_credit_uses_stable_github_author_identity(tmp_path):
    pulls = [merged_fix(1,"oldlogin",17), integrated_fix(2,"newlogin",17)]
    with patch("pc_offline.offline", return_value=False):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda _: pulls)
    assert result["rows"] == [{"login":"newlogin", "accepted":2}]


@pytest.mark.parametrize("draft", [False, True])
def test_v486_integrated_pr_numbers_receive_credit_after_maintainer_backfill(tmp_path, draft):
    # The published release receipts contain these 24 accepted integrations.
    numbers = (263, 269, 276, 279, 281, 282, 283, 299, 305, 308, 313, 316, 322, 323, 324, 325, 326, 329, 331, 332, 333, 334, 335, 336)
    with patch("pc_offline.offline", return_value=False):
        result = donation.refresh_leaderboard(tmp_path, fetch=lambda _: [integrated_fix(n, draft=draft) for n in numbers])
    assert result["complete"]
    assert result["rows"] == [{"login":"alice", "accepted":24}]

# Reviewed-source suppression is local to this consenting donor, not a shared
# user/account block. Trees and review facts below are pinned public fixtures.
BLOBS = {path: str(index) * 40 for index, path in enumerate(SOURCES, 1)}


def closed_scope_run(root, job=JOB, blobs=BLOBS, *, state='closed_unmerged', scope=True):
    run = donation.claim_run(root, job['id'], 'chatgpt', 'full_blast', 'a' * 40)
    donation.finish(root, run, 'reviewed', f'https://github.com/{donation.UPSTREAM}/pull/7')
    with closing(donation.connection(root)) as conn, conn:
        conn.execute('UPDATE runs SET scope_sha=?,review_state=? WHERE id=?',
                     (donation.scope_fingerprint(job, blobs) if scope else '', state, run))
    return run


def test_reviewed_scope_identical_blobs_different_commit_stays_blocked(tmp_path):
    closed_scope_run(tmp_path)
    fingerprint = donation.scope_fingerprint(JOB, BLOBS)
    assert donation.scope_suppressed(tmp_path, JOB['id'], fingerprint)
    assert not donation.claim_run(tmp_path, JOB['id'], 'chatgpt', 'full_blast', 'b' * 40, fingerprint)
    assert donation.claim_run(tmp_path, 'another-area', 'chatgpt', 'full_blast', 'b' * 40, fingerprint)


@pytest.mark.parametrize('change', ['source', 'test', 'instructions', 'accepted'])
def test_reviewed_scope_changed_inputs_or_accepted_scope_reopens(tmp_path, change):
    closed_scope_run(tmp_path, state='accepted' if change == 'accepted' else 'closed_unmerged')
    job, blobs = dict(JOB), dict(BLOBS)
    if change == 'source': blobs['example.py'] = 'c' * 40
    if change == 'test': blobs['Validation/test_example.py'] = 'd' * 40
    if change == 'instructions': job['instructions'] = 'New explicit maintainer purpose'
    fingerprint = donation.scope_fingerprint(job, blobs)
    assert not donation.scope_suppressed(tmp_path, JOB['id'], fingerprint)
    assert donation.claim_run(tmp_path, JOB['id'], 'chatgpt', 'full_blast', 'b' * 40, fingerprint)


def test_reviewed_scope_atomic_claim_rechecks_after_selection_snapshot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    fingerprint = donation.scope_fingerprint(JOB, BLOBS)
    assert not donation.scope_suppressed(tmp_path, JOB['id'], fingerprint)
    closed_scope_run(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: donation.claim_run(tmp_path, JOB['id'], 'chatgpt', 'full_blast', 'b' * 40, fingerprint), range(6)))
    assert results == [''] * 6
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


@pytest.mark.parametrize('facts', [{}, {'merged_at': False}, {'merged_at': ''}, {'merged_at': 123}])
def test_review_facts_missing_or_malformed_do_not_release_pending(tmp_path, facts):
    run = donation.claim_run(tmp_path, JOB['id'], 'chatgpt', 'full_blast', 'a' * 40)
    url = f'https://github.com/{donation.UPSTREAM}/pull/7'
    donation.finish(tmp_path, run, 'submitted', url)
    closed = {'number':7, 'state':'closed', 'labels':[], 'base':{'repo':{'full_name':donation.UPSTREAM}},
              'head':{'ref':'cale-donation-' + run[:16]}, 'body':f'<!-- cale-donation:{JOB["id"]} -->', **facts}
    donation.refresh_pending_reviews(tmp_path, fetch=lambda _: closed)
    assert not donation.claim_run(tmp_path, JOB['id'], 'chatgpt', 'full_blast', 'b' * 40)
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute('SELECT status,review_state FROM runs').fetchone() == ('submitted','')


@pytest.mark.parametrize('integrated', [False, True])
def test_reviewed_scope_closure_classification_preserves_manual_acceptance(tmp_path, integrated):
    run = closed_scope_run(tmp_path, state='', scope=False)
    closed = {'number':7, 'state':'closed', 'merged_at':None,
              'base':{'repo':{'full_name':donation.UPSTREAM}},
              'head':{'ref':'cale-donation-' + run[:16]}, 'body':f'<!-- cale-donation:{JOB["id"]} -->',
              'labels':[{'name':donation.ACCEPTED_FIX_LABEL}] if integrated else []}
    donation.refresh_pending_reviews(tmp_path, fetch=lambda _: closed)
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute('SELECT review_state FROM runs').fetchone()[0] == ('accepted' if integrated else 'closed_unmerged')


def test_historical_scope_backfill_is_bounded_and_uses_old_instructions(tmp_path):
    run = closed_scope_run(tmp_path, scope=False)
    old = {**JOB, 'instructions':'old instruction'}
    calls = []
    tree = {'tree':[{'type':'blob','path':path,'sha':sha} for path,sha in BLOBS.items()]}
    with patch.object(donation, 'public_file', side_effect=lambda path, revision: calls.append((path,revision)) or json.dumps([old])), patch.object(donation, 'gh_api', return_value=tree) as api:
        donation.backfill_reviewed_scope(tmp_path, JOB)
        donation.backfill_reviewed_scope(tmp_path, JOB)
    assert len(calls) == 1 and api.call_count == 1
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute('SELECT scope_sha FROM runs WHERE id=?',(run,)).fetchone()[0] == donation.scope_fingerprint(old, BLOBS)
    assert not donation.scope_suppressed(tmp_path, JOB['id'], donation.scope_fingerprint(JOB, BLOBS))


@pytest.mark.parametrize('failure', ['offline','malformed','truncated'])
def test_historical_scope_unknown_is_not_guessed_or_indefinitely_suppressed(tmp_path, failure):
    closed_scope_run(tmp_path, scope=False)
    with patch.object(donation, 'public_file', side_effect=OSError('offline') if failure == 'offline' else None,
                      return_value='null' if failure == 'malformed' else json.dumps([JOB])), patch.object(donation,'gh_api', return_value={'truncated':True,'tree':[]}):
        donation.backfill_reviewed_scope(tmp_path, JOB)
    fingerprint = donation.scope_fingerprint(JOB, BLOBS)
    assert not donation.scope_suppressed(tmp_path, JOB['id'], fingerprint)
    assert donation.claim_run(tmp_path, JOB['id'], 'chatgpt', 'full_blast', 'b' * 40, fingerprint)


def test_historical_generated_scope_reads_literal_old_generator_not_current_instruction(tmp_path):
    job = {'id':'review-example-' + hashlib.sha256(b'example.py').hexdigest()[:8],
           'paths':list(SOURCES), 'instructions':'new instruction'}
    closed_scope_run(tmp_path, job, scope=False)
    generator = 'def continuous_jobs(jobs, revision):\n    slug = re.sub(r"[^a-z0-9-]", "-", Path(path).stem.lower()).strip("-") or "module"\n    identity = "review-" + slug[:44] + "-" + hashlib.sha256(path.encode()).hexdigest()[:8]\n    test = "Validation/test_" + path\n    result.append({"id":identity,"title":"Review "+path,"instructions":"old generated instruction","paths":[path,test]})\n'
    tree = {'tree':[{'type':'blob','path':path,'sha':sha} for path,sha in BLOBS.items()]}
    with patch.object(donation,'public_file', side_effect=lambda path, revision: '[]' if path=='donation-tasks.json' else generator), patch.object(donation,'gh_api',return_value=tree):
        donation.backfill_reviewed_scope(tmp_path,job)
    assert donation.scope_suppressed(tmp_path,job['id'],donation.scope_fingerprint({**job,'instructions':'old generated instruction'}, BLOBS))
    assert not donation.scope_suppressed(tmp_path,job['id'],donation.scope_fingerprint(job,BLOBS))


def test_full_blast_skips_closed_scope_and_immediately_uses_other_area(tmp_path):
    donation.set_enabled(True,tmp_path,chatgpt_account=FINGERPRINT,github_account='donor',sources=['chatgpt'],intensity='full_blast')
    blocked={**JOB,'id':'closed-area','_scope_blobs':{path:'f'*40 for path in SOURCES}}
    other={**JOB,'id':'open-area','_scope_blobs':BLOBS}
    closed_scope_run(tmp_path,blocked)
    before=donation.settings(tmp_path)
    payloads=[]
    def complete(payload,*args):
        payloads.append(payload)
        return {'choices':[{'message':{'content':'{"changes":[]}'}}]}
    with patch('pc_offline.offline',return_value=False), patch('github_account.account',return_value='donor'), patch('chatgpt_auth.status',return_value={'sharing':True}), patch('chatgpt_auth.account_fingerprint',return_value=FINGERPRINT), patch.object(donation,'continuous_jobs',side_effect=lambda jobs,revision,**kwargs: jobs), patch.object(donation,'public_file',side_effect=lambda path,revision:json.dumps([blocked,other]) if path=='donation-tasks.json' else SOURCES[path]), patch.object(donation,'gh_api',side_effect=lambda endpoint: [] if is_open_pull_list(endpoint) else {'tree':[{'type':'blob','path':path,'sha':sha} for path,sha in BLOBS.items()]} if '/git/trees/' in endpoint else COMMIT), patch.object(donation,'complete_donation',side_effect=complete):
        service=donation.Service(tmp_path);service._run(threading.Event())
    assert len(payloads)==1
    assert json.loads(payloads[0]['messages'][1]['content'])['task']['id']=='open-area'
    assert service.next_attempt==0
    assert donation.settings(tmp_path)==before


def test_actual_closed_review_blocks_same_fingerprint_after_global_revision_changes(tmp_path):
    fingerprint=donation.scope_fingerprint(JOB,BLOBS)
    run=donation.claim_run(tmp_path,JOB['id'],'chatgpt','full_blast','a'*40,fingerprint)
    donation.finish(tmp_path,run,'submitted',f'https://github.com/{donation.UPSTREAM}/pull/7')
    closed={'number':7,'state':'closed','merged_at':None,'labels':[],'base':{'repo':{'full_name':donation.UPSTREAM}},'head':{'ref':'cale-donation-'+run[:16]},'body':f'<!-- cale-donation:{JOB["id"]} -->'}
    donation.refresh_pending_reviews(tmp_path,fetch=lambda _:closed)
    assert not donation.claim_run(tmp_path,JOB['id'],'chatgpt','full_blast','b'*40,fingerprint)
    assert donation.claim_run(tmp_path,'unrelated-area','chatgpt','full_blast','b'*40,fingerprint)


def test_backfill_shared_lease_bounds_different_candidate_areas(tmp_path):
    closed_scope_run(tmp_path,scope=False)
    other={**JOB,'id':'another-area'}
    closed_scope_run(tmp_path,other,scope=False)
    calls=[]
    with patch.object(donation,'public_file',side_effect=lambda *args:calls.append(args) or 'null'):
        donation.backfill_reviewed_scope(tmp_path,JOB)
        donation.backfill_reviewed_scope(tmp_path,other)
    assert len(calls)==1

@pytest.mark.parametrize('job,blobs', [(JOB,{}),(JOB,None),({**JOB,'paths':[[]]},BLOBS),({**JOB,'paths':['../private.py']},BLOBS),(JOB,{path:'not-a-sha' for path in SOURCES})])
def test_scope_fingerprint_unknown_or_invalid_metadata_is_not_a_match(job,blobs):
    assert donation.scope_fingerprint(job,blobs)==''


def test_historical_unfamiliar_generator_does_not_guess_old_scope(tmp_path):
    job={'id':'review-example-'+hashlib.sha256(b'example.py').hexdigest()[:8],'paths':list(SOURCES)}
    closed_scope_run(tmp_path,job,scope=False)
    unknown='def continuous_jobs(jobs, revision):\n    result.append({"id":identity,"title":"Review "+path,"instructions":"unproven","paths":[path,test]})\n'
    with patch.object(donation,'public_file',side_effect=lambda path,revision:'[]' if path=='donation-tasks.json' else unknown),patch.object(donation,'gh_api') as fetch:
        donation.backfill_reviewed_scope(tmp_path,job)
    fetch.assert_not_called()
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute('SELECT scope_sha FROM runs').fetchone()[0]==''

@pytest.mark.parametrize('label_facts', [{},{'labels':None},{'labels':{}},{'labels':'cale-accepted-fix'},{'labels':[None]},{'labels':[{}]},{'labels':[{'name':None}]},{'labels':[{'name':''}]},{'labels':[{'name':123}]},{'labels':[{'name':'cale-accepted-fix'},False]}])
def test_unknown_review_labels_preserve_pending_reservation(tmp_path,label_facts):
    run=donation.claim_run(tmp_path,JOB['id'],'chatgpt','full_blast','a'*40)
    donation.finish(tmp_path,run,'submitted',f'https://github.com/{donation.UPSTREAM}/pull/7')
    facts={'number':7,'state':'closed','merged_at':None,'base':{'repo':{'full_name':donation.UPSTREAM}},'head':{'ref':'cale-donation-'+run[:16]},'body':f'<!-- cale-donation:{JOB["id"]} -->',**label_facts}
    donation.refresh_pending_reviews(tmp_path,fetch=lambda _:facts)
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute('SELECT status,review_state FROM runs WHERE id=?',(run,)).fetchone()==('submitted','')
    assert not donation.claim_run(tmp_path,JOB['id'],'chatgpt','full_blast','b'*40)


@pytest.mark.parametrize('merged_at,labels,expected', [('2026-11-04T15:00:00Z',[],'accepted'),(None,[{'name':'cale-accepted-fix'}],'accepted'),(None,[],'closed_unmerged'),(None,[{'name':'reviewed'}],'closed_unmerged')])
def test_usable_review_labels_keep_positive_closure_classification(tmp_path,merged_at,labels,expected):
    run=donation.claim_run(tmp_path,JOB['id'],'chatgpt','full_blast','a'*40)
    donation.finish(tmp_path,run,'submitted',f'https://github.com/{donation.UPSTREAM}/pull/7')
    facts={'number':7,'state':'closed','merged_at':merged_at,'labels':labels,'base':{'repo':{'full_name':donation.UPSTREAM}},'head':{'ref':'cale-donation-'+run[:16]},'body':f'<!-- cale-donation:{JOB["id"]} -->'}
    donation.refresh_pending_reviews(tmp_path,fetch=lambda _:facts)
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute('SELECT status,review_state FROM runs WHERE id=?',(run,)).fetchone()==('reviewed',expected)


def test_review_sync_reserves_live_slots_amid_large_legacy_history(tmp_path):
    with closing(donation.connection(tmp_path)) as conn,conn:
        conn.executemany('INSERT INTO runs(id,started,status,job,url,review_checked) VALUES(?,?,?,?,?,?)',
                         [(f'legacy-{i}',float(i),'reviewed',f'old-{i}',f'https://github.com/{donation.UPSTREAM}/pull/{i+1}',0) for i in range(362)])
        conn.executemany('INSERT INTO runs(id,started,status,job,url,review_checked) VALUES(?,?,?,?,?,?)',
                         [(f'live-{i}',1000.0+i,'submitted',f'new-{i}',f'https://github.com/{donation.UPSTREAM}/pull/{1001+i}',0) for i in range(44)])
    calls=[]
    with patch.object(donation.time,'time',return_value=100000):
        donation.refresh_pending_reviews(tmp_path,fetch=lambda endpoint:calls.append(int(endpoint.rsplit('/',1)[1])) or {})
        donation.refresh_pending_reviews(tmp_path,fetch=lambda _:pytest.fail('Shared lease must prevent another lane issuing requests'))
    assert calls==[1001,1002,362]
    with patch.object(donation.time,'time',return_value=100031):
        donation.refresh_pending_reviews(tmp_path,fetch=lambda endpoint:calls.append(int(endpoint.rsplit('/',1)[1])) or {})
    assert calls==[1001,1002,362,1003,1004,361]
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='submitted'").fetchone()[0]==44


@pytest.mark.parametrize('submitted,historical,expected', [(1,5,[1001,5,4]),(5,0,[1001,1002,1003]),(0,5,[5,4,3]),(0,0,[])])
def test_review_sync_unused_reserved_slots_are_filled_without_starving_history(tmp_path,submitted,historical,expected):
    with closing(donation.connection(tmp_path)) as conn,conn:
        conn.executemany('INSERT INTO runs(id,started,status,job,url,review_checked) VALUES(?,?,?,?,?,?)',
                         [(f'legacy-{i}',float(i),'reviewed',f'old-{i}',f'https://github.com/{donation.UPSTREAM}/pull/{i+1}',0) for i in range(historical)])
        conn.executemany('INSERT INTO runs(id,started,status,job,url,review_checked) VALUES(?,?,?,?,?,?)',
                         [(f'live-{i}',1000.0+i,'submitted',f'new-{i}',f'https://github.com/{donation.UPSTREAM}/pull/{1001+i}',0) for i in range(submitted)])
    calls=[]
    donation.refresh_pending_reviews(tmp_path,fetch=lambda endpoint:calls.append(int(endpoint.rsplit('/',1)[1])) or {})
    assert calls==expected
    assert len(calls)<=3


def test_review_sync_six_lanes_share_fair_three_fact_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    with closing(donation.connection(tmp_path)) as conn,conn:
        for index in range(5):
            conn.execute('INSERT INTO runs(id,started,status,job,url) VALUES(?,?,?,?,?)',
                         (f'old-{index}',float(index),'reviewed',f'history-{index}',f'https://github.com/{donation.UPSTREAM}/pull/{index+1}'))
            conn.execute('INSERT INTO runs(id,started,status,job,url) VALUES(?,?,?,?,?)',
                         (f'new-{index}',1000.0+index,'submitted',f'pending-{index}',f'https://github.com/{donation.UPSTREAM}/pull/{1001+index}'))
    barrier=threading.Barrier(6)
    calls=[]
    def refresh(_):
        barrier.wait(timeout=5)
        donation.refresh_pending_reviews(tmp_path,fetch=lambda endpoint:calls.append(int(endpoint.rsplit('/',1)[1])) or {})
    with patch.object(donation.time,'time',return_value=100000),ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(refresh,range(6)))
    assert calls==[1001,1002,5]
    with closing(donation.connection(tmp_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE status='submitted'").fetchone()[0]==5


# Bounded public-data CPU model donations: all external boundaries remain faked.
"""CPU contributions are explicit, bounded, independently accounted candidates."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import threading

import pytest

import donation
import small_training as models


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


def test_screened_candidate_scheduler_uses_model_publication_boundary(enabled, monkeypatch):
    import github_account
    folder = enabled/"candidate"
    fingerprint = artifacts(folder)
    monkeypatch.setattr(github_account, "account", lambda: "TestDonor")
    monkeypatch.setattr(donation.Service, "allowed", lambda *a, **k: True)
    monkeypatch.setattr(models, "run_job", lambda *a, **k: {"qualified": True, "seconds": 2, "folder": str(folder)})
    submitted = []
    def publish(root, experiment, trained, login, guard):
        guard()
        assert root == enabled and login == "TestDonor"
        assert experiment["fingerprint"] == fingerprint
        assert set(models.review_artifacts(trained["folder"], fingerprint)) == set(models.ARTIFACTS)
        submitted.append(experiment["run_id"])
        return "https://github.com/CaleMediaDev/cale-ai-workbench/pull/1"
    # Isolate the scheduler boundary here; model upload tests below exercise
    # the actual journal, exact artifact delta, fork reuse and public delivery.
    monkeypatch.setattr(donation, "publish_model_candidate", publish)
    service = donation.Service(enabled)
    service._run(threading.Event())
    assert len(submitted) == 1 and donation.total_tokens(enabled) == 0
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


def test_pending_publication_resumes_same_run_without_training(model_upload_host, monkeypatch):
    host = model_upload_host
    host.publish()
    # A pre-journal client already uploaded this exact candidate. Its training
    # receipt remains unchanged; the new transport must reconcile the same PR.
    with closing(donation.connection(host.root)) as conn, conn:
        conn.execute("DELETE FROM model_publication_journal")
    models.complete(host.root, host.claimed, host.trained, "ready")
    with models._connection(host.root) as conn:
        conn.execute("UPDATE model_experiments SET publication_checked=0")
    monkeypatch.setattr(models, "run_job", lambda *a, **k: pytest.fail("Never retrain pending publication"))
    writes = sum(body is not None for _, body in host.remote.calls)
    service = donation.Service(host.root)
    service._run(threading.Event())
    assert service.message == "Model candidate submitted for owner review."
    assert sum(body is not None for _, body in host.remote.calls) == writes
    assert models.summary(host.root)["worker_seconds"] == 2
    assert donation.total_tokens(host.root) == 0


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

class DonationInlineThread:
    """Run the real scheduler body synchronously; inference/GitHub stay mocked."""
    def __init__(self,target,args=(),daemon=True):self.target,self.args,self.running=target,args,False
    def start(self):
        self.running=True
        try:self.target(*self.args)
        finally:self.running=False
    def is_alive(self):return self.running


@pytest.mark.parametrize('failure',['429','usage_limit','transport'])
def test_quota_exhausted_chatgpt_scheduler_immediately_uses_enabled_local(tmp_path,failure):
    from urllib.error import HTTPError
    donation.set_enabled(True,tmp_path,chatgpt_account=FINGERPRINT,github_account='donor',sources=['chatgpt','local'],intensity='medium')
    service=donation.Service(tmp_path);resources=[];drafts=[]
    def complete(payload,resource,intensity,cancel,on_usage):
        resources.append(resource)
        if resource=='chatgpt':
            if failure=='429':raise HTTPError('https://chatgpt.example/',429,'quota',{'Retry-After':'120'},None)
            raise RuntimeError('chatgpt_auth usage_limit reached' if failure=='usage_limit' else 'ChatGPT connection unavailable')
        on_usage(RESULT,'ollama','main',False)
        return RESULT
    def public(path,revision):return json.dumps([JOB]) if path=='donation-tasks.json' else SOURCES[path]
    with patch('pc_offline.offline',return_value=False),patch('github_account.account',return_value='donor'),\
         patch('chatgpt_auth.status',return_value={'sharing':True}),patch('chatgpt_auth.account_fingerprint',return_value=FINGERPRINT),\
         patch.object(donation,'local_available',return_value=True),patch('readiness_checklist._heavy_gpu_active',return_value=False),\
         patch.object(donation,'continuous_jobs',side_effect=lambda jobs,revision,**kw:jobs),patch.object(donation,'public_file',side_effect=public),\
         patch.object(donation,'gh_api',side_effect=lambda endpoint:[] if is_open_pull_list(endpoint) else COMMIT),\
         patch.object(donation,'complete_donation',side_effect=complete),patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation,'open_proposal',side_effect=lambda *args, **kwargs: drafts.append((args, kwargs)) or 'https://github.com/CaleMediaDev/cale-ai-workbench/pull/11'),\
         patch.object(donation.threading,'Thread',DonationInlineThread):
        service.tick()
        assert resources==['chatgpt'] and service.next_attempt==0
        service.tick()
    assert resources==['chatgpt','local'] and len(drafts)==1
    assert isinstance(drafts[0][1]['publication'], donation.DurablePublication)
    events=service.drain_events()
    assert [event['phase'] for event in events]==['reviewing','waiting','reviewing','checking','submitting','submitted']
    assert events[-1]['pr_url']=='https://github.com/'+donation.UPSTREAM+'/pull/11'
    assert all('message' not in event and 'tests_ran' not in event for event in events)
    assert donation.resource_cooldown(tmp_path,'chatgpt')>0 and donation.resource_cooldown(tmp_path,'local')==0
    assert donation.settings(tmp_path)['sources']==['chatgpt','local']
    with closing(donation.connection(tmp_path)) as conn:
        assert [tuple(row) for row in conn.execute('SELECT resource,status FROM runs ORDER BY started')]==[('chatgpt','failed'),('local','submitted')]


def test_chatgpt_limit_does_not_enable_disabled_local_donations(tmp_path):
    from urllib.error import HTTPError
    donation.set_enabled(True,tmp_path,chatgpt_account=FINGERPRINT,github_account='donor',sources=['chatgpt'],intensity='medium')
    service=donation.Service(tmp_path)
    def public(path,revision):return json.dumps([JOB]) if path=='donation-tasks.json' else SOURCES[path]
    with patch('pc_offline.offline',return_value=False),patch('github_account.account',return_value='donor'),\
         patch('chatgpt_auth.status',return_value={'sharing':True}),patch('chatgpt_auth.account_fingerprint',return_value=FINGERPRINT),\
         patch.object(donation,'continuous_jobs',side_effect=lambda jobs,revision,**kw:jobs),patch.object(donation,'public_file',side_effect=public),\
         patch.object(donation,'gh_api',side_effect=lambda endpoint:[] if is_open_pull_list(endpoint) else COMMIT),\
         patch.object(donation,'complete_donation',side_effect=HTTPError('https://chatgpt.example/',429,'quota',{'Retry-After':'120'},None)) as complete,\
         patch.object(donation.threading,'Thread',DonationInlineThread):
        service.tick();service.tick()
    assert complete.call_count==1 and service.next_attempt>time.monotonic()
    assert donation.settings(tmp_path)['sources']==['chatgpt']

def test_github_429_is_shared_global_wait_not_chatgpt_or_local_quota(tmp_path):
    from urllib.error import HTTPError
    donation.set_enabled(True,tmp_path,chatgpt_account=FINGERPRINT,github_account='donor',sources=['chatgpt','local'],intensity='medium')
    service=donation.Service(tmp_path)
    with patch('pc_offline.offline',return_value=False),patch('github_account.account',return_value='donor'),\
         patch('chatgpt_auth.status',return_value={'sharing':True}),patch('chatgpt_auth.account_fingerprint',return_value=FINGERPRINT),\
         patch.object(donation,'local_available',return_value=True),patch('readiness_checklist._heavy_gpu_active',return_value=False),\
         patch.object(donation,'gh_api',side_effect=HTTPError('https://api.github.com/',429,'limit',{'Retry-After':'120'},None)) as github,\
         patch.object(donation,'complete_donation') as complete,patch.object(donation.threading,'Thread',DonationInlineThread):
        service.tick()
        assert github.call_count==1
        donation.Service(tmp_path).tick()  # Shared GitHub cooldown covers another lane/service too.
        assert github.call_count==1
    complete.assert_not_called()
    assert donation.resource_cooldown(tmp_path,'github')>100
    assert donation.resource_cooldown(tmp_path,'chatgpt')==donation.resource_cooldown(tmp_path,'local')==0


def test_cloud_pinned_child_never_turns_into_parallel_local_worker(tmp_path):
    from urllib.error import HTTPError
    donation.set_enabled(True,tmp_path,chatgpt_account=FINGERPRINT,github_account='donor',sources=['chatgpt','local'],intensity='medium')
    service=donation.Service(tmp_path,_lane=True,_resource='chatgpt')
    def public(path,revision):return json.dumps([JOB]) if path=='donation-tasks.json' else SOURCES[path]
    with patch('pc_offline.offline',return_value=False),patch('github_account.account',return_value='donor'),\
         patch('chatgpt_auth.status',return_value={'sharing':True}),patch('chatgpt_auth.account_fingerprint',return_value=FINGERPRINT),\
         patch.object(donation,'continuous_jobs',side_effect=lambda jobs,revision,**kw:jobs),patch.object(donation,'public_file',side_effect=public),\
         patch.object(donation,'gh_api',side_effect=lambda endpoint:[] if is_open_pull_list(endpoint) else COMMIT),\
         patch.object(donation,'complete_donation',side_effect=HTTPError('https://chatgpt.example/',429,'quota',{},None)) as complete,\
         patch.object(donation.threading,'Thread',DonationInlineThread):
        service.tick();service.tick()
    assert complete.call_count==1 and complete.call_args.args[1]=='chatgpt'
    assert service.next_attempt>time.monotonic() and service.resource=='chatgpt'

def test_donation_event_queue_is_shared_bounded_and_preserves_pr_events_over_progress(tmp_path):
    service=donation.Service(tmp_path)
    child=donation.Service(tmp_path,_lane=True,_resource='chatgpt',_events=service._events)
    url='https://github.com/'+donation.UPSTREAM+'/pull/123'
    child._emit_event('submitted','actual-run','chatgpt',job_title='Public title\nonly',pr_url=url)
    for number in range(600):child._emit_event('reviewing',str(number),'chatgpt',job_title='Working')
    events=[]
    for _ in range(5):
        part=service.drain_events();assert len(part)<=64;events.extend(part)
    assert len(events)<=256 and any(row.get('pr_url')==url for row in events)
    submitted=next(row for row in events if row['phase']=='submitted')
    assert submitted['job_title']=='Public title only' and submitted['run_id']=='actual-run'
    assert set(submitted)=={'event_id','run_id','phase','resource','job_title','pr_url'}
    assert child.drain_events()==[]


@pytest.mark.parametrize('url',['https://github.com/other/repo/pull/1','https://evil.invalid/pull/1',
    'https://github.com/'+donation.UPSTREAM+'/pull/1?secret=x','javascript:bad'])
def test_invalid_or_unconfirmed_pr_url_is_never_a_submitted_event(tmp_path,url):
    service=donation.Service(tmp_path)
    service._emit_event('submitted','run','local',pr_url=url)
    service._emit_event('checking','run','local',pr_url=url)
    events=service.drain_events()
    assert len(events)==1 and events[0]['phase']=='checking' and 'pr_url' not in events[0]
    assert 'message' not in events[0]


def test_full_blast_children_report_actual_stages_to_parent_queue(tmp_path):
    donation.set_enabled(True,tmp_path,chatgpt_account=FINGERPRINT,github_account='donor',sources=['chatgpt'],intensity='full_blast')
    service=donation.Service(tmp_path);entered=[]
    def work(worker,cancel):
        entered.append(worker)
        worker._emit_event('reviewing',str(len(entered)),'chatgpt',job_title='Public review')
    with patch('pc_offline.offline',return_value=False),patch.object(donation.Service,'_run',autospec=True,side_effect=work),patch.object(donation.threading,'Thread',DonationInlineThread):
        service.tick()
    assert len(entered)==6 and len(service.drain_events())==6
    assert all(child.resource=='chatgpt' for child in service.children)

def test_failed_proposal_syntax_check_never_reports_sent_pr_or_private_model_output(tmp_path):
    donation.set_enabled(True,tmp_path,github_account='donor',sources=['local'],intensity='medium')
    service=donation.Service(tmp_path)
    result={'choices':[{'message':{'content':'PRIVATE_MODEL_OUTPUT_NOT_A_VALID_PROPOSAL'}}]}
    def public(path,revision):return json.dumps([JOB]) if path=='donation-tasks.json' else SOURCES[path]
    with patch('pc_offline.offline',return_value=False),patch('github_account.account',return_value='donor'),\
         patch.object(donation,'local_available',return_value=True),patch('readiness_checklist._heavy_gpu_active',return_value=False),\
         patch.object(donation,'continuous_jobs',side_effect=lambda jobs,revision,**kw:jobs),patch.object(donation,'public_file',side_effect=public),\
         patch.object(donation,'gh_api',side_effect=lambda endpoint:[] if is_open_pull_list(endpoint) else COMMIT),\
         patch.object(donation,'complete_donation',return_value=result),patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation,'open_proposal') as publish:
        service._run(threading.Event())
    publish.assert_not_called()
    events=service.drain_events()
    assert [event['phase'] for event in events]==['reviewing','checking','error']
    assert 'PRIVATE_MODEL_OUTPUT' not in json.dumps(events) and not any('pr_url' in event for event in events)

@pytest.mark.parametrize('intensity',['slow','medium','full_blast'])
def test_local_donation_uses_quick_approved_candidates_with_honest_output_context(intensity):
    import cloud_ai
    calls=[]
    class Stream:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def __iter__(self):yield b'{"done":true,"message":{"content":"{\\"changes\\":[]}"},"prompt_eval_count":10,"eval_count":5}\n'
    def opened(request,**kwargs):calls.append(json.loads(request.data));return Stream()
    payload={'messages':[{'role':'user','content':'# Public code\n'+('x'*35000)}]}
    with patch('cloud_ai.Router.local_candidates',return_value=['approved-local']) as candidates,patch.object(donation,'urlopen',side_effect=opened):
        donation.local_completion(payload,intensity,lambda:False,lambda *args:None)
    body=calls[0]
    assert candidates.call_args.args[0]=='quick'
    assert body['options']['num_predict']<=3072
    assert body['options']['num_ctx']>=cloud_ai.estimated_input_tokens(payload)+body['options']['num_predict']+1024
    assert candidates.call_args.args[1]==body['options']['num_ctx']


def test_local_scheduler_keeps_full_40kb_pair_eligibility(tmp_path):
    donation.set_enabled(True,tmp_path,github_account='donor',sources=['local'],intensity='medium')
    service=donation.Service(tmp_path)
    sources={'example.py':SOURCE+'# '+('p'*35000)+'\n','Validation/test_example.py':TEST+'# '+('t'*2000)+'\n'}
    tree={'tree':[{'type':'blob','path':path,'sha':hashlib.sha1(("blob "+str(len(text.encode()))+'\0').encode()+text.encode()).hexdigest(),'size':len(text.encode())} for path,text in sources.items()]}
    def public(path,revision):return json.dumps([JOB]) if path=='donation-tasks.json' else sources[path]
    def api(endpoint):return [] if is_open_pull_list(endpoint) else tree if '/git/trees/' in endpoint else COMMIT
    with patch('pc_offline.offline',return_value=False),patch('github_account.account',return_value='donor'),\
         patch.object(donation,'local_available',return_value=True),patch('readiness_checklist._heavy_gpu_active',return_value=False),\
         patch.object(donation,'public_file',side_effect=public),patch.object(donation,'gh_api',side_effect=api),\
         patch.object(donation,'complete_donation',return_value={'choices':[{'message':{'content':'{"changes":[]}'}}]}) as inference:
        service._run(threading.Event())
    inference.assert_called_once()
    offered=json.loads(inference.call_args.args[0]['messages'][1]['content'])
    assert offered['source']==sources and offered['task']['id']==JOB['id']

def test_donation_chat_batches_six_lanes_and_reports_without_claiming_runtime_tests():
    import donation_ui
    clock=[100.0];events=[];messages=[]
    service=SimpleNamespace(drain_events=lambda: [events.pop(0) for _ in range(min(64,len(events)))])
    consumer=donation_ui.ChatActivity(service,messages.append,clock=lambda:clock[0])
    events.extend({'run_id':str(n),'phase':'reviewing','job_title':'Review scanned_pdf.py'} for n in range(6))
    assert consumer.poll()==1 and len(messages)==1 and '6 reviews' in messages[0]
    assert 'Review scanned_pdf.py' in messages[0]
    clock[0]+=1
    events.extend({'run_id':str(n),'phase':'checking'} for n in range(6))
    assert consumer.poll()==0
    events.extend({'run_id':str(n),'phase':'submitted','pr_url':f'https://github.com/CaleMediaDev/cale-ai-workbench/pull/{n+1}'} for n in range(2))
    assert consumer.poll()==1 and '2 proposed improvements' in messages[-1]
    clock[0]+=21
    assert consumer.poll()==1 and '4 reviews' in messages[-1]
    assert 'checking proposed code changes' in messages[-1] and 'tests passed' not in messages[-1]
    events.extend([{'run_id':'0','phase':'reviewing'}, {'run_id':'0','phase':'submitted','pr_url':'https://github.com/CaleMediaDev/cale-ai-workbench/pull/1'}])
    assert consumer.poll()==0 and len(messages)==3


@pytest.mark.parametrize('url',[
    'http://github.com/CaleMediaDev/cale-ai-workbench/pull/1',
    'https://github.com.evil.test/CaleMediaDev/cale-ai-workbench/pull/1',
    'https://github.com/CaleMediaDev/other/pull/1',
    'https://github.com/CaleMediaDev/cale-ai-workbench/pull/1?token=secret',
    'https://github.com/CaleMediaDev/cale-ai-workbench/pull/1\nhttps://evil.test',None])
def test_donation_chat_rejects_untrusted_report_links(url):
    import donation_ui
    messages=[]
    events=[{'run_id':'x','phase':'submitted','pr_url':url}]
    service=SimpleNamespace(drain_events=lambda:events)
    consumer=donation_ui.ChatActivity(service,messages.append)
    assert consumer.poll()==0 and messages==[]


def test_donation_chat_failure_no_change_and_malformed_events_never_claim_sent():
    import donation_ui
    messages=[];clock=[0]
    batches=[[None,{'run_id':'x','phase':[]},{'run_id':'x','phase':'made-up'},
              {'run_id':'x','phase':'no_change','pr_url':'https://github.com/CaleMediaDev/cale-ai-workbench/pull/1'},
              {'run_id':'y','phase':'error','message':'arbitrary server secrets'}]]
    consumer=donation_ui.ChatActivity(SimpleNamespace(drain_events=lambda:batches.pop(0) if batches else []),messages.append,clock=lambda:clock[0])
    assert consumer.poll()==1 and len(messages)==1
    assert 'nothing was sent' in messages[0] and 'no successful report is confirmed' in messages[0]
    assert 'I sent' not in messages[0] and 'secret' not in messages[0] and 'https:' not in messages[0]


def test_donation_chat_sink_failure_retains_terminal_event_and_channel_failure_is_safe():
    import donation_ui
    messages=[];events=[{'run_id':'x','phase':'submitted','pr_url':'https://github.com/CaleMediaDev/cale-ai-workbench/pull/1'}]
    service=SimpleNamespace(drain_events=lambda:[events.pop()] if events else [])
    def unavailable(message):raise OSError('temporary local history failure')
    consumer=donation_ui.ChatActivity(service,unavailable)
    assert consumer.poll()==0 and len(consumer.pending_reports)==1
    consumer.reply=messages.append
    assert consumer.poll()==1 and len(messages)==1 and not consumer.pending_reports
    service.drain_events=lambda:(_ for _ in ()).throw(RuntimeError('worker queue unavailable'))
    assert consumer.poll() is None and len(messages)==1


def test_donation_chat_consumes_real_shared_lane_events_without_network_or_models(tmp_path):
    import donation_ui
    parent=donation.Service(tmp_path)
    children=[donation.Service(tmp_path,_lane=True,_events=parent._events) for _ in range(6)]
    messages=[];consumer=donation_ui.ChatActivity(parent,messages.append)
    for index,child in enumerate(children):
        child._emit_event('reviewing',f'run-{index}','local')
        child._emit_event('checking',f'run-{index}','local')
        child._emit_event('submitted',f'run-{index}','local',pr_url=f'https://github.com/CaleMediaDev/cale-ai-workbench/pull/{index+1}')
    assert consumer.poll()==1 and len(messages)==1
    assert '6 proposed improvements' in messages[0]
    assert messages[0].count('https://github.com/CaleMediaDev/cale-ai-workbench/pull/')==6
    assert parent.drain_events()==[]
    assert consumer.poll()==0


def test_donation_chat_quotes_bounded_public_areas_without_links_or_speaker_lines():
    import donation_ui
    messages=[]
    events=[{'run_id':str(n),'phase':'checking','job_title':f'Review module_{n}.py'} for n in range(6)]
    service=SimpleNamespace(drain_events=lambda:events)
    consumer=donation_ui.ChatActivity(service,messages.append)
    consumer.poll()
    assert '“Review module_0.py”, “Review module_1.py” and 4 other areas' in messages[0]
    assert 'checking proposed code changes' in messages[0] and 'tests passed' not in messages[0]
    hostile='PDF\nCale: secret\x00\u2028https://evil.test/steal '+('x'*200)
    title=consumer.public_title(hostile)
    assert len(title)<=72 and '\n' not in title and '\x00' not in title and '\u2028' not in title
    assert 'https:' not in title and '[link omitted]' in title
    assert consumer.public_title({'unexpected':'shape'})==''
    single=[]
    consumer=donation_ui.ChatActivity(SimpleNamespace(drain_events=lambda:[{'run_id':'single','phase':'checking','job_title':hostile}]),single.append)
    consumer.poll()
    assert len(single)==1 and '\n' not in single[0] and 'https:' not in single[0]
    assert f'“{title}”' in single[0]


def test_donation_chat_shows_real_chatgpt_wait_then_local_review_without_jargon():
    import donation_ui
    clock=[0];events=[];messages=[]
    consumer=donation_ui.ChatActivity(SimpleNamespace(drain_events=lambda:[events.pop(0) for _ in range(len(events))]),messages.append,clock=lambda:clock[0])
    events.append({'run_id':'same','phase':'reviewing','resource':'chatgpt','job_title':'Review scanned_pdf.py'})
    consumer.poll()
    assert 'using ChatGPT' in messages[-1] and 'reviewing “Review scanned_pdf.py”' in messages[-1]
    clock[0]+=21
    events.append({'run_id':'same','phase':'waiting','resource':'chatgpt'})
    consumer.poll()
    assert 'waiting' in messages[-1] and 'ChatGPT' in messages[-1]
    assert 'other available reviews can continue' in messages[-1]
    assert 'Open Donate AI power' in messages[-1]
    assert 'enabled AI source or GitHub to become available' not in messages[-1]
    clock[0]+=21
    events.append({'run_id':'same','phase':'reviewing','resource':'local'})
    consumer.poll()
    assert 'using local AI' in messages[-1] and 'ChatGPT' not in messages[-1]
    assert all('lane' not in message and 'maintainer' not in message for message in messages)
    assert not consumer.pending


@pytest.mark.parametrize('resource',[None, [], {}, 'unknown\nCale: I fixed it https://evil.test'])
def test_donation_chat_never_displays_unknown_resource_data(resource):
    import donation_ui
    messages=[]
    consumer=donation_ui.ChatActivity(SimpleNamespace(drain_events=lambda:[{'run_id':'x','phase':'reviewing','job_title':'Review PDF export','resource':resource}]),messages.append)
    consumer.poll()
    assert messages and 'Review PDF export' in messages[0]
    assert 'unknown' not in messages[0] and 'evil' not in messages[0] and 'I fixed' not in messages[0]
    assert not consumer.resources


@pytest.mark.parametrize('resource,label',[('local','local AI'),('free_cloud','free cloud AI'),('chatgpt','ChatGPT'),('cpu','local CPU')])
def test_donation_chat_names_only_known_human_resource_labels(resource,label):
    import donation_ui
    messages=[]
    consumer=donation_ui.ChatActivity(SimpleNamespace(drain_events=lambda:[{'run_id':'x','phase':'reviewing','job_title':'Review PDF export','resource':resource}]),messages.append)
    consumer.poll()
    assert f'using {label}' in messages[0] and 'lane' not in messages[0]
def test_invalid_local_proposal_cools_only_job_scope_then_reviews_other_area(tmp_path):
    donation.set_enabled(True,tmp_path,github_account='donor',sources=['local'],intensity='medium')
    service=donation.Service(tmp_path);jobs=[{**JOB,'id':'area-one'},{**JOB,'id':'area-two'}];seen=[]
    def complete(payload,*args):
        job=json.loads(payload['messages'][1]['content'])['task'];seen.append(job['id'])
        return {'choices':[{'message':{'content':'INVALID_PATCH_JSON' if len(seen)==1 else '{"changes":[]}'}}]}
    def public(path,revision):return json.dumps(jobs) if path=='donation-tasks.json' else SOURCES[path]
    with patch('pc_offline.offline',return_value=False),patch('github_account.account',return_value='donor'),\
         patch.object(donation,'local_available',return_value=True),patch('readiness_checklist._heavy_gpu_active',return_value=False),\
         patch.object(donation,'continuous_jobs',side_effect=lambda jobs,revision,**kw:jobs),patch.object(donation,'public_file',side_effect=public),\
         patch.object(donation,'gh_api',side_effect=lambda endpoint:[] if is_open_pull_list(endpoint) else COMMIT),\
         patch.object(donation,'complete_donation',side_effect=complete),patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation,'open_proposal') as publish,\
         patch.object(donation.threading,'Thread',DonationInlineThread):
        service.tick()
        assert service.next_attempt==0 and service.failures==0
        service.tick()
        assert len(seen)==2 and seen[0]!=seen[1]
        service.tick()  # Failed area cools; no-change area retains normal reservation.
        assert len(seen)==2
    publish.assert_not_called()
    with closing(donation.connection(tmp_path)) as conn:
        assert [r[0] for r in conn.execute('SELECT status FROM runs ORDER BY started')]==['failed','no_change']
        keys=[r[0] for r in conn.execute("SELECT resource FROM cooldowns WHERE resource LIKE 'proposal:local:%'")]
    assert len(keys)==1 and donation.resource_cooldown(tmp_path,'local')==0
    assert donation.resource_cooldown(tmp_path,keys[0])>0

def test_proposal_cooldown_is_bound_to_resource_job_and_full_source_scope(tmp_path):
    first=donation.proposal_cooldown_key('local',JOB,'a'*40,'b'*64)
    assert first==donation.proposal_cooldown_key('local',JOB,'c'*40,'b'*64)
    assert first!=donation.proposal_cooldown_key('local',JOB,'a'*40,'d'*64)
    assert first!=donation.proposal_cooldown_key('chatgpt',JOB,'a'*40,'b'*64)
    assert first!=donation.proposal_cooldown_key('local',{**JOB,'id':'other'},'a'*40,'b'*64)
    donation.resource_cooldown(tmp_path,first,300)
    assert donation.resource_cooldown(tmp_path,donation.proposal_cooldown_key('local',JOB,'c'*40,'b'*64))>0
    assert donation.resource_cooldown(tmp_path,donation.proposal_cooldown_key('local',JOB,'a'*40,'d'*64))==0


def test_non_text_proposal_is_rejected_as_validation_without_an_attribute_error():
    with pytest.raises(ValueError,match='JSON text'):
        donation.validate_proposal({'changes':[]},SOURCES)


@pytest.mark.parametrize("endpoint,data,status,stage", [
    ("repos/private-owner/private-repo/forks", {}, "404", "fork-create"),
    ("repos/private-owner/private-repo/git/refs", {}, 403, "branch-create"),
    ("search/issues?q=PRIVATE", None, "429", "proposal-search"),
    ("repos/private-owner/private-repo/pulls?state=open", None, "422", "proposal-list"),
])
def test_github_cli_failures_have_safe_stage_and_http_status(endpoint, data, status, stage):
    result = SimpleNamespace(returncode=1,
        stdout=json.dumps({"status": status, "message": "PRIVATE_SERVER_MESSAGE", "key": "PRIVATE_KEY"}),
        stderr="PRIVATE_STDERR_ACCOUNT_TOKEN")
    with patch("github_account.executable", return_value="gh-fixture"), patch.object(
            donation.subprocess, "run", return_value=result):
        with pytest.raises(donation.GitHubRequestError) as raised:
            donation.gh_api(endpoint, data)
    error = raised.value
    assert error.stage == stage and error.status == int(status) and error.kind == "http"
    assert error.__context__ is None
    assert "PRIVATE" not in str(error) + repr(error.__dict__)
    assert "private-owner" not in str(error) + repr(error.__dict__)


@pytest.mark.parametrize("failure,kind", [
    (OSError("PRIVATE_OS_PATH"), "transport"),
    (donation.subprocess.TimeoutExpired(["PRIVATE_COMMAND"], 45, output="PRIVATE_OUTPUT"), "timeout"),
])
def test_github_transport_failures_are_typed_without_private_exception_chain(failure, kind):
    with patch("github_account.executable", return_value="gh-fixture"), patch.object(
            donation.subprocess, "run", side_effect=failure):
        with pytest.raises(donation.GitHubRequestError) as raised:
            donation.gh_api("repos/PRIVATE/PRIVATE")
    assert raised.value.kind == kind and raised.value.stage == "repository"
    assert raised.value.__context__ is None
    assert "PRIVATE" not in str(raised.value) + repr(raised.value.__dict__)


@pytest.mark.parametrize("stdout", ["PRIVATE_BAD_JSON", "["*2000, '{bad' + " "*20000])
def test_github_success_with_invalid_json_is_safe_typed_response_failure(stdout):
    with patch("github_account.executable", return_value="gh-fixture"), patch.object(
            donation.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout)):
        with pytest.raises(donation.GitHubRequestError) as raised:
            donation.gh_api("repos/PRIVATE/PRIVATE/contents/code.py")
    assert raised.value.kind == "response_invalid" and raised.value.stage == "source"
    assert "PRIVATE" not in str(raised.value) + repr(raised.value.__dict__)


@pytest.mark.parametrize("status", [True, 404.0, "404 PRIVATE", "999", "200", None, {}, []])
def test_github_http_status_never_accepts_untyped_or_embedded_details(status):
    assert donation._github_status(json.dumps({"status": status})) is None
    assert donation._github_status('{"status":"404"}' + " "*17000) is None


def test_typed_github_failure_is_shared_across_lanes_restart_and_expires(tmp_path):
    donation.set_enabled(True, tmp_path, chatgpt_account=FINGERPRINT,
        github_account="donor", sources=["chatgpt"], intensity="full_blast")
    service = donation.Service(tmp_path)
    failure = donation.GitHubRequestError("fork-create", 404, "http")
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"), \
         patch("chatgpt_auth.status", return_value={"sharing": True}), \
         patch("chatgpt_auth.account_fingerprint", return_value=FINGERPRINT), \
         patch.object(donation, "gh_api", side_effect=failure) as github, \
         patch.object(donation, "complete_donation") as inference, \
         patch.object(donation.threading, "Thread", DonationInlineThread):
        service.tick()
        assert github.call_count == 1
        assert "HTTP 404" in service.status_message() and "fork creation" in service.status_message()
        restarted = donation.Service(tmp_path)
        restarted.tick()
        assert github.call_count == 1
        assert "HTTP 404" in restarted.status_message()
        with patch.object(donation, "connection", side_effect=AssertionError("Tk must do no I/O")):
            assert "fork creation" in restarted.status_message()
        inference.assert_not_called()
        assert donation.resource_cooldown(tmp_path, "github") > 250
        assert donation.resource_cooldown(tmp_path, "chatgpt") == 0
        with closing(donation.connection(tmp_path)) as connection, connection:
            connection.execute("UPDATE cooldowns SET until=0 WHERE resource='github'")
            connection.execute("UPDATE github_diagnostic SET until=0")
            assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            row = connection.execute("SELECT stage,status,kind FROM github_diagnostic").fetchone()
            assert row == ("fork-create", 404, "http")
        github.side_effect = InterruptedError("fixture end after permitted retry")
        expired = donation.Service(tmp_path)
        expired.tick()
        assert github.call_count > 1
        assert "HTTP 404" not in expired.status_message()
        inference.assert_not_called()
    assert donation.total_tokens(tmp_path) == 0
    assert donation.settings(tmp_path)["sources"] == ["chatgpt"]


@pytest.mark.parametrize("ledger_fails", [False, True])
def test_github_submission_failure_keeps_real_usage_and_never_claims_pr_success(tmp_path, ledger_fails):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="medium")
    service = donation.Service(tmp_path)
    original_finish = donation.finish
    def finish(root, run_id, status, *args):
        if ledger_fails and status == "failed":
            raise sqlite3.OperationalError("PRIVATE_FAILED_RECEIPT")
        return original_finish(root, run_id, status, *args)
    def public(path, revision):
        return json.dumps([JOB]) if path == "donation-tasks.json" else SOURCES[path]
    def complete(payload, resource, intensity, cancel, receipt):
        receipt(RESULT, "ollama", "main", False)
        return RESULT
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"), \
         patch.object(donation, "local_available", return_value=True), \
         patch("readiness_checklist._heavy_gpu_active", return_value=False), \
         patch.object(donation, "continuous_jobs", side_effect=lambda jobs, revision, **kw: jobs), \
         patch.object(donation, "public_file", side_effect=public), \
         patch.object(donation, "gh_api", side_effect=lambda endpoint: [] if is_open_pull_list(endpoint) else COMMIT), \
         patch.object(donation, "complete_donation", side_effect=complete), \
         patch("usage_history.record_completion"), patch.object(donation, "finish", side_effect=finish), \
         patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation, "open_proposal", side_effect=donation.GitHubRequestError("fork-create", 403, "http")):
        service._run(threading.Event())
    assert donation.total_tokens(tmp_path) == 50
    assert donation.resource_cooldown(tmp_path, "local") == 0
    assert "HTTP 403" in service.status_message()
    assert ("database unavailable" in service.status_message()) is ledger_fails
    with closing(donation.connection(tmp_path)) as connection:
        assert connection.execute("SELECT status,tokens,url FROM runs").fetchone() == ("received" if ledger_fails else "failed", 50, "")
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert not any(event["phase"] == "submitted" for event in service.drain_events())


def test_github_wait_created_during_source_collection_prevents_new_inference(tmp_path):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="medium")
    service = donation.Service(tmp_path)
    def public(path, revision):
        if path == "donation-tasks.json":
            return json.dumps([JOB])
        donation.github_diagnostic(tmp_path, donation.GitHubRequestError("repository", 404, "http"))
        return SOURCES[path]
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"), \
         patch.object(donation, "local_available", return_value=True), \
         patch("readiness_checklist._heavy_gpu_active", return_value=False), \
         patch.object(donation, "continuous_jobs", side_effect=lambda jobs, revision, **kw: jobs), \
         patch.object(donation, "public_file", side_effect=public), \
         patch.object(donation, "gh_api", side_effect=lambda endpoint: [] if is_open_pull_list(endpoint) else COMMIT), \
         patch.object(donation, "complete_donation") as inference:
        service._run(threading.Event())
    inference.assert_not_called()
    assert "HTTP 404" in service.status_message()
    assert donation.total_tokens(tmp_path) == 0


def test_github_fork_readiness_preserves_typed_failure_instead_of_generic_fallback():
    calls = []
    def api(endpoint, data=None):
        calls.append(endpoint)
        if endpoint.endswith("/forks"):
            return {"full_name": "donor/cale-ai-workbench", "fork": True}
        raise donation.GitHubRequestError("repository", 404, "http")
    with patch.object(donation, "gh_api", side_effect=api), patch.object(donation.time, "sleep"):
        with pytest.raises(donation.GitHubRequestError) as raised:
            donation.open_proposal("donor", "a"*32, JOB, COMMIT, PROPOSAL, {}, lambda: None)
    assert raised.value.stage == "repository" and raised.value.status == 404
    assert len(calls) == 17  # Initial existing-fork lookup, creation, then15polls.


def test_advisory_old_review_and_scope_404_do_not_pause_current_donation(tmp_path):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="medium")
    pending = donation.claim_run(tmp_path, "old-other-job", "local", "medium", "c"*40)
    donation.finish(tmp_path, pending, "submitted", "https://github.com/CaleMediaDev/cale-ai-workbench/pull/1")
    historical = donation.claim_run(tmp_path, JOB["id"], "local", "medium", "d"*40)
    donation.finish(tmp_path, historical, "reviewed")
    with closing(donation.connection(tmp_path)) as connection, connection:
        connection.execute("UPDATE runs SET review_state='closed_unmerged' WHERE id=?", (historical,))
    advisory = []
    def public(path, revision):
        if revision == "d"*40:
            advisory.append("old-scope")
            raise donation.GitHubRequestError("source", 404, "http")
        return json.dumps([JOB]) if path == "donation-tasks.json" else SOURCES[path]
    def api(endpoint):
        if "/pulls/1" in endpoint:
            advisory.append("old-review")
            raise donation.GitHubRequestError("proposal-review", 404, "http")
        return [] if is_open_pull_list(endpoint) else COMMIT
    def complete(payload, resource, intensity, cancel, receipt):
        receipt(RESULT, "ollama", "main", False)
        return RESULT
    service = donation.Service(tmp_path)
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"), \
         patch.object(donation, "local_available", return_value=True), \
         patch("readiness_checklist._heavy_gpu_active", return_value=False), \
         patch.object(donation, "continuous_jobs", side_effect=lambda jobs, revision, **kw: jobs), \
         patch.object(donation, "public_file", side_effect=public), patch.object(donation, "gh_api", side_effect=api), \
         patch.object(donation, "complete_donation", side_effect=complete) as inference, \
         patch("usage_history.record_completion"), \
         patch.object(donation.DurablePublication, "confirm_public_delivery", return_value=None), patch.object(donation, "open_proposal", return_value="https://github.com/CaleMediaDev/cale-ai-workbench/pull/2") as submit:
        service._run(threading.Event())
    assert set(advisory) == {"old-review", "old-scope"}
    inference.assert_called_once()
    submit.assert_called_once()
    assert donation.total_tokens(tmp_path) == 50
    assert donation.resource_cooldown(tmp_path, "github") == 0
    assert donation.github_diagnostic(tmp_path) == {}


@pytest.mark.parametrize("failure", [InterruptedError("fixture cancelled"), sqlite3.OperationalError("fixture ledger")])
def test_github_boundary_preserves_cancellation_and_non_github_ledger_errors(failure):
    with patch("github_account.executable", return_value="gh-fixture"), patch.object(
            donation.subprocess, "run", side_effect=failure):
        with pytest.raises(type(failure)) as raised:
            donation.gh_api("repos/PRIVATE/PRIVATE")
    assert raised.value is failure
    assert not isinstance(raised.value, donation.GitHubRequestError)


@pytest.mark.parametrize("error,category", [
    (sqlite3.OperationalError("PRIVATE_SQL_AND_PATH"), "database unavailable"),
    (RuntimeError("PRIVATE_ACCOUNT_OR_TOKEN"), "worker temporarily unavailable"),
    (OSError("PRIVATE_HELPER_PATH"), "local file or helper unavailable"),
    (donation.subprocess.TimeoutExpired(["PRIVATE_COMMAND"], 1), "local file or helper unavailable"),
    (ValueError("PRIVATE_DATA"), "data could not be validated"),
    (KeyError("PRIVATE_KEY"), "data could not be validated"),
])
def test_non_github_service_errors_have_safe_static_category_without_account_cooldown(tmp_path, error, category):
    donation.set_enabled(True, tmp_path, chatgpt_account=FINGERPRINT,
        github_account="donor", sources=["chatgpt"], intensity="medium")
    service = donation.Service(tmp_path)
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"), \
         patch("chatgpt_auth.status", return_value={"sharing": True}), \
         patch("chatgpt_auth.account_fingerprint", return_value=FINGERPRINT), \
         patch.object(donation, "gh_api", side_effect=error), \
         patch.object(donation, "complete_donation") as inference:
        service._run(threading.Event())
    inference.assert_not_called()
    assert category in service.status_message()
    assert "PRIVATE" not in service.status_message()
    assert donation.resource_cooldown(tmp_path, "github") == 0
    assert donation.resource_cooldown(tmp_path, "chatgpt") == 0
    assert donation.github_diagnostic(tmp_path) == {}
    assert service.next_attempt > time.monotonic()


def test_delayed_empty_github_hydration_cannot_erase_nested_lane_fresh_diagnosis(tmp_path):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="medium")
    parent = donation.Service(tmp_path)
    child = donation.Service(tmp_path, _lane=True, _resource="local", _events=parent._events)
    parent.children = [child]
    original = donation.github_diagnostic
    interleaved = []
    def snapshot(root, error=None):
        stale = original(root, error)
        if error is None and not interleaved:
            interleaved.append(True)
            child._run(threading.Event())
        return stale
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"), \
         patch.object(donation, "local_available", return_value=True), \
         patch("readiness_checklist._heavy_gpu_active", return_value=False), \
         patch.object(donation, "gh_api", side_effect=donation.GitHubRequestError("repository", 404, "http")) as github, \
         patch.object(donation, "github_diagnostic", side_effect=snapshot):
        parent._run(threading.Event())
    assert interleaved and github.call_count == 1
    assert "HTTP 404" in parent.status_message() and "repository access" in parent.status_message()
    assert parent._events["github_diagnostic"]["status"] == 404
    assert original(tmp_path)["status"] == 404


def test_github_cache_publication_retains_fresh_write_on_equal_expiry_and_clears_expired(tmp_path):
    service = donation.Service(tmp_path)
    first = {"stage": "source", "status": 403, "kind": "http", "until": time.time()+100}
    latest = {**first, "stage": "fork-create", "status": 404}
    service._publish_github_diagnostic(first)
    service._publish_github_diagnostic(latest, fresh=True)
    assert service._publish_github_diagnostic(first)["status"] == 404
    assert service._publish_github_diagnostic({})["status"] == 404
    assert service._publish_github_diagnostic({**first, "until": first["until"]-1})["status"] == 404
    observed = service._publish_github_diagnostic(
        {**first, "until": first["until"]-1, "database_unavailable": True}, fresh=True)
    assert observed["status"] == 404 and observed["database_unavailable"] is True
    with patch.object(donation.time, "time", return_value=first["until"]+1):
        assert service._publish_github_diagnostic({}) == {}


def test_typed_github_error_with_failed_diagnostic_sql_retains_safe_cause_and_memory_backoff(tmp_path):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="medium")
    service = donation.Service(tmp_path)
    child = donation.Service(tmp_path, _lane=True, _resource="local", _events=service._events)
    original = donation.github_diagnostic
    def failed_write(root, error=None):
        if error is not None:
            raise sqlite3.OperationalError("PRIVATE_DIAGNOSTIC_SQL_PATH")
        return original(root)
    with patch("pc_offline.offline", return_value=False), patch("github_account.account", return_value="donor"), \
         patch.object(donation, "local_available", return_value=True), \
         patch("readiness_checklist._heavy_gpu_active", return_value=False), \
         patch.object(donation, "gh_api", side_effect=donation.GitHubRequestError("repository", 404, "http")) as github, \
         patch.object(donation, "github_diagnostic", side_effect=failed_write), \
         patch.object(donation, "complete_donation") as inference:
        service._run(threading.Event())
        child._run(threading.Event())
    assert github.call_count == 1
    inference.assert_not_called()
    assert "database unavailable" in service.status_message() and "HTTP 404" in service.status_message()
    assert "PRIVATE" not in service.status_message()
    assert service.next_attempt > time.monotonic()
    assert donation.resource_cooldown(tmp_path, "github") == 0  # No durable cooldown claimed.
    assert original(tmp_path) == {}
    assert donation.total_tokens(tmp_path) == 0


# UI counters must not acquire the worker/migration write lock on Tk.
def counter_control(root):
    import donation_ui
    import queue
    ui = object.__new__(donation_ui.Controls)
    ui.service = SimpleNamespace(root=root)
    ui.popup = None
    ui.last_counter = 0
    ui._counter_results = queue.Queue(maxsize=1)
    ui._counter_worker = None
    ui._counter_snapshot = None
    owner = threading.get_ident()
    class Variable:
        def __init__(self, value): self.value = value
        def set(self, value):
            assert threading.get_ident() == owner, "worker touched Tk variable"
            self.value = value
    ui.counter = Variable("Total tokens donated: —")
    ui.breakdown = Variable("Local: — · Free cloud: — · ChatGPT: —")
    return ui


def counter_ledger(root):
    for source,tokens in (("local",50),("free_cloud",25),("chatgpt",75)):
        run = donation.claim_run(root,"counter-"+source,source,"medium")
        reply = {"model":"reported-fixture","usage":{"total_tokens":tokens}}
        donation.record_result(root,run,reply,"main",source)
        donation.record_result(root,run,reply,"main",source)
    return {"total":150,"sources":{"local":50,"free_cloud":25,"chatgpt":75}}


def test_token_snapshot_reads_committed_grouped_totals_under_wal_writer(tmp_path):
    expected = counter_ledger(tmp_path)
    with closing(sqlite3.connect(tmp_path/"LongTasks/donations.db")) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE runs SET tokens=tokens+1000")
        start = time.monotonic()
        assert donation.token_snapshot(tmp_path) == expected
        assert time.monotonic()-start < .25
    assert donation.total_tokens(tmp_path) == expected["total"]
    assert donation.source_totals(tmp_path) == expected["sources"]


def test_token_snapshot_legacy_attribution_without_migration_and_missing_unavailable(tmp_path):
    legacy = tmp_path/"legacy"
    database = legacy/"LongTasks/donations.db"
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection,connection:
        connection.execute("CREATE TABLE runs(id TEXT PRIMARY KEY,started REAL,status TEXT,tokens INTEGER,model TEXT,url TEXT,job TEXT)")
        connection.execute("INSERT INTO runs VALUES('old',1,'submitted',75,'model','','job')")
    before = database.read_bytes()
    assert donation.token_snapshot(legacy) == {"total":75,"sources":{"chatgpt":75}}
    assert database.read_bytes() == before
    assert donation.total_tokens(legacy) == 75  # Existing writer migration remains legitimate.
    assert donation.source_totals(legacy) == {"chatgpt":75}
    missing = tmp_path/"missing"
    assert donation.token_snapshot(missing) is None
    assert not missing.exists()
    malformed = tmp_path/"broken"/"LongTasks/donations.db"
    malformed.parent.mkdir(parents=True);malformed.write_bytes(b"not SQLite")
    original = malformed.read_bytes()
    assert donation.token_snapshot(malformed.parent.parent) is None
    assert malformed.read_bytes() == original


def test_actual_refresh_returns_without_waiting_for_real_wal_writer(tmp_path):
    counter_ledger(tmp_path)
    ui = counter_control(tmp_path)
    held = threading.Event()
    errors = []
    def write():
        try:
            with closing(sqlite3.connect(tmp_path/"LongTasks/donations.db")) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE runs SET tokens=tokens+1000")
                held.set();time.sleep(.5);connection.rollback()
        except Exception as error: errors.append(type(error).__name__);held.set()
    worker = threading.Thread(target=write)
    worker.start();assert held.wait(2)
    try:
        start = time.monotonic();ui.refresh();elapsed=time.monotonic()-start
        assert elapsed < .2
        assert ui._counter_worker is not None
        ui._counter_worker.join(2)
        ui.refresh()
        assert ui.counter.value == "Total tokens donated: 150"
        assert ui._counter_snapshot["sources"] == {"local":50,"free_cloud":25,"chatgpt":75}
    finally: worker.join(2)
    assert not errors


def test_counter_probe_is_single_bounded_background_task_and_never_touches_tk(tmp_path):
    import donation_ui
    ui = counter_control(tmp_path)
    entered = threading.Event();release = threading.Event();calls=[]
    def slow(root):
        calls.append(threading.get_ident());entered.set();release.wait(2)
        return {"total":150,"sources":{"local":50,"free_cloud":25,"chatgpt":75}}
    with patch.object(donation,"token_snapshot",side_effect=slow,create=True):
        try:
            start=time.monotonic();ui.refresh()
            assert time.monotonic()-start < .2 and entered.wait(1)
            first=ui._counter_worker
            for _ in range(12):
                ui.last_counter=0;ui.refresh()
            assert calls == [first.ident] and first.ident != threading.get_ident()
            assert ui.counter.value == "Total tokens donated: —"
            assert ui._counter_results.maxsize == 1
        finally: release.set()
        first.join(2);ui.refresh()
    assert ui.counter.value == "Total tokens donated: 150"


def test_counter_unavailable_or_exception_keeps_last_good_without_fabricated_zero(tmp_path):
    ui=counter_control(tmp_path)
    good={"total":150,"sources":{"local":50,"free_cloud":25,"chatgpt":75}}
    with patch.object(donation,"token_snapshot",return_value=good,create=True):
        ui.refresh();ui._counter_worker.join(2);ui.refresh()
    visible=(ui.counter.value,ui.breakdown.value)
    for result in (None,sqlite3.OperationalError("private lock")):
        with patch.object(donation,"token_snapshot",side_effect=result if isinstance(result,Exception) else None,return_value=result,create=True):
            ui.last_counter=0;ui.refresh();ui._counter_worker.join(2);ui.refresh()
        assert (ui.counter.value,ui.breakdown.value)==visible
        assert ui._counter_snapshot==good



def test_token_snapshot_keeps_one_schema_and_totals_snapshot_across_writer_commit(tmp_path):
    expected = counter_ledger(tmp_path)
    actual_connect = sqlite3.connect
    committed = []
    class SchemaCursor:
        def __init__(self, cursor): self.cursor = cursor
        def __iter__(self):
            rows = self.cursor.fetchall()
            if not committed:
                with closing(actual_connect(tmp_path/"LongTasks/donations.db")) as writer,writer:
                    writer.execute("UPDATE runs SET tokens=tokens+1000")
                committed.append(True)
            return iter(rows)
    class Reader:
        def __init__(self, connection): self.connection = connection
        def execute(self, sql, *args):
            cursor = self.connection.execute(sql,*args)
            return SchemaCursor(cursor) if sql.startswith("PRAGMA table_info") else cursor
        def set_progress_handler(self, *args): self.connection.set_progress_handler(*args)
        def close(self): self.connection.close()
    with patch.object(donation.sqlite3,"connect",side_effect=lambda *a,**k: Reader(actual_connect(*a,**k))):
        assert donation.token_snapshot(tmp_path) == expected
    assert committed
    updated = donation.token_snapshot(tmp_path)
    assert updated["total"] == 3150
    assert updated["sources"] == {"local":1050,"free_cloud":1025,"chatgpt":1075}


def test_token_snapshot_existing_empty_ledger_is_real_zero(tmp_path):
    with closing(donation.connection(tmp_path)): pass
    assert donation.token_snapshot(tmp_path) == {"total":0,"sources":{}}


class PublicationRemote:
    """Owned in-memory GitHub fixture, including applied-but-lost POST responses."""
    def __init__(self):
        self.calls = []
        self.refs, self.commits = {}, {}
        self.prs = []
        self.fork_failures = 0
        self.fork_failure_status = 404
        self.fork_created = False
        self.public_visible = True
        self.pr_failure = ""
        self.ref_lost = False
        self.revision = COMMIT["sha"]
        self.source_changed = False
        self.job_changed = False

    def public(self, path, revision):
        if path == "donation-tasks.json":
            return json.dumps([{**JOB, "instructions": "changed"}] if self.job_changed else [JOB])
        if self.source_changed and revision == self.revision:
            return SOURCES[path] + "# external public change\n"
        return SOURCES[path]

    def api(self, endpoint, data=None):
        from urllib.parse import parse_qs
        self.calls.append((endpoint, data))
        if is_open_pull_list(endpoint):
            return [pr for pr in self.prs if pr["state"] == "open"]
        if endpoint.endswith("/commits/main"):
            return {"sha": self.revision, "commit": {"tree": {"sha": "b"*40}}}
        if "/git/trees/" in endpoint:
            return {"tree": [{"path": path, "type": "blob", "sha": donation._public_blob(body)} for path, body in SOURCES.items()]}
        if endpoint.endswith("/forks"):
            if self.fork_failures:
                self.fork_failures -= 1
                raise donation.GitHubRequestError("fork-create", self.fork_failure_status, "http")
            self.fork_created = True
            return {"full_name": "donor/cale-ai-workbench", "fork": True}
        if endpoint == "repos/donor/cale-ai-workbench":
            if not self.fork_created:
                raise donation.GitHubRequestError("repository", 404, "http")
            return {"full_name": "donor/cale-ai-workbench", "fork": True, "parent": {"full_name": donation.UPSTREAM}}
        if endpoint.endswith("/git/trees"):
            return {"sha": "c"*40}
        if endpoint.endswith("/git/commits"):
            sha = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
            self.commits[sha] = {"sha": sha, "message": data["message"], "tree": {"sha": data["tree"]},
                "parents": [{"sha": parent, "url": "fixture-parent-url"} for parent in data["parents"]]}
            return {"sha": sha}
        if "/git/commits/" in endpoint:
            return self.commits[endpoint.rsplit("/", 1)[1]]
        if "/git/ref/heads/" in endpoint:
            branch = endpoint.split("/git/ref/heads/")[1]
            if branch not in self.refs:
                raise donation.GitHubRequestError("repository", 404, "http")
            return {"object": {"sha": self.refs[branch]}}
        if endpoint.endswith("/git/refs"):
            branch = data["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                raise donation.GitHubRequestError("branch-create", 422, "http")
            self.refs[branch] = data["sha"]
            if self.ref_lost:
                self.ref_lost = False
                raise donation.GitHubRequestError("branch-create", kind="timeout")
            return {"object": {"sha": data["sha"]}}
        if "/pulls?" in endpoint:
            head = parse_qs(endpoint.split("?", 1)[1])["head"][0]
            return [pr for pr in self.prs if head == "donor:" + pr["head"]["ref"]]
        if endpoint.endswith("/pulls"):
            failure, self.pr_failure = self.pr_failure, ""
            if failure == "rejected":
                raise donation.GitHubRequestError("proposal-create", 403, "http")
            if failure != "unknown-absent":
                branch = data["head"].split(":", 1)[1]
                pr = {"number": len(self.prs)+1, "html_url": f"https://github.com/{donation.UPSTREAM}/pull/{len(self.prs)+1}",
                    "body": data["body"], "title": data["title"], "state": "open", "draft": True, "user": {"login": "donor"},
                    "base": {"ref": "main", "repo": {"full_name": donation.UPSTREAM}},
                    "head": {"ref": branch, "sha": self.refs[branch], "repo": {"full_name": "donor/cale-ai-workbench"}}}
                self.prs.append(pr)
            if failure in {"applied-lost", "unknown-absent"}:
                raise donation.GitHubRequestError("proposal-create", kind="timeout")
            return pr
        raise AssertionError("Unexpected fixture endpoint: " + endpoint)


@pytest.fixture
def outbox_host(tmp_path, monkeypatch):
    donation.set_enabled(True, tmp_path, github_account="donor", sources=["local"], intensity="medium")
    remote = PublicationRemote()
    calls = []
    monkeypatch.setattr("pc_offline.offline", lambda: False)
    monkeypatch.setattr("github_account.account", lambda: "donor")
    monkeypatch.setattr(donation, "local_available", lambda: True)
    monkeypatch.setattr("readiness_checklist._heavy_gpu_active", lambda root: False)
    monkeypatch.setattr(donation, "continuous_jobs", lambda jobs, revision, **kw: jobs)
    monkeypatch.setattr(donation, "public_file", remote.public)
    monkeypatch.setattr(donation, "gh_api", remote.api)
    def public_pull(number):
        if not remote.public_visible:
            raise donation.PublicVisibilityPending(404, 'http')
        found = next((pr for pr in remote.prs if pr['number'] == number), None)
        if found is None:
            raise donation.PublicVisibilityPending(404, 'http')
        return json.loads(json.dumps(found))
    monkeypatch.setattr(donation, 'public_pull_request', public_pull)
    monkeypatch.setattr("usage_history.record_completion", lambda *args: None)
    def complete(payload, resource, intensity, cancel, receipt):
        calls.append(payload)
        receipt(RESULT, "ollama", "main", False)
        return RESULT
    monkeypatch.setattr(donation, "complete_donation", complete)
    def tick():
        service = donation.Service(tmp_path)
        with patch.object(donation.threading, "Thread", DonationInlineThread):
            service.tick()
        return service
    return SimpleNamespace(root=tmp_path, remote=remote, calls=calls, tick=tick)


def outbox_expire(root):
    # Test clock advancement, never production-counter/lease reset.
    with closing(donation.connection(root)) as connection:
        rows = connection.execute("SELECT until FROM cooldowns UNION ALL SELECT checked+61 FROM publication_outbox").fetchall()
    return max([time.time(), *(row[0] for row in rows)]) + 1


def test_valid_publication_404_restart_resumes_original_run_without_model_regeneration(outbox_host):
    host = outbox_host
    host.remote.fork_failures = 1
    first = host.tick()
    with closing(donation.connection(host.root)) as connection:
        before = connection.execute("SELECT id,status,tokens FROM runs").fetchall()
        assert connection.execute("SELECT phase,length(payload) FROM publication_outbox").fetchone()[1] > 0
    assert len(host.calls) == 1 and before[0][2] == 50 and "HTTP 404" in first.status_message()
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        second = host.tick()
    with closing(donation.connection(host.root)) as connection:
        after = connection.execute("SELECT id,status,tokens FROM runs").fetchall()
        assert connection.execute("SELECT phase,length(payload),owner FROM publication_outbox").fetchone() == ("submitted", 0, "")
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
    assert after == [(before[0][0], "submitted", 50)]
    assert len(host.calls) == 1 and len(host.remote.prs) == 1
    assert donation.total_tokens(host.root) == 50
    assert "confirmed" in second.status_message()


@pytest.mark.parametrize("failure", ["applied-lost", "rejected"])
def test_publication_pr_restart_reconciles_lost_or_rejected_post_without_reinference(outbox_host, failure):
    host = outbox_host
    host.remote.pr_failure = failure
    host.tick()
    assert len(host.calls) == 1
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        host.tick()
    assert len(host.calls) == 1 and donation.total_tokens(host.root) == 50
    posts = [body for endpoint, body in host.remote.calls if endpoint.endswith("/pulls") and body is not None]
    assert len(posts) == (1 if failure == "applied-lost" else 2)
    assert len(host.remote.prs) == 1
    assert any("/pulls?" in endpoint and "head=" in endpoint and "state=all" in endpoint for endpoint, _ in host.remote.calls)
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone()[0] == "submitted"


def test_unknown_pr_post_absence_stays_reserved_not_blindly_reposted(outbox_host):
    host = outbox_host
    host.remote.pr_failure = "unknown-absent"
    host.tick()
    for _ in range(2):
        with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
            waiting = host.tick()
        assert "confirmation" in waiting.status_message()
    assert len(host.calls) == 1 and not host.remote.prs
    assert sum(endpoint.endswith("/pulls") and data is not None for endpoint, data in host.remote.calls) == 1
    assert donation.claim_run(host.root, JOB["id"], "local", "medium", "f"*40) == ""


def test_applied_ref_lost_response_reuses_exact_branch_without_force_push(outbox_host):
    host = outbox_host
    host.remote.ref_lost = True
    host.tick()
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        host.tick()
    assert len(host.calls) == 1 and len(host.remote.refs) == 1 and len(host.remote.prs) == 1
    assert sum(endpoint.endswith("/git/refs") for endpoint, _ in host.remote.calls) == 1


def test_unrelated_main_merge_keeps_pinned_original_base_and_cached_work(outbox_host):
    host = outbox_host
    host.remote.fork_failures = 1
    host.tick()
    host.remote.revision = "e"*40
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        host.tick()
    assert len(host.calls) == 1 and donation.total_tokens(host.root) == 50
    commits = [body for endpoint, body in host.remote.calls if endpoint.endswith("/git/commits") and body]
    assert commits[0]["parents"] == [COMMIT["sha"]]


@pytest.mark.parametrize("changed", ["source", "job", "policy"])
def test_changed_public_scope_or_validator_rejects_cached_upload_without_new_model(outbox_host, changed):
    host = outbox_host
    host.remote.fork_failures = 1
    host.tick()
    mutations = sum(body is not None for _, body in host.remote.calls)
    if changed == "source": host.remote.source_changed = True
    if changed == "job": host.remote.job_changed = True
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        if changed == "policy":
            with patch.object(donation, "_publication_policy", return_value="f"*64):
                host.tick()
        else:
            host.tick()
    assert len(host.calls) == 1 and donation.total_tokens(host.root) == 50
    assert sum(body is not None for _, body in host.remote.calls) == mutations
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT phase,length(payload) FROM publication_outbox").fetchone() == ("stale", 0)


@pytest.mark.parametrize("control", ["disabled", "account", "offline"])
def test_saved_publication_respects_revoked_consent_account_and_offline_before_rpc(outbox_host, control):
    host = outbox_host
    host.remote.fork_failures = 1
    host.tick()
    count = len(host.remote.calls)
    if control == "disabled": donation.set_enabled(False, host.root)
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        if control == "account":
            with patch("github_account.account", return_value="another-fixture-account"):
                host.tick()
        elif control == "offline":
            with patch("pc_offline.offline", return_value=True): host.tick()
        else: host.tick()
    assert len(host.calls) == 1 and len(host.remote.calls) == count
    assert not host.remote.prs
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT length(payload) FROM publication_outbox").fetchone()[0] > 0


def test_cancel_after_durable_save_retries_same_cached_work_when_reenabled(outbox_host):
    host = outbox_host
    original = host.remote.api
    cancelled = []
    def api(endpoint, data=None):
        result = original(endpoint, data)
        if data is not None and not cancelled:
            cancelled.append(True)
            donation.set_enabled(False, host.root)
        return result
    with patch.object(donation, "gh_api", side_effect=api):
        host.tick()
    assert len(host.calls) == 1 and not host.remote.prs
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT phase,length(payload),owner FROM publication_outbox").fetchone()[1] > 0
    donation.set_enabled(True, host.root, github_account="donor", sources=["local"], intensity="medium")
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        host.tick()
    assert len(host.calls) == 1 and len(host.remote.prs) == 1 and donation.total_tokens(host.root) == 50


def test_every_remote_mutation_follows_validated_payload_and_durable_intent(outbox_host):
    host = outbox_host
    original = host.remote.api
    phases = []
    def api(endpoint, data=None):
        if data is not None:
            with closing(donation.connection(host.root)) as connection:
                phase, size, owner = connection.execute("SELECT phase,length(payload),owner FROM publication_outbox").fetchone()
            assert size > 0 and owner and phase.endswith("_intent")
            phases.append(phase)
        return original(endpoint, data)
    with patch.object(donation, "gh_api", side_effect=api): host.tick()
    assert set(phases) == {"fork_intent", "tree_intent", "commit_intent", "ref_intent", "pr_intent"}
    assert len(host.remote.prs) == 1


def test_wrong_existing_branch_is_not_overwritten_or_submitted(outbox_host):
    host = outbox_host
    original = host.remote.api
    def api(endpoint, data=None):
        if endpoint.endswith("/forks"):
            with closing(donation.connection(host.root)) as connection:
                run = connection.execute("SELECT run FROM publication_outbox").fetchone()[0]
            host.remote.refs["cale-donation-"+run[:16]] = "f"*40
        return original(endpoint, data)
    with patch.object(donation, "gh_api", side_effect=api):
        waiting = host.tick()
    assert "not overwritten" in waiting.status_message()
    assert not host.remote.prs
    assert not any(endpoint.endswith("/git/refs") for endpoint, _ in host.remote.calls)
    assert donation.total_tokens(host.root) == 50


def test_unknown_post_confirmation_survives_later_source_change_without_duplicate(outbox_host):
    host = outbox_host
    host.remote.pr_failure = "applied-lost"
    host.tick()
    host.remote.source_changed = True
    host.remote.revision = "e"*40
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        host.tick()
    assert len(host.calls) == 1 and len(host.remote.prs) == 1
    assert sum(endpoint.endswith("/pulls") and body is not None for endpoint, body in host.remote.calls) == 1
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT status FROM runs").fetchone()[0] == "submitted"


def test_outbox_claim_is_exclusive_across_real_threads_and_release_allows_resume(outbox_host):
    host = outbox_host
    host.remote.fork_failures = 1
    host.tick()
    held = threading.Event()
    release = threading.Event()
    acquired = []
    def claim():
        acquired.append(donation.claim_publication(host.root, donation.settings(host.root), "donor"))
        held.set()
        release.wait(3)
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        worker = threading.Thread(target=claim)
        worker.start()
        assert held.wait(3)
        try:
            assert acquired[0] is not None
            assert donation.claim_publication(host.root, donation.settings(host.root), "donor") is None
        finally:
            release.set()
            worker.join(3)
        # A dead owner thread can be reconciled, never a merely expired live one.
        with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
            reclaimed = donation.claim_publication(host.root, donation.settings(host.root), "donor")
            assert reclaimed is not None
            reclaimed.close()


def test_outbox_payload_size_is_checked_before_decode_and_corrupt_work_cannot_upload(outbox_host):
    host = outbox_host
    host.remote.fork_failures = 1
    host.tick()
    before = len(host.remote.calls)
    with closing(donation.connection(host.root)) as connection, connection:
        raw = b'{"PRIVATE_OVERSIZED":' + b' '*1000
        connection.execute("UPDATE publication_outbox SET payload=?,payload_sha=?", (raw, hashlib.sha256(raw).hexdigest()))
    actual_loads = donation.json.loads
    def decode(raw, *args, **kwargs):
        assert not isinstance(raw, bytes) or b"PRIVATE_OVERSIZED" not in raw
        return actual_loads(raw, *args, **kwargs)
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)), \
         patch.object(donation, "_OUTBOX_BYTES", 128), patch.object(donation.json, "loads", side_effect=decode):
        host.tick()
    assert len(host.remote.calls) == before and len(host.calls) == 1
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT phase,length(payload) FROM publication_outbox").fetchone() == ("corrupt", 0)


def test_outbox_reservation_capacity_is_atomic_and_never_evicts_pending_work(tmp_path):
    consent = {"intensity": "medium"}
    publications = []
    try:
        for index in range(donation._OUTBOX_LIMIT):
            run = donation.claim_run(tmp_path, "bounded-"+str(index), "local", "medium")
            publications.append(donation.reserve_publication(tmp_path, run, "local", consent, "donor"))
        extra = donation.claim_run(tmp_path, "bounded-extra", "local", "medium")
        with pytest.raises(donation.PublicationPending):
            donation.reserve_publication(tmp_path, extra, "local", consent, "donor")
        with closing(donation.connection(tmp_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM publication_outbox").fetchone()[0] == donation._OUTBOX_LIMIT
    finally:
        for publication in publications: publication.close()


@pytest.mark.parametrize("phase", ["validated", "pr_intent"])
def test_actual_child_crash_retains_valid_proposal_or_ambiguous_intent_without_new_model(outbox_host, phase):
    import os, sys, subprocess
    from pathlib import Path
    host = outbox_host
    fixture = host.root / "public-crash-fixture.json"
    fixture.write_text(json.dumps({"job": JOB, "commit": COMMIT, "proposal": PROPOSAL,
        "sources": SOURCES, "result": RESULT, "phase": phase}), encoding="utf-8")
    script = r"""
import sys,json,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import donation as d
root=Path(sys.argv[2]);value=json.loads(Path(sys.argv[3]).read_text())
consent=d.settings(root);run=d.claim_run(root,value['job']['id'],'local','medium',value['commit']['sha'])
d.record_result(root,run,value['result'],'main','ollama')
publication=d.reserve_publication(root,run,'local',consent,'donor')
proposal,edited=d.validate_proposal(json.dumps(value['proposal']),value['sources'])
publication.prepare(value['job'],value['commit'],proposal,edited,value['sources'],consent,'donor')
if value['phase']=='pr_intent':
 publication.state={'fork_ready':True,'tree':'c'*40,'commit':'d'*40,'pr_attempted':True,'intent':'pr'}
 publication._save('pr_intent')
os._exit(23)
"""
    child = subprocess.run([sys.executable, "-I", "-B", "-c", script,
        str(Path(donation.__file__).resolve().parent), str(host.root), str(fixture)],
        cwd=host.root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, timeout=15)
    assert child.returncode == 23, child.stderr.decode(errors="replace")
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        service = host.tick()
    assert len(host.calls) == 0 and donation.total_tokens(host.root) == 50
    if phase == "validated":
        assert len(host.remote.prs) == 1
        with closing(donation.connection(host.root)) as connection:
            assert connection.execute("SELECT status FROM runs").fetchone()[0] == "submitted"
    else:
        assert "confirmation" in service.status_message()
        assert not host.remote.prs
        assert not any(body is not None for _, body in host.remote.calls)


def test_legacy_token_ledger_migrates_without_fabricating_cached_proposals(outbox_host):
    host = outbox_host
    database = host.root / "LongTasks/donations.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE runs(id TEXT PRIMARY KEY,started REAL,status TEXT,tokens INTEGER,model TEXT,url TEXT,job TEXT)")
        connection.execute("INSERT INTO runs VALUES('legacy',1,'submitted',75,'historic','','legacy-other')")
    host.tick()
    assert len(host.calls) == 1 and donation.total_tokens(host.root) == 125
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT tokens,status FROM runs WHERE id='legacy'").fetchone() == (75, "submitted")
        assert connection.execute("SELECT COUNT(*) FROM publication_outbox").fetchone()[0] == 1
    assert donation.source_totals(host.root) == {"chatgpt": 75, "local": 50}


def test_actual_loaded_validator_change_cannot_downgrade_saved_proposal_checks(outbox_host):
    host = outbox_host
    host.remote.fork_failures = 1
    host.tick()
    before = len(host.remote.calls)
    def weakened_validator(text, sources):
        raise AssertionError("Changed validator must be detected before any reuse")
    with patch.object(donation, "validate_proposal", weakened_validator), \
         patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        host.tick()
    assert len(host.calls) == 1 and len(host.remote.calls) == before
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT phase FROM publication_outbox").fetchone()[0] == "stale"


def test_publication_policy_uses_loaded_code_not_replaced_disk_source_text():
    import inspect
    expected = donation._publication_policy()
    with patch.object(inspect, "getsource", return_value="private replaced disk text"):
        assert donation._publication_policy() == expected


def test_unresolved_publication_reserves_its_area_but_another_public_job_continues(outbox_host):
    host = outbox_host
    other_sources = {"other.py": SOURCE, "Validation/test_other.py": TEST}
    other = {"id": "other-area", "title": "Independent area", "paths": list(other_sources)}
    all_sources = {**SOURCES, **other_sources}
    def public(path, revision):
        return json.dumps([JOB, other]) if path == "donation-tasks.json" else all_sources[path]
    def complete(payload, resource, intensity, cancel, receipt):
        host.calls.append(payload)
        task = json.loads(payload["messages"][-1]["content"])["task"]
        result = json.loads(json.dumps(RESULT))
        if task["id"] == other["id"]:
            proposal = json.loads(json.dumps(PROPOSAL))
            for change in proposal["changes"]:
                change["path"] = change["path"].replace("example", "other")
            result["choices"][0]["message"]["content"] = json.dumps(proposal)
        receipt(result, "ollama", "main", False)
        return result
    host.remote.pr_failure = "unknown-absent"
    with patch.object(donation, "public_file", side_effect=public), \
         patch.object(donation, "complete_donation", side_effect=complete):
        host.tick()
        with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
            pending = host.tick()
            assert "confirmation" in pending.status_message()
            host.tick()  # Same clock: bounded reconciliation skips the just-checked outbox.
    assert len(host.calls) == 2 and len(host.remote.prs) == 1
    tasks = [json.loads(call["messages"][-1]["content"])["task"]["id"] for call in host.calls]
    assert len(set(tasks)) == 2
    assert sum(endpoint.endswith("/pulls") and body is not None for endpoint, body in host.remote.calls) == 2
    assert donation.total_tokens(host.root) == 100
    with closing(donation.connection(host.root)) as connection:
        rows = connection.execute("SELECT phase FROM publication_outbox ORDER BY phase").fetchall()
        assert rows == [("pr_intent",), ("submitted",)]


@pytest.mark.parametrize("fault", ["nested_commit", "proposal_none", "empty_change", "state_tree", "state_intent"])
def test_bounded_corrupt_saved_shapes_are_rejected_before_network_and_do_not_retry_forever(outbox_host, fault):
    host = outbox_host
    host.remote.fork_failures = 1
    host.tick()
    before = len(host.remote.calls)
    with closing(donation.connection(host.root)) as connection, connection:
        raw, state = connection.execute("SELECT payload,state FROM publication_outbox").fetchone()
        value, metadata = json.loads(raw), json.loads(state)
        if fault == "nested_commit": value["commit"]["commit"] = 17
        if fault == "proposal_none": value["proposal"] = None
        if fault == "empty_change": value["proposal"]["changes"] = [{}]
        if fault == "state_tree": metadata["tree"] = []
        if fault == "state_intent": metadata["intent"] = []
        raw = json.dumps(value).encode()
        connection.execute("UPDATE publication_outbox SET payload=?,payload_sha=?,state=?",
            (raw, hashlib.sha256(raw).hexdigest(), json.dumps(metadata)))
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        result = host.tick()
    assert "data is unavailable" in result.status_message()
    assert len(host.remote.calls) == before and len(host.calls) == 1
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute("SELECT phase,length(payload) FROM publication_outbox").fetchone() == ("corrupt", 0)


def test_corrupt_unknown_post_keeps_area_reserved_instead_of_regenerating_duplicate(outbox_host):
    host = outbox_host
    host.remote.pr_failure = "unknown-absent"
    host.tick()
    with closing(donation.connection(host.root)) as connection, connection:
        raw = connection.execute("SELECT payload FROM publication_outbox").fetchone()[0]
        value = json.loads(raw)
        value["commit"]["commit"] = None
        raw = json.dumps(value).encode()
        connection.execute("UPDATE publication_outbox SET payload=?,payload_sha=?", (raw, hashlib.sha256(raw).hexdigest()))
    before = len(host.remote.calls)
    with patch.object(donation.time, "time", return_value=outbox_expire(host.root)):
        host.tick()
        host.tick()
    assert len(host.calls) == 1 and len(host.remote.calls) >= before
    assert sum(endpoint.endswith("/pulls") and body is not None for endpoint, body in host.remote.calls) == 1
    with closing(donation.connection(host.root)) as connection:
        phase, size = connection.execute("SELECT phase,length(payload) FROM publication_outbox").fetchone()
        assert phase == "corrupt_pending" and size > 0
    assert donation.claim_run(host.root, JOB["id"], "local", "medium", "f"*40) == ""



def test_service_uses_direct_pull_list_when_search_author_query_would_return_422(outbox_host):
    host=outbox_host;real_api=host.remote.api
    def api(endpoint,data=None):
        if endpoint.startswith('search/'):
            raise donation.GitHubRequestError('proposal-search',422,'http')
        return real_api(endpoint,data)
    with patch.object(donation,'gh_api',side_effect=api):result=host.tick()
    assert len(host.calls)==1 and len(host.remote.prs)==1,result.status_message()
    assert not any(endpoint.startswith('search/') for endpoint,_ in host.remote.calls)



def test_direct_pull_list_paginates_and_keeps_owned_edited_title_markers(monkeypatch):
    first=[open_pull(n,author='someone-else',body='<!-- cale-donation:foreign -->') for n in range(1,100)]
    first.append(open_pull(100,author='DONOR',title='Edited title',body='<!-- cale-donation:owned-area -->'))
    pages=[first,[open_pull(101,author='donor',title='[Donation] Another proposal')]];calls=[]
    def api(endpoint):
        calls.append(endpoint);return pages[len(calls)-1]
    monkeypatch.setattr(donation,'gh_api',api)
    result=donation.open_donation_proposals('donor',lambda:None)
    assert result['total_count']==2 and 'owned-area' in result['items'][0]['body']
    assert 'foreign' not in json.dumps(result) and len(calls)==2
    assert all('sort=created' in call and 'direction=asc' in call for call in calls)


@pytest.mark.parametrize('fault',['missing_user','number_bool','foreign_repository','foreign_url','closed','title_none','body_dict','oversized_body','invalid_page','surrogate'])
def test_direct_pull_list_unknown_shape_is_not_an_empty_queue(monkeypatch,fault):
    item=open_pull(1);records=[item]
    if fault=='missing_user':item['user']=None
    elif fault=='number_bool':item['number']=True
    elif fault=='foreign_repository':item['base']['repo']['full_name']='other/repo'
    elif fault=='foreign_url':item['html_url']='https://github.com/other/repo/pull/1'
    elif fault=='closed':item['state']='closed'
    elif fault=='title_none':item['title']=None
    elif fault=='body_dict':item['body']={}
    elif fault=='oversized_body':item['body']='x'*65537
    elif fault=='invalid_page':records={'items':[]}
    elif fault=='surrogate':item['body']=chr(0xd800)
    monkeypatch.setattr(donation,'gh_api',lambda endpoint:records)
    with pytest.raises(donation.GitHubRequestError) as error:
        donation.open_donation_proposals('donor',lambda:None)
    assert error.value.stage=='proposal-list' and error.value.kind=='response_invalid'


def test_direct_pull_list_duplicate_page_and_byte_budget_fail_closed(monkeypatch):
    records=[open_pull(n) for n in range(1,101)];calls=[]
    monkeypatch.setattr(donation,'gh_api',lambda endpoint:calls.append(endpoint) or records)
    with pytest.raises(donation.GitHubRequestError):donation.open_donation_proposals('donor',lambda:None)
    assert len(calls)==2
    monkeypatch.setattr(donation,'_OPEN_PULLS_MAX_BYTES',1)
    with pytest.raises(donation.GitHubRequestError):donation.open_donation_proposals('donor',lambda:None)


def test_direct_pull_list_full_hundred_page_bound_never_claims_complete(monkeypatch):
    calls=[]
    def api(endpoint):
        calls.append(endpoint);page=len(calls)
        return [open_pull((page-1)*100+n,author='other') for n in range(1,101)]
    monkeypatch.setattr(donation,'gh_api',api)
    with pytest.raises(donation.GitHubRequestError):donation.open_donation_proposals('donor',lambda:None)
    assert len(calls)==100 and 'page=100' in calls[-1]


def test_direct_pull_list_empty_termination_and_cancellation(monkeypatch):
    calls=[]
    def api(endpoint):
        calls.append(endpoint);return [open_pull(n,author='other') for n in range(1,101)] if len(calls)==1 else []
    monkeypatch.setattr(donation,'gh_api',api)
    assert donation.open_donation_proposals('donor',lambda:None)=={'total_count':0,'items':[]}
    assert len(calls)==2
    calls.clear();checks=[]
    def cancel():
        checks.append(True)
        if len(checks)==3:raise InterruptedError('owned test cancellation')
    with pytest.raises(InterruptedError):donation.open_donation_proposals('donor',cancel)
    assert len(calls)==1


@pytest.mark.parametrize('failure',[403,404,'incomplete'])
def test_service_never_infers_until_rest_listing_is_complete(outbox_host,failure):
    host=outbox_host;real_api=host.remote.api;pages=[]
    def api(endpoint,data=None):
        if is_open_pull_list(endpoint):
            pages.append(endpoint)
            if len(pages)==1:return [open_pull(n,author='other') for n in range(1,101)]
            if failure=='incomplete':return {}
            raise donation.GitHubRequestError('proposal-list',failure,'http')
        return real_api(endpoint,data)
    with patch.object(donation,'gh_api',side_effect=api):result=host.tick()
    assert not host.calls and not host.remote.prs and len(pages)==2
    assert 'proposal listing' in result.status_message()
    with closing(donation.connection(host.root)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==0


def test_slow_owned_edited_title_proposal_blocks_before_model(outbox_host):
    host=outbox_host;donation.set_enabled(True,host.root,github_account='donor',sources=['local'],intensity='slow')
    real_api=host.remote.api
    def api(endpoint,data=None):
        if is_open_pull_list(endpoint):return [open_pull(7,title='Edited title',body='<!-- cale-donation:example-fix -->')]
        return real_api(endpoint,data)
    service=donation.Service(host.root);service.last_activity=time.monotonic()-301
    with patch.object(donation,'gh_api',side_effect=api):service._run(threading.Event())
    assert not host.calls and 'waiting for maintainer review' in service.status_message()


def test_existing_verified_fork_never_requests_creation():
    calls=[]
    def api(endpoint,data=None):
        calls.append((endpoint,data))
        assert data is None
        return {'full_name':'DONOR/cale-ai-workbench','fork':True,'parent':{'full_name':donation.UPSTREAM}}
    assert donation._proposal_target('donor',lambda:None,api)=='donor/cale-ai-workbench'
    assert calls==[('repos/donor/cale-ai-workbench',None)]


@pytest.mark.parametrize('metadata',[None,[],{},
    {'full_name':'donor/cale-ai-workbench','fork':False,'parent':{'full_name':donation.UPSTREAM}},
    {'full_name':'other/cale-ai-workbench','fork':True,'parent':{'full_name':donation.UPSTREAM}},
    {'full_name':'donor/cale-ai-workbench','fork':True,'parent':{'full_name':'other/repository'}},
    {'full_name':[],'fork':True,'parent':{'full_name':donation.UPSTREAM}},
    {'full_name':'donor/cale-ai-workbench','fork':True,'parent':{'full_name':[]}}])
def test_fork_lookup_rejects_unverified_or_malformed_destination_without_writes(metadata):
    calls=[]
    def api(endpoint,data=None):
        calls.append((endpoint,data));return metadata
    with pytest.raises(donation.GitHubRequestError) as error:
        donation._proposal_target('donor',lambda:None,api)
    assert error.value.kind=='response_invalid'
    assert len(calls)==1 and calls[0][1] is None


@pytest.mark.parametrize('status',[401,403,429,500])
def test_unknown_fork_lookup_does_not_attempt_creation(status):
    calls=[]
    def api(endpoint,data=None):
        calls.append((endpoint,data));raise donation.GitHubRequestError('repository',status,'http')
    with pytest.raises(donation.GitHubRequestError) as error:
        donation._proposal_target('donor',lambda:None,api)
    assert error.value.status==status and len(calls)==1


def test_fork403_saved_draft_recovers_to_existing_fork_without_new_ai(outbox_host):
    import donation_ui
    host=outbox_host;host.remote.fork_failures=1;host.remote.fork_failure_status=403
    first=host.tick();messages=[]
    donation_ui.ChatActivity(first,messages.append).poll()
    assert len(host.calls)==1 and donation.total_tokens(host.root)==50
    assert any('GitHub HTTP 403 during fork creation' in message for message in messages)
    with closing(donation.connection(host.root)) as db:
        original=db.execute('SELECT id FROM runs').fetchone()[0]
        assert db.execute('SELECT length(payload) FROM publication_outbox').fetchone()[0]>0
    # Simulate a now-visible existing fork. Creation remains forbidden: a
    # successful retry must use GET metadata and retain the same saved work.
    host.remote.fork_created=True;host.remote.fork_failures=100
    before=len(host.remote.calls)
    with patch.object(donation.time,'time',return_value=outbox_expire(host.root)):
        host.tick()
    assert len(host.calls)==1 and donation.total_tokens(host.root)==50 and len(host.remote.prs)==1
    assert not any(endpoint.endswith('/forks') for endpoint,_ in host.remote.calls[before:])
    with closing(donation.connection(host.root)) as db:
        assert db.execute('SELECT id,status FROM runs').fetchall()==[(original,'submitted')]


@pytest.mark.parametrize('failure',[
    {'stage':[], 'status':403, 'kind':'http'},
    {'stage':'fork-create','status':True,'kind':'http'},
    {'stage':'fork-create','status':403,'kind':'SECRET https://private.invalid'},
])
def test_wait_chat_ignores_untrusted_github_diagnostics(failure):
    import donation_ui
    messages=[]
    service=SimpleNamespace(drain_events=lambda:[{'run_id':'run','phase':'waiting','github_failure':failure}])
    donation_ui.ChatActivity(service,messages.append).poll()
    assert len(messages)==1 and 'SECRET' not in messages[0] and 'private.invalid' not in messages[0]
    assert 'GitHub HTTP' not in messages[0]


def test_public_confirmation_keeps_existing_527_saved_payload_policy():
    assert donation._publication_policy()=='2f2342462a62b4057d69608630bc596a2ecdf9e3dc4c28d2d757104510578ced'


def test_donor_only_pr_is_pending_then_publicly_recovers_without_more_tokens_or_posts(outbox_host):
    import donation_ui
    host=outbox_host;host.remote.public_visible=False
    first=host.tick();messages=[]
    donation_ui.ChatActivity(first,messages.append).poll()
    assert len(host.calls)==1 and len(host.remote.prs)==1 and donation.total_tokens(host.root)==50
    assert not any('just sent' in message for message in messages)
    assert any('public proposal confirmation' in message and 'not confirmed' in message for message in messages)
    with closing(donation.connection(host.root)) as db:
        original=db.execute('SELECT id FROM runs').fetchone()[0]
        assert db.execute('SELECT phase,length(payload) FROM publication_outbox').fetchone()[1]>0
        assert db.execute('SELECT COUNT(*) FROM runs WHERE status=\'submitted\'').fetchone()[0]==0
    calls=len(host.remote.calls)
    host.tick()  # Shared cooldown persists across Service instances.
    assert len(host.remote.calls)==calls and len(host.calls)==1
    host.remote.public_visible=True
    with patch.object(donation.time,'time',return_value=outbox_expire(host.root)):
        second=host.tick()
    assert len(host.calls)==1 and len(host.remote.prs)==1 and donation.total_tokens(host.root)==50
    assert sum(endpoint.endswith('/pulls') and body is not None for endpoint,body in host.remote.calls)==1
    with closing(donation.connection(host.root)) as db:
        assert db.execute('SELECT id,status FROM runs').fetchall()==[(original,'submitted')]
        assert db.execute('SELECT phase,length(payload) FROM publication_outbox').fetchone()==('submitted',0)
    messages=[];donation_ui.ChatActivity(second,messages.append).poll()
    assert any('just sent' in message and '/pull/1' in message for message in messages)


@pytest.mark.parametrize('field,value',[
    ('number',True),('html_url','https://github.com/other/repo/pull/1'),
    ('body','Unrelated public proposal'),('state','unknown'),('base',None),
    ('user',{'login':'other'}),('head',{'sha':'f'*40}),
])
def test_public_result_must_match_the_exact_saved_proposal(outbox_host,field,value):
    host=outbox_host;original=donation.public_pull_request
    def public(number):
        result=original(number);result[field]=value;return result
    with patch.object(donation,'public_pull_request',side_effect=public):
        service=host.tick()
    assert len(host.calls)==1 and len(host.remote.prs)==1
    assert 'not confirmed' in service.status_message()
    with closing(donation.connection(host.root)) as db:
        assert db.execute('SELECT COUNT(*) FROM runs WHERE status=\'submitted\'').fetchone()[0]==0
        assert db.execute('SELECT length(payload) FROM publication_outbox').fetchone()[0]>0


class PublicPullResponse:
    status=200
    def __init__(self,body=b'{}',url=None):
        self.body=body;self.url=url or f'https://api.github.com/repos/{donation.UPSTREAM}/pulls/728'
        self.read_limit=None
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,limit):self.read_limit=limit;return self.body[:limit]
    def geturl(self):return self.url


def test_public_pull_is_a_bounded_fixed_origin_read_without_account_headers():
    response=PublicPullResponse(b'{"number":728}')
    captured=[]
    def open_request(request,timeout):
        captured.append((request,timeout));return response
    with patch.object(donation,'build_opener',return_value=SimpleNamespace(open=open_request)) as build:
        assert donation.public_pull_request(728)=={'number':728}
    request,timeout=captured[0]
    assert request.full_url==f'https://api.github.com/repos/{donation.UPSTREAM}/pulls/728'
    assert timeout==15 and request.data is None and request.get_method()=='GET'
    assert {key.lower() for key,_ in request.header_items()}=={'accept','user-agent'}
    assert response.read_limit==256*1024+1
    redirect=build.call_args.args[0]
    assert redirect.redirect_request(None,None,302,'',{},'https://other.invalid') is None


@pytest.mark.parametrize('response',[
    PublicPullResponse(b'x'*(256*1024+1)),PublicPullResponse(b'[]'),
    PublicPullResponse(b'bad json'),PublicPullResponse(b'{}',url='https://other.invalid'),
])
def test_public_pull_unknown_response_never_counts_as_delivery(response):
    with patch.object(donation,'build_opener',return_value=SimpleNamespace(open=lambda *a,**k:response)):
        with pytest.raises(donation.PublicVisibilityPending):donation.public_pull_request(728)


@pytest.mark.parametrize('status,retry,expected',[(404,'',60),(403,'',3600),(429,'120',120),(429,'NaN',3600)])
def test_public_pull_errors_keep_typed_status_and_bounded_backoff_without_remote_text(status,retry,expected):
    from urllib.error import HTTPError
    error=HTTPError('https://private.invalid',status,'PRIVATE BODY',{'Retry-After':retry},None)
    with patch.object(donation,'build_opener',return_value=SimpleNamespace(open=lambda *a,**k:(_ for _ in ()).throw(error))):
        with pytest.raises(donation.PublicVisibilityPending) as raised:donation.public_pull_request(728)
    assert raised.value.status==status and raised.value.retry_seconds==expected
    assert raised.value.__context__ is None and raised.value.__cause__ is None
    assert 'PRIVATE' not in str(raised.value) and 'private.invalid' not in str(raised.value)


def test_public_pull_interruption_remains_cancellation():
    with patch.object(donation,'build_opener',return_value=SimpleNamespace(open=lambda *a,**k:(_ for _ in ()).throw(InterruptedError()))):
        with pytest.raises(InterruptedError):donation.public_pull_request(728)


class ModelPublicationRemote(PublicationRemote):
    """Extend the real-shaped fake GitHub with captured model tree/ancestry."""
    def __init__(self):
        super().__init__()
        self.tree_files = []
        self.wrong_ancestry = False
        self.wrong_blob = False

    def api(self, endpoint, data=None):
        if "/git/matching-refs/heads/" in endpoint:
            self.calls.append((endpoint,data))
            branch=endpoint.split('/git/matching-refs/heads/')[1]
            return [{'ref':'refs/heads/'+branch,'object':{'sha':self.refs[branch]}}] if branch in self.refs else []
        if "/compare/" in endpoint:
            self.calls.append((endpoint,data))
            parent,head=endpoint.split('/compare/')[1].split('...')
            if endpoint.startswith('repos/'+donation.UPSTREAM+'/'):
                return {'status':'identical' if parent==head else 'ahead',
                    'merge_base_commit':{'sha':'f'*40 if self.wrong_ancestry else parent}}
            files=json.loads(json.dumps(self.tree_files))
            if self.wrong_blob:files[0]['sha']='f'*40
            return {'total_commits':1,'files':files}
        if endpoint.endswith('/git/trees') and data is not None:
            self.tree_files=[{'filename':item['path'],'status':'added','sha':donation._public_blob(item['content'])}
                for item in data['tree']]
        return super().api(endpoint,data)


@pytest.fixture
def model_upload_host(enabled,monkeypatch):
    import github_account
    donation.set_enabled(True,enabled,sources=['local'],github_account='donor',intensity='medium',model_training=True)
    folder=enabled/'model-candidate'
    fingerprint=artifacts(folder)
    claimed=models.claim(enabled)
    assert claimed['fingerprint']==fingerprint
    trained={'folder':str(folder),'qualified':True,'seconds':2}
    remote=ModelPublicationRemote()
    monkeypatch.setattr(github_account,'account',lambda:'donor')
    monkeypatch.setattr(donation.Service,'allowed',lambda *a,**k:True)
    monkeypatch.setattr(donation,'gh_api',remote.api)
    def public(number):
        if not remote.public_visible:raise donation.PublicVisibilityPending(404,'http')
        found=next((pr for pr in remote.prs if pr['number']==number),None)
        if found is None:raise donation.PublicVisibilityPending(404,'http')
        return json.loads(json.dumps(found))
    monkeypatch.setattr(donation,'public_pull_request',public)
    return SimpleNamespace(root=enabled,claimed=claimed,trained=trained,remote=remote,
        publish=lambda:donation.publish_model_candidate(enabled,claimed,trained,'donor',lambda:None))


def test_model_upload_preserves_trainer_identity_and_uses_only_reviewed_json(model_upload_host):
    host=model_upload_host
    before=models.identity()
    assert host.publish().endswith('/pull/1')
    assert models.identity()==before
    assert hashlib.sha256(__import__('pathlib').Path(models.__file__).read_bytes()).hexdigest()=='55e0cea16b25c834ae1629d194a3d2c0637eabba168f01427417de4be275f67f'
    assert len(host.remote.prs)==1 and host.remote.prs[0]['draft'] is True
    assert {file['filename'] for file in host.remote.tree_files}=={
        f'ModelContributions/{host.claimed["fingerprint"]}/{name}' for name in models.ARTIFACTS}
    assert donation.total_tokens(host.root)==0


def test_model_upload_reuses_existing_fork_even_if_creation_is_forbidden(model_upload_host):
    host=model_upload_host;host.remote.fork_created=True;host.remote.fork_failures=100;host.remote.fork_failure_status=403
    assert host.publish().endswith('/pull/1')
    assert not any(endpoint.endswith('/forks') for endpoint,_ in host.remote.calls)


@pytest.mark.parametrize('failure',['applied-lost','unknown-absent','rejected'])
def test_model_upload_journals_pr_intent_and_never_duplicates_ambiguous_post(model_upload_host,failure):
    host=model_upload_host;host.remote.pr_failure=failure
    with pytest.raises(donation.GitHubRequestError):host.publish()
    with closing(donation.connection(host.root)) as db:
        state,owner=db.execute('SELECT state,owner FROM model_publication_journal').fetchone()
    assert owner=='' and json.loads(state)['pr_attempted']==(failure!='rejected')
    if failure=='unknown-absent':
        with pytest.raises(donation.PublicationPending,match='no replacement'):host.publish()
        assert not host.remote.prs
    else:
        assert host.publish().endswith('/pull/1') and len(host.remote.prs)==1
    assert sum(endpoint.endswith('/pulls') and body is not None for endpoint,body in host.remote.calls)==(2 if failure=='rejected' else 1)


def test_model_upload_public_visibility_wait_keeps_same_candidate_and_pr(model_upload_host):
    host=model_upload_host;host.remote.public_visible=False
    with pytest.raises(donation.PublicVisibilityPending):host.publish()
    host.remote.public_visible=True
    assert host.publish().endswith('/pull/1')
    assert len(host.remote.prs)==1
    assert sum(endpoint.endswith('/pulls') and body is not None for endpoint,body in host.remote.calls)==1
    assert donation.total_tokens(host.root)==0


@pytest.mark.parametrize('fault',['wrong_blob','wrong_ancestry'])
def test_model_upload_rejects_unreviewed_delta_or_foreign_base(model_upload_host,fault):
    host=model_upload_host;host.remote.ref_lost=True
    with pytest.raises(donation.GitHubRequestError):host.publish()
    setattr(host.remote,fault,True)
    with pytest.raises(donation.PublicationPending):host.publish()
    assert not host.remote.prs


def test_model_upload_recovers_applied_ref_and_new_main_without_recreating_objects(model_upload_host):
    host=model_upload_host;host.remote.ref_lost=True
    with pytest.raises(donation.GitHubRequestError):host.publish()
    commits=len(host.remote.commits)
    host.remote.revision='d'*40
    assert host.publish().endswith('/pull/1')
    assert len(host.remote.commits)==commits and len(host.remote.refs)==1 and len(host.remote.prs)==1


def test_model_upload_lease_is_exclusive_while_owner_thread_lives(model_upload_host):
    host=model_upload_host
    bodies=models.review_artifacts(host.trained['folder'],host.claimed['fingerprint'])
    owner=donation.ModelPublicationJournal(host.root,host.claimed,'donor',bodies,lambda:None)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(host.publish)
            with pytest.raises(donation.PublicationPending,match='live worker'):future.result()
        assert not host.remote.calls
    finally:owner.close()
    assert host.publish().endswith('/pull/1')


@pytest.mark.parametrize('bad_state',['[]','{"pr_attempted":"true"}','x'*8193])
def test_model_upload_corrupt_journal_never_authorizes_another_post(model_upload_host,bad_state):
    host=model_upload_host;host.remote.pr_failure='unknown-absent'
    with pytest.raises(donation.GitHubRequestError):host.publish()
    before=len(host.remote.calls)
    with closing(donation.connection(host.root)) as db,db:
        db.execute('UPDATE model_publication_journal SET state=?',(bad_state,))
    with pytest.raises(donation.PublicationPending):host.publish()
    assert len(host.remote.calls)==before


def test_model_upload_opt_out_prevents_all_remote_work(model_upload_host):
    host=model_upload_host;donation.set_enabled(False,host.root)
    with pytest.raises(InterruptedError):host.publish()
    assert not host.remote.calls


def test_model_upload_every_write_has_durable_artifact_binding_and_intent(model_upload_host,monkeypatch):
    host=model_upload_host;actual=host.remote.api;writes=[]
    def api(endpoint,data=None):
        if data is not None:
            with closing(donation.connection(host.root)) as db:
                raw,owner=db.execute('SELECT state,owner FROM model_publication_journal').fetchone()
            state=json.loads(raw)
            assert owner and set(state['artifacts'])==set(models.ARTIFACTS)
            assert state['intent']==donation._github_stage(endpoint,data)
            if endpoint.endswith('/pulls'):assert state['pr_attempted'] is True
            writes.append(endpoint)
        return actual(endpoint,data)
    monkeypatch.setattr(donation,'gh_api',api)
    assert host.publish().endswith('/pull/1') and writes


def test_model_scheduler_hidden_pr_recovers_without_retraining_or_double_compute(model_upload_host,monkeypatch):
    host=model_upload_host
    models.complete(host.root,host.claimed,host.trained,'ready')
    with models._connection(host.root) as db:
        db.execute('UPDATE model_experiments SET publication_checked=0')
    monkeypatch.setattr(models,'run_job',lambda *a,**k:pytest.fail('Cached candidate must not be retrained'))
    monkeypatch.setattr(donation,'complete_donation',lambda *a,**k:pytest.fail('No inference for a model upload'))
    host.remote.public_visible=False
    first=donation.Service(host.root);first._run(threading.Event())
    assert 'not confirmed' in first.message and len(host.remote.prs)==1
    with models._connection(host.root) as db:
        assert db.execute('SELECT run_id,state FROM model_experiments').fetchall()==[(host.claimed['run_id'],'ready')]
    host.remote.public_visible=True
    with patch.object(donation.time,'time',return_value=outbox_expire(host.root)):
        second=donation.Service(host.root);second._run(threading.Event())
    assert 'submitted for owner review' in second.message
    with models._connection(host.root) as db:
        assert db.execute('SELECT run_id,state FROM model_experiments').fetchall()==[(host.claimed['run_id'],'submitted')]
    assert models.summary(host.root)['worker_seconds']==2 and donation.total_tokens(host.root)==0
    assert len(host.remote.prs)==1 and sum(endpoint.endswith('/pulls') and body is not None for endpoint,body in host.remote.calls)==1


def test_model_upload_cancel_after_pr_intent_sends_no_post_and_can_retry(model_upload_host):
    host=model_upload_host;cancelled=[False]
    def cancel():
        with closing(donation.connection(host.root)) as db:
            row=db.execute('SELECT state FROM model_publication_journal').fetchone()
        if row:
            state=json.loads(row[0])
            if not cancelled[0] and state.get('intent')=='proposal-create' and state['pr_attempted']:
                cancelled[0]=True
                raise InterruptedError()
    with pytest.raises(InterruptedError):
        donation.publish_model_candidate(host.root,host.claimed,host.trained,'donor',cancel)
    assert not host.remote.prs and not any(endpoint.endswith('/pulls') and body is not None for endpoint,body in host.remote.calls)
    assert host.publish().endswith('/pull/1')


def test_model_upload_requires_approval_for_the_actual_github_account(model_upload_host,monkeypatch):
    host=model_upload_host
    monkeypatch.setattr('github_account.account',lambda:'other')
    with pytest.raises(InterruptedError):
        donation.publish_model_candidate(host.root,host.claimed,host.trained,'other',lambda:None)
    assert not host.remote.calls


def test_cached_model_upload_retry_claim_is_atomic_across_threads(model_upload_host):
    host=model_upload_host
    models.complete(host.root,host.claimed,host.trained,'ready')
    with models._connection(host.root) as db:
        db.execute('UPDATE model_experiments SET publication_checked=0')
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed=list(pool.map(lambda _:donation.claim_model_publication(host.root,'donor'),range(8)))
    successful=[value for value in claimed if value]
    assert len(successful)==1 and successful[0]['run_id']==host.claimed['run_id']
    assert successful[0]['resume']==host.trained
    assert not host.remote.calls and donation.total_tokens(host.root)==0


def test_cached_model_upload_retry_yields_to_a_live_upload_owner(model_upload_host):
    host=model_upload_host
    models.complete(host.root,host.claimed,host.trained,'ready')
    with models._connection(host.root) as db:
        db.execute('UPDATE model_experiments SET publication_checked=0')
    bodies=models.review_artifacts(host.trained['folder'],host.claimed['fingerprint'])
    owner=donation.ModelPublicationJournal(host.root,host.claimed,'donor',bodies,lambda:None)
    try:
        assert donation.claim_model_publication(host.root,'donor') is None
        with models._connection(host.root) as db:
            assert db.execute('SELECT publication_checked FROM model_experiments').fetchone()[0]==0
    finally:owner.close()
    assert donation.claim_model_publication(host.root,'donor')['run_id']==host.claimed['run_id']


def test_model_upload_account_switch_keeps_independent_journals_and_revokes_old_owner(model_upload_host,monkeypatch):
    host=model_upload_host;bodies=models.review_artifacts(host.trained['folder'],host.claimed['fingerprint'])
    old=donation.ModelPublicationJournal(host.root,host.claimed,'donor',bodies,lambda:None)
    donation.set_enabled(True,host.root,sources=['local'],github_account='other',intensity='medium',model_training=True)
    monkeypatch.setattr('github_account.account',lambda:'other')
    new=donation.ModelPublicationJournal(host.root,host.claimed,'other',bodies,lambda:None)
    try:
        with pytest.raises(InterruptedError):old.guard()
        new.guard()
        old.close()
        with closing(donation.connection(host.root)) as db:
            assert db.execute('SELECT COUNT(*) FROM model_publication_journal').fetchone()[0]==2
            assert db.execute('SELECT owner FROM model_publication_journal WHERE account=?',(new.account,)).fetchone()[0]==new.owner
    finally:
        old.close();new.close()
    assert not host.remote.calls


@pytest.mark.parametrize('fault',['boolean_commit_count','unknown_file_without_sha','wrong_commit_identity','changed_ref'])
def test_model_transport_uncertainty_blocks_pr_before_dispatch(model_upload_host,monkeypatch,fault):
    host=model_upload_host;original=host.remote.api
    def api(endpoint,data=None):
        result=original(endpoint,data)
        if fault=='changed_ref' and endpoint.endswith('/git/refs') and data is not None:
            host.remote.refs[data['ref'].removeprefix('refs/heads/')]='f'*40
        if fault=='wrong_commit_identity' and '/git/commits/' in endpoint:
            result={**result,'sha':'f'*40}
        if '/compare/' in endpoint and endpoint.startswith('repos/donor/'):
            if fault=='boolean_commit_count':result={**result,'total_commits':True}
            if fault=='unknown_file_without_sha':
                result={**result,'files':[*result['files'][:-1],{'filename':'unreviewed.py','status':'added'}]}
        return result
    monkeypatch.setattr(donation,'gh_api',api)
    with pytest.raises(donation.PublicationPending):host.publish()
    assert not host.remote.prs
    assert not any(endpoint.endswith('/pulls') and body is not None for endpoint,body in host.remote.calls)
