"""The countdown survives restarts and excludes pauses and supervisor downtime."""

import json

import task_eta


def test_total_time_shows_wall_clock_and_freezes_on_acceptance():
    assert task_eta.total_time(1000, 1000, "working", now=1065) == "1m 05s"
    assert task_eta.total_time(1000, 1090, "paused", now=1200) == "3m 20s"
    assert task_eta.total_time(1000, 1125, "ready_for_review", now=1200) == "2m 05s"
    assert task_eta.total_time(1000, 1125, "accepted", now=9999) == "2m 05s"
    assert task_eta.total_time(1000, 1500, "accepted", now=9999, finished=1125) == "2m 05s"
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


def test_music_eta_uses_completed_local_songs_and_explains_overrun(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    for index, elapsed in enumerate((48, 60, 72, 800)):
        (jobs / f"{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "duration_seconds": 180, "created": 1000,
            "finished": 1000 + elapsed,
        }), encoding="utf-8")
    estimate = task_eta.music_estimate_seconds(jobs, 180, False)
    assert estimate == 66
    assert task_eta.music_estimate_seconds(jobs, 180, True) is None
    current = {"status": "working", "requested_at": 2000,
               "estimated_seconds": estimate}
    assert task_eta.music_countdown(current, now=2030) == (
        "About 36s left · estimate from recent songs"
    )
    assert task_eta.music_countdown(current, now=2100) == (
        "Taking longer than recent songs · still working"
    )
    assert task_eta.music_countdown({**current, "status": "complete"}, now=2100) == ""


def test_short_vocal_eta_prefers_similar_songs_over_scaled_long_renders(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    for index in range(6):
        (jobs / f"long-{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "duration_seconds": 180, "created": 1000,
            "finished": 1060,
        }), encoding="utf-8")
    for index, elapsed in enumerate((54, 155)):
        (jobs / f"short-{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "duration_seconds": 60, "created": 1000,
            "finished": 1000 + elapsed,
        }), encoding="utf-8")
    (jobs / "retry.json").write_text(json.dumps({
        "status": "complete", "instrumental": False, "vocal_retry": 1,
        "duration_seconds": 60, "created": 1000, "finished": 1010,
    }), encoding="utf-8")
    assert task_eta.music_estimate_seconds(jobs, 60, False) == 104
