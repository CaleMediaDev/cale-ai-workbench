"""Hardware and measured throughput must both qualify a local agent model."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_fitness
import model_library


def test_12gb_gpu_rejects_79b_but_accepts_compact_tool_models():
    assert not model_fitness.hardware_allows(48.2, 128, 12)
    assert model_fitness.hardware_allows(21.1, 128, 12)
    assert model_fitness.hardware_allows(6.1, 32, 12)


def test_unknown_gpu_keeps_only_small_models():
    assert model_fitness.hardware_allows(6.1, 24, 0)
    assert not model_fitness.hardware_allows(21.1, 128, 0)


def test_repeated_slow_completions_disqualify_model():
    with patch.object(model_fitness, "measured_speed", return_value=2.2):
        assert not model_fitness.suitable("cale-qwen35", 20.6, 128, 12)
    with patch.object(model_fitness, "measured_speed", return_value=18.4):
        assert model_fitness.suitable("cale-qwen36", 21.1, 128, 12)


def test_retirement_waits_for_idle_and_preserves_unowned_source():
    item = {"alias": "cale-slow", "source": "someone/slow:latest"}
    with (patch.object(model_library, "alias_update_waits_for_tasks", return_value=True),
          patch.object(model_library, "delete_model") as delete):
        assert not model_library.retire_slow_model(item)
        delete.assert_not_called()
    with (patch.object(model_library, "alias_update_waits_for_tasks", return_value=False),
          patch.object(model_library, "owned_sources", return_value=set()),
          patch.object(model_library, "delete_model") as delete):
        assert model_library.retire_slow_model(item)
        delete.assert_called_once_with("cale-slow")
