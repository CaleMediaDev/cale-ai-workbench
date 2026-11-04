"""Token graph counts only real provider-reported usage."""

import sqlite3

import usage_history


def test_radio_usage_keeps_exact_station_attribution(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    station = "radio-" + "a" * 32
    marker = usage_history.CURRENT_TASK.set(station)
    try:
        assert usage_history.record_completion({"usage": {"total_tokens": 775}}, "ollama")
    finally:
        usage_history.CURRENT_TASK.reset(marker)
    assert usage_history.task_totals()[station] == 775
    assert usage_history.history()["total"] == 775


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
    assert daily["points"][-1]["models"] == {"Local · m": 150, "ChatGPT · cloud": 50}
    assert monthly["points"][-1]["tokens"] == 200
    assert monthly["points"][-1]["models"] == {"Local · m": 150, "ChatGPT · cloud": 50}
    assert daily["current_speed"] == 5
    assert daily["top_speed"] == 5
    assert daily["models"] == [
        {"model": "Local · m", "tokens": 150},
        {"model": "ChatGPT · cloud", "tokens": 50},
    ]
    assert daily["sources"] == {"ollama": 150, "chatgpt": 50}
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


def test_provider_speed_uses_recent_median_after_three_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for speed in (20, 40):
        assert usage_history.record_completion(
            {"model": "cloud", "usage": {"completion_tokens": speed}},
            "bigmodel", elapsed_seconds=1,
        )
    assert usage_history.recent_provider_speed("bigmodel", 150) == 150
    assert usage_history.record_completion(
        {"model": "cloud", "usage": {"completion_tokens": 300}},
        "bigmodel", elapsed_seconds=1,
    )
    assert usage_history.recent_provider_speed("bigmodel", 150) == 40
    assert usage_history.recent_provider_speed("groq", 900) == 900


def test_local_speed_does_not_mix_fast_and_large_models(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for model, speed in (("cale-fast", 80), ("cale-fast", 90),
                         ("cale-qwen36", 7), ("cale-qwen36", 9)):
        assert usage_history.record_completion(
            {"model": model, "usage": {"completion_tokens": speed}},
            "ollama", elapsed_seconds=1,
        )
    assert usage_history.recent_local_speed(35, models=("cale-qwen36",)) == 8
    assert usage_history.recent_local_speed(70, models=("cale-fast",)) == 85
    assert usage_history.recent_local_speed(15, models=("missing",)) == 15


def test_model_totals_include_unreported_model_names(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for model, tokens in (("alpha", 10), ("beta", 30), ("alpha", 15), ("", 3)):
        assert usage_history.record_completion(
            {"model": model, "usage": {"total_tokens": tokens}}, "ollama"
        )
    assert usage_history.history()["models"] == [
        {"model": "Local · beta", "tokens": 30},
        {"model": "Local · alpha", "tokens": 25},
        {"model": "Local · unnamed model", "tokens": 3},
    ]


def test_same_model_name_from_two_providers_stays_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for provider, tokens in (("ollama", 12), ("chatgpt", 25)):
        assert usage_history.record_completion(
            {"model": "shared", "usage": {"total_tokens": tokens}}, provider
        )
    summary = usage_history.history()
    assert summary["models"] == [
        {"model": "ChatGPT · shared", "tokens": 25},
        {"model": "Local · shared", "tokens": 12},
    ]
    assert summary["points"][-1]["models"] == {
        "Local · shared": 12, "ChatGPT · shared": 25,
    }


def test_task_totals_attribute_only_valid_worker_context(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    one = "a" * 12
    two = "b" * 12
    for task, tokens in ((one, 25), (two, 15), (one, 5), ("spoofed", 100)):
        marker = usage_history.CURRENT_TASK.set(task)
        try:
            assert usage_history.record_completion(
                {"model": "m", "usage": {"total_tokens": tokens}}, "ollama")
        finally:
            usage_history.CURRENT_TASK.reset(marker)
    assert usage_history.task_totals() == {one: 30, two: 15}
    assert usage_history.task_sources() == {
        one: [("Local", 30)], two: [("Local", 15)],
    }
    assert usage_history.history()["total"] == 145
