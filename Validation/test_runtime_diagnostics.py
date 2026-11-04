"""Real device failures remain visible after the desktop closes."""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

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


def test_ongoing_failures_do_not_extend_reported_episode(tmp_path):
    start = 100
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=start)
    assert diagnostics.record(tmp_path, "camera", "unavailable", now=start + 1)
    assert not diagnostics.record(
        tmp_path, "camera", "unavailable",
        now=start + diagnostics.EPISODE_SECONDS // 2,
    )

    # The episode expires even though the most recent failure is still recent.
    new_start = start + diagnostics.EPISODE_SECONDS + 1
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=new_start)
    issue = diagnostics.issues(tmp_path)[0]
    assert issue["first_seen"] == new_start
    assert issue["last_seen"] == new_start
    assert issue["count"] == 1
    assert issue["reported"] == 0
    assert diagnostics.record(tmp_path, "camera", "unavailable", now=new_start + 1)
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=new_start + 2)


def test_failure_at_expiration_boundary_starts_new_episode(tmp_path):
    start = 100
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=start)
    assert diagnostics.record(tmp_path, "camera", "unavailable", now=start + 1)

    boundary = start + diagnostics.EPISODE_SECONDS
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=boundary)
    issue = diagnostics.issues(tmp_path)[0]
    assert issue["first_seen"] == boundary
    assert issue["last_seen"] == boundary
    assert issue["count"] == 1
    assert issue["reported"] == 0

    assert diagnostics.record(tmp_path, "camera", "unavailable", now=boundary + 1)
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=boundary + 2)
    issue = diagnostics.issues(tmp_path)[0]
    assert issue["count"] == 3
    assert issue["reported"] == 1


def test_issue_message_is_bounded_and_categories_are_validated(tmp_path):
    diagnostics.record(tmp_path, "speech", "x" * 1000, now=10)
    assert len(diagnostics.issues(tmp_path)[0]["detail"]) == diagnostics.MAX_DETAIL
    try:
        diagnostics.record(tmp_path, "speech; DROP TABLE issues", "bad", now=11)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid category should be rejected")


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("sample_clock", [False, True])
def test_nonfinite_timestamp_does_not_discard_episode(tmp_path, monkeypatch, timestamp, sample_clock):
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=100)
    original = diagnostics.issues(tmp_path)

    with pytest.raises(ValueError, match="finite failure timestamp"):
        if sample_clock:
            monkeypatch.setattr(diagnostics.time, "time", lambda: timestamp)
            diagnostics.record(tmp_path, "camera", "unavailable")
        else:
            diagnostics.record(tmp_path, "camera", "unavailable", now=timestamp)

    assert diagnostics.issues(tmp_path) == original
    assert diagnostics.record(tmp_path, "camera", "unavailable", now=101)
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=102)


def test_backward_clock_starts_a_new_reportable_episode(tmp_path):
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=10000)
    assert diagnostics.record(tmp_path, "camera", "unavailable", now=10010)

    # Persisted wall-clock timestamps may be ahead after a clock correction.
    assert not diagnostics.record(tmp_path, "camera", "disconnected", now=10)
    issue = diagnostics.issues(tmp_path)[0]
    assert issue["first_seen"] == 10
    assert issue["last_seen"] == 10
    assert issue["count"] == 1
    assert issue["reported"] == 0
    assert diagnostics.record(tmp_path, "camera", "disconnected", now=20)
    assert not diagnostics.record(tmp_path, "camera", "disconnected", now=30)


def test_delayed_recorder_does_not_reset_concurrent_episode(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event

    entered = Event()
    release = Event()
    original_open = diagnostics._open
    timestamps = iter((100, 101))
    monkeypatch.setattr(diagnostics.time, "time", lambda: next(timestamps))

    @contextmanager
    def delayed_open(state):
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=10)
        with original_open(state) as db:
            yield db

    monkeypatch.setattr(diagnostics, "_open", delayed_open)
    with ThreadPoolExecutor(max_workers=1) as pool:
        delayed = pool.submit(diagnostics.record, tmp_path, "camera", "unavailable")
        try:
            assert entered.wait(timeout=10)
            assert not diagnostics.record(tmp_path, "camera", "unavailable")
        finally:
            release.set()
        assert delayed.result(timeout=10)

    issue = diagnostics.issues(tmp_path)[0]
    assert issue["first_seen"] == 100
    assert issue["last_seen"] == 101
    assert issue["count"] == 2
    assert issue["reported"] == 1


def test_concurrent_failures_are_counted_and_reported_once(tmp_path):
    # Initialize the database before exercising concurrent episode updates.
    assert not diagnostics.record(tmp_path, "camera", "unavailable", now=100)
    workers = 8
    failures_per_worker = 10
    ready = Barrier(workers)

    def record_failures(_):
        ready.wait(timeout=10)
        return [
            diagnostics.record(tmp_path, "camera", "unavailable", now=100)
            for _ in range(failures_per_worker)
        ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(record_failures, range(workers)))

    assert sum(sum(result) for result in results) == 1
    issue = diagnostics.issues(tmp_path)[0]
    assert issue["count"] == 1 + workers * failures_per_worker
    assert issue["reported"] == 1
