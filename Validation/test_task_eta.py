"""The countdown survives restarts and excludes pauses and supervisor downtime."""

import task_eta


def test_total_time_shows_wall_clock_and_freezes_on_acceptance():
    assert task_eta.total_time(1000, 1000, "working", now=1065) == "1m 05s"
    assert task_eta.total_time(1000, 1090, "paused", now=1200) == "3m 20s"
    assert task_eta.total_time(1000, 1125, "ready_for_review", now=1200) == "2m 05s"
    assert task_eta.total_time(1000, 1125, "accepted", now=9999) == "2m 05s"
    assert task_eta.total_time(0, 0, "working", now=1000) == "—"


def test_countdown_needs_real_progress_and_survives_restart(tmp_path):
    db = tmp_path / "projects.db"
    assert task_eta.countdown(db, "task", 10, "working", 1000) == "Estimating time…"
    assert task_eta.countdown(db, "task", 30, "working", 1030) == "Estimating time…"
    result = task_eta.countdown(db, "task", 50, "working", 1060)
    assert result == "About 1m 07s to review"
    # Each call reads persisted samples; the remaining time falls as work continues.
    assert task_eta.countdown(db, "task", 50, "working", 1070) == "About 57s to review"


def test_pause_does_not_advance_active_clock(tmp_path):
    db = tmp_path / "projects.db"
    task_eta.countdown(db, "task", 10, "working", 1000)
    task_eta.countdown(db, "task", 30, "working", 1030)
    task_eta.countdown(db, "task", 50, "working", 1060)
    assert task_eta.countdown(db, "task", 50, "paused", 1070) == "Paused"
    assert task_eta.countdown(db, "task", 50, "working", 2000) == "Estimate pending progress"
    assert task_eta.countdown(db, "task", 70, "working", 2030).startswith("About ")


def test_stalled_and_completed_tasks(tmp_path):
    db = tmp_path / "projects.db"
    task_eta.countdown(db, "task", 10, "working", 1000)
    task_eta.countdown(db, "task", 30, "working", 1030)
    task_eta.countdown(db, "task", 50, "working", 1060)
    assert task_eta.countdown(db, "task", 50, "working", 1700) == "Estimate pending progress"
    assert task_eta.countdown(db, "task", 100, "ready_for_review", 1701) == ""
    assert task_eta.countdown(db, "task", 100, "accepted", 1702) == ""
