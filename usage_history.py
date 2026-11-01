"""Persist actual provider-reported token counts for the desktop graph."""

import sqlite3
import statistics
import time
import re
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "LongTasks/usage.db"
CURRENT_TASK = ContextVar("cale_usage_task", default="")


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
    if "task_id" not in columns:
        conn.execute("ALTER TABLE usage_events ADD COLUMN task_id TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS usage_task ON usage_events(task_id)")
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
    task_id = CURRENT_TASK.get()
    if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{12}", task_id):
        task_id = ""
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
                "INSERT INTO usage_events(timestamp,source,model,prompt_tokens,completion_tokens,total_tokens,tokens_per_second,task_id) VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), source[:40], model, prompt, completion, total, speed, task_id),
            )
        return True
    except (OSError, sqlite3.Error):
        return False  # A usage display failure must not discard a model answer.


def task_totals() -> dict[str, int]:
    """Return reported token totals for each task since attribution began."""
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT task_id, SUM(total_tokens) FROM usage_events "
                "WHERE task_id <> '' GROUP BY task_id"
            ).fetchall()
        return {task_id: total for task_id, total in rows}
    except (OSError, sqlite3.Error):
        return {}


def recent_local_speed(default: float = 35.0) -> float:
    """Use actual recent Ollama output speed to decide if cloud is worthwhile."""
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT tokens_per_second FROM usage_events WHERE source='ollama' "
                "AND tokens_per_second > 0 ORDER BY id DESC LIMIT 12"
            ).fetchall()
        return sum(row[0] for row in rows) / len(rows) if rows else default
    except (OSError, sqlite3.Error):
        return default


def recent_provider_speed(source: str, default: float) -> float:
    """Use a provider's measured throughput once three calls establish a sample.

    Cloud speeds include network latency, so the median of recent calls is
    more useful than a single peak or a model-card tokens-per-second claim.
    """
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT tokens_per_second FROM usage_events WHERE source=? "
                "AND tokens_per_second > 0 AND timestamp > ? "
                "ORDER BY id DESC LIMIT 8",
                (source, time.time() - 30 * 86400),
            ).fetchall()
        return statistics.median(row[0] for row in rows) if len(rows) >= 3 else default
    except (OSError, sqlite3.Error):
        return default


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
        top_speed = conn.execute(
            "SELECT MAX(tokens_per_second) FROM usage_events"
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT {expression}, SUM(total_tokens) FROM usage_events GROUP BY 1"
        ).fetchall()
        model_rows = conn.execute(
            f"SELECT {expression}, "
            "COALESCE(NULLIF(TRIM(model),''), '[' || source || '] unnamed model'), "
            "SUM(total_tokens) FROM usage_events GROUP BY 1, 2"
        ).fetchall()
        models = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(model),''), '[' || source || '] unnamed model'), "
            "SUM(total_tokens) FROM usage_events GROUP BY 1 ORDER BY 2 DESC, 1"
        ).fetchall()
    counts = dict(rows)
    model_counts = {}
    for date, model, tokens in model_rows:
        model_counts.setdefault(date, {})[model] = tokens
    return {"period": period, "total": total,
            "current_speed": latest[0] if latest else None,
            "top_speed": top_speed,
            "speed_source": latest[1] if latest else None,
            "speed_model": latest[2] if latest else None,
            "models": [{"model": name, "tokens": tokens} for name, tokens in models],
            "points": [{"date": key, "tokens": counts.get(key, 0),
                        "models": model_counts.get(key, {})} for key in keys]}
