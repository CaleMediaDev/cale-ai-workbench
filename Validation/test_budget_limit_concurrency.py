"""Concurrent requests hitting one daily cap must produce one routing signal."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import routing_memory


def test_concurrent_budget_cap_is_recorded_once_per_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(routing_memory, "DATABASE", tmp_path / "routing.db")
    now = [1_800_000_000]
    monkeypatch.setattr(routing_memory.time, "time", lambda: now[0])
    routing_memory._connection().close()
    readers = threading.Barrier(8)
    original_connect = sqlite3.connect

    class DelayedConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            cursor = super().execute(statement, parameters)
            if statement.startswith("SELECT timestamp,outcome"):
                # All callers observe the same pre-recording snapshot.
                readers.wait(timeout=5)
            return cursor

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=DelayedConnection)

    with monkeypatch.context() as patch:
        patch.setattr(routing_memory.sqlite3, "connect", connect)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: routing_memory.record_budget_limit("groq"), range(8)))
    assert routing_memory.pressure("groq")["limits"] == 1
    routing_memory.record_budget_limit("groq")
    assert routing_memory.pressure("groq")["limits"] == 1
    routing_memory.record_budget_limit("openrouter")
    assert routing_memory.pressure("openrouter")["limits"] == 1
    now[0] += 86400
    routing_memory.record_budget_limit("groq")
    assert routing_memory.pressure("groq")["limits"] == 2
    assert routing_memory.pressure("groq")["last_limit"] == now[0]
