"""Compare real local step-argument formatting; never execute generated plans.

This opt-in fixture isolates schema/serialization behavior. It does not measure
application quality, browser correctness or end-to-end native-worker reliability.
"""

import argparse
import copy
import json
import sys
import time
from pathlib import Path

from jsonschema import ValidationError, validate

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_ai
import model_library
import native_agent
import task_context

STEP = {"phase": "Controls", "title": "Verify movement and shooting", "check_tool": "check_webapp",
        "check_arguments": {"path": "index.html", "cases": [
            {"keys": [{"key": "ArrowRight", "hold_ms": 200}], "state": {"game.player.x": {"gt": 300}}},
            {"keys": [{"key": "Space"}], "state": {"game.bullets.length": {"gte": 1}}}]}}


def grade(arguments: str, schema: dict) -> dict:
    """Demand real nested data and unchanged cases; no task state is written."""
    result = {"outer_json": False, "schema_valid": False, "real_array": False,
              "semantics_preserved": False}
    try:
        value = json.loads(arguments)
        result["outer_json"] = isinstance(value, dict)
        validate(value, schema)
        result["schema_valid"] = True
        result["real_array"] = isinstance(value.get("steps"), list)
        steps = task_context._validate_steps(value.get("steps"))
        result["semantics_preserved"] = (value.get("action") == "revise" and type(value.get("step_id")) is int
            and value["step_id"] == 7 and len(steps) == 1
            and steps[0][:2] == (STEP["phase"], STEP["title"])
            and steps[0][4] == "check_webapp"
            and json.loads(steps[0][5]) == STEP["check_arguments"])
    except (ValueError, TypeError, AttributeError, ValidationError) as error:
        result["error"] = str(error)[:600]
    result["passed"] = all(result[key] for key in ("outer_json", "schema_valid", "real_array", "semantics_preserved"))
    return result


def router_repair(tool: dict) -> dict:
    """Fault-inject one schema rejection, then let a real local model correct it."""
    from unittest.mock import patch

    request = {"model": cloud_ai.LOCAL_MODEL, "tools": [tool], "stream": False,
        "tool_choice": {"type": "function", "function": {"name": "task_steps"}},
        "messages": [{"role": "user", "content": "Revise active SQLite step 7 using the step below. "
            "Preserve its phase, title and every acceptance case exactly. Call task_steps; do not execute "
            "commands or change source. Corrected step:\n" + json.dumps(STEP)}]}
    upstream = cloud_ai.post
    calls = []
    injected = False
    router = cloud_ai.Router()
    def post(url, payload, *args, **kwargs):
        nonlocal injected
        if not url.endswith("/v1/chat/completions"):
            return upstream(url, payload, *args, **kwargs)
        injecting = not injected
        calls.append({"model": payload["model"], "injected_response": injecting,
            "has_correction_feedback": any("[Tool argument correction]" in str(m.get("content", ""))
                                           for m in payload.get("messages", [])),
            "max_tokens": payload.get("max_tokens"), "timeout": kwargs.get("timeout")})
        if injecting:
            injected = True
            return {"model": payload["model"], "choices": [{"message": {"role": "assistant", "tool_calls": [
                {"id": "injected", "type": "function", "function": {"name": "task_steps", "arguments":
                    json.dumps({"action": "revise", "step_id": 7, "steps": json.dumps([STEP])})}}]}}]}
        response = upstream(url, payload, *args, **kwargs)
        calls[-1]["response_choices"] = response.get("choices", [])
        calls[-1]["usage"] = response.get("usage", {})
        try:
            cloud_ai.valid_completion(copy.deepcopy(response), payload)
        except ValueError as error:
            calls[-1]["validation_error"] = str(error)[:800]
        return response
    report = {"fault_injection": "First response has a quoted steps array. It is synthetic and executes no tool.",
              "calls": calls, "passed": False}
    started = time.monotonic()
    with patch("routing_mode.load_scope", return_value="local_only"), patch.object(cloud_ai, "post", side_effect=post):
        try:
            result = router.complete(request, "argument-format-benchmark")
            actual = result["choices"][0]["message"]["tool_calls"]
            report["usage"] = result.get("usage", {})
            report["model"] = router.last_local_model
            report["weights"] = router.last_local_weights
            report["grade"] = grade(actual[0]["function"]["arguments"], tool["function"]["parameters"])
            report["passed"] = (len(actual) == 1 and report["grade"]["passed"]
                                and len(calls) == 2 and calls[1]["has_correction_feedback"])
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {str(error)[:600]}"
    report["seconds"] = round(time.monotonic() - started, 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="cale-fast")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repair-only", action="store_true", help="Inject one rejection and exercise the production local router")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preserve the existing report; choose a new output path")
    info = model_library.post("/api/show", {"model": args.model})
    if info.get("remote_host") or info.get("remote_model") or "tools" not in info.get("capabilities", []):
        parser.error("Use physically installed tool-capable local weights")
    current = copy.deepcopy(next(tool for tool in native_agent.TOOLS if tool["function"]["name"] == "task_steps"))
    if args.repair_only:
        report = router_repair(current)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    previous = copy.deepcopy(current)
    previous["function"]["parameters"]["properties"]["steps"] = {"anyOf": [
        copy.deepcopy(current["function"]["parameters"]["properties"]["steps"]),
        {"type": "string", "maxLength": 64000}]}
    previous["function"]["description"] = previous["function"]["description"].replace(
        'steps must be an actual JSON array, never a quoted or escaped string. ', '')
    records = []
    for seed in (41, 79, 101):
        # Alternate order to avoid always warming one variant with the other.
        variants = [("array_or_string", previous), ("array_only", current)]
        for name, tool in (variants if seed != 79 else list(reversed(variants))):
            started = time.monotonic()
            record = {"schema": name, "seed": seed}
            try:
                response = cloud_ai.post("http://127.0.0.1:11434/v1/chat/completions", {
                    "model": args.model, "messages": [{"role": "system", "content":
                        "You are a native project worker. Make the requested tool call; do not answer with prose."},
                        {"role": "user", "content": "Revise active SQLite step 7 to the corrected acceptance step below. "
                         "Call task_steps action=revise, preserving phase, title and every case exactly. "
                         "Do not run commands or change application source. Corrected step:\n" + json.dumps(STEP)}],
                    "tools": [tool], "tool_choice": {"type": "function", "function": {"name": "task_steps"}},
                    "stream": False, "temperature": 0.2, "seed": seed, "max_tokens": 1800}, timeout=120)
                calls = response.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []
                record["usage"] = response.get("usage", {})
                if len(calls) != 1 or calls[0].get("function", {}).get("name") != "task_steps":
                    raise ValueError("The model did not produce exactly one task_steps call")
                record["arguments"] = calls[0]["function"]["arguments"]
                record["grade"] = grade(record["arguments"], tool["function"]["parameters"])
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {str(error)[:500]}"
                record["grade"] = {"passed": False}
            record["seconds"] = round(time.monotonic() - started, 2)
            records.append(record)
            # Preserve completed samples if a later API call is interrupted.
            args.output.write_text(json.dumps({"scope": __doc__, "model": args.model,
                "weights": model_library.weight_blob(info), "step": STEP,
                "tools": {"array_or_string": previous, "array_only": current},
                "request_options": {"temperature": 0.2, "max_tokens": 1800,
                                    "endpoint": "local Ollama OpenAI-compatible chat completions"},
                "runs": records}, indent=2), encoding="utf-8")
            print(json.dumps({key: record[key] for key in ("schema", "seed", "seconds", "grade")}), flush=True)
    return 0 if all(record["grade"]["passed"] for record in records if record["schema"] == "array_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
