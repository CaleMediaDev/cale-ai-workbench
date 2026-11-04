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
          patch.object(donation, "gh_api", side_effect=lambda endpoint: {"total_count": 0} if "search/" in endpoint else COMMIT),
          patch.object(donation, "open_proposal", return_value="https://github.com/CaleMediaDev/cale-ai-workbench/pull/1") as publish):
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
          patch.object(donation, "gh_api", side_effect=lambda endpoint: {"total_count": 0} if "search/" in endpoint else COMMIT),
          patch.object(donation, "open_proposal", return_value="https://github.com/CaleMediaDev/cale-ai-workbench/pull/2") as publish):
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
        if "search/" in endpoint:
            return {"total_count": 101, "items": [{"body": "<!-- cale-donation:other -->"}] * (1 if "page=2" in endpoint else 100)}
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
          patch.object(donation, "gh_api", side_effect=lambda endpoint: {"total_count": 0} if "search/" in endpoint else COMMIT),
          patch.object(donation, "complete_donation", side_effect=complete),
          patch.object(donation, "open_proposal", return_value="https://github.com/CaleMediaDev/cale-ai-workbench/pull/7") as publish):
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
          patch.object(donation, "gh_api", side_effect=lambda endpoint: {"total_count": 0} if "search/" in endpoint else COMMIT),
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
        return {"total_count": 0} if "search/" in endpoint else COMMIT
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
    with patch('pc_offline.offline',return_value=False), patch('github_account.account',return_value='donor'), patch('chatgpt_auth.status',return_value={'sharing':True}), patch('chatgpt_auth.account_fingerprint',return_value=FINGERPRINT), patch.object(donation,'continuous_jobs',side_effect=lambda jobs,revision,**kwargs: jobs), patch.object(donation,'public_file',side_effect=lambda path,revision:json.dumps([blocked,other]) if path=='donation-tasks.json' else SOURCES[path]), patch.object(donation,'gh_api',side_effect=lambda endpoint: {'total_count':0} if 'search/' in endpoint else {'tree':[{'type':'blob','path':path,'sha':sha} for path,sha in BLOBS.items()]} if '/git/trees/' in endpoint else COMMIT), patch.object(donation,'complete_donation',side_effect=complete):
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
         patch.object(donation,'gh_api',side_effect=lambda endpoint:{'total_count':0} if 'search/' in endpoint else COMMIT),\
         patch.object(donation,'complete_donation',side_effect=complete),patch.object(donation,'open_proposal',side_effect=lambda *args: drafts.append(args) or 'https://github.com/CaleMediaDev/cale-ai-workbench/pull/11'),\
         patch.object(donation.threading,'Thread',DonationInlineThread):
        service.tick()
        assert resources==['chatgpt'] and service.next_attempt==0
        service.tick()
    assert resources==['chatgpt','local'] and len(drafts)==1
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
         patch.object(donation,'gh_api',side_effect=lambda endpoint:{'total_count':0} if 'search/' in endpoint else COMMIT),\
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
         patch.object(donation,'gh_api',side_effect=lambda endpoint:{'total_count':0} if 'search/' in endpoint else COMMIT),\
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
         patch.object(donation,'gh_api',side_effect=lambda endpoint:{'total_count':0} if 'search/' in endpoint else COMMIT),\
         patch.object(donation,'complete_donation',return_value=result),patch.object(donation,'open_proposal') as publish:
        service._run(threading.Event())
    publish.assert_not_called()
    events=service.drain_events()
    assert [event['phase'] for event in events]==['reviewing','checking','error']
    assert 'PRIVATE_MODEL_OUTPUT' not in json.dumps(events) and not any('pr_url' in event for event in events)
