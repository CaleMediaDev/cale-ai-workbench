"""Simultaneous radio startups must not multiply built-in stations."""

import sqlite3
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import radio_station


def test_concurrent_radio_startup_creates_each_preset_once(tmp_path, monkeypatch):
    radio_station.database(tmp_path).close()
    readers = threading.Barrier(4)
    original_connect = sqlite3.connect

    class DelayedConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            cursor = super().execute(statement, parameters)
            if statement.startswith("SELECT * FROM stations") and not self.in_transaction:
                # Reproduce startup workers reading the same empty snapshot
                # before any of them has reserved the database writer.
                readers.wait(timeout=5)
            return cursor

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=DelayedConnection)

    with monkeypatch.context() as patch:
        patch.setattr(radio_station.sqlite3, "connect", connect)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: radio_station.ensure_stations(tmp_path), range(4)))
    counts = Counter(row["name"] for row in radio_station.stations(tmp_path))
    assert counts == Counter(name for name, _, _ in radio_station.DEFAULT_STATIONS)
    radio_station.ensure_stations(tmp_path)
    assert Counter(row["name"] for row in radio_station.stations(tmp_path)) == counts
