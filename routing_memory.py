"""Learn provider availability from local outcomes, without saving prompts or keys.

This is a routing signal, not an account quota meter. Provider dashboards and
the ChatGPT usage page remain authoritative for actual remaining allowance.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "LongTasks/routing.db"
WINDOW = 7 * 86400


def _connection():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, source TEXT NOT NULL,
        size TEXT NOT NULL, outcome TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS attempt_source_time ON attempts(source,timestamp)")
    return conn


def failure_kind(error: Exception) -> str:
    """Distinguish exhausted allowance from outages and credential errors."""
    if isinstance(error, HTTPError):
        if error.code in (402, 429):
            return "limit"
        return "error"
    text = str(error).casefold()
    return "limit" if any(word in text for word in (
        "quota", "rate_limit", "rate limit", "usage_limit", "usage limit",
        "credit", "billing_limit", "capacity_limit",
    )) else "error"


def record(source: str, size: str, outcome: str) -> None:
    """Keep only bounded metadata; routing must survive a database failure."""
    if (not source or len(source) > 40 or size not in ("quick", "balanced", "deep")
            or outcome not in ("success", "limit", "error")):
        return
    try:
        with closing(_connection()) as conn, conn:
            conn.execute("INSERT INTO attempts(timestamp,source,size,outcome) VALUES(?,?,?,?)",
                         (time.time(), source, size, outcome))
            conn.execute("DELETE FROM attempts WHERE timestamp < ?", (time.time() - 30 * 86400,))
    except (OSError, sqlite3.Error):
        pass


def record_budget_limit(source: str) -> None:
    """Count a client-side daily cap once, even if later tasks keep trying."""
    now = time.time()
    if now - pressure(source, now)["last_limit"] >= 86400:
        record(source, "balanced", "limit")


def pressure(source: str, now: float | None = None) -> dict:
    """Summarize recent success and limit events for one provider or tier."""
    current = time.time() if now is None else now
    try:
        with closing(_connection()) as conn:
            if source == "free":
                rows = conn.execute(
                    "SELECT timestamp,outcome FROM attempts WHERE source <> 'chatgpt' "
                    "AND timestamp >= ?", (current - WINDOW,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT timestamp,outcome FROM attempts WHERE source=? "
                    "AND timestamp >= ?", (source, current - WINDOW)
                ).fetchall()
    except (OSError, sqlite3.Error):
        rows = []
    return {
        "successes": sum(outcome == "success" for _, outcome in rows),
        "limits": sum(outcome == "limit" for _, outcome in rows),
        "last_limit": max((when for when, outcome in rows if outcome == "limit"), default=0),
    }


def use_paid(size: str, local_speed: float, paid: dict, now: float | None = None) -> bool:
    """Spend abundant plan allowance more freely; conserve it after limits."""
    current = time.time() if now is None else now
    if size == "quick":
        return False
    if current - paid["last_limit"] < 3600:
        return False
    if size == "deep":
        return True
    if paid["limits"]:
        return False
    # Fifteen successful calls without a limit is evidence that the plan is
    # not scarce for this person's current pattern. Still keep fast local
    # responses for routine work.
    return local_speed < (60 if paid["successes"] >= 15 else 25)


def use_free(size: str, local_speed: float, free: dict) -> bool:
    """Save repeatedly exhausted free quotas for deep or painfully slow work."""
    if size == "deep" or free["limits"] < 2:
        return True
    return size == "balanced" and local_speed < 18
