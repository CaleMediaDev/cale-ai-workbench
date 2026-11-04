"""Developer trial deadlines must stop dispatch and cap blocking HTTP calls."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import browser_assertion_repair as probe

import cloud_ai


def stats():
    return {"model_calls": 0, "tool_calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "tools_used": [], "routes": []}


def test_expired_budget_never_dispatches_inference():
    receipt = stats()
    with patch.object(probe.time, "monotonic", return_value=100), patch.object(cloud_ai.Router, "complete") as dispatch:
        router = probe.observed_router(receipt, deadline=100)
        with pytest.raises(RuntimeError, match="time budget exhausted"):
            router.complete({})
    dispatch.assert_not_called()
    assert receipt["time_budget_exhausted"] and receipt["model_calls"] == 0


@pytest.mark.parametrize("timeout", [False, True])
def test_live_call_uses_remaining_budget_and_records_uncertain_usage(timeout):
    receipt = stats()
    clock = {"now": 100}
    response = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    def dispatch(self, payload, task_id, **kwargs):
        self.last_local_model, self.last_local_weights = "local", "weights"
        return cloud_ai.post("http://127.0.0.1:11434/v1/chat/completions", payload, timeout=1800)
    def transport(*args, **kwargs):
        if timeout:
            clock["now"] = 111
            raise TimeoutError("request exceeded remaining budget")
        return response
    with (patch.object(probe.time, "monotonic", side_effect=lambda: clock["now"]),
          patch.object(probe, "require_idle_media"),
          patch.object(cloud_ai.Router, "complete", dispatch),
          patch.object(cloud_ai, "post", side_effect=transport) as upstream):
        router = probe.observed_router(receipt, deadline=110)
        if timeout:
            with pytest.raises(TimeoutError):
                router.complete({})
            assert receipt["time_budget_exhausted"] and receipt["token_usage_incomplete"]
        else:
            assert router.complete({}) == response
            assert receipt["model_calls"] == 1
    assert upstream.call_count == 1 and upstream.call_args.kwargs["timeout"] == 10
