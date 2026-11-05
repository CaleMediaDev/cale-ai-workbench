"""Token graph counts only real provider-reported usage."""

import sqlite3
import pytest

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


def test_tiny_fast_local_answers_do_not_disqualify_hardware_fit_model(tmp_path, monkeypatch):
    import model_fitness
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    monkeypatch.setattr(model_fitness, "hardware_allows", lambda *args: True)
    for _ in range(3):
        assert usage_history.record_completion(
            {"model": "tiny", "usage": {"completion_tokens": 1}},
            "ollama", elapsed_seconds=0.5,
        )
    assert usage_history.recent_model_speed("tiny") == 2
    assert model_fitness.suitable("tiny", 1, 128, 12)


def _local_timed(model, tokens, seconds, native=False):
    result = {"model": model, "usage": {"completion_tokens": tokens}}
    if native:
        result.update(eval_count=tokens, eval_duration=int(seconds * 1_000_000_000))
    assert usage_history.record_completion(result, "ollama", elapsed_seconds=seconds)


def test_sustained_compatible_slow_calls_still_disqualify(tmp_path, monkeypatch):
    import model_fitness
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    monkeypatch.setattr(model_fitness, "hardware_allows", lambda *args: True)
    for _ in range(3):
        _local_timed("slow", 128, 64)
    assert usage_history.recent_model_retirement_speed("slow") == 2
    assert not model_fitness.suitable("slow", 1, 128, 12)


def test_qualified_fast_samples_count_even_under_ten_seconds(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for seconds in (64, 64, 2, 2, 2, 2, 2):
        _local_timed("mixed", 128, seconds)
    assert usage_history.recent_model_retirement_speed("mixed") == 64


def test_native_decode_basis_and_short_sample_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for _ in range(3):
        _local_timed("native", 64, 32, native=True)
    for _ in range(20):
        _local_timed("native", 1, .01)
    assert usage_history.recent_model_retirement_speed("native") == 2
    with sqlite3.connect(usage_history.DATABASE) as conn:
        assert conn.execute("SELECT speed_basis,speed_tokens,speed_seconds,elapsed_seconds "
                            "FROM usage_events ORDER BY id LIMIT 1").fetchone() == ("decode",64,32,32)


def test_hardware_gate_still_overrides_fast_or_unknown_timing(tmp_path, monkeypatch):
    import model_fitness
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    monkeypatch.setattr(model_fitness, "hardware_allows", lambda *args: False)
    for _ in range(3):
        _local_timed("fast", 128, 1)
    assert not model_fitness.suitable("fast", 20, 8, 0)
    assert not model_fitness.suitable("unknown", 20, 8, 0)


def test_legacy_low_speed_is_preserved_for_display_not_retirement(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    with sqlite3.connect(usage_history.DATABASE) as conn:
        conn.execute("CREATE TABLE usage_events(id INTEGER PRIMARY KEY,timestamp REAL,source TEXT,"
                     "model TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER,"
                     "tokens_per_second REAL,task_id TEXT)")
        conn.executemany("INSERT INTO usage_events VALUES(?,?,?,?,?,?,?,?,?)",
                         [(i,usage_history.time.time(),"ollama","old",0,128,128,2,"") for i in range(1,4)])
    assert usage_history.recent_model_speed("old") == 2
    assert usage_history.recent_model_retirement_speed("old") is None
    assert usage_history.history()["total"] == 384


def test_retirement_window_alias_provider_and_last_twelve(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for _ in range(12):
        _local_timed("window:latest", 128, 64)
    for _ in range(12):
        _local_timed("window:latest", 128, 1)
    assert usage_history.recent_model_retirement_speed("window") == 128
    with sqlite3.connect(usage_history.DATABASE) as conn:
        conn.execute("UPDATE usage_events SET timestamp=?", (usage_history.time.time()-31*86400,))
    assert usage_history.recent_model_retirement_speed("window") is None
    for _ in range(3):
        assert usage_history.record_completion({"model":"window","usage":{"completion_tokens":128}},
                                              "chatgpt", elapsed_seconds=64)
    assert usage_history.recent_model_retirement_speed("window") is None


def test_invalid_elapsed_cannot_create_retirement_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for seconds in (float("inf"),float("nan"),-1,0,None):
        _local_timed("invalid",128,seconds)
    assert usage_history.recent_model_retirement_speed("invalid") is None
    with sqlite3.connect(usage_history.DATABASE) as conn:
        assert conn.execute("SELECT DISTINCT speed_basis,speed_tokens,speed_seconds,elapsed_seconds "
                            "FROM usage_events").fetchall() == [("unknown",0,None,None)]


def test_native_provenance_uses_decode_time_not_wall_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for _ in range(3):
        assert usage_history.record_completion({"model":"decode", "usage":{"completion_tokens":64},
               "eval_count":64,"eval_duration":1_000_000_000}, "ollama", elapsed_seconds=100)
    assert usage_history.recent_model_retirement_speed("decode") == 64
    with sqlite3.connect(usage_history.DATABASE) as conn:
        assert conn.execute("SELECT speed_seconds,elapsed_seconds FROM usage_events LIMIT 1").fetchone() == (1,100)


def test_retirement_requires_three_length_qualified_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for _ in range(3):
        _local_timed("short",63,63,native=True)
        _local_timed("short",127,127)
    assert usage_history.recent_model_retirement_speed("short") is None
    for _ in range(2):
        _local_timed("qualified",128,128)
    assert usage_history.recent_model_retirement_speed("qualified") is None
    _local_timed("qualified",128,128)
    assert usage_history.recent_model_retirement_speed("qualified") == 1


def test_concurrent_legacy_usage_migration_preserves_rows(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    with sqlite3.connect(usage_history.DATABASE) as conn:
        # Existing app-created ledgers already use WAL before this migration.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE usage_events(id INTEGER PRIMARY KEY,timestamp REAL,source TEXT,"
                     "model TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER)")
        conn.execute("INSERT INTO usage_events VALUES(1,0,'ollama','legacy',1,1,2)")
    def record(_):
        return usage_history.record_completion({"model":"concurrent","usage":{"completion_tokens":128}},
                                              "ollama",elapsed_seconds=1)
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(pool.map(record,range(6)))
    with sqlite3.connect(usage_history.DATABASE) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 7
        assert conn.execute("SELECT speed_basis FROM usage_events WHERE id=1").fetchone()[0] == "unknown"
    assert usage_history.recent_model_retirement_speed("concurrent") == 128


def test_migrated_usage_reader_does_not_reserve_writer_lock(tmp_path, monkeypatch):
    """Reading established WAL metrics must coexist with another worker's write."""
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for _ in range(3):
        _local_timed("fast", 128, 1)
    original_connect = sqlite3.connect
    writer = original_connect(usage_history.DATABASE)
    writer.execute("BEGIN IMMEDIATE")
    def short_connect(*args, **kwargs):
        kwargs["timeout"] = .05
        return original_connect(*args, **kwargs)
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(sqlite3, "connect", short_connect)
            # A direct open exposes lock failures rather than a display's
            # legitimate fallback hiding the failed database read.
            reader = usage_history._connection()
            try:
                assert reader.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 3
                assert not reader.in_transaction
            finally:
                reader.close()
            assert usage_history.recent_model_retirement_speed("fast") == 128
    finally:
        writer.rollback()
        writer.close()


def test_routing_response_speed_compares_native_and_compat_end_to_end(tmp_path, monkeypatch):
    """A fast decoder with slow prefill is not a fast complete response."""
    import model_fitness
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for _ in range(3):
        assert usage_history.record_completion(
            {"model": "slow:latest", "usage": {"completion_tokens": 200},
             "eval_count": 200, "eval_duration": 1_000_000_000}, "ollama", 100)
        assert usage_history.record_completion(
            {"model": "fast", "usage": {"completion_tokens": 100}}, "ollama", 2)
    assert model_fitness.measured_speed("slow") == 2
    assert model_fitness.measured_speed("fast:latest") == 50
    from auto_route import model_order
    assert model_order("deep", {"slow": 35, "fast": 21}, "slow", "fast", "slow",
                       speeds={m: model_fitness.measured_speed(m) for m in ("slow", "fast")})[0] == "fast"
    assert usage_history.recent_local_response_speed(99, models=("slow:latest",)) == 2
    # Decode/display and destructive retirement retain their original contract.
    assert usage_history.recent_model_speed("slow") == 200
    assert usage_history.recent_model_retirement_speed("slow") == 200


def test_response_speed_mixed_transports_short_calls_and_no_decode_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for source in ("ollama", "cloud"):
        for index, (tokens, elapsed) in enumerate(((1, .5), (100, 2), (200, 8))):
            result = {"model": "mixed:latest" if index % 2 else "mixed",
                      "usage": {"completion_tokens": tokens}}
            if index % 2:
                result.update(eval_count=100, eval_duration=1_000_000_000)
            assert usage_history.record_completion(result, source, elapsed)
    assert usage_history.recent_model_response_speed("mixed") == 25
    assert usage_history.recent_model_response_speed("mixed:latest") == 25
    assert usage_history.recent_local_response_speed(99, models=("mixed:latest", "mixed")) == 25
    assert usage_history.recent_provider_response_speed("cloud", 99) == 25
    for _ in range(4):
        assert usage_history.record_completion(
            {"model": "decode-only", "usage": {"completion_tokens": 200},
             "eval_count": 200, "eval_duration": 1_000_000_000}, "ollama")
    assert usage_history.recent_model_speed("decode-only") == 200
    assert usage_history.recent_model_response_speed("decode-only") is None
    assert usage_history.recent_local_response_speed(99, models=("decode-only",)) == 99
    # Old timing-basis labels are immaterial when actual completion/elapsed exist;
    # an absent elapsed field is never inferred from legacy TPS.
    with sqlite3.connect(usage_history.DATABASE) as conn:
        conn.execute("UPDATE usage_events SET speed_basis='unknown' WHERE model LIKE 'mixed%'")
    assert usage_history.recent_model_response_speed("mixed") == 25


def test_response_speed_rejects_malformed_rows_and_requires_three_valid_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    conn = usage_history._connection()
    with conn:
        for tokens, elapsed in ((0, 1), (-1, 1), (1, 0), (1, -1), (1, None),
                                (1, float("inf")), (1, float("nan")),
                                (1, "bad"), ("bad", 1), (1.5, 1),
                                (1_000_000_001, 1), (100, 1e-310)):
            conn.execute("INSERT INTO usage_events(timestamp,source,model,prompt_tokens,completion_tokens,"
                         "total_tokens,tokens_per_second,elapsed_seconds) VALUES(?,?,?,?,?,?,?,?)",
                         (usage_history.time.time(), "ollama", "bad", 0, tokens, 200, 999, elapsed))
    conn.close()
    assert usage_history.recent_model_response_speed("bad") is None
    for _ in range(2):
        _local_timed("bad", 100, 2)
    assert usage_history.recent_model_response_speed("bad") is None
    assert usage_history.recent_local_response_speed(13, models=("bad",)) == 13
    _local_timed("bad", 100, 2)
    assert usage_history.recent_model_response_speed("bad") == 50
    assert usage_history.recent_model_response_speed("unrelated") is None


def test_response_speed_keeps_recent_valid_sample_window_and_source_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_history, "DATABASE", tmp_path / "usage.db")
    for _ in range(12):
        _local_timed("window:latest", 100, 100)
    for _ in range(12):
        _local_timed("window", 100, 2)
    assert usage_history.recent_model_response_speed("window:latest") == 50
    for _ in range(8):
        assert usage_history.record_completion({"model": "window", "usage": {"completion_tokens": 100}},
                                              "cloud", elapsed_seconds=4)
    assert usage_history.recent_provider_response_speed("cloud", 99) == 25
    # Invalid recent rows must not displace the bounded most-recent valid rows.
    with sqlite3.connect(usage_history.DATABASE) as conn:
        conn.execute("UPDATE usage_events SET elapsed_seconds=NULL WHERE source='cloud'")
    assert usage_history.recent_provider_response_speed("cloud", 99) == 99
    assert usage_history.recent_model_response_speed("window") == 50
    with sqlite3.connect(usage_history.DATABASE) as conn:
        conn.execute("UPDATE usage_events SET timestamp=?", (usage_history.time.time()-31*86400,))
    assert usage_history.recent_model_response_speed("window") is None


@pytest.mark.parametrize('timestamp_kind',['text','infinite','future','stale'])
def test_response_speed_requires_finite_numeric_current_window_timestamp(tmp_path,monkeypatch,timestamp_kind):
    monkeypatch.setattr(usage_history,'DATABASE',tmp_path/'usage.db')
    now=usage_history.time.time()
    stamp={'text':'not-a-timestamp','infinite':float('inf'),
           'future':now+3600,'stale':now-31*86400}[timestamp_kind]
    with usage_history._connection() as conn:
        for source in ('ollama','cloud'):
            for _ in range(3):
                conn.execute('INSERT INTO usage_events(timestamp,source,model,prompt_tokens,completion_tokens,total_tokens,elapsed_seconds) VALUES(?,?,?,?,?,?,?)',
                             (stamp,source,'window-invalid',0,900,900,1))
    assert usage_history.recent_model_response_speed('window-invalid') is None
    assert usage_history.recent_provider_response_speed('cloud',77)==77
    for _ in range(3):_local_timed('window-invalid',100,2)
    # Bad latest rows cannot crowd real samples out of the bounded valid window.
    with usage_history._connection() as conn:
        for _ in range(15):
            conn.execute('INSERT INTO usage_events(timestamp,source,model,prompt_tokens,completion_tokens,total_tokens,elapsed_seconds) VALUES(?,?,?,?,?,?,?)',
                         (stamp,'ollama','window-invalid',0,900,900,1))
    assert usage_history.recent_model_response_speed('window-invalid')==50
