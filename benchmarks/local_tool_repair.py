"""Exercise local argument repair and a real isolated SQLite planning effect.

The initial malformed response is injected; the correction comes from the
installed local model. This proves transport/validation, not task reasoning.
No user project, account, or cloud route is used.
"""
import argparse
import copy
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from browser_assertion_repair import require_idle_media

import cloud_ai
import native_agent
import task_context


def run(runtime: Path, model: str) -> dict:
    require_idle_media(runtime)
    info = cloud_ai.post("http://127.0.0.1:11434/api/show", {"model": model}, timeout=10)
    if info.get("remote_host") or info.get("remote_model") or "tools" not in info.get("capabilities", []):
        raise ValueError("Use an installed local model with tool support")
    expected = {"action": "plan", "steps": [{"phase": "Check", "title": "Verify identity",
        "check_tool": "check_python_function", "check_arguments": {"path": "logic.py",
        "function": "identity", "cases": [{"args": [2], "expected": 2}]}}]}
    tool = next(item for item in native_agent.TOOLS if item["function"]["name"] == "task_steps")
    request = {"model": model, "messages": [{"role": "user", "content":
        "Return task_steps arguments exactly matching this plan. steps must be a JSON array: " + json.dumps(expected)}],
        "tools": [tool], "tool_choice": {"type": "function", "function": {"name": "task_steps"}},
        "stream": False, "temperature": 0, "reasoning_effort": "none", "max_tokens": 512}
    bad = copy.deepcopy(expected)
    bad["steps"] = json.dumps(bad["steps"]) + " trailing invalid data"
    malformed = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "injected-bad",
        "type": "function", "function": {"name": "task_steps", "arguments": json.dumps(bad)}}]}}]}
    try:
        cloud_ai.valid_completion(malformed, request)
    except cloud_ai.ToolArgumentError as error:
        guarded = cloud_ai.argument_repair_request(request, error)
    else:
        raise AssertionError("Malformed quoted array must fail before any tool executes")
    repair = cloud_ai.local_json_repair_request(guarded)
    require_idle_media(runtime)
    started = time.monotonic()
    raw = cloud_ai.post("http://127.0.0.1:11434/v1/chat/completions", repair, timeout=120)
    reply = cloud_ai.validated_json_tool_reply(raw, guarded, repair)
    call = reply["choices"][0]["message"]["tool_calls"][0]
    arguments = json.loads(call["function"]["arguments"])
    if call["function"]["name"] != "task_steps" or arguments != expected:
        raise AssertionError("The live model did not return the requested plan")
    with tempfile.TemporaryDirectory(prefix="cale-tool-repair-") as temporary:
        folder = Path(temporary)
        assert not (folder / ".local-ai/memory.sqlite3").exists()
        result = json.loads(native_agent.execute_tool(folder, "task_steps", arguments))
        queue = task_context.step_status(folder)
        assert result["total"] == queue["total"] == 1
        assert queue["done"] == 0 and queue["active"]["check_arguments"] == expected["steps"][0]["check_arguments"]
    message = raw["choices"][0]["message"]
    return {"passed": True, "scenario": "injected malformed arguments; real local correction and SQLite plan",
        "seconds": round(time.monotonic() - started, 2), "model": model,
        "initial_bad_response": "injected quoted steps array with trailing invalid data", "correction": "live local inference",
        "tool_choice_sent": "tool_choice" in repair, "requested_format": repair["response_format"]["type"],
        "raw_tool_calls": len(message.get("tool_calls", [])), "validated_tool": call["function"]["name"],
        "sql_units_created": 1, "sql_units_completed": 0, "user_projects_touched": False,
        "usage": raw.get("usage", {})}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.runtime_root.resolve(strict=True), args.model)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
