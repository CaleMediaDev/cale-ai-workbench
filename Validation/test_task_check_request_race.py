"""A check waiting for SQLite must not advance superseded user instructions."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import project_memory
import task_context


@pytest.mark.parametrize("changed", [False, True])
def test_request_is_revalidated_after_waiting_for_database(tmp_path, monkeypatch, changed):
    requests = tmp_path / ".local-ai/USER-REQUESTS.md"
    requests.parent.mkdir()
    requests.write_text("Original request", encoding="utf-8")
    queued = task_context.plan_steps(tmp_path, [
        {"phase": "Build", "title": "First", "check_command": "exit 0"},
        {"phase": "Build", "title": "Second", "check_command": "exit 0"},
    ])
    fingerprint = task_context.request_identity(tmp_path)
    active = queued["active"]
    observed = threading.Event()
    original_identity = task_context.request_identity

    def observe_identity(folder):
        value = original_identity(folder)
        observed.set()
        return value

    monkeypatch.setattr(task_context, "request_identity", observe_identity)
    with project_memory.connection(tmp_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(task_context.record_step_check, tmp_path,
                                  active["id"], fingerprint, "Exit code: 0\nPASS",
                                  unchanged=True,
                                  expected_check=task_context.step_check_identity(active))
            try:
                assert observed.wait(5), "Check did not reach its initial request validation"
                requests.write_text("New user instructions" if changed else "Original request", encoding="utf-8")
            finally:
                blocker.commit()
            if changed:
                with pytest.raises(ValueError, match="instructions changed"):
                    pending.result(timeout=5)
            else:
                result = pending.result(timeout=5)
                assert result["passed"] and result["done"] == 1
    with project_memory.connection(tmp_path) as conn:
        rows = conn.execute("SELECT status,attempts,evidence FROM task_steps ORDER BY id").fetchall()
    if changed:
        assert [tuple(row) for row in rows] == [("active", 0, ""), ("pending", 0, "")]
        assert task_context.step_status(tmp_path)["total"] == 0
    else:
        assert [(row["status"], row["attempts"]) for row in rows] == [("done", 1), ("active", 0)]
