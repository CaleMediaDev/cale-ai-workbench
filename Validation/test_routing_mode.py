"""A visible mode choice persists and has predictable cost priorities."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routing_mode


def test_mode_is_saved_and_bad_data_returns_to_medium(tmp_path):
    assert routing_mode.load(tmp_path) == "medium"
    routing_mode.save(tmp_path, "floor_it")
    assert routing_mode.load(tmp_path) == "floor_it"
    (tmp_path / "routing-mode.json").write_text('{"mode":"unknown"}')
    assert routing_mode.load(tmp_path) == "medium"
    with pytest.raises(ValueError):
        routing_mode.save(tmp_path, "unlimited_paid")


def test_paid_saver_and_floor_it_choose_different_priorities():
    assert not routing_mode.paid_first("paid_saver", "deep", 10, 200, 100, True)
    assert routing_mode.paid_first("medium", "deep", 10, 20, 100, True)
    assert not routing_mode.paid_first("floor_it", "deep", 10, 200, 300, True)
    assert routing_mode.paid_first("floor_it", "quick", 10, 300, 200, False)
    assert routing_mode.free_first("paid_saver", False, True)
    assert not routing_mode.free_first("floor_it", True, False)
