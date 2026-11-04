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
          patch.object(donation, "open_proposal", return_value="https://github.com/test/pull/2") as publish):
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


def test_leaderboard_continues_partial_history_without_double_credit(tmp_path):
    def pages(endpoint):
        page = int(endpoint.rsplit("page=", 1)[1])
        return [merged_fix(n) for n in range((page - 1) * 100 + 1, page * 100 + 1)] if page <= 10 else []
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1000):
        partial = donation.refresh_leaderboard(tmp_path, fetch=pages)
    assert not partial["complete"] and partial["rows"][0]["accepted"] == 1000
    with patch("pc_offline.offline", return_value=False), patch.object(donation.time, "time", return_value=1061):
        complete = donation.refresh_leaderboard(tmp_path, fetch=pages, force=True)
    assert complete["complete"] and complete["rows"][0]["accepted"] == 1000



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
          patch.object(donation, "open_proposal", return_value="https://github.com/test/pull/7") as publish):
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


def test_parallel_lanes_cannot_claim_the_same_public_revision_job(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    # Initialize schema before racing the actual transactional claim path.
    with closing(donation.connection(tmp_path)):
        pass
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", COMMIT["sha"]), range(3)))
    assert sum(bool(result) for result in results) == 1
    donation.finish(tmp_path, next(result for result in results if result), "no_change")
    assert not donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", COMMIT["sha"])
    assert donation.claim_run(tmp_path, "different-area", "chatgpt", "full_blast", COMMIT["sha"])
    assert donation.claim_run(tmp_path, "same-area", "chatgpt", "full_blast", "c"*40)


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
