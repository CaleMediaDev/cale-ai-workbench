"""Persist actual provider-reported token counts for the desktop graph."""

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "LongTasks/usage.db"


def _connection():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, source TEXT NOT NULL,
        model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS usage_time ON usage_events(timestamp)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
    if "tokens_per_second" not in columns:
        conn.execute("ALTER TABLE usage_events ADD COLUMN tokens_per_second REAL")
    return conn


def _count(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), 1_000_000_000))
    except (TypeError, ValueError, OverflowError):
        return 0


def record_completion(result: dict, source: str, elapsed_seconds: float | None = None) -> bool:
    """Record one completed call only when the provider reports usage."""
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return False
    prompt = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _count(usage.get("completion_tokens", usage.get("output_tokens")))
    total = _count(usage.get("total_tokens")) or prompt + completion
    if total <= 0:
        return False
    model = str(result.get("model") or "")[:150]
    # Ollama's own nanosecond duration is the most accurate local generation
    # speed. Other providers expose usage but usually no generation duration,
    # so use the measured end-to-end request time when available.
    eval_count = _count(result.get("eval_count"))
    try:
        eval_duration = max(0, int(result.get("eval_duration", 0)))
    except (TypeError, ValueError, OverflowError):
        eval_duration = 0
    if eval_count and eval_duration:
        speed = eval_count / (eval_duration / 1_000_000_000)
    elif elapsed_seconds and elapsed_seconds > 0 and completion:
        speed = completion / elapsed_seconds
    else:
        speed = None
    try:
        with closing(_connection()) as conn, conn:
            conn.execute(
                "INSERT INTO usage_events(timestamp,source,model,prompt_tokens,completion_tokens,total_tokens,tokens_per_second) VALUES (?,?,?,?,?,?,?)",
                (time.time(), source[:40], model, prompt, completion, total, speed),
            )
        return True
    except (OSError, sqlite3.Error):
        return False  # A usage display failure must not discard a model answer.


def history(period: str = "day") -> dict:
    """Return lifetime total and zero-filled local-day or local-month history."""
    if period not in ("day", "month"):
        raise ValueError("Choose day or month.")
    today = datetime.now().date()
    if period == "day":
        keys = [(today - timedelta(days=offset)).isoformat() for offset in range(13, -1, -1)]
        expression = "date(timestamp, 'unixepoch', 'localtime')"
    else:
        keys = []
        for offset in range(11, -1, -1):
            month = today.month - offset
            year = today.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            keys.append(f"{year:04d}-{month:02d}")
        expression = "strftime('%Y-%m',timestamp,'unixepoch','localtime')"
    with closing(_connection()) as conn:
        total = conn.execute("SELECT COALESCE(SUM(total_tokens),0) FROM usage_events").fetchone()[0]
        latest = conn.execute(
            "SELECT tokens_per_second,source,model FROM usage_events "
            "WHERE tokens_per_second IS NOT NULL ORDER BY timestamp DESC,id DESC LIMIT 1"
        ).fetchone()
        rows = conn.execute(
            f"SELECT {expression}, SUM(total_tokens) FROM usage_events GROUP BY 1"
        ).fetchall()
    counts = dict(rows)
    return {"period": period, "total": total,
            "current_speed": latest[0] if latest else None,
            "speed_source": latest[1] if latest else None,
            "speed_model": latest[2] if latest else None,
            "points": [{"date": key, "tokens": counts.get(key, 0)} for key in keys]}
