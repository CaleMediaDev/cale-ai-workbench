"""Persist real desktop failures that the regression suite cannot observe.

Only error categories and short messages are stored. Audio and camera frames
never enter this database. An episode is reported after its second failure;
successful operation clears the episode so a later regression is visible.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from math import isfinite
from pathlib import Path

EPISODE_SECONDS = 60 * 60
MAX_DETAIL = 300


@contextmanager
def _open(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / "runtime-issues.sqlite3", timeout=5)
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS issues (
            kind TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            count INTEGER NOT NULL, detail TEXT NOT NULL, reported INTEGER NOT NULL
        )""")
        with db:
            yield db
    finally:
        db.close()


def record(state: Path, kind: str, detail: str, now: float | None = None) -> bool:
    """Record a failure; return True once when an episode repeats."""
    if not kind or not kind.replace("_", "").isalnum():
        raise ValueError("A stable error category is required")
    detail = " ".join(str(detail).split())[:MAX_DETAIL]
    with _open(state) as db:
        # Lock before reading so concurrent recorders cannot lose increments
        # or both report the same episode.
        db.execute("BEGIN IMMEDIATE")
        # Sample the clock in transaction order so a delayed recorder cannot
        # mistake another recorder's newer timestamp for a clock correction.
        now = time.time() if now is None else now
        if not isfinite(now):
            raise ValueError("A finite failure timestamp is required")
        row = db.execute(
            "SELECT first_seen,last_seen,count,reported FROM issues WHERE kind=?", (kind,)
        ).fetchone()
        if row and 0 <= now - row[1] <= EPISODE_SECONDS:
            first_seen, count, reported = row[0], row[2] + 1, row[3]
        else:
            first_seen, count, reported = now, 1, 0
        notify = count >= 2 and not reported
        db.execute("""INSERT INTO issues(kind,first_seen,last_seen,count,detail,reported)
            VALUES(?,?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET
            first_seen=excluded.first_seen,last_seen=excluded.last_seen,
            count=excluded.count,detail=excluded.detail,reported=excluded.reported""",
            (kind, first_seen, now, count, detail, int(reported or notify)),
        )
    return bool(notify)


def resolve(state: Path, kind: str) -> None:
    """A real successful operation ends the current failure episode."""
    with _open(state) as db:
        db.execute("DELETE FROM issues WHERE kind=?", (kind,))


def issues(state: Path) -> list[dict]:
    """Return current failures for support and future automated checks."""
    if not (state / "runtime-issues.sqlite3").is_file():
        return []
    with _open(state) as db:
        rows = db.execute(
            "SELECT kind,first_seen,last_seen,count,detail,reported "
            "FROM issues ORDER BY last_seen DESC"
        ).fetchall()
    return [dict(zip(("kind", "first_seen", "last_seen", "count", "detail", "reported"), row))
            for row in rows]
