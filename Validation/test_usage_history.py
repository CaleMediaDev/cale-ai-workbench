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
    assert daily["top_speed"] == 5
    assert daily["models"] == [
        {"model": "m", "tokens": 150},
        {"model": "cloud", "tokens": 50},
    ]
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


def test_top_speed_survives_slower_later_response(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for tokens, seconds in ((50, 1), (20, 2)):
        assert usage_history.record_completion(
            {"model": "local", "usage": {"completion_tokens": tokens}},
            "ollama", elapsed_seconds=seconds,
        )
    summary = usage_history.history()
    assert summary["current_speed"] == 10
    assert summary["top_speed"] == 50


def test_model_totals_include_unreported_model_names(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for model, tokens in (("alpha", 10), ("beta", 30), ("alpha", 15), ("", 3)):
        assert usage_history.record_completion(
            {"model": model, "usage": {"total_tokens": tokens}}, "ollama"
        )
    assert usage_history.history()["models"] == [
        {"model": "beta", "tokens": 30},
        {"model": "alpha", "tokens": 25},
        {"model": "[ollama] unnamed model", "tokens": 3},
    ]
