"""Token graph counts only real provider-reported usage."""

import sqlite3

import usage_history


def test_daily_monthly_and_lifetime_totals(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    assert not usage_history.record_completion({"model": "m"}, "ollama")
    assert usage_history.record_completion(
        {"model": "m", "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}},
        "ollama",
    )
    assert usage_history.record_completion(
        {"model": "cloud", "usage": {"input_tokens": 40, "output_tokens": 10}},
        "chatgpt", elapsed_seconds=2,
    )
    daily = usage_history.history("day")
    monthly = usage_history.history("month")
    assert daily["total"] == monthly["total"] == 200
    assert daily["points"][-1]["tokens"] == 200
    assert monthly["points"][-1]["tokens"] == 200
    assert daily["current_speed"] == 5
    with sqlite3.connect(usage_history.DATABASE) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 2


def test_bad_usage_is_not_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    assert not usage_history.record_completion(
        {"usage": {"prompt_tokens": "invalid", "completion_tokens": -20}}, "ollama"
    )
    assert usage_history.history("day")["total"] == 0


def test_ollama_generation_duration_takes_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    assert usage_history.record_completion(
        {"model": "local", "usage": {"prompt_tokens": 4, "completion_tokens": 20},
         "eval_count": 20, "eval_duration": 2_000_000_000},
        "ollama", elapsed_seconds=5,
    )
    assert usage_history.history()["current_speed"] == 10
