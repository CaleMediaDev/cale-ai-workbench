"""Persist actual provider-reported token counts for the desktop graph."""

import math
import re
import sqlite3
import statistics
import time
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "LongTasks/usage.db"
CURRENT_TASK = ContextVar("cale_usage_task", default="")
OBSERVER = ContextVar("cale_usage_observer", default=None)


def _connection():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE, timeout=20)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, source TEXT NOT NULL,
            model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL
        )""")
        additions = (("tokens_per_second", "REAL"), ("task_id", "TEXT NOT NULL DEFAULT ''"),
                     ("speed_basis", "TEXT NOT NULL DEFAULT 'unknown'"),
                     ("speed_tokens", "INTEGER NOT NULL DEFAULT 0"),
                     ("speed_seconds", "REAL"), ("elapsed_seconds", "REAL"))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(usage_events)")}
        if (any(name not in columns for name, _ in additions)
                or not {"usage_time", "usage_task"}.issubset(indexes)):
            # Serialize migrations only. Established WAL readers must not
            # reserve a writer lock while the graph or router reads metrics.
            conn.execute("BEGIN IMMEDIATE")
            # Another worker may have migrated while this connection waited.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
            for name, definition in additions:
                if name not in columns:
                    conn.execute(f"ALTER TABLE usage_events ADD COLUMN {name} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS usage_time ON usage_events(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS usage_task ON usage_events(task_id)")
            conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


def _count(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), 1_000_000_000))
    except (TypeError, ValueError, OverflowError):
        return 0


def _usage_label(source: str, model: str) -> str:
    """Keep equal model names from different providers distinct in the graph."""
    names = {"ollama": "Local", "chatgpt": "ChatGPT",
             "claude_subscription": "Claude plan", "grok_subscription": "Grok plan"}
    provider = names.get(source, source.title())
    return f"{provider} · {model.strip() or 'unnamed model'}"


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
    observer = OBSERVER.get()
    if observer:
        try:
            observer(result, source)
        except Exception:
            pass  # Optional accounting must not corrupt an inference reply.
    model = str(result.get("model") or "")[:150]
    task_id = CURRENT_TASK.get()
    if not isinstance(task_id, str) or not re.fullmatch(r"(?:[a-f0-9]{12}|radio-[a-f0-9]{32})", task_id):
        task_id = ""
    # Ollama's own nanosecond duration is the most accurate local generation
    # speed. Other providers expose usage but usually no generation duration,
    # so use the measured end-to-end request time when available.
    eval_count = _count(result.get("eval_count"))
    try:
        eval_duration = max(0, int(result.get("eval_duration", 0)))
    except (TypeError, ValueError, OverflowError):
        eval_duration = 0
    try:
        elapsed = float(elapsed_seconds)
        if not math.isfinite(elapsed) or elapsed <= 0:
            elapsed = None
    except (TypeError, ValueError, OverflowError):
        elapsed = None
    basis, tokens, seconds = "unknown", 0, None
    if eval_count and eval_duration:
        basis, tokens, seconds = "decode", eval_count, eval_duration / 1_000_000_000
    elif elapsed and completion:
        basis, tokens, seconds = "end_to_end", completion, elapsed
    speed = tokens / seconds if seconds else None
    try:
        with closing(_connection()) as conn, conn:
            conn.execute(
                "INSERT INTO usage_events(timestamp,source,model,prompt_tokens,completion_tokens,total_tokens,tokens_per_second,task_id,speed_basis,speed_tokens,speed_seconds,elapsed_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), source[:40], model, prompt, completion, total, speed, task_id,
                 basis, tokens, seconds, elapsed),
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


def task_sources() -> dict[str, list[tuple[str, int]]]:
    """Show the real upstream providers used by each task's router calls."""
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT task_id, source, SUM(total_tokens) FROM usage_events "
                "WHERE task_id <> '' GROUP BY task_id, source "
                "ORDER BY task_id, SUM(total_tokens) DESC"
            ).fetchall()
        result = {}
        for task_id, source, tokens in rows:
            result.setdefault(task_id, []).append((
                "Local" if source == "ollama" else
                "ChatGPT" if source == "chatgpt" else source.title(),
                tokens,
            ))
        return result
    except (OSError, sqlite3.Error):
        return {}


def recent_local_speed(default: float = 35.0, models=None) -> float:
    """Measure recent local speed for comparable models only.

    A fast 9B answer must not make a 35B project appear fast. Missing samples
    use the caller's tier estimate rather than an unrelated model's history.
    """
    aliases = tuple(dict.fromkeys(models or ()))
    model_filter = f"AND model IN ({','.join('?' for _ in aliases)}) " if aliases else ""
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT tokens_per_second FROM usage_events WHERE source='ollama' "
                "AND tokens_per_second > 0 AND timestamp > ? "
                + model_filter + "ORDER BY id DESC LIMIT 12",
                (time.time() - 30 * 86400, *aliases),
            ).fetchall()
        return statistics.median(row[0] for row in rows) if rows else default
    except (OSError, sqlite3.Error):
        return default


def recent_model_speed(model: str) -> float | None:
    """Recent local throughput for ranking/display, including short replies."""
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT tokens_per_second FROM usage_events WHERE source='ollama' "
                "AND model IN (?, ?) AND tokens_per_second > 0 AND timestamp > ? "
                "ORDER BY id DESC LIMIT 12",
                (model, model + ":latest", time.time() - 30 * 86400),
            ).fetchall()
        return statistics.median(row[0] for row in rows) if len(rows) >= 3 else None
    except (OSError, sqlite3.Error):
        return None



def _response_speed(source, models=(), limit=12):
    """Median completed-output productivity on one comparable timing basis.

    This includes load, prefill, decode and transport latency. It is a routing
    hint, not decode throughput, quality evidence or a model retirement signal.
    Missing elapsed time never falls back to the stored mixed-basis TPS column.
    """
    aliases = tuple(dict.fromkeys(
        alias for model in models
        for alias in (model.removesuffix(":latest"), model.removesuffix(":latest") + ":latest")
    ))
    model_filter = f"AND model IN ({','.join('?' for _ in aliases)}) " if aliases else ""
    now = time.time()
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT 1.0 * completion_tokens / elapsed_seconds FROM usage_events "
                "WHERE source=? AND typeof(timestamp) IN ('integer','real') "
                "AND timestamp>? AND timestamp<=? "
                # SQLite's dynamic types otherwise admit arbitrary text as
                # numeric comparisons. Reject malformed persisted metrics.
                "AND typeof(completion_tokens) IN ('integer','real') "
                "AND completion_tokens>0 AND completion_tokens<=1000000000 "
                "AND completion_tokens=CAST(completion_tokens AS INTEGER) "
                "AND typeof(elapsed_seconds) IN ('integer','real') "
                "AND elapsed_seconds>0 AND elapsed_seconds<1e100 "
                "AND 1.0 * completion_tokens / elapsed_seconds > 0 "
                "AND 1.0 * completion_tokens / elapsed_seconds < 1e100 "
                + model_filter + "ORDER BY id DESC LIMIT ?",
                (source, now - 30 * 86400, now, *aliases, limit),
            ).fetchall()
        return statistics.median(row[0] for row in rows) if len(rows) >= 3 else None
    except (OSError, sqlite3.Error):
        return None


def recent_model_response_speed(model: str) -> float | None:
    """Recent local completion tokens per end-to-end second, or unknown."""
    return _response_speed("ollama", (model,))


def recent_local_response_speed(default: float = 35.0, models=None) -> float:
    """Compare only the requested local tier; estimates are explicit defaults."""
    speed = _response_speed("ollama", tuple(models or ()))
    return default if speed is None else speed


def recent_provider_response_speed(source: str, default: float) -> float:
    """Compare cloud and local responses using the same end-to-end basis."""
    speed = _response_speed(source, limit=8)
    return default if speed is None else speed

def recent_model_retirement_speed(model: str) -> float | None:
    """Use sustained local responses for exclusion/removal, not tiny replies.

    Native decode samples need 64 generated tokens; compatibility endpoints
    lacking decode timing need 128 output tokens. Both fast and slow qualifying
    calls count. End-to-end rates include prompt/load time, so establish sustained
    task slowness, not pure decode speed. Legacy unknown timing is display-only.
    """
    try:
        with closing(_connection()) as conn:
            rows = conn.execute(
                "SELECT tokens_per_second FROM usage_events WHERE source='ollama' "
                "AND model IN (?, ?) AND timestamp > ? "
                "AND tokens_per_second > 0 AND tokens_per_second < 1e100 "
                "AND speed_seconds > 0 AND speed_seconds < 1e100 "
                "AND ((speed_basis='decode' AND speed_tokens >= 64) "
                "OR (speed_basis='end_to_end' AND speed_tokens >= 128)) "
                "ORDER BY id DESC LIMIT 12",
                (model, model + ":latest", time.time() - 30 * 86400),
            ).fetchall()
        return statistics.median(row[0] for row in rows) if len(rows) >= 3 else None
    except (OSError, sqlite3.Error):
        return None


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
            f"SELECT {expression}, source, model, SUM(total_tokens) "
            "FROM usage_events GROUP BY 1, 2, 3"
        ).fetchall()
        models = conn.execute(
            "SELECT source, model, SUM(total_tokens) FROM usage_events "
            "GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2"
        ).fetchall()
        sources = dict(conn.execute(
            "SELECT source, SUM(total_tokens) FROM usage_events GROUP BY source"
        ).fetchall())
    counts = dict(rows)
    model_counts = {}
    for date, source, model, tokens in model_rows:
        model_counts.setdefault(date, {})[_usage_label(source, model)] = tokens
    return {"period": period, "total": total,
            "current_speed": latest[0] if latest else None,
            "top_speed": top_speed,
            "speed_source": latest[1] if latest else None,
            "speed_model": latest[2] if latest else None,
            "sources": sources,
            "models": [{"model": _usage_label(source, model), "tokens": tokens}
                       for source, model, tokens in models],
            "points": [{"date": key, "tokens": counts.get(key, 0),
                        "models": model_counts.get(key, {})} for key in keys]}
