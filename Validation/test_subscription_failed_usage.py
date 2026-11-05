"""Failed official-agent runs can still consume reported subscription usage."""

import json
import sqlite3
from unittest.mock import patch

import pytest

import cloud_ai
import cloud_providers
import pc_offline
import routing_mode
import usage_history


@pytest.mark.parametrize("per_model", [False, True])
def test_failed_claude_run_records_reported_usage(tmp_path, per_model):
    report = {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 4,
        },
    }
    if per_model:
        report["modelUsage"] = {
            "first": {
                "inputTokens": 2,
                "outputTokens": 3,
                "cacheReadInputTokens": 10,
                "cacheCreationInputTokens": 4,
            },
            "second": {"inputTokens": 5, "outputTokens": 6},
        }
    with (
        patch.object(pc_offline, "offline", return_value=False),
        patch.object(routing_mode, "load_scope", return_value="both"),
        patch.object(cloud_ai, "task_route", return_value="automatic"),
        patch.object(
            cloud_providers, "subscription_agent_status", return_value={"enabled": True}
        ),
        patch.object(
            cloud_providers,
            "build_subscription_task_command",
            return_value=["test-agent"],
        ),
        patch.object(
            cloud_providers,
            "_agent_process",
            side_effect=[
                (0, json.dumps({"authMethod": "claude.ai"}), ""),
                (1, json.dumps(report), "private diagnostic"),
            ],
        ),
        patch.object(usage_history, "DATABASE", tmp_path / "usage.db"),
    ):
        token = usage_history.CURRENT_TASK.set("c" * 12)
        try:
            with pytest.raises(RuntimeError, match="Official agent could not finish"):
                cloud_providers.run_subscription_task("claude", tmp_path, "task")
        finally:
            usage_history.CURRENT_TASK.reset(token)
        with sqlite3.connect(usage_history.DATABASE) as conn:
            rows = conn.execute(
                "SELECT source,task_id,total_tokens FROM usage_events ORDER BY total_tokens"
            ).fetchall()
    expected = [11, 19] if per_model else [19]
    assert rows == [("claude_subscription", "c" * 12, total) for total in expected]


@pytest.mark.parametrize(
    "output",
    [
        "not JSON",
        "[]",
        "null",
        '{"is_error":true}',
        '{"usage":{"input_tokens":true,"output_tokens":3}}',
    ],
)
def test_failed_run_without_valid_usage_keeps_generic_error(output, tmp_path):
    with (
        patch.object(pc_offline, "offline", return_value=False),
        patch.object(routing_mode, "load_scope", return_value="both"),
        patch.object(cloud_ai, "task_route", return_value="automatic"),
        patch.object(
            cloud_providers, "subscription_agent_status", return_value={"enabled": True}
        ),
        patch.object(
            cloud_providers,
            "build_subscription_task_command",
            return_value=["test-agent"],
        ),
        patch.object(
            cloud_providers,
            "_agent_process",
            return_value=(1, output, "private diagnostic"),
        ),
        patch.object(usage_history, "record_completion") as record,
    ):
        with pytest.raises(
            RuntimeError, match="Official agent could not finish"
        ) as error:
            cloud_providers.run_subscription_task("grok", tmp_path, "task")
        assert "private diagnostic" not in str(error.value)
        record.assert_not_called()
