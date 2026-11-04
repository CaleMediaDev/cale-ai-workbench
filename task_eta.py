"""Persist honest, best-effort countdowns for long-running task cards.

The task board reports milestone percentages, not a fixed amount of work. An
estimate therefore appears only after two measured advances and is labelled
as an estimate to review. Pauses and time while the supervisor is stopped are excluded.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from contextlib import closing
from pathlib import Path


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def total_time(created: float, updated: float, status: str,
               now: float | None = None, finished: float | None = None) -> str:
    """Show a task's wall-clock lifetime, frozen when finished or stopped.

    This is elapsed time since the request was created, including setup and
    pauses. It is deliberately separate from the active-time ETA estimator.
    """
    now = time.time() if now is None else now
    try:
        start = float(created)
    except (TypeError, ValueError, OverflowError):
        return "—"
    if not math.isfinite(start) or start <= 0:
        return "—"
    if status in ("ready_for_review", "accepted", "stopped"):
        try:
            end = float(finished or 0)
        except (TypeError, ValueError, OverflowError):
            end = 0
        if not math.isfinite(end) or end < start:
            try:
                end = float(updated)
            except (TypeError, ValueError, OverflowError):
                return "—"
    else:
        end = now
    if not math.isfinite(end):
        return "—"
    return _duration(max(0, end - start))


def music_estimate_seconds(jobs_dir: Path, duration_seconds: int,
                           instrumental: bool, source_path: str = "",
                           reference_mode: str = "cover",
                           make_video: bool = False) -> int | None:
    """Estimate a local song from recent completed renders on this PC.

    The median resists first-use downloads and other unusually slow renders.
    Prefer comparable song lengths. Scaling many long renders down to a short
    request can make its countdown implausibly optimistic, especially when
    transcription has a nearly fixed setup cost. Audio-only jobs and jobs that
    explicitly include a video are kept separate because their finish times
    are not comparable.
    """
    samples = []
    comparable = []
    try:
        paths = sorted(jobs_dir.glob("*.json"), key=lambda path: path.stat().st_mtime,
                       reverse=True)[:80]
    except OSError:
        return None
    for path in paths:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            length = float(job.get("duration_seconds") or 0)
            elapsed = float(job.get("finished") or 0) - float(job.get("requested_at") or job.get("created") or 0)
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            job_made_video = bool(job.get("make_video") or result.get("video"))
            if (job.get("status") != "complete" or not length or
                    bool(job.get("instrumental")) != instrumental or
                    bool(job.get("source_path")) != bool(source_path) or
                    (bool(source_path) and job.get("reference_mode") != reference_mode) or
                    job_made_video != bool(make_video) or
                    int(job.get("vocal_retry") or 0) > 0 or
                    not 2 <= elapsed <= 3600):
                continue
            ratio = max(0.25, min(4.0, duration_seconds / length))
            samples.append(15 + max(0, elapsed - 15) * ratio)
            if abs(length - duration_seconds) <= max(15, duration_seconds * 0.25):
                comparable.append(elapsed)
            if len(samples) == 20:
                break
        except (OSError, ValueError, TypeError, OverflowError):
            continue
    if len(comparable) >= 2:
        return round(statistics.median(comparable))
    return round(statistics.median(samples)) if len(samples) >= 3 else None


def music_countdown(job: dict, now: float | None = None) -> str:
    """Show a live, honest countdown or explain when a render exceeds history."""
    if job.get("status") == "paused":
        return "Paused"
    if job.get("status") not in ("queued", "working"):
        return ""
    try:
        expected = float(job.get("estimated_seconds") or 0)
        started = float(job.get("requested_at") or job.get("created") or 0)
    except (TypeError, ValueError, OverflowError):
        return "Estimating from recent songs…"
    if (not math.isfinite(expected) or not math.isfinite(started) or
            expected <= 0 or started <= 0):
        return "Estimating from recent songs…"
    remaining = expected - max(0, (time.time() if now is None else now) - started)
    if remaining <= 0:
        return "Taking longer than recent songs · still working"
    return f"About {_duration(remaining)} left · estimate from recent songs"


def countdown(db_path: Path, task_id: str, percent: int, status: str,
              now: float | None = None) -> str:
    """Record one observation and return the card's estimated time to review.

    The estimate is based on active elapsed time per completed milestone point.
    A gap over a minute is treated as an app restart, not working time.
    """
    now = time.time() if now is None else now
    percent = max(0, min(100, int(percent)))
    active = status in ("working", "checking")
    if status in ("ready_for_review", "accepted"):
        return ""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # sqlite3's context manager commits but does not close the connection.
    # On Windows, an open handle can prevent task-folder cleanup or updates.
    with closing(sqlite3.connect(db_path, timeout=30)) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS task_eta_state (
            task_id TEXT PRIMARY KEY, active_seconds REAL NOT NULL DEFAULT 0,
            last_seen REAL NOT NULL, was_active INTEGER NOT NULL,
            last_percent INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS task_eta_samples (
            task_id TEXT NOT NULL, percent INTEGER NOT NULL,
            active_seconds REAL NOT NULL, observed REAL NOT NULL,
            PRIMARY KEY(task_id, percent))""")
        row = conn.execute(
            "SELECT active_seconds,last_seen,was_active,last_percent FROM task_eta_state WHERE task_id=?",
            (task_id,),
        ).fetchone()
        elapsed = float(row[0]) if row else 0.0
        if row and row[2] and active and 0 <= now - row[1] <= 60:
            elapsed += now - row[1]
        previous = int(row[3]) if row else 0
        conn.execute("""INSERT INTO task_eta_state(task_id,active_seconds,last_seen,was_active,last_percent)
            VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            active_seconds=excluded.active_seconds,last_seen=excluded.last_seen,
            was_active=excluded.was_active,last_percent=excluded.last_percent""",
            (task_id, elapsed, now, int(active), percent),
        )
        # A restarted or revised plan may lower progress. Retire old samples so
        # an unrelated previous estimate cannot contaminate the new one.
        if row and percent < previous:
            conn.execute("DELETE FROM task_eta_samples WHERE task_id=?", (task_id,))
        if active and percent > previous and percent < 95:
            conn.execute("""INSERT OR REPLACE INTO task_eta_samples
                (task_id,percent,active_seconds,observed) VALUES(?,?,?,?)""",
                (task_id, percent, elapsed, now),
            )
        samples = conn.execute("""SELECT percent,active_seconds,observed FROM task_eta_samples
            WHERE task_id=? ORDER BY active_seconds DESC LIMIT 4""", (task_id,)).fetchall()
    if not active:
        return "Paused" if status == "paused" else ""
    if percent >= 95:
        return "Ready for review"
    if len(samples) < 3:
        return "Estimating time…"
    newest, oldest = samples[0], samples[-1]
    points = newest[0] - oldest[0]
    spent = newest[1] - oldest[1]
    if points < 5 or spent < 20:
        return "Estimating time…"
    # Repeated checklist edits can cause jumps. Limit the displayed horizon and
    # stop counting down when the task has made no recent observable progress.
    if now - newest[2] > max(600, 3 * spent):
        return "Estimate pending progress"
    remaining = min(30 * 86400, (95 - percent) * spent / points)
    remaining -= max(0, elapsed - newest[1])
    if remaining <= 0:
        return "Estimate pending progress"
    return f"About {_duration(remaining)} to review"
