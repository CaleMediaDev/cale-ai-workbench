"""Real device failures remain visible after the desktop closes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_diagnostics as diagnostics


def test_repeated_failure_is_reported_once_and_persists(tmp_path):
    assert not diagnostics.record(tmp_path, "microphone", "  device   unavailable  ", now=10)
    assert diagnostics.record(tmp_path, "microphone", "device unavailable", now=20)
    assert not diagnostics.record(tmp_path, "microphone", "device unavailable", now=30)
    issue = diagnostics.issues(tmp_path)[0]
    assert issue["kind"] == "microphone"
    assert issue["count"] == 3
    assert issue["detail"] == "device unavailable"
    assert issue["reported"] == 1


def test_success_and_expired_episode_allow_new_report(tmp_path):
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=10)
    assert diagnostics.record(tmp_path, "camera", "unavailable", now=20)
    diagnostics.resolve(tmp_path, "camera")
    assert diagnostics.issues(tmp_path) == []
    assert not diagnostics.record(tmp_path, "camera", "disconnected", now=30)
    assert diagnostics.record(tmp_path, "camera", "disconnected", now=40)
    assert not diagnostics.record(tmp_path, "camera", "disconnected", now=4000)
    assert diagnostics.record(tmp_path, "camera", "disconnected", now=4010)


def test_issue_message_is_bounded_and_categories_are_validated(tmp_path):
    diagnostics.record(tmp_path, "speech", "x" * 1000, now=10)
    assert len(diagnostics.issues(tmp_path)[0]["detail"]) == diagnostics.MAX_DETAIL
    try:
        diagnostics.record(tmp_path, "speech; DROP TABLE issues", "bad", now=11)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid category should be rejected")
