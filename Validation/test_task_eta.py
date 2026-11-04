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


def test_total_time_only_requires_updated_when_used_as_fallback():
    for updated in (None, "invalid", float("nan"), float("inf")):
        for status in ("working", "paused"):
            assert task_eta.total_time(1000, updated, status, now=1065) == "1m 05s"
        for status in ("ready_for_review", "accepted", "stopped"):
            assert task_eta.total_time(
                1000, updated, status, now=9999, finished=1125,
            ) == "2m 05s"
            assert task_eta.total_time(
                1000, updated, status, now=9999,
            ) == "—"
    for finished in (None, "invalid", float("nan"), 999):
        assert task_eta.total_time(
            1000, 1125, "accepted", now=9999, finished=finished,
        ) == "2m 05s"


def test_total_time_handles_invalid_current_timestamps():
    for invalid in ("invalid", "nan", "inf", "-inf",
                    float("nan"), float("inf"), float("-inf")):
        for status in ("working", "paused"):
            assert task_eta.total_time(
                1000, 1125, status, now=invalid,
            ) == "—"
        # Frozen lifetimes do not depend on the current timestamp.
        for status in ("ready_for_review", "accepted", "stopped"):
            assert task_eta.total_time(
                1000, 1125, status, now=invalid,
            ) == "2m 05s"
    assert task_eta.total_time(1000, 1000, "working", now="1065") == "1m 05s"


def test_total_time_rejects_terminal_fallback_before_creation():
    for status in ("ready_for_review", "accepted", "stopped"):
        for finished in (None, "invalid", float("nan"), 999):
            for updated in (0, -1, 999):
                assert task_eta.total_time(
                    1000, updated, status, now=2000, finished=finished,
                ) == "—"
            assert task_eta.total_time(
                1000, 1000, status, now=2000, finished=finished,
            ) == "0s"


def test_countdown_needs_real_progress_and_survives_restart(tmp_path):
    db = tmp_path / "projects.db"
    assert task_eta.countdown(db, "task", 10, "working", 1000) == "Estimating time…"
    assert task_eta.countdown(db, "task", 30, "working", 1030) == "Estimating time…"
    result = task_eta.countdown(db, "task", 50, "working", 1060)
    assert result == "About 1m 07s to review"
    # Each call reads persisted samples; the remaining time falls as work continues.
    assert task_eta.countdown(db, "task", 50, "working", 1070) == "About 57s to review"


def test_countdown_invalid_timestamps_leave_observations_unchanged(tmp_path):
    db = tmp_path / "projects.db"
    task_eta.countdown(db, "task", 10, "working", 1000)
    task_eta.countdown(db, "task", 30, "working", 1030)
    assert task_eta.countdown(db, "task", 50, "working", 1060) == (
        "About 1m 07s to review"
    )
    for invalid in (float("nan"), float("inf"), float("-inf"),
                    "invalid", "nan", "inf"):
        assert task_eta.countdown(db, "task", 70, "working", invalid) == (
            "Estimating time…"
        )
        assert task_eta.countdown(db, "task", 50, "paused", invalid) == "Paused"
    # Invalid observations must neither add progress nor replace the last
    # valid timestamp or active status used to accumulate elapsed time.
    assert task_eta.countdown(db, "task", 50, "working", 1070) == (
        "About 57s to review"
    )


def test_pause_preserves_work_before_pause_and_excludes_paused_time(tmp_path):
    db = tmp_path / "projects.db"
    task_eta.countdown(db, "task", 10, "working", 1000)
    task_eta.countdown(db, "task", 30, "working", 1030)
    assert task_eta.countdown(db, "task", 50, "working", 1060) == (
        "About 1m 07s to review"
    )
    # Ten seconds of work precede the pause; the following thirty are paused.
    assert task_eta.countdown(db, "task", 50, "paused", 1070) == "Paused"
    assert task_eta.countdown(db, "task", 50, "working", 1100) == (
        "About 57s to review"
    )
    assert task_eta.countdown(db, "task", 50, "working", 1110) == (
        "About 47s to review"
    )


def test_pause_does_not_advance_active_clock(tmp_path):
    db = tmp_path / "projects.db"
    task_eta.countdown(db, "task", 10, "working", 1000)
    task_eta.countdown(db, "task", 30, "working", 1030)
    task_eta.countdown(db, "task", 50, "working", 1060)
    assert task_eta.countdown(db, "task", 50, "paused", 1070) == "Paused"
    assert task_eta.countdown(db, "task", 50, "working", 2000) == "Estimate pending progress"
    assert task_eta.countdown(db, "task", 70, "working", 2030).startswith("About ")


def test_resuming_completed_tasks_rebuilds_progress_samples(tmp_path):
    db = tmp_path / "projects.db"
    for status in ("ready_for_review", "accepted"):
        task_eta.countdown(db, status, 10, "working", 1000)
        task_eta.countdown(db, status, 30, "working", 1030)
        assert task_eta.countdown(db, status, 50, "working", 1060) == (
            "About 1m 07s to review"
        )
        assert task_eta.countdown(db, status, 100, status, 1070) == ""
        # Resumption lowers progress from completion, retiring old samples.
        assert task_eta.countdown(db, status, 50, "working", 1100) == (
            "Estimating time…"
        )
        assert task_eta.countdown(db, status, 70, "working", 1130) == (
            "Estimating time…"
        )
        assert task_eta.countdown(db, status, 90, "working", 1160) == (
            "About 7s to review"
        )


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


def test_music_eta_requires_a_valid_historical_start_timestamp(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    for index, elapsed in enumerate((48, 60, 72)):
        (jobs / f"{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "duration_seconds": 180, "finished": elapsed,
        }), encoding="utf-8")
    assert task_eta.music_estimate_seconds(jobs, 180, False) is None

    # The same render durations are usable when measured from a valid start.
    for index, elapsed in enumerate((48, 60, 72)):
        (jobs / f"{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "duration_seconds": 180, "created": 1000,
            "finished": 1000 + elapsed,
        }), encoding="utf-8")
    assert task_eta.music_estimate_seconds(jobs, 180, False) == 60


def test_music_countdown_rejects_nonfinite_estimates_and_timestamps():
    for invalid in (float("nan"), float("inf"), float("-inf"),
                    "nan", "inf", "-inf"):
        for field in ("estimated_seconds", "requested_at", "created"):
            job = {"status": "working", "estimated_seconds": 60,
                   "created": 1000}
            job[field] = invalid
            assert task_eta.music_countdown(job, now=1030) == (
                "Estimating from recent songs…"
            ), (field, invalid)


def test_music_countdown_rejects_invalid_current_timestamps():
    job = {"status": "working", "estimated_seconds": 60,
           "created": 1000}
    for invalid in (float("nan"), float("inf"), float("-inf"),
                    "nan", "inf", "-inf", "invalid"):
        assert task_eta.music_countdown(job, now=invalid) == (
            "Estimating from recent songs…"
        ), invalid
    assert task_eta.music_countdown(job, now=1030) == (
        "About 30s left · estimate from recent songs"
    )


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


def test_audio_only_eta_excludes_jobs_that_rendered_a_video(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    for index, elapsed in enumerate((48, 60, 72)):
        (jobs / f"audio-{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "duration_seconds": 180, "created": 1000,
            "finished": 1000 + elapsed, "make_video": False,
            "result": {"path": "song.mp3"},
        }), encoding="utf-8")
    for index, elapsed in enumerate((180, 210, 240)):
        (jobs / f"video-{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "duration_seconds": 180, "created": 1000,
            "finished": 1000 + elapsed, "make_video": True,
            "result": {"path": "song.mp3", "video": {"path": "song.mp4"}},
        }), encoding="utf-8")
    assert task_eta.music_estimate_seconds(jobs, 180, False) == 60
    assert task_eta.music_estimate_seconds(
        jobs, 180, False, make_video=True,
    ) == 210


def test_style_reference_eta_excludes_quick_covers_and_legacy_jobs(tmp_path):
    jobs = tmp_path / "music-jobs"
    jobs.mkdir()
    for index, elapsed in enumerate((12, 20, 28)):
        (jobs / f"cover-{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "source_path": "clip.mp4", "reference_mode": "cover",
            "duration_seconds": 180, "created": 1000,
            "finished": 1000 + elapsed,
        }), encoding="utf-8")
    (jobs / "old.json").write_text(json.dumps({
        "status": "complete", "instrumental": False,
        "source_path": "clip.mp4", "duration_seconds": 180,
        "created": 1000, "finished": 1010,
    }), encoding="utf-8")
    assert task_eta.music_estimate_seconds(
        jobs, 180, False, "clip.mp4", "style",
    ) is None
    for index, elapsed in enumerate((90, 130)):
        (jobs / f"style-{index}.json").write_text(json.dumps({
            "status": "complete", "instrumental": False,
            "source_path": "clip.mp4", "reference_mode": "style",
            "duration_seconds": 180, "created": 1000,
            "finished": 1000 + elapsed,
        }), encoding="utf-8")
    assert task_eta.music_estimate_seconds(
        jobs, 180, False, "clip.mp4", "style",
    ) == 110
