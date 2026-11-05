"""Checks use fake upstreams and temporary DPAPI files, never a real cloud account."""

import base64
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_ai as cloud
import cloud_providers

ANSWER = {
    "id": "test",
    "created": 1,
    "model": "free-test",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "checked"},
            "finish_reason": "stop",
        }
    ],
}
REFUSAL = {
    **ANSWER,
    "choices": [{"message": {"role": "assistant", "content": "I can't help with that."},
                 "finish_reason": "stop"}],
}
REQUEST = {
    "model": cloud.LOCAL_MODEL,
    "messages": [{"role": "user", "content": "test"}],
}

ARGUMENT_REQUEST = {**REQUEST, "max_tokens": 1000, "tools": [{"type": "function", "function": {
    "name": "save_plan", "parameters": {"type": "object", "properties": {
        "steps": {"type": "array", "items": {"type": "integer"}}},
        "required": ["steps"], "additionalProperties": False}}}]}


def argument_reply(steps):
    return {**ANSWER, "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "plan", "type": "function", "function": {"name": "save_plan",
            "arguments": json.dumps({"steps": steps})}}]}}]}


class CloudTests(unittest.TestCase):
    def test_non_game_worker_keeps_distinct_repair_model_eligible(self):
        import native_agent

        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        request = {**REQUEST, "messages": [
            {"role": "system", "content": native_agent.prompt({
                "id": "expense-route", "folder": "C:/Tasks/test",
                "brief": "Build an offline expense tracker."})},
            {"role": "user", "content": "Continue the next saved step."}]}
        router = cloud.Router()
        router.model_weights = {cloud.LOCAL_MODEL: "a" * 64, "alternate": "b" * 64}
        router.model_contexts = {cloud.LOCAL_MODEL: 65536, "alternate": 65536}
        with (patch.object(router, "local_candidates", return_value=[cloud.LOCAL_MODEL, "alternate"]),
              patch("model_fitness.rank_game_models") as game_rank,
              patch.object(cloud, "post", return_value=ANSWER) as post):
            router.complete(request, repair_avoid=[{"model": cloud.LOCAL_MODEL, "weights": "a" * 64}])
        game_rank.assert_not_called()
        self.assertEqual(post.call_args.args[1]["model"], "alternate")

    def test_native_step_list_transport_decodes_once_then_validates_same_schema(self):
        import native_agent

        tool = next(t for t in native_agent.TOOLS if t["function"]["name"] == "task_steps")
        request = {**REQUEST, "tools": [tool]}
        step = {"phase": "Check", "title": "Run checks", "check_tool": "run_verifier"}
        def reply(value):
            result = argument_reply(value)
            function = result["choices"][0]["message"]["tool_calls"][0]["function"]
            function["name"] = "task_steps"
            function["arguments"] = json.dumps({"action": "plan", "steps": value})
            return result
        result = cloud.valid_completion(reply(json.dumps([step])), request)
        parsed = json.loads(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(parsed, {"action": "plan", "steps": [step]})
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: 65536}
        with patch.object(cloud, "post", return_value=reply(json.dumps([step]))) as post:
            completed = router.complete(request)
        self.assertEqual(post.call_count, 1)
        self.assertIsInstance(json.loads(completed["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["steps"], list)
        for invalid in ('[{broken', '[1]', json.dumps(json.dumps([step])), '[NaN]', ' ' * 64001):
            with self.assertRaises(cloud.ToolArgumentError):
                cloud.valid_completion(reply(invalid), request)
        for choice in ("none", {"type": "function", "function": {"name": "other"}}):
            with self.assertRaises(cloud.ToolArgumentError):
                cloud.valid_completion(reply(json.dumps([step])), {**request, "tool_choice": choice})
        # Other tools retain their declared strict behavior; no generic coercion.
        with self.assertRaises(cloud.ToolArgumentError):
            cloud.valid_completion(argument_reply('[1,2]'), ARGUMENT_REQUEST)

    def test_valid_tool_data_cannot_override_disabled_or_forced_selection(self):
        for choice in ("none", {"type": "function", "function": {"name": "other"}}):
            with self.assertRaises(ValueError):
                cloud.valid_completion(argument_reply([1, 2]), {**ARGUMENT_REQUEST, "tool_choice": choice})

    def test_local_schema_feedback_repairs_once_without_mutating_original_request(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        request = json.loads(json.dumps(ARGUMENT_REQUEST))
        original = json.loads(json.dumps(request))
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: 65536}
        good = argument_reply([1, 2])
        with patch.object(cloud, "post", side_effect=[argument_reply('[1,2]'), good]) as post:
            self.assertEqual(router.complete(request, "repair-task"), good)
        self.assertEqual(request, original)
        self.assertEqual(post.call_count, 2)
        repair = post.call_args_list[1].args[1]
        feedback = "\n".join(message.get("content", "") for message in repair["messages"][-2:])
        self.assertIn('steps', feedback)
        self.assertIn('array', feedback)
        self.assertIn('No tool or source edit ran', feedback)
        self.assertEqual(repair["max_tokens"], 1000)
        self.assertEqual(post.call_args_list[1].kwargs["timeout"], 120)
        self.assertNotIn("tool_choice", repair)
        self.assertEqual(repair["tools"], [])
        self.assertEqual(repair["response_format"]["type"], "json_schema")
        self.assertEqual(router.last_local_model, cloud.LOCAL_MODEL)

    def test_local_schema_repair_validates_json_arguments_before_wrapping_the_call(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        raw = {**ANSWER, "choices": [{**ANSWER["choices"][0], "message": {
            "role": "assistant", "content": '{"steps":[1,2]}'}}]}
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: 65536}
        with patch.object(cloud, "post", side_effect=[argument_reply("[1,2]"), raw]) as post:
            result = router.complete(ARGUMENT_REQUEST)
        call = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "save_plan")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"steps": [1, 2]})
        repair = post.call_args_list[1].args[1]
        self.assertEqual(repair["response_format"]["json_schema"]["schema"], ARGUMENT_REQUEST["tools"][0]["function"]["parameters"])
        self.assertEqual(repair["tools"], [])

    def test_local_json_repair_rejects_invalid_arguments_and_wrong_tool_selection(self):
        with self.assertRaises(cloud.ToolArgumentError) as caught:
            cloud.valid_completion(argument_reply("[1,2]"), ARGUMENT_REQUEST)
        guarded = cloud.argument_repair_request(ARGUMENT_REQUEST, caught.exception)
        repair = cloud.local_json_repair_request(guarded)
        for content in ('not json', '[]', '{"steps":"[1,2]"}', '{"steps":[1],"extra":true}', '{"steps":[NaN]}'):
            with self.subTest(content=content):
                raw = {**ANSWER, "choices": [{**ANSWER["choices"][0], "message": {
                    "role": "assistant", "content": content}}]}
                with self.assertRaises(cloud.ToolArgumentError):
                    cloud.validated_json_tool_reply(raw, guarded, repair)
        wrong = argument_reply([1])
        wrong["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "not_offered"
        with self.assertRaises(ValueError):
            cloud.validated_json_tool_reply(wrong, guarded, repair)
        self.assertGreater(cloud.estimated_input_tokens({**REQUEST, "response_format": {
            "type": "json_schema", "json_schema": {"schema": {"enum": ["x" * 12000]}}}}),
            cloud.estimated_input_tokens(REQUEST) + 3000)

    def test_nested_plan_checker_arguments_are_repaired_before_plan_dispatch(self):
        import native_agent
        tool = next(item for item in native_agent.TOOLS if item["function"]["name"] == "task_steps")
        request = {**REQUEST, "tools": [tool]}
        valid = {"action": "plan", "steps": [{"phase": "Create", "title": "Check total",
            "check_tool": "check_webapp", "check_arguments": {"path": "index.html", "cases": [
                {"set": {"amount": "10"}, "click": ["Add expense"], "expect": {"tot": "10.00"}}]}}]}
        bad = json.loads(json.dumps(valid))
        bad["steps"][0]["check_arguments"]["cases"] = [{"input": "amount=10", "expect": "Total: $10.00"}]
        malformed = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "bad-plan",
            "type": "function", "function": {"name": "task_steps", "arguments": json.dumps(bad)}}]}}]}
        with self.assertRaises(cloud.ToolArgumentError) as caught:
            cloud.valid_completion(malformed, request)
        guarded = cloud.argument_repair_request(request, caught.exception)
        guarded_schema = guarded["tools"][0]["function"]["parameters"]
        self.assertEqual(guarded_schema["properties"]["steps"]["maxItems"], 1)
        self.assertEqual(guarded_schema["properties"]["action"]["enum"], ["plan"])
        self.assertEqual(tool["function"]["parameters"]["properties"]["steps"]["maxItems"], 8)
        expanded = json.loads(json.dumps(valid))
        expanded["steps"].append(expanded["steps"][0])
        from jsonschema import ValidationError, validate
        with self.assertRaises(ValidationError):
            validate(expanded, guarded_schema)
        serialized = json.loads(json.dumps(malformed))
        quoted = json.loads(serialized["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
        quoted["steps"][0]["check_arguments"]["cases"] = json.dumps(quoted["steps"][0]["check_arguments"]["cases"])
        serialized["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(quoted)
        with self.assertRaises(cloud.ToolArgumentError):
            cloud.valid_completion(serialized, request)
        corrected = {**ANSWER, "choices": [{**ANSWER["choices"][0], "message": {
            "role": "assistant", "content": json.dumps(valid)}}]}
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: 65536}
        with patch.object(cloud, "post", side_effect=[malformed, corrected]) as upstream:
            result = router.complete(request)
        self.assertEqual(upstream.call_count, 2)
        call = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), valid)

    def test_native_checker_schema_feedback_uses_relevant_branch_and_precise_path(self):
        import native_agent

        tool = next(item for item in native_agent.TOOLS if item["function"]["name"] == "task_steps")
        request = {**REQUEST, "tools": [tool]}
        step = {"phase": "Create", "title": "Check total", "check_tool": "check_webapp",
                "check_arguments": {"path": "index.html", "cases": [{"expect": {"total": "10.00"}}]}}
        missing = {key: value for key, value in step.items() if key != "check_arguments"}
        extra = {**step, "check_command": "PRIVATE_BAD_VALUE" * 1000}
        bad_cases = json.loads(json.dumps(step))
        bad_cases["check_arguments"]["cases"][0]["expect"] = "PRIVATE_BAD_VALUE" * 1000
        examples = [
            ({"action": "plan", "steps": [missing]}, ["steps", 0, "check_arguments"], "required"),
            ({"action": "plan", "steps": [extra]}, ["steps", 0, "check_command"], "additionalProperties"),
            ({"action": "plan", "steps": [{**extra, "check_tool": "PRIVATE_BAD_VALUE"}]}, ["steps", 0, "check_tool"], "enum"),
            ({"action": "plan", "steps": [bad_cases]}, ["steps", 0, "check_arguments", "cases", 0, "expect"], "type"),
            ({"action": "complete", "step_id": 3, "steps": [step]}, ["steps"], "additionalProperties"),
        ]
        for arguments, expected_path, rule in examples:
            with self.subTest(path=expected_path, action=arguments["action"]):
                response = {"choices": [{"message": {"tool_calls": [{"id": "bad-check", "function": {
                    "name": "task_steps", "arguments": json.dumps(arguments)}}]}}]}
                with self.assertRaises(cloud.ToolArgumentError) as caught:
                    cloud.valid_completion(response, request)
                error = caught.exception
                self.assertEqual(error.path, expected_path)
                self.assertIn("Schema rule " + rule, error.problem)
                self.assertNotIn("PRIVATE_BAD_VALUE", str(error))
                self.assertNotIn("anyOf", str(error))
                self.assertLess(len(error.problem), 400)
                guarded = cloud.argument_repair_request(request, error)
                self.assertLess(len(guarded["messages"][-1]["content"]), 1500)
                if len(expected_path) > 1:
                    properties = guarded["tools"][0]["function"]["parameters"]["properties"]
                    self.assertEqual(properties["steps"]["maxItems"], 1)
                    self.assertEqual(properties["action"]["enum"], ["plan"])
                # Precise feedback never relaxes the original contract.
                with self.assertRaises(cloud.ToolArgumentError):
                    cloud.valid_completion(response, guarded)

    def test_native_plan_tool_name_is_not_a_repeatable_shell_check(self):
        import native_agent

        tool = next(item for item in native_agent.TOOLS if item["function"]["name"] == "task_steps")
        request = {**REQUEST, "tools": [tool]}

        def response(command):
            arguments = {"action": "plan", "steps": [{"phase": "Repair", "title": "Verify result",
                "check_command": command}]}
            return {"choices": [{"message": {"tool_calls": [{"id": "bad-command", "function": {
                "name": "task_steps", "arguments": json.dumps(arguments)}}]}}]}

        for command in ("write_file", "edit_file", "check_webapp"):
            with self.subTest(command=command):
                with self.assertRaises(cloud.ToolArgumentError) as caught:
                    cloud.valid_completion(response(command), request)
                error = caught.exception
                self.assertEqual(error.path, ["steps", 0, "check_command"])
                self.assertIn("tool names are not shell commands", error.problem)
                self.assertIn("check_tool", error.problem)
                self.assertIn("typed check_arguments", error.problem)
                self.assertIn("real repeatable shell command", error.problem)
                self.assertNotIn(command, error.problem)
                self.assertLess(len(error.problem), 250)
                guarded = cloud.argument_repair_request(request, error)
                with self.assertRaises(cloud.ToolArgumentError):
                    cloud.valid_completion(response(command), guarded)
        cloud.valid_completion(response("python verify.py"), request)

    def test_narrow_step_checker_repair_pins_valid_action_without_steps_array(self):
        schema = {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["complete", "revise_check"]},
            "check_arguments": {"type": "object", "properties": {"cases": {"type": "array"}},
                                "required": ["cases"], "additionalProperties": False}},
            "required": ["action"], "additionalProperties": False}
        request = {**REQUEST, "tools": [{"type": "function", "function": {
            "name": "task_steps", "parameters": schema}}]}
        for action in ("revise_check", "unsupported_action"):
            with self.subTest(action=action):
                arguments = {"action": action, "check_arguments": {"cases": "bad array"}}
                response = {"choices": [{"message": {"tool_calls": [{"id": "bad-check", "function": {
                    "name": "task_steps", "arguments": json.dumps(arguments)}}]}}]}
                with self.assertRaises(cloud.ToolArgumentError) as caught:
                    cloud.valid_completion(response, request)
                guarded = cloud.argument_repair_request(request, caught.exception)
                repaired_schema = guarded["tools"][0]["function"]["parameters"]
                expected = ["revise_check"] if action == "revise_check" else ["complete", "revise_check"]
                self.assertEqual(repaired_schema["properties"]["action"]["enum"], expected)
                self.assertNotIn("steps", repaired_schema["properties"])
                self.assertEqual(schema["properties"]["action"]["enum"], ["complete", "revise_check"])
                if action == "revise_check":
                    from jsonschema import ValidationError, validate
                    validate({"action": "revise_check", "check_arguments": {"cases": []}}, repaired_schema)
                    with self.assertRaises(ValidationError):
                        validate({"action": "complete"}, repaired_schema)

    def test_local_step_repair_materializes_typed_branch_and_prunes_other_actions(self):
        schema = {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["complete", "revise_check"]},
            "step_id": {"type": "integer", "const": 7},
            "check_arguments": {"type": "object", "properties": {"cases": {"type": "array"}},
                                "required": ["cases"], "additionalProperties": False}},
            "required": ["action", "step_id"], "additionalProperties": False,
            "anyOf": [
                {"properties": {"action": {"const": "complete"}, "step_id": {}},
                 "required": ["action", "step_id"], "additionalProperties": False},
                {"properties": {"action": {"const": "revise_check"}, "step_id": {}, "check_arguments": {}},
                 "required": ["action", "step_id", "check_arguments"], "additionalProperties": False}]}
        request = {**REQUEST, "tools": [{"type": "function", "function": {
            "name": "task_steps", "parameters": schema}}]}
        original = json.loads(json.dumps(request))
        arguments = {"action": "revise_check", "step_id": 7, "check_arguments": {"cases": "bad array"}}
        response = {"choices": [{"message": {"tool_calls": [{"id": "bad-check", "function": {
            "name": "task_steps", "arguments": json.dumps(arguments)}}]}}]}
        with self.assertRaises(cloud.ToolArgumentError) as caught:
            cloud.valid_completion(response, request)
        guarded = cloud.argument_repair_request(request, caught.exception)
        guarded_before = json.loads(json.dumps(guarded))
        repair = cloud.local_json_repair_request(guarded)
        generated = repair["response_format"]["json_schema"]["schema"]
        self.assertNotIn("anyOf", generated)
        self.assertEqual(generated["properties"]["action"]["enum"], ["revise_check"])
        self.assertEqual(generated["properties"]["check_arguments"], schema["properties"]["check_arguments"])
        self.assertEqual(generated["properties"]["step_id"], {"type": "integer", "const": 7})
        self.assertEqual(generated["required"], ["action", "step_id", "check_arguments"])
        self.assertFalse(generated["additionalProperties"])
        from jsonschema import ValidationError, validate
        valid = {"action": "revise_check", "step_id": 7, "check_arguments": {"cases": []}}
        validate(valid, generated)
        validate(valid, guarded["tools"][0]["function"]["parameters"])
        for invalid in ({"action": "complete", "step_id": 7}, arguments,
                        {**valid, "step_id": 8}, {**valid, "extra": True}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    validate(invalid, generated)
                raw = {**ANSWER, "choices": [{"message": {"content": json.dumps(invalid)}}]}
                with self.assertRaises(cloud.ToolArgumentError):
                    cloud.validated_json_tool_reply(raw, guarded, repair)
        self.assertEqual(request, original)
        self.assertEqual(guarded, guarded_before)

    def test_json_repair_keeps_quoted_refusal_text_as_tool_data(self):
        guarded = {**REQUEST, "tools": [{"type": "function", "function": {"name": "save_quote",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"], "additionalProperties": False}}}],
            "tool_choice": {"type": "function", "function": {"name": "save_quote"}}}
        repair = cloud.local_json_repair_request(guarded)
        raw = {**ANSWER, "choices": [{**ANSWER["choices"][0], "message": {
            "role": "assistant", "content": json.dumps({"text": "I can't help with that."})}}]}
        result = cloud.validated_json_tool_reply(raw, guarded, repair)
        call = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"text": "I can't help with that."})
        self.assertIs(cloud.validated_json_tool_reply(REFUSAL, guarded, repair), REFUSAL)

    def test_schema_feedback_is_one_extra_call_across_all_local_candidates(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: 65536, "other": 65536}
        with (patch.object(router, "local_candidates", return_value=[cloud.LOCAL_MODEL, "other"]),
              patch.object(cloud, "post", return_value=argument_reply('[1,2]')) as post):
            with self.assertRaises(cloud.ToolArgumentError):
                router.complete(ARGUMENT_REQUEST)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[1]["model"] for call in post.call_args_list],
                         [cloud.LOCAL_MODEL, cloud.LOCAL_MODEL, "other"])

    def test_schema_feedback_does_not_overfill_context_or_repair_unparsed_json(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: cloud.estimated_context(ARGUMENT_REQUEST)}
        with patch.object(cloud, "post", return_value=argument_reply('[1,2]')) as post:
            with self.assertRaises(cloud.ToolArgumentError):
                router.complete(ARGUMENT_REQUEST)
        self.assertEqual(post.call_count, 1)
        router.model_contexts[cloud.LOCAL_MODEL] = 65536
        broken = argument_reply([1])
        broken["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{broken'
        with patch.object(cloud, "post", return_value=broken) as post:
            with self.assertRaises(json.JSONDecodeError):
                router.complete(ARGUMENT_REQUEST)
        self.assertEqual(post.call_count, 1)

    def test_schema_feedback_cancellation_stops_before_retry_or_fallback(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: 65536, "other": 65536}
        cancelled = False
        def invalid(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return argument_reply('[1,2]')
        with (patch.object(router, "local_candidates", return_value=[cloud.LOCAL_MODEL, "other"]),
              patch.object(cloud, "post", side_effect=invalid) as post):
            with self.assertRaises(InterruptedError):
                router.complete(ARGUMENT_REQUEST, cancel=lambda: cancelled)
        self.assertEqual(post.call_count, 1)

    def test_local_server_error_retries_same_payload_once_with_bounded_timeout(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        error = HTTPError("http://127.0.0.1:11434/v1/chat/completions", 500, "temporary", {}, None)
        with patch.object(cloud, "post", side_effect=[error, ANSWER]) as upstream:
            result = cloud.Router().complete(REQUEST, size_hint="quick")
        self.assertEqual(result, ANSWER)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(upstream.call_args_list[0].args, upstream.call_args_list[1].args)
        self.assertEqual(upstream.call_args_list[1].kwargs["timeout"], 60)

    def test_local_server_retry_is_bounded_and_does_not_retry_client_errors(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        for code, count in ((500, 2), (401, 1), (422, 1), (429, 1)):
            with self.subTest(code=code), patch.object(cloud, "post", side_effect=HTTPError(
                    "http://127.0.0.1:11434/v1/chat/completions", code, "failure", {}, None)) as upstream:
                with self.assertRaises(HTTPError):
                    cloud.Router().complete(REQUEST)
                self.assertEqual(upstream.call_count, count)

    def test_cancel_after_local_server_error_prevents_retry(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        cancelled = False
        def fail(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            raise HTTPError("http://127.0.0.1:11434/v1/chat/completions", 500, "temporary", {}, None)
        with patch.object(cloud, "post", side_effect=fail) as upstream:
            with self.assertRaises(InterruptedError):
                cloud.Router().complete(REQUEST, cancel=lambda: cancelled)
        self.assertEqual(upstream.call_count, 1)

    def test_schema_feedback_never_adds_local_calls_to_free_cloud_only(self):
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (patch.object(cloud_providers, "complete", return_value=(argument_reply('[1,2]'), 0.5)) as free,
              patch.object(cloud, "post") as local):
            with self.assertRaises(RuntimeError):
                cloud.Router().complete(ARGUMENT_REQUEST)
        free.assert_called_once()
        local.assert_not_called()

    def test_schema_feedback_bounds_output_and_diagnostic_data(self):
        request = {**ARGUMENT_REQUEST, "max_tokens": 20000, "max_completion_tokens": 20000}
        with self.assertRaises(cloud.ToolArgumentError) as failure:
            cloud.valid_completion(argument_reply('x' * 10000), request)
        repair = cloud.argument_repair_request(request, failure.exception)
        self.assertEqual(repair["max_tokens"], 4096)
        self.assertNotIn("max_completion_tokens", repair)
        self.assertLess(len(repair["messages"][-1]["content"]), 1500)
        self.assertEqual(request["max_tokens"], 20000)

    def test_schema_feedback_cannot_enable_disabled_tools_or_override_named_choice(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_contexts = {cloud.LOCAL_MODEL: 65536}
        for choice in ("none", {"type": "function", "function": {"name": "other"}}):
            request = {**ARGUMENT_REQUEST, "tool_choice": choice}
            with patch.object(cloud, "post", return_value=argument_reply('[1,2]')) as post:
                with self.assertRaises(cloud.ToolArgumentError):
                    router.complete(request)
            self.assertEqual(post.call_count, 1)

    def test_local_semantic_repair_uses_other_weights_and_keeps_hint_out_of_upstream_payload(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_weights = {cloud.LOCAL_MODEL: "a" * 64, cloud.FAST_MODEL: "a" * 64, "alternate": "b" * 64}
        with (patch.object(router, "local_candidates", return_value=[cloud.LOCAL_MODEL, cloud.FAST_MODEL, "alternate"]),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            self.assertEqual(router.complete(REQUEST, repair_avoid=[{"model": cloud.FAST_MODEL, "weights": "a" * 64}]), ANSWER)
        self.assertEqual(post.call_args.args[1]["model"], "alternate")
        self.assertNotIn("repair_avoid", post.call_args.args[1])
        self.assertEqual((router.last_local_model, router.last_local_weights), ("alternate", "b" * 64))

    def test_local_repair_keeps_deferred_models_as_api_failure_fallbacks(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        router = cloud.Router()
        router.model_weights = {cloud.LOCAL_MODEL: "a" * 64, "alternate": "b" * 64}
        with (patch.object(router, "local_candidates", return_value=[cloud.LOCAL_MODEL, "alternate"]),
              patch.object(cloud, "post", side_effect=[OSError("alternate unavailable"), ANSWER]) as post):
            self.assertEqual(router.complete(REQUEST, repair_avoid=[{"model": cloud.LOCAL_MODEL, "weights": "a" * 64}]), ANSWER)
        self.assertEqual([call.args[1]["model"] for call in post.call_args_list], ["alternate", cloud.LOCAL_MODEL])

    def test_repair_preference_respects_explicit_manual_model_and_free_cloud_only(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        (cloud.STATE / "model-choice.json").write_text('{"profile":"fast"}')
        router = cloud.Router()
        router.model_weights = {cloud.FAST_MODEL: "a" * 64, "alternate": "b" * 64}
        avoid = [{"model": cloud.FAST_MODEL, "weights": "a" * 64}]
        with (patch.object(router, "local_candidates", return_value=[cloud.FAST_MODEL, "alternate"]),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            router.complete(REQUEST, repair_avoid=avoid)
        self.assertEqual(post.call_args.args[1]["model"], cloud.FAST_MODEL)
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
              patch.object(cloud, "post") as local):
            router.complete(REQUEST, repair_avoid=avoid)
        free.assert_called_once()
        local.assert_not_called()
        self.assertIsNone(router.last_local_model)

    def test_free_request_override_never_uses_paid_or_changes_global_mode(self):
        cloud.routing_mode.save_scope(cloud.STATE, "both")
        cloud.routing_mode.save(cloud.STATE, "floor_it")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (patch("chatgpt_auth.status", return_value={"enabled": True}),
              patch("chatgpt_auth.complete") as paid,
              patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.2)) as free,
              patch.object(cloud, "post") as local):
            self.assertEqual(cloud.Router().complete(REQUEST, scope_override="free_cloud_only"), ANSWER)
        paid.assert_not_called()
        local.assert_not_called()
        free.assert_called_once()
        self.assertEqual(cloud.routing_mode.load_scope(cloud.STATE), "both")

    def test_free_request_override_cannot_bypass_local_only_or_fall_back(self):
        cloud.routing_mode.save_scope(cloud.STATE, "local_only")
        with patch("chatgpt_auth.complete") as paid, patch.object(cloud, "post") as inference:
            with self.assertRaisesRegex(RuntimeError, "blocked"):
                cloud.Router().complete(REQUEST, scope_override="free_cloud_only")
        paid.assert_not_called()
        inference.assert_not_called()
        cloud.routing_mode.save_scope(cloud.STATE, "both")
        with patch("chatgpt_auth.complete") as paid, patch.object(cloud, "post") as inference:
            with self.assertRaisesRegex(RuntimeError, "Free cloud only"):
                cloud.Router().complete(REQUEST, scope_override="free_cloud_only")
        paid.assert_not_called()
        inference.assert_not_called()

    def test_free_cloud_only_uses_connected_free_model_and_keeps_tool_calls(self):
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        request = {**REQUEST, "tools": [{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object", "properties": {}}}}]}
        with (patch("chatgpt_auth.status", return_value={"enabled": True}),
              patch("chatgpt_auth.complete") as paid,
              patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
              patch.object(cloud, "post") as local):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertEqual(free.call_args.args[2]["tools"], request["tools"])
        paid.assert_not_called()
        local.assert_not_called()

    def test_free_cloud_only_never_spends_paid_or_uses_local_when_unavailable(self):
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        with (patch("chatgpt_auth.status", return_value={"enabled": True}),
              patch("chatgpt_auth.complete") as paid,
              patch.object(cloud, "post") as local):
            with self.assertRaisesRegex(RuntimeError, "Free cloud only is selected"):
                cloud.Router().complete(REQUEST)
        paid.assert_not_called()
        local.assert_not_called()

    def test_free_cloud_only_does_not_retry_refusal_on_local_model(self):
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (patch.object(cloud_providers, "complete", return_value=(REFUSAL, 0.5)),
              patch("chatgpt_auth.complete") as paid,
              patch.object(cloud, "post") as local):
            with self.assertRaisesRegex(RuntimeError, "Free cloud only is selected"):
                cloud.Router().complete(REQUEST)
        paid.assert_not_called()
        local.assert_not_called()

    def test_cloud_only_does_not_fall_back_to_local(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(cloud, "STATE", Path(directory)),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("chatgpt_auth.status", return_value={"enabled": False}),
              patch.object(cloud, "post") as post):
            cloud.routing_mode.save_scope(Path(directory), "cloud_only")
            with self.assertRaisesRegex(RuntimeError, "Cloud only is selected"):
                cloud.Router().complete(REQUEST)
            self.assertFalse(any(call.args[0].startswith("http://127.0.0.1:11434")
                                 for call in post.call_args_list))

    def test_cloud_only_uses_connected_free_provider_even_when_local_is_faster(self):
        cloud.routing_mode.save_scope(cloud.STATE, "cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (patch("usage_history.recent_local_speed", return_value=500),
              patch("usage_history.recent_provider_speed", return_value=10),
              patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
              patch.object(cloud, "post") as local):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        free.assert_called_once()
        local.assert_not_called()

    def test_cloud_only_medium_uses_enabled_plan_when_no_free_route_exists(self):
        cloud.routing_mode.save_scope(cloud.STATE, "cloud_only")
        cloud.routing_mode.save(cloud.STATE, "medium")
        with (patch("usage_history.recent_local_speed", return_value=500),
              patch("chatgpt_auth.status", return_value={"enabled": True}),
              patch("chatgpt_auth.complete", return_value=ANSWER) as paid,
              patch.object(cloud, "post") as local):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        paid.assert_called_once()
        local.assert_not_called()

    def test_task_route_reads_live_project_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LongTasks").mkdir()
            with sqlite3.connect(root / "LongTasks/projects.db") as conn:
                conn.execute("CREATE TABLE projects(id TEXT, ai_route TEXT)")
                conn.execute("INSERT INTO projects VALUES (?,?)", ("a" * 12, "local_only"))
            conn.close()
            with patch.object(cloud, "ROOT", root):
                self.assertEqual(cloud.task_route("a" * 12), "local_only")
                self.assertEqual(cloud.task_route("b" * 12), "automatic")
                self.assertEqual(cloud.task_route("invalid"), "automatic")

    def test_speed_estimate_uses_the_expected_local_model_tier(self):
        self.assertEqual(cloud.local_speed_models("quick"), (cloud.FAST_MODEL,))
        balanced = cloud.local_speed_models("balanced")
        self.assertIn(cloud.BALANCED_MODEL, balanced)
        self.assertNotIn(cloud.FAST_MODEL, balanced)
        self.assertIn(cloud.LOCAL_MODEL, cloud.local_speed_models("deep"))
        with (patch("usage_history.recent_local_speed", return_value=30) as speed,
              patch.object(cloud, "post", return_value=ANSWER)):
            cloud.Router().complete(REQUEST)
        speed.assert_called_once_with(70, models=(cloud.FAST_MODEL,))

    def test_approved_new_balanced_model_contributes_to_speed_estimate(self):
        candidate = {"alias": "cale-qwen36", "download_gb": 23, "candidate": True}
        with (patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch.dict(cloud.CATALOG, {"new_balanced": candidate})):
            self.assertIn("cale-qwen36", cloud.local_speed_models("balanced"))

    def test_provider_key_requires_free_plan_and_is_encrypted(self):
        with self.assertRaisesRegex(ValueError, "free plan"):
            cloud.save_provider_key("groq", "gsk_test_secret_value")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        self.assertEqual(cloud.credentials()["provider_keys"]["groq"], "gsk_test_secret_value")
        self.assertNotIn(b"gsk_test_secret_value", (cloud.STATE / "credentials.dpapi").read_bytes())
        cloud.disconnect_provider("groq")
        self.assertNotIn("groq", cloud.credentials()["provider_keys"])

    def test_cloudflare_requires_account_id_and_caps_neuron_budget(self):
        with self.assertRaisesRegex(ValueError, "account ID"):
            cloud.save_provider_key("cloudflare", "cloudflare-test-secret", True)
        account_id = "a" * 32
        cloud.save_provider_key("cloudflare", "cloudflare-test-secret", True,
                                account_id=account_id)
        self.assertEqual(cloud.credentials()["provider_accounts"]["cloudflare"], account_id)
        cost = cloud_providers.estimated_neurons("cloudflare", REQUEST)
        self.assertGreater(cost, 0)
        router = cloud.Router()
        self.assertFalse(router.reserve_provider_request("cloudflare", 8501))
        self.assertTrue(router.reserve_provider_request("cloudflare", cost))
        self.assertEqual(json.loads((cloud.STATE / "provider-quota.json").read_text())
                         ["neurons"]["cloudflare"], cost)
        cloud.disconnect_provider("cloudflare")
        self.assertNotIn("cloudflare", cloud.credentials()["provider_accounts"])

    def test_aion_reserves_free_tier_token_budget(self):
        request = dict(REQUEST, max_tokens=9999, tool_choice="required")
        body = cloud_providers.request_body("aion", request)
        self.assertEqual(body["model"], "aion-labs/aion-3.5-mini")
        self.assertEqual(body["max_tokens"], 2048)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertNotIn("tool_choice", body)
        cost = cloud_providers.estimated_tokens("aion", request)
        self.assertGreater(cost, 2048)
        router = cloud.Router()
        self.assertFalse(router.reserve_provider_request("aion", estimated_tokens=10001))
        self.assertTrue(router.reserve_provider_request("aion", estimated_tokens=cost))
        self.assertEqual(json.loads((cloud.STATE / "provider-quota.json").read_text())
                         ["tokens"]["aion"], cost)

    def test_cloudflare_endpoint_uses_validated_account_id(self):
        with self.assertRaisesRegex(ValueError, "account ID"):
            cloud_providers.complete("cloudflare", "secret", REQUEST,
                                     account_id="../another-account")
        with patch.object(cloud_providers, "urlopen", side_effect=OSError("stop")) as fetch:
            with self.assertRaises(OSError):
                cloud_providers.complete("cloudflare", "secret", REQUEST,
                                         account_id="b" * 32)
        self.assertEqual(fetch.call_args.args[0].full_url,
                         "https://api.cloudflare.com/client/v4/accounts/" + "b" * 32 +
                         "/ai/v1/chat/completions")

    def test_task_attribution_requires_managed_worker_socket(self):
        project_id = "a" * 12
        folder = cloud.ROOT / "Projects" / project_id
        folder.mkdir(parents=True)
        socket = SimpleNamespace(pid=123, laddr=SimpleNamespace(port=45678),
                                 raddr=SimpleNamespace(port=cloud.PORT))
        process = SimpleNamespace(cmdline=lambda: ["python", "-m", "agent.managed_worker"],
                                  cwd=lambda: str(folder), parents=lambda: [])
        with (patch("psutil.net_connections", return_value=[socket]),
              patch("psutil.Process", return_value=process)):
            self.assertEqual(cloud.task_for_client(45678), project_id)
            self.assertEqual(cloud.task_for_client(99999), "")

    def test_connected_fast_provider_handles_balanced_task_before_local(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        request = dict(REQUEST, messages=[{"role": "user", "content": "Build a working website"}])
        with (patch.object(cloud, "task_size", return_value="balanced"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=25),
              patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as remote,
              patch.object(cloud, "post") as local):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        remote.assert_called_once()
        local.assert_not_called()

    def test_measured_provider_speed_changes_cloud_order(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        cloud.save_provider_key("bigmodel", "bigmodel-test-secret", True)
        request = dict(REQUEST, messages=[{"role": "user", "content": "Build a working website"}])
        with (patch.object(cloud, "task_size", return_value="balanced"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=25),
              patch("usage_history.recent_provider_speed",
                    side_effect=lambda name, default: {"groq": 8, "bigmodel": 200}[name]),
              patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as remote,
              patch.object(cloud, "post") as local):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertEqual(remote.call_args.args[0], "bigmodel")
        local.assert_not_called()

    def test_offline_switch_keeps_connected_provider_local(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (patch.object(cloud.pc_offline, "offline", return_value=True),
              patch.object(cloud_providers, "complete") as remote,
              patch.object(cloud, "post", return_value=ANSWER)):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        remote.assert_not_called()

    def test_quick_work_stays_local_if_cloud_latency_would_dominate(self):
        cloud.save_provider_key("google", "test_secret_value_123", True)
        with (patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=80),
              patch.object(cloud_providers, "complete") as remote,
              patch.object(cloud, "post", return_value=ANSWER)):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        remote.assert_not_called()

    def test_provider_failure_falls_back_to_local(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        request = dict(REQUEST, messages=[{"role": "user", "content": "Build a working website"}])
        with (patch.object(cloud, "task_size", return_value="balanced"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=25),
              patch.object(cloud_providers, "complete", side_effect=OSError("unavailable")) as remote,
              patch.object(cloud, "post", return_value=ANSWER) as local):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        remote.assert_called_once()
        local.assert_called_once()

    def test_provider_payload_cannot_forward_untrusted_model_or_paid_plugins(self):
        request = dict(REQUEST, model="paid/model", models=["paid/model"],
                       plugins=[{"id": "web"}], provider={"max_price": 100})
        body = cloud_providers.request_body("groq", request)
        self.assertEqual(body["model"], cloud_providers.PROVIDERS["groq"]["model"])
        self.assertNotIn("plugins", body)
        self.assertNotIn("models", body)
        self.assertNotIn("provider", body)

    def test_new_free_provider_routes_use_fixed_models_and_endpoints(self):
        expected = {
            "sambanova": ("https://api.sambanova.ai/v1/chat/completions",
                          "Meta-Llama-3.3-70B-Instruct"),
            "bigmodel": ("https://open.bigmodel.cn/api/paas/v4/chat/completions",
                         "glm-4.7-flash"),
            "modelscope": ("https://api-inference.modelscope.cn/v1/chat/completions",
                           "Qwen/Qwen3.5-35B-A3B"),
            "aion": ("https://api.aionlabs.ai/v1/chat/completions",
                     "aion-labs/aion-3.5-mini"),
            "merkium": ("https://chat.merkium.nl/api/v1/chat/completions",
                         "ganymede-1.1"),
        }
        for provider, (url, model) in expected.items():
            with self.subTest(provider=provider):
                request = dict(REQUEST, model="paid-model", max_tokens=999999)
                body = cloud_providers.request_body(provider, request)
                self.assertEqual(body["model"], model)
                self.assertLessEqual(body["max_tokens"], 8192)
                with patch.object(cloud_providers, "urlopen", side_effect=OSError("offline")) as fetch:
                    with self.assertRaises(OSError):
                        cloud_providers.complete(provider, "test-key-value", request)
                sent = fetch.call_args.args[0]
                self.assertEqual(sent.full_url, url)
                self.assertEqual(json.loads(sent.data)["model"], model)

    def test_merkium_keeps_tools_and_stays_within_its_free_model_contract(self):
        request = {**REQUEST, "max_tokens": 9000, "seed": 42,
                   "response_format": {"type": "json_object"},
                   "parallel_tool_calls": True,
                   "tools": [{"type": "function", "function": {"name": "read_file"}}]}
        body = cloud_providers.request_body("merkium", request)
        self.assertEqual(body["model"], "ganymede-1.1")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["tools"], request["tools"])
        for unsupported in ("seed", "response_format", "parallel_tool_calls"):
            self.assertNotIn(unsupported, body)
        self.assertEqual(cloud_providers.PROVIDERS["merkium"]["rpm"], 5)

    def test_cerebras_trial_credit_is_not_a_recurring_free_route(self):
        with self.assertRaisesRegex(ValueError, "no longer has a recurring free route"):
            cloud.save_provider_key("cerebras", "trial-only-test-key", True)
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        with (patch.object(cloud, "credentials", return_value={
                  "provider_keys": {"cerebras": "previously-saved-key"},
                  "free_confirmed": {"cerebras": True}}),
              patch.object(cloud_providers, "complete") as remote):
            with self.assertRaisesRegex(RuntimeError, "Free cloud only is selected"):
                cloud.Router().complete(REQUEST)
        remote.assert_not_called()

    def test_hugging_face_has_small_monthly_budget_and_short_output(self):
        request = dict(REQUEST, max_tokens=8192)
        body = cloud_providers.request_body("huggingface", request)
        self.assertEqual(body["max_tokens"], 1024)
        router = cloud.Router()
        self.assertTrue(router.reserve_provider_request("huggingface"))
        router.provider_last_attempts.clear()
        self.assertTrue(router.reserve_provider_request("huggingface"))
        router.provider_last_attempts.clear()
        self.assertFalse(router.reserve_provider_request("huggingface"))

    def test_provider_availability_check_does_not_spend_a_free_request(self):
        router = cloud.Router()
        quota = cloud.STATE / "provider-quota.json"
        self.assertTrue(router.provider_request_available("groq"))
        self.assertFalse(quota.exists())
        self.assertTrue(router.reserve_provider_request("groq"))
        saved = quota.read_bytes()
        router.provider_last_attempts.clear()
        self.assertTrue(router.provider_request_available("groq"))
        self.assertEqual(quota.read_bytes(), saved)

    def test_openrouter_availability_check_preserves_daily_allowance(self):
        router = cloud.Router()
        quota = cloud.STATE / "quota.json"
        self.assertTrue(router.free_request_available())
        self.assertFalse(quota.exists())
        self.assertTrue(router.reserve_free_request())
        saved = quota.read_bytes()
        router.last_attempt = 0
        self.assertTrue(router.free_request_available())
        self.assertEqual(quota.read_bytes(), saved)

    def test_provider_adapter_uses_fixed_endpoint_and_normalizes_cohere(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return json.dumps({"id": "co-test", "model": "command-a-plus-05-2026",
                    "message": {"content": [{"type": "text", "text": "hello"}]},
                    "usage": {"billed_units": {"input_tokens": 3,
                                               "output_tokens": 1}}}).encode()

        with patch.object(cloud_providers, "urlopen", return_value=Response()) as fetch:
            result, elapsed = cloud_providers.complete("cohere", "private-test-key", REQUEST)
        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        self.assertEqual(result["usage"]["prompt_tokens"], 3)
        self.assertEqual(json.loads(fetch.call_args.args[0].data)["model"],
                         "command-a-plus-05-2026")
        self.assertEqual(fetch.call_args.args[0].full_url,
                         "https://api.cohere.com/v2/chat")
        self.assertEqual(fetch.call_args.args[0].get_header("Authorization"),
                         "Bearer private-test-key")
        self.assertGreaterEqual(elapsed, 0)

    def test_missing_required_tool_call_falls_back_to_next_local_model(self):
        request = dict(
            REQUEST,
            tools=[{"type": "function", "function": {"name": "multiply"}}],
            tool_choice="required",
        )
        tool_answer = {
            **ANSWER,
            "choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function",
                                "function": {"name": "multiply", "arguments": '{"a":7,"b":13}'}}]}}],
        }
        with (
            patch.object(cloud.Router, "local_candidates", return_value=["small", "strong"]),
            patch.object(cloud, "post", side_effect=[ANSWER, tool_answer]) as post,
        ):
            self.assertEqual(cloud.Router().complete(request), tool_answer)
        self.assertEqual([call.args[1]["model"] for call in post.call_args_list], ["small", "strong"])

    def test_repeated_tool_errors_add_repair_instruction_to_local_request(self):
        request = dict(REQUEST, messages=[
            {"role": "user", "content": "Build an app"},
            *({"role": "tool", "content": '{"isError":true,"content":[]}'} for _ in range(3)),
        ])
        with (patch.object(cloud.pc_offline, "offline", return_value=True),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        sent = post.call_args.args[1]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("Do not repeat", sent[0]["content"])
        self.assertEqual(request["messages"][0]["role"], "user")

    def test_unoffered_tool_call_is_rejected_and_dict_arguments_are_normalized(self):
        request = dict(REQUEST, tools=[{"type": "function", "function": {"name": "multiply"}}])
        wrong = {"choices": [{"message": {"tool_calls": [
            {"id": "call-1", "function": {"name": "erase", "arguments": "{}"}}
        ]}}]}
        with self.assertRaisesRegex(ValueError, "not offered"):
            cloud.valid_completion(wrong, request)
        correct = {"choices": [{"message": {"tool_calls": [
            {"id": "call-2", "function": {"name": "multiply", "arguments": {"a": 7, "b": 13}}}
        ]}}]}
        result = cloud.valid_completion(correct, request)
        self.assertEqual(json.loads(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]),
                         {"a": 7, "b": 13})

    def test_bad_tool_arguments_retry_another_local_model(self):
        request = dict(REQUEST, tools=[{"type": "function", "function": {
            "name": "read_file", "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        }}], tool_choice="required")

        def response(arguments):
            return {"choices": [{"message": {"tool_calls": [{
                "id": "call-1", "type": "function", "function": {
                    "name": "read_file", "arguments": arguments,
                },
            }]}}]}

        with self.assertRaisesRegex(ValueError, "path"):
            cloud.valid_completion(response("{}"), request)
        with self.assertRaisesRegex(ValueError, "string"):
            cloud.valid_completion(response('{"path": 7}'), request)
        with (
            patch.object(cloud.Router, "local_candidates", return_value=["small", "strong"]),
            patch.object(cloud, "post", side_effect=[response("{}"),
                                                    response('{"path":"C:/file.txt"}')]) as post,
        ):
            cloud.Router().complete(request)
        self.assertEqual([call.args[1]["model"] for call in post.call_args_list],
                         ["small", "strong"])

    def test_named_tool_choice_requires_the_selected_tool(self):
        request = dict(REQUEST, tools=[
            {"type": "function", "function": {"name": "multiply"}},
            {"type": "function", "function": {"name": "divide"}},
        ], tool_choice={"type": "function", "function": {"name": "multiply"}})
        other = {"choices": [{"message": {"tool_calls": [
            {"id": "call-1", "function": {"name": "divide", "arguments": "{}"}}
        ]}}]}
        with self.assertRaisesRegex(ValueError, "Required tool call"):
            cloud.valid_completion(other, request)

    def test_request_size_keeps_short_work_on_small_model(self):
        short = {"messages": [{"role": "user", "content": "Hello"}], "tools": []}
        long = {"messages": [{"role": "user", "content": "x" * 300_000}], "tools": []}
        self.assertLess(cloud.estimated_context(short), 65536)
        self.assertGreater(cloud.estimated_context(long), 65536)

    def test_image_payload_uses_vision_budget_instead_of_encoded_byte_count(self):
        image = "data:image/png;base64," + "A" * 1_000_000
        request = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image_url", "image_url": {"url": image}},
        ]}], "tools": []}
        self.assertLess(cloud.estimated_context(request), 65536)
        self.assertLess(cloud.estimated_input_tokens(request), 16384)

    def test_many_images_and_long_text_still_require_a_large_window(self):
        image = "data:image/jpeg;base64," + "A" * 100_000
        request = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "x" * 100_000},
            *({"type": "image_url", "image_url": {"url": image}} for _ in range(4)),
        ]}], "tools": []}
        self.assertGreater(cloud.estimated_context(request), 65536)

    def test_image_request_is_detected_without_scanning_tools(self):
        image = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}]}
        self.assertTrue(cloud.request_needs_vision(image))
        self.assertFalse(cloud.request_needs_vision({"messages": [
            {"role": "user", "content": "Describe an image"}],
            "tools": [{"description": "Accepts an image_url parameter"}],
        }))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = patch.object(cloud, "STATE", Path(self.temp.name))
        self.state.start()
        self.root = patch.object(cloud, "ROOT", Path(self.temp.name))
        self.root.start()
        self.routing_db = patch.object(
            cloud.routing_memory, "DATABASE", Path(self.temp.name) / "routing.db"
        )
        self.routing_db.start()
        self.candidates = patch.object(
            cloud.Router, "local_candidates", return_value=[cloud.LOCAL_MODEL]
        )
        self.candidates.start()
        self.loaded = patch.object(cloud, "loaded_local_models", return_value=[])
        self.loaded.start()
        # Routing tests must not inherit live speed samples from this PC.
        # Benchmark runs can retire a slow model while this suite is running.
        self.model_speed = patch("model_fitness.measured_speed", return_value=None)
        self.model_speed.start()
        self.ram = patch("model_library.total_ram_gb", return_value=128)
        self.ram.start()
        self.vram = patch("model_fitness.gpu_vram_gb", return_value=12)
        self.vram.start()
        # An installed Workbench may have a real ChatGPT connection and an
        # offline switch. Never let tests route to that account or inherit its
        # machine state: the fake upstreams must be the only possible routes.
        self.plan = patch("chatgpt_auth.status", return_value={"enabled": False})
        self.plan.start()
        self.offline = patch.object(cloud.pc_offline, "offline", return_value=False)
        self.offline.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.state.stop)
        self.addCleanup(self.root.stop)
        self.addCleanup(self.routing_db.stop)
        self.addCleanup(self.candidates.stop)
        self.addCleanup(self.loaded.stop)
        self.addCleanup(self.model_speed.stop)
        self.addCleanup(self.ram.stop)
        self.addCleanup(self.vram.stop)
        self.addCleanup(self.plan.stop)
        self.addCleanup(self.offline.stop)

    def test_native_sized_transport_preserves_history_and_actual_usage(self):
        router = cloud.Router()
        router.model_native_contexts = {cloud.LOCAL_MODEL: 262144}
        router.native_version_ready = True
        router.native_version_until = time.monotonic() + 60
        payload = {**REQUEST, "max_tokens": 80, "messages": [
            {"role": "user", "content": "Return the checked answer."}]}
        original = json.loads(json.dumps(payload))
        seen = []
        def upstream(url, body, **kwargs):
            seen.append((url, body))
            if url.endswith("/api/chat"):
                return {"model": cloud.LOCAL_MODEL, "done": True,
                        "message": {"role": "assistant", "content": "checked"},
                        "prompt_eval_count": 12, "eval_count": 3, "eval_duration": 300000000}
            return ANSWER
        with patch.object(cloud, "post", side_effect=upstream), patch("usage_history.record_completion") as usage:
            result = router.complete(payload)
        self.assertEqual(seen[0][0], "http://127.0.0.1:11434/api/chat")
        body = seen[0][1]
        self.assertEqual(body["messages"], payload["messages"])
        self.assertLess(body["options"]["num_ctx"], 262144)
        self.assertGreaterEqual(body["options"]["num_ctx"], cloud.estimated_input_tokens(payload) + 80 + 1024)
        self.assertIs(body["truncate"], False)
        self.assertIs(body["shift"], False)
        self.assertEqual(body["options"]["num_predict"], 80)
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(result["eval_duration"], 300000000)
        usage.assert_called_once()
        self.assertEqual(payload, original)

    def test_native_transport_unknown_version_or_features_preserves_legacy_request(self):
        for version, extra in ((False, {}), (True, {"reasoning_effort": "high"}),
                               (True, {"tool_choice": "required"}), (True, {"max_tokens": None})):
            with self.subTest(version=version, extra=extra):
                router = cloud.Router()
                router.model_native_contexts = {cloud.LOCAL_MODEL: 262144}
                router.native_version_ready = version
                router.native_version_until = time.monotonic() + 60
                with patch.object(cloud, "post", return_value=ANSWER) as upstream:
                    if extra.get("tool_choice") == "required":
                        with self.assertRaisesRegex(ValueError, "Required tool call"):
                            router.complete({**REQUEST, "max_tokens": 80, **extra})
                    else:
                        router.complete({**REQUEST, "max_tokens": 80, **extra})
                self.assertTrue(upstream.call_args.args[0].endswith("/v1/chat/completions"))
                for key, value in extra.items():
                    self.assertEqual(upstream.call_args.args[1].get(key), value)

    def test_native_transport_version_probe_and_retry_are_bounded(self):
        router = cloud.Router()
        router.model_native_contexts = {cloud.LOCAL_MODEL: 262144}
        raw = {"model": cloud.LOCAL_MODEL, "done": True,
               "message": {"role": "assistant", "content": "checked"}}
        with (patch.object(cloud, "urlopen", return_value=io.BytesIO(b'{"version":"0.40.13"}')) as version,
              patch.object(cloud, "post", side_effect=[HTTPError("local", 503, "busy", {}, None), raw]) as upstream,
              patch("usage_history.record_completion") as usage):
            result = router.complete({**REQUEST, "max_tokens": 80})
        version.assert_called_once_with("http://127.0.0.1:11434/api/version", timeout=3)
        self.assertEqual(upstream.call_count, 2)
        self.assertTrue(all(call.args[0].endswith("/api/chat") for call in upstream.call_args_list))
        self.assertEqual(upstream.call_args_list[0].args, upstream.call_args_list[1].args)
        self.assertNotIn("usage", result)
        usage.assert_called_once()

    def test_native_actual_usage_survives_invalid_reply_without_a_fake_success(self):
        router = cloud.Router()
        router.model_native_contexts = {cloud.LOCAL_MODEL: 262144}
        router.native_version_ready = True
        router.native_version_until = time.monotonic() + 60
        raw = {"model": cloud.LOCAL_MODEL, "done": True, "prompt_eval_count": 9,
               "eval_count": 7, "eval_duration": 800000000,
               "message": {"role": "assistant", "tool_calls": [{"function": {"name": "lookup", "arguments": "malformed"}}]}}
        with patch.object(cloud, "post", return_value=raw), patch("usage_history.record_completion") as usage:
            with self.assertRaisesRegex(ValueError, "Invalid native Ollama tool arguments"):
                router.complete({**REQUEST, "max_tokens": 80})
        usage.assert_called_once()
        self.assertEqual(usage.call_args.args[0]["usage"]["total_tokens"], 16)
        self.assertEqual(usage.call_args.args[0]["eval_duration"], 800000000)

    def test_native_failure_does_not_switch_to_legacy_transport(self):
        router = cloud.Router()
        router.model_native_contexts = {cloud.LOCAL_MODEL: 262144}
        router.native_version_ready = True
        router.native_version_until = time.monotonic() + 60
        with (patch.object(router, "local_candidates", return_value=[cloud.LOCAL_MODEL, "other-local"]),
              patch.object(cloud, "post", side_effect=HTTPError("local", 400, "context overflow", {}, None)) as upstream):
            with self.assertRaisesRegex(RuntimeError, "legacy transport was not attempted"):
                router.complete({**REQUEST, "max_tokens": 80})
        self.assertTrue(upstream.called)
        self.assertTrue(all(call.args[0].endswith("/api/chat") for call in upstream.call_args_list))

    def test_native_optional_metadata_cannot_hide_existing_vision_thinking(self):
        self.candidates.stop()
        alias = "cale-code7"
        tags = {"models": [{"name": alias+":latest", "size": 5*2**30}]}
        for metadata in (None, [], {}):
            with (self.subTest(metadata=metadata),
                  patch.object(cloud, "post", return_value={"capabilities": ["tools", "vision", "thinking"],
                        "parameters": "num_ctx 32768\n", "details": {"parameter_size": "7.6B"}, "model_info": metadata}),
                  patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
                  patch.object(cloud, "candidate_approved", return_value=True), patch.object(cloud, "refresh_catalog")):
                router = cloud.Router()
                self.assertEqual(router.local_candidates("quick", required_context=16000), [alias])
                self.assertIn(alias, router.model_vision)
                self.assertIn(alias, router.model_thinking)
                self.assertNotIn(alias, router.model_native_contexts)

    def test_native_version_probe_cancellation_prevents_inference(self):
        router = cloud.Router()
        router.model_native_contexts = {cloud.LOCAL_MODEL: 262144}
        cancelled = False
        def version(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return io.BytesIO(b'{"version":"0.40.13"}')
        with patch.object(cloud, "urlopen", side_effect=version), patch.object(cloud, "post") as inference:
            with self.assertRaises(InterruptedError):
                router.complete({**REQUEST, "max_tokens": 80}, cancel=lambda: cancelled)
        inference.assert_not_called()

    def test_native_32k_candidate_only_serves_requests_that_fit(self):
        self.candidates.stop()
        alias = "cale-code7"
        tags = {"models": [{"name": alias+":latest", "size": 5*2**30}]}
        with (patch.object(cloud, "post", return_value={"capabilities": ["tools"],
                    "parameters": "num_ctx 32768\n", "details": {"parameter_size": "7.6B"}}),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch.object(cloud, "refresh_catalog")):
            router = cloud.Router()
            self.assertEqual(router.local_candidates("quick", required_context=16000), [alias])
            self.assertEqual(router.local_candidates("quick", required_context=40000), [])

    def test_loaded_balanced_model_avoids_unnecessary_model_swap(self):
        self.candidates.stop()
        sizes = {cloud.FAST_MODEL: "9B", cloud.BALANCED_MODEL: "35B",
                 cloud.LOCAL_MODEL: "79B", cloud.STANDARD_MAIN_ALIAS: "79B"}
        tags = {"models": [
            {"name": alias + ":latest", "size": weight * 2**30}
            for alias, weight in ((cloud.FAST_MODEL, 6), (cloud.BALANCED_MODEL, 22),
                                  (cloud.LOCAL_MODEL, 52), (cloud.STANDARD_MAIN_ALIAS, 52))
        ]}

        def show(_url, payload, **_kwargs):
            return {"capabilities": ["tools"], "parameters":
                    "num_ctx 65536\n" if payload["model"] != cloud.LOCAL_MODEL
                    else "num_ctx 262144\n",
                    "details": {"parameter_size": sizes[payload["model"]]}}

        with (patch.object(cloud, "post", side_effect=show),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "loaded_local_models", return_value=[cloud.BALANCED_MODEL])):
            router = cloud.Router()
            self.assertEqual(router.local_candidates("quick")[0], cloud.FAST_MODEL)
            self.assertEqual(router.local_candidates("balanced")[0], cloud.BALANCED_MODEL)
            self.assertNotIn(cloud.STANDARD_MAIN_ALIAS, router.local_candidates("deep"))
            self.assertEqual(router.local_candidates("deep", required_context=100000), [])

    def test_loaded_model_lookup_excludes_unloading_runner(self):
        now = datetime.now(timezone.utc)
        response = {"models": [
            {"name": "cale-qwen35:latest", "expires_at": (now + timedelta(minutes=4)).isoformat()},
            {"name": "qwen3-coder-local:latest", "expires_at": (now - timedelta(minutes=1)).isoformat()},
        ]}
        self.loaded.stop()
        with patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            self.assertEqual(cloud.loaded_local_models(), ["cale-qwen35"])

    def test_image_routing_only_uses_models_advertising_vision(self):
        self.candidates.stop()
        text_only = cloud.CATALOG["gptoss_official"]["alias"]
        tags = {"models": [
            {"name": cloud.FAST_MODEL + ":latest", "size": 6 * 2**30},
            {"name": text_only + ":latest", "size": 13 * 2**30},
        ]}

        def show(_url, payload, **_kwargs):
            name = payload["model"]
            if name not in (cloud.FAST_MODEL, text_only):
                raise OSError("not installed")
            return {"capabilities": (["completion", "tools", "vision"]
                                     if name == cloud.FAST_MODEL else ["completion", "tools"]),
                    "parameters": "num_ctx 65536\n",
                    "details": {"parameter_size": "9B" if name == cloud.FAST_MODEL else "20B"}}

        with (patch.object(cloud, "post", side_effect=show),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch("model_fitness.suitable", return_value=True),
              patch.object(cloud, "model_order", side_effect=lambda _size, available, *_, **__: list(available)),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "loaded_local_models", return_value=[])):
            router = cloud.Router()
            self.assertIn(text_only, router.local_candidates("quick"))
            self.assertEqual(router.local_candidates("quick", require_vision=True),
                             [cloud.FAST_MODEL])

    def test_fallback_only_model_can_be_chosen_manually_but_not_auto_routed(self):
        self.candidates.stop()
        alias = cloud.CATALOG["permissive_small"]["alias"]
        tags = {"models": [{"name": alias + ":latest", "size": 6 * 2**30}]}
        info = {"capabilities": ["completion", "tools"],
                "parameters": "num_ctx 65536\n",
                "details": {"parameter_size": "9B"}}
        with (patch.object(cloud, "post", return_value=info),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch("model_fitness.suitable", return_value=True),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "loaded_local_models", return_value=[])):
            router = cloud.Router()
            self.assertNotIn(alias, router.local_candidates("quick"))
            self.assertIn(alias, router.local_candidates("manual", include_fallback=True))

    def connected(self):
        data = cloud.credentials()
        data["cloud_key"] = "sk-or-test-secret"
        cloud.save_credentials(data)

    def test_direct_free_provider_precedes_openrouter_for_deep_task(self):
        self.connected()
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        request = dict(REQUEST, messages=[{"role": "user", "content": "Build and test an app"}])
        with (patch.object(cloud, "task_size", return_value="deep"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("chatgpt_auth.status", return_value={"enabled": False}),
              patch("usage_history.recent_local_speed", return_value=15),
              patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as direct,
              patch.object(cloud, "post") as post,
              patch.object(cloud.Router, "reserve_free_request") as reserve):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertEqual(direct.call_args.args[0], "groq")
        reserve.assert_not_called()
        post.assert_not_called()

    def test_image_request_skips_faster_text_only_free_provider(self):
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        cloud.save_provider_key("google", "google-test-secret-value", True)
        request = dict(REQUEST, messages=[{"role": "user", "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}])
        with (patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as direct,
              patch.object(cloud, "post") as local,
              patch("usage_history.recent_provider_speed",
                    side_effect=lambda provider, _default: {"groq": 900, "google": 100}[provider])):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertEqual(direct.call_args.args[0], "google")
        local.assert_not_called()

    def test_image_request_does_not_use_text_only_free_provider(self):
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        request = dict(REQUEST, messages=[{"role": "user", "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}])
        with patch.object(cloud_providers, "complete") as direct:
            with self.assertRaisesRegex(RuntimeError, "Free cloud only is selected"):
                cloud.Router().complete(request)
        direct.assert_not_called()

    def test_slow_long_balanced_task_tries_connected_free_route(self):
        self.connected()
        request = dict(REQUEST, messages=[{"role": "user", "content": "x" * 60_000}])
        with (patch.object(cloud, "task_size", return_value="balanced"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=5),
              patch.object(cloud.Router, "reserve_free_request", return_value=True),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertTrue(post.call_args.args[0].startswith(cloud.API))

    def test_fast_balanced_task_stays_local_even_with_free_route(self):
        self.connected()
        request = dict(REQUEST, messages=[{"role": "user", "content": "x" * 60_000}])
        with (patch.object(cloud, "task_size", return_value="balanced"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=35),
              patch.object(cloud.Router, "reserve_free_request") as reserve,
              patch.object(cloud, "post", return_value=ANSWER) as post):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        reserve.assert_not_called()
        self.assertTrue(post.call_args.args[0].startswith("http://127.0.0.1:11434/"))

    def test_very_large_balanced_prompt_tries_free_route_without_speed_history(self):
        self.connected()
        request = dict(REQUEST, messages=[{"role": "user", "content": "x" * 90_000}])
        with (patch.object(cloud, "task_size", return_value="balanced"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=35),
              patch.object(cloud.Router, "reserve_free_request", return_value=True),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertTrue(post.call_args.args[0].startswith(cloud.API))

    def test_slow_balanced_task_falls_back_to_local_after_free_error(self):
        self.connected()
        request = dict(REQUEST, messages=[{"role": "user", "content": "x" * 60_000}])
        with (patch.object(cloud, "task_size", return_value="balanced"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("usage_history.recent_local_speed", return_value=5),
              patch.object(cloud.Router, "reserve_free_request", return_value=True),
              patch.object(cloud, "post", side_effect=[OSError("cloud unavailable"), ANSWER]) as post):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertTrue(post.call_args_list[0].args[0].startswith(cloud.API))
        self.assertTrue(post.call_args_list[1].args[0].startswith("http://127.0.0.1:11434/"))

    def test_credentials_encrypted_for_windows_user(self):
        self.connected()
        self.assertNotIn(
            b"sk-or-test-secret", (cloud.STATE / "credentials.dpapi").read_bytes()
        )
        self.assertEqual(cloud.credentials()["cloud_key"], "sk-or-test-secret")
        token = cloud.credentials()["router_token"]
        cloud.disconnect()
        self.assertEqual(cloud.credentials(), {"router_token": token, "cloud_key": ""})

    def test_disconnected_never_calls_cloud(self):
        with patch.object(cloud, "post", return_value=ANSWER) as post:
            cloud.Router().complete(REQUEST)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].startswith("http://127.0.0.1:11434/"))

    def test_any_refusal_tries_installed_derestricted_model(self):
        alias = cloud.CATALOG["permissive_small"]["alias"]

        def response(url, payload, **kwargs):
            if url.endswith("/api/show"):
                self.assertEqual(payload["model"], alias)
                return {"capabilities": ["completion", "tools"]}
            return REFUSAL if payload["model"] == cloud.LOCAL_MODEL else ANSWER

        with (patch.object(cloud, "candidate_approved", return_value=True),
              patch.object(cloud, "post", side_effect=response) as post):
            result = cloud.Router().complete(REQUEST)
        self.assertEqual(result, ANSWER)
        self.assertEqual(post.call_args.args[1]["model"], alias)

    def test_quick_chat_refusal_retry_has_a_shared_deadline(self):
        clock = [0.0]
        completions = []

        def response(url, _payload, **kwargs):
            if url.endswith("/api/show"):
                clock[0] += 1
                return {"capabilities": ["completion", "tools"]}
            completions.append(kwargs["timeout"])
            clock[0] += kwargs["timeout"] + 1
            raise OSError("model timed out")

        with (
            patch.object(cloud.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(cloud, "DERESTRICTED_PROFILES", ("permissive_small", "permissive")),
            patch.object(cloud, "candidate_approved", return_value=True),
            patch.object(cloud, "post", side_effect=response),
        ):
            self.assertEqual(cloud.retry_derestricted(
                REQUEST, REFUSAL, cloud.LOCAL_MODEL, deadline=120
            ), REFUSAL)
        self.assertEqual(len(completions), 1)
        self.assertLessEqual(completions[0], 120)

    def test_derestricted_retry_scales_with_task_size(self):
        small = cloud.CATALOG["permissive_small"]["alias"]

        def response(url, payload, **kwargs):
            if url.endswith("/api/show"):
                return {"capabilities": ["completion", "tools"]}
            return REFUSAL if payload["model"] == cloud.LOCAL_MODEL else ANSWER

        with (
            patch.object(cloud, "candidate_approved", return_value=True),
            patch.object(cloud, "post", side_effect=response) as post,
        ):
            cloud.Router().complete(REQUEST)
            self.assertEqual(post.call_args.args[1]["model"], small)
            self.assertEqual(post.call_args.args[1]["reasoning_effort"], "none")
            post.reset_mock()
            cloud.Router().complete(dict(
                REQUEST, messages=[{"role": "user", "content": "Build a working website"}]
            ))
            self.assertEqual(post.call_args.args[1]["model"], small)

    def test_small_derestricted_stays_reserved_for_refusals(self):
        self.candidates.stop()
        small = cloud.CATALOG["permissive_small"]["alias"]

        def show(url, payload, **kwargs):
            if payload["model"] not in (small, cloud.FAST_MODEL):
                raise OSError("not installed")
            return {"capabilities": ["completion", "tools"],
                    "parameters": "num_ctx 65536\n",
                    "details": {"parameter_size": "9B"}}

        with (
            patch.object(cloud, "candidate_approved", return_value=True),
            patch.object(cloud, "refresh_catalog"),
            patch.object(cloud, "post", side_effect=show),
        ):
            candidates = cloud.Router().local_candidates("quick")
        self.assertIn(cloud.FAST_MODEL, candidates)
        self.assertNotIn(small, candidates)

    def test_tool_refusal_skips_derestricted_model_without_tools(self):
        request = dict(REQUEST, tools=[{"type": "function", "function": {"name": "x"}}])
        seen = []

        def response(url, payload, **kwargs):
            if url.endswith("/api/show"):
                return {"capabilities": (["completion"] if payload["model"] == cloud.CATALOG["gptoss"]["alias"]
                                         else ["completion", "tools"])}
            seen.append(payload["model"])
            return REFUSAL if payload["model"] == cloud.LOCAL_MODEL else ANSWER

        with (
            patch.object(cloud, "DERESTRICTED_PROFILES", ("gptoss", "permissive_small")),
            patch.object(cloud, "candidate_approved", return_value=True),
            patch.object(cloud, "post", side_effect=response),
        ):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertEqual(seen, [cloud.LOCAL_MODEL, cloud.CATALOG["permissive_small"]["alias"]])

    def test_image_refusal_skips_derestricted_model_without_vision(self):
        request = dict(REQUEST, messages=[{"role": "user", "content": [
            {"type": "text", "text": "Describe this."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}])
        seen = []

        def response(url, payload, **kwargs):
            if url.endswith("/api/show"):
                return {"capabilities": (["completion", "tools"]
                        if payload["model"] == cloud.CATALOG["llama"]["alias"]
                        else ["completion", "tools", "vision"])}
            seen.append(payload["model"])
            return ANSWER

        with (
            patch.object(cloud, "DERESTRICTED_PROFILES", ("llama", "permissive_small")),
            patch.object(cloud, "candidate_approved", return_value=True),
            patch.object(cloud, "post", side_effect=response),
        ):
            self.assertEqual(cloud.retry_derestricted(request, REFUSAL, cloud.LOCAL_MODEL), ANSWER)
        self.assertEqual(seen, [cloud.CATALOG["permissive_small"]["alias"]])

    def test_refusal_detection_accepts_policy_field_without_reinterpreting_tools(self):
        self.assertTrue(cloud.is_refusal(REFUSAL))
        self.assertTrue(cloud.is_refusal({"choices": [{"message": {"refusal": "blocked"}}]}))
        self.assertFalse(cloud.is_refusal(ANSWER))
        self.assertFalse(cloud.is_refusal({"choices": [{"message": {
            "content": "I can't help with that.", "tool_calls": [{"id": "x"}]}}]}))
        cloud.valid_completion({"choices": [{"message": {"refusal": "blocked"}}]})
        cloud.valid_completion(REFUSAL, {
            "tools": [{"type": "function", "function": {"name": "multiply"}}],
            "tool_choice": "required",
        })

    def test_connected_chatgpt_refusal_retries_locally(self):
        with (
            patch.object(cloud, "task_size", return_value="deep"),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", return_value=REFUSAL),
            patch.object(cloud, "CATALOG", {"gptoss": cloud.CATALOG["gptoss"]}),
            patch.object(cloud, "candidate_approved", return_value=True),
            patch("model_fitness.suitable", return_value=True),
            patch.object(cloud, "post", side_effect=[
                {"capabilities": ["completion"]}, ANSWER,
            ]) as post,
        ):
            result = cloud.Router().complete(REQUEST)
        self.assertEqual(result, ANSWER)
        self.assertEqual(post.call_args.args[1]["model"], cloud.CATALOG["gptoss"]["alias"])

    def test_slow_balanced_task_uses_lighter_chatgpt_model(self):
        with (
            patch.object(cloud, "task_size", return_value="balanced"),
            patch("usage_history.recent_local_speed", return_value=12),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", return_value=ANSWER) as complete,
        ):
            result = cloud.Router().complete(REQUEST)
        self.assertEqual(result, ANSWER)
        self.assertEqual(complete.call_args.kwargs["size"], "balanced")

    def test_abundant_paid_plan_expands_then_limit_reserves_it_for_deep_work(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        for _ in range(15):
            cloud.routing_memory.record("chatgpt", "deep", "success")
        with (
            patch.object(cloud, "task_size", return_value="balanced"),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", return_value=ANSWER) as paid,
            patch("usage_history.recent_local_speed", return_value=40),
            patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
        ):
            cloud.Router().complete(REQUEST)
            paid.assert_called_once()
            free.assert_not_called()
            cloud.routing_memory.record("chatgpt", "deep", "limit")
            cloud.Router().complete(REQUEST)
            paid.assert_called_once()
            free.assert_called_once()

    def test_paid_saver_uses_fast_free_account_before_enabled_paid_plan(self):
        cloud.routing_mode.save(cloud.STATE, "paid_saver")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (
            patch.object(cloud, "task_size", return_value="deep"),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete") as paid,
            patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
        ):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        free.assert_called_once()
        paid.assert_not_called()

    def test_paid_saver_uses_enabled_plan_only_after_local_failure(self):
        cloud.routing_mode.save(cloud.STATE, "paid_saver")
        with (
            patch.object(cloud, "task_size", return_value="deep"),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", return_value=ANSWER) as paid,
            patch.object(cloud, "post", side_effect=OSError("local model unavailable")),
        ):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        paid.assert_called_once()

    def test_local_only_task_never_uses_enabled_chatgpt(self):
        with (
            patch.object(cloud, "task_route", return_value="local_only"),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete") as paid,
            patch.object(cloud, "post", return_value=ANSWER) as local,
        ):
            self.assertEqual(cloud.Router().complete(REQUEST, task_id="a" * 12), ANSWER)
        paid.assert_not_called()
        local.assert_called()

    def test_local_task_restriction_overrides_every_global_scope_even_on_failure(self):
        # Use the real per-task DB lookup, including Cloud only: a mock of
        # task_route under the default Both mode missed this precedence bug.
        (cloud.ROOT / "LongTasks").mkdir()
        task_id = "a" * 12
        with sqlite3.connect(cloud.ROOT / "LongTasks/projects.db") as conn:
            conn.execute("CREATE TABLE projects(id TEXT, ai_route TEXT)")
            conn.execute("INSERT INTO projects VALUES (?,?)", (task_id, "local_only"))
        conn.close()
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        for scope in cloud.routing_mode.SCOPES:
            for unavailable in (False, True):
                with self.subTest(scope=scope, unavailable=unavailable):
                    cloud.routing_mode.save_scope(cloud.STATE, scope)
                    cloud.routing_mode.save(cloud.STATE, "floor_it")
                    def local_only(url, *_args, **_kwargs):
                        self.assertTrue(url.startswith("http://127.0.0.1:11434/"), url)
                        if unavailable:
                            raise OSError("local model unavailable")
                        return ANSWER
                    with (patch("chatgpt_auth.status", return_value={"enabled": True}),
                          patch("chatgpt_auth.complete") as paid,
                          patch.object(cloud_providers, "complete") as free,
                          patch.object(cloud, "post", side_effect=local_only) as local):
                        if unavailable:
                            with self.assertRaisesRegex(OSError, "local model unavailable"):
                                cloud.Router().complete(REQUEST, task_id=task_id)
                        else:
                            self.assertEqual(cloud.Router().complete(REQUEST, task_id=task_id), ANSWER)
                    local.assert_called()
                    paid.assert_not_called()
                    free.assert_not_called()

    def test_floor_it_chooses_fastest_eligible_connected_route(self):
        cloud.routing_mode.save(cloud.STATE, "floor_it")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (
            patch.object(cloud, "task_size", return_value="quick"),
            patch("usage_history.recent_local_speed", return_value=30),
            patch("usage_history.recent_provider_speed",
                  side_effect=lambda name, default: {"groq": 900, "chatgpt": 150}.get(name, default)),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete") as paid,
            patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
        ):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        free.assert_called_once()
        paid.assert_not_called()

    def test_floor_it_can_spend_enabled_plan_on_quick_work_when_fastest(self):
        cloud.routing_mode.save(cloud.STATE, "floor_it")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (
            patch.object(cloud, "task_size", return_value="quick"),
            patch("usage_history.recent_local_speed", return_value=30),
            patch("usage_history.recent_provider_speed",
                  side_effect=lambda name, default: {"groq": 150, "chatgpt": 900}.get(name, default)),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", return_value=ANSWER) as paid,
            patch.object(cloud_providers, "complete") as free,
        ):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        paid.assert_called_once()
        free.assert_not_called()

    def test_floor_it_keeps_quick_answer_local_when_network_delay_wins(self):
        cloud.routing_mode.save(cloud.STATE, "floor_it")
        with (
            patch.object(cloud, "task_size", return_value="quick"),
            patch("usage_history.recent_local_speed", return_value=70),
            patch("usage_history.recent_provider_speed", return_value=140),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete") as paid,
            patch.object(cloud, "post", return_value=ANSWER) as local,
        ):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        local.assert_called_once()
        paid.assert_not_called()

    def test_floor_it_ignores_exhausted_free_account_in_speed_ranking(self):
        cloud.routing_mode.save(cloud.STATE, "floor_it")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        (cloud.STATE / "provider-quota.json").write_text(json.dumps({
            "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "attempts": {"groq": cloud_providers.PROVIDERS["groq"]["daily"]},
        }))
        router = cloud.Router()
        with (
            patch.object(cloud, "task_size", return_value="deep"),
            patch("usage_history.recent_local_speed", return_value=10),
            patch("usage_history.recent_provider_speed",
                  side_effect=lambda name, default: {"groq": 900, "chatgpt": 200}.get(name, default)),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", return_value=ANSWER) as paid,
            patch.object(cloud_providers, "complete") as free,
            patch.object(router, "reserve_provider_request",
                  wraps=router.reserve_provider_request) as reserve,
        ):
            self.assertEqual(router.complete(REQUEST), ANSWER)
        paid.assert_called_once()
        free.assert_not_called()
        reserve.assert_not_called()

    def test_floor_it_ignores_exhausted_openrouter_in_speed_ranking(self):
        cloud.routing_mode.save(cloud.STATE, "floor_it")
        account = cloud.credentials()
        account["cloud_key"] = "sk-or-test-secret"
        cloud.save_credentials(account)
        (cloud.STATE / "quota.json").write_text(json.dumps({
            "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "attempts": 50,
        }))
        router = cloud.Router()
        with (
            patch.object(cloud, "task_size", return_value="deep"),
            patch("usage_history.recent_local_speed", return_value=10),
            patch("usage_history.recent_provider_speed",
                  side_effect=lambda name, default: {
                      "openrouter": 900, "chatgpt": 200,
                  }.get(name, default)),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", return_value=ANSWER) as paid,
            patch.object(router, "reserve_free_request",
                  wraps=router.reserve_free_request) as reserve,
        ):
            self.assertEqual(router.complete(REQUEST), ANSWER)
        paid.assert_called_once()
        reserve.assert_not_called()

    def test_paid_quota_error_is_learned_and_falls_back_to_free(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        with (
            patch.object(cloud, "task_size", return_value="deep"),
            patch("chatgpt_auth.status", return_value={"enabled": True}),
            patch("chatgpt_auth.complete", side_effect=HTTPError(
                "plan", 429, "quota", {}, None)),
            patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
        ):
            cloud.Router().complete(REQUEST)
        self.assertEqual(cloud.routing_memory.pressure("chatgpt")["limits"], 1)
        free.assert_called_once()

    def test_repeated_free_limits_move_balanced_work_local(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        cloud.routing_memory.record("groq", "balanced", "limit")
        cloud.routing_memory.record("openrouter", "deep", "limit")
        with (
            patch.object(cloud, "task_size", return_value="balanced"),
            patch("usage_history.recent_local_speed", return_value=30),
            patch.object(cloud_providers, "complete") as free,
            patch.object(cloud, "post", return_value=ANSWER) as local,
        ):
            cloud.Router().complete(REQUEST)
        free.assert_not_called()
        self.assertTrue(local.call_args.args[0].startswith("http://127.0.0.1:11434/"))

    def test_one_exhausted_free_provider_does_not_hide_another(self):
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        cloud.save_provider_key("google", "google-test-secret", True)
        cloud.routing_memory.record("groq", "balanced", "limit")
        cloud.routing_memory.record("groq", "balanced", "limit")
        with (
            patch.object(cloud, "task_size", return_value="balanced"),
            patch("usage_history.recent_local_speed", return_value=30),
            patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
            patch.object(cloud, "post") as local,
        ):
            cloud.Router().complete(REQUEST)
        self.assertEqual(free.call_args.args[0], "google")
        local.assert_not_called()

    def test_bounded_review_routes_quick_without_forwarding_private_hint(self):
        catalog = dict(cloud.CATALOG, fast_review={
            "alias": "review-fast", "tool_reasoning_effort": "none"})
        request = dict(REQUEST, response_format={"type": "json_object"})
        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "task_size", return_value="deep"),
              patch.object(cloud.routing_mode, "load_scope", return_value="local_only"),
              patch.object(cloud.Router, "local_candidates", return_value=["review-fast"]) as choose,
              patch.object(cloud, "post", return_value=ANSWER) as post):
            cloud.Router().complete(request, size_hint="quick")
        self.assertEqual(choose.call_args.args[0], "quick")
        body = post.call_args.args[1]
        self.assertEqual(body["model"], "review-fast")
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertNotIn("size_hint", body)
        self.assertNotIn("_native_workload", body)
        self.assertTrue(post.call_args.args[0].startswith("http://127.0.0.1:11434/"))

    def test_normal_work_uses_smaller_fitting_alias_and_distinct_weights_on_failure(self):
        with (patch.object(cloud.Router, "local_candidates", return_value=["giant-window", "other-model", "fitting-window"]),
              patch.object(cloud, "post", side_effect=[ValueError("inference failed"), ANSWER]) as post):
            router = cloud.Router()
            router.model_weights = {"giant-window": "same", "fitting-window": "same", "other-model": "different"}
            router.model_contexts = {"giant-window": 262144, "fitting-window": 65536, "other-model": 65536}
            self.assertEqual(router.complete(REQUEST), ANSWER)
        self.assertEqual([call.args[1]["model"] for call in post.call_args_list], ["fitting-window", "other-model"])
        self.assertEqual(post.call_args_list[0].args[1]["messages"], REQUEST["messages"])
        self.assertNotIn("reasoning_effort", post.call_args_list[0].args[1])

    def test_large_request_keeps_larger_eligible_window_and_full_input(self):
        request = {**REQUEST, "messages": [{"role": "user", "content": "large document " * 30000}]}
        def candidates(size, required_context, *args):
            self.assertGreater(required_context, 65536)
            return ["giant-window"]  # The small alias cannot fit this request.
        with (patch.object(cloud.Router, "local_candidates", side_effect=candidates),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            router = cloud.Router()
            router.model_weights = {"giant-window": "same", "fitting-window": "same"}
            router.model_contexts = {"giant-window": 262144, "fitting-window": 65536}
            self.assertEqual(router.complete(request), ANSWER)
        self.assertEqual(post.call_args.args[1]["model"], "giant-window")
        self.assertEqual(post.call_args.args[1]["messages"], request["messages"])

    def test_explicit_large_context_model_is_not_replaced_by_automatic_alias_choice(self):
        catalog = dict(cloud.CATALOG, manual_window={"alias": "giant-window"})
        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "local_profile", return_value="manual_window"),
              patch.object(cloud.Router, "local_candidates", return_value=["giant-window", "fitting-window"]),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            router = cloud.Router()
            router.model_weights = {"giant-window": "same", "fitting-window": "same"}
            router.model_contexts = {"giant-window": 262144, "fitting-window": 65536}
            self.assertEqual(router.complete(REQUEST), ANSWER)
        self.assertEqual(post.call_args.args[1]["model"], "giant-window")

    def test_bounded_review_honors_manual_model_and_same_weights_reasoning(self):
        catalog = dict(cloud.CATALOG, selected_review={"alias": "review-alias"},
                       fast_review={"alias": "review-fast", "tool_reasoning_effort": "none"})
        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "local_profile", return_value="selected_review"),
              patch.object(cloud.Router, "local_candidates", return_value=["review-alias"]) as choose,
              patch.object(cloud, "post", return_value=ANSWER) as post):
            router = cloud.Router()
            router.model_weights = {"review-alias": "same-weights", "review-fast": "same-weights"}
            router.complete(REQUEST, size_hint="quick")
        self.assertEqual(choose.call_args.args[0], "manual")
        self.assertEqual(post.call_args.args[1]["model"], "review-alias")
        self.assertEqual(post.call_args.args[1]["reasoning_effort"], "none")

    def test_bounded_review_uses_advertised_thinking_control_and_no_alias_retry(self):
        with (patch.object(cloud.Router, "local_candidates", return_value=["bounded", "huge-context-alias"]),
              patch.object(cloud, "post", side_effect=ValueError("empty")) as post):
            router = cloud.Router()
            router.model_weights = {"bounded": "same", "huge-context-alias": "same"}
            router.model_thinking = {"bounded", "huge-context-alias"}
            with self.assertRaisesRegex(ValueError, "empty"):
                router.complete(REQUEST, size_hint="quick")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[1]["reasoning_effort"], "none")
        self.assertEqual(post.call_args.kwargs["timeout"], 90)

    def test_empty_thinking_response_retries_once_without_accepting_unvalidated_output(self):
        empty = {"choices": [{"message": {"role": "assistant", "content": "", "reasoning": "unfinished"},
                              "finish_reason": "length"}]}
        with patch.object(cloud, "post", side_effect=[empty, ANSWER]) as post:
            router = cloud.Router()
            router.model_thinking = {cloud.LOCAL_MODEL}
            self.assertEqual(router.complete(REQUEST), ANSWER)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.args[1]["reasoning_effort"], "none")
        self.assertEqual(post.call_args.kwargs["timeout"], 120)

    def test_empty_response_retry_still_rejects_malformed_tool_arguments(self):
        empty = {"choices": [{"message": {"content": ""}}]}
        with patch.object(cloud, "post", side_effect=[empty, argument_reply("not an array")]) as post:
            router = cloud.Router()
            router.model_thinking = {cloud.LOCAL_MODEL}
            with self.assertRaises(cloud.ToolArgumentError):
                router.complete(ARGUMENT_REQUEST)
        self.assertEqual(post.call_count, 2)

    def test_failed_empty_response_recovery_does_not_retry_same_weights_larger_alias(self):
        empty = {"choices": [{"message": {"content": ""}}]}
        with (patch.object(cloud.Router, "local_candidates", return_value=["primary", "large-context"]),
              patch.object(cloud, "post", return_value=empty) as post):
            router = cloud.Router()
            router.model_thinking = {"primary", "large-context"}
            router.model_weights = {"primary": "same-weights", "large-context": "same-weights"}
            with self.assertRaises(cloud.EmptyInferenceResponse):
                router.complete(REQUEST)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(call.args[1]["model"] == "primary" for call in post.call_args_list))

    def test_unknown_workload_hint_fails_before_inference(self):
        with patch.object(cloud, "post") as post:
            with self.assertRaisesRegex(ValueError, "size hint"):
                cloud.Router().complete(REQUEST, size_hint="unbounded")
        post.assert_not_called()

    def test_candidate_tool_calls_use_verified_reasoning_setting(self):
        request = dict(
            REQUEST, tools=[{"type": "function", "function": {"name": "multiply"}}]
        )
        catalog = dict(
            cloud.CATALOG,
            qwen36={"alias": "cale-qwen36", "tool_reasoning_effort": "none"},
        )
        with (
            patch.object(cloud, "CATALOG", catalog),
            patch.object(
                cloud.Router, "local_candidates", return_value=["cale-qwen36"]
            ),
            patch.object(cloud, "post", return_value=ANSWER) as post,
        ):
            cloud.Router().complete(request)
        self.assertEqual(post.call_args.args[1]["reasoning_effort"], "none")

    def test_installed_tool_models_auto_join_routing(self):
        self.candidates.stop()
        sizes = {
            cloud.LOCAL_MODEL: "79B",
            cloud.FAST_MODEL: "9B",
            cloud.BALANCED_MODEL: "35B",
            cloud.CATALOG["gptoss_official"]["alias"]: "20B",
        }

        def show(url, payload, **kwargs):
            name = payload["model"]
            if name not in sizes:
                raise OSError("not installed")
            return {
                "capabilities": ["completion", "tools"],
                "parameters": "num_ctx 65536\n",
                "details": {"parameter_size": sizes[name]},
            }

        with (patch.object(cloud, "post", side_effect=show),
              patch("model_fitness.measured_speed", return_value=None),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps({
                  "models": [{"name": name, "size": int(weight * 2**30)}
                             for name, weight in ((cloud.LOCAL_MODEL, 48.2),
                                                  (cloud.FAST_MODEL, 6.1),
                                                  (cloud.BALANCED_MODEL, 20.6),
                                                  (cloud.CATALOG["gptoss_official"]["alias"], 12.8))]
              }).encode())),
              patch.object(cloud, "candidate_approved", return_value=True)):
            router = cloud.Router()
            self.assertEqual(router.local_candidates("quick")[0], cloud.FAST_MODEL)
            self.assertEqual(
                router.local_candidates("balanced")[0], cloud.BALANCED_MODEL
            )
            self.assertNotIn(cloud.LOCAL_MODEL, router.local_candidates("deep"))
            self.assertIn(
                cloud.CATALOG["gptoss_official"]["alias"],
                router.local_candidates("deep"),
            )
            self.assertEqual(
                router.local_candidates("quick", required_context=131072),
                [],
            )

    def test_deep_route_uses_newer_or_measured_faster_large_model(self):
        self.candidates.stop()
        router = cloud.Router()
        router.model_cache_until = time.monotonic() + 60
        router.model_cache = {cloud.STANDARD_MAIN_ALIAS: 34.7, "cale-qwen36": 35.5}
        router.model_contexts = {name: 65536 for name in router.model_cache}
        router.model_speeds = {cloud.STANDARD_MAIN_ALIAS: None, "cale-qwen36": 18.5}
        self.assertEqual(router.local_candidates("deep")[0], "cale-qwen36")
        router.model_cache[cloud.STANDARD_MAIN_ALIAS] = 79
        router.model_speeds[cloud.STANDARD_MAIN_ALIAS] = 6.0
        self.assertEqual(router.local_candidates("deep")[0], "cale-qwen36")

    def test_approved_recommended_35b_model_becomes_balanced_preference(self):
        self.candidates.stop()
        catalog = dict(cloud.CATALOG, newer={
            "alias": "cale-qwen36", "source": "qwen3.6:35b", "candidate": True,
        })
        sizes = {cloud.BALANCED_MODEL: "34.7B", "cale-qwen36": "35.5B"}
        tags = {"models": [
            {"name": alias + ":latest", "size": 22 * 2**30}
            for alias in sizes
        ]}

        def show(_url, payload, **_kwargs):
            if payload["model"] not in sizes:
                raise OSError("not installed")
            return {"capabilities": ["completion", "tools"],
                    "parameters": "num_ctx 65536\n",
                    "details": {"parameter_size": sizes[payload["model"]]}}

        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(cloud, "post", side_effect=show)):
            self.assertEqual(cloud.Router().local_candidates("balanced")[0], "cale-qwen36")
        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "candidate_approved", side_effect=lambda item: not item.get("candidate")),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(cloud, "post", side_effect=show)):
            self.assertEqual(cloud.Router().local_candidates("balanced")[0], cloud.BALANCED_MODEL)

    def test_loaded_balanced_model_cannot_override_much_faster_measured_model(self):
        self.candidates.stop()
        newer = "cale-qwen36"
        catalog = dict(cloud.CATALOG, newer={
            "alias": newer, "source": "qwen3.6:35b", "candidate": True,
        })
        router = cloud.Router()
        router.model_cache_until = time.monotonic() + 60
        router.model_cache = {cloud.BALANCED_MODEL: 35, newer: 35}
        router.model_contexts = {name: 65536 for name in router.model_cache}
        router.model_speeds = {cloud.BALANCED_MODEL: 9.1, newer: 18.5}
        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch.object(cloud, "loaded_local_models", return_value=[cloud.BALANCED_MODEL])):
            self.assertEqual(router.local_candidates("balanced")[0], newer)
            router.model_speeds[cloud.BALANCED_MODEL] = 15.0
            self.assertEqual(router.local_candidates("balanced")[0], cloud.BALANCED_MODEL)

    def test_loaded_largest_model_does_not_hijack_quick_or_balanced_route(self):
        self.candidates.stop()
        sizes = {
            cloud.FAST_MODEL: "9B",
            cloud.BALANCED_MODEL: "35B",
            cloud.LOCAL_MODEL: "79B",
        }
        tags = {"models": [
            {"name": alias + ":latest", "size": weight * 2**30}
            for alias, weight in ((cloud.FAST_MODEL, 6), (cloud.BALANCED_MODEL, 22),
                                  (cloud.LOCAL_MODEL, 52))
        ]}

        def show(_url, payload, **_kwargs):
            return {"capabilities": ["completion", "tools"],
                    "parameters": "num_ctx 65536\n",
                    "details": {"parameter_size": sizes[payload["model"]]}}

        with (patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "urlopen", return_value=io.BytesIO(json.dumps(tags).encode())),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch.object(cloud, "post", side_effect=show),
              patch.object(cloud, "loaded_local_models", return_value=[cloud.LOCAL_MODEL])):
            router = cloud.Router()
            self.assertEqual(router.local_candidates("quick")[0], cloud.FAST_MODEL)
            self.assertEqual(router.local_candidates("balanced")[0], cloud.BALANCED_MODEL)
            self.assertNotIn(cloud.LOCAL_MODEL, router.local_candidates("deep"))

    def test_oversized_request_uses_connected_free_fallback(self):
        self.candidates.stop()
        self.connected()
        def show(url, payload, *args, **kwargs):
            if url.endswith("/api/show"):
                return {"capabilities": ["completion", "tools"],
                        "parameters": "num_ctx 65536\n",
                        "details": {"parameter_size": "9B"}}
            if url.startswith(cloud.API):
                return ANSWER
            raise AssertionError("A known undersized local model was called")
        request = dict(REQUEST, messages=[{"role": "user", "content": "x" * 300_000}])
        with (patch.object(cloud, "post", side_effect=show) as post,
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud.pc_offline, "offline", return_value=False),
              patch("chatgpt_auth.status", return_value={"enabled": False}),
              patch.object(cloud.Router, "reserve_free_request", return_value=True),
              patch.object(cloud, "candidate_approved", return_value=True)):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertTrue(any(call.args[0].startswith(cloud.API) for call in post.call_args_list))

    def test_oversized_request_without_cloud_explains_context_limit(self):
        self.candidates.stop()
        def show(url, payload, **kwargs):
            if url.endswith("/api/show"):
                return {"capabilities": ["completion", "tools"],
                        "parameters": "num_ctx 65536\n",
                        "details": {"parameter_size": "9B"}}
            raise AssertionError("A known undersized local model was called")
        request = dict(REQUEST, messages=[{"role": "user", "content": "x" * 300_000}])
        with (patch.object(cloud, "post", side_effect=show),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "candidate_approved", return_value=True)):
            with self.assertRaisesRegex(RuntimeError, "installed local models have smaller windows"):
                cloud.Router().complete(request)

    def test_old_32k_alias_is_not_auto_routed_to_long_task(self):
        self.candidates.stop()

        def show(url, payload, **kwargs):
            if payload["model"] != cloud.FAST_MODEL:
                raise OSError("not installed")
            return {"capabilities": ["completion", "tools"],
                    "parameters": "num_ctx 32768\n",
                    "details": {"parameter_size": "9B"}}

        with patch.object(cloud, "post", side_effect=show):
            self.assertEqual(cloud.Router().local_candidates("quick"), [])

    def test_paid_model_rejected_before_network(self):
        self.connected()
        with patch.object(cloud, "post") as post:
            with self.assertRaises(ValueError):
                cloud.Router().complete(dict(REQUEST, model="paid/model"))
        post.assert_not_called()

    def test_cloud_forces_free_removes_paid_plugins_and_fallbacks(self):
        self.connected()
        request = dict(
            REQUEST,
            plugins=[{"id": "web"}],
            models=["paid/model"],
            provider={"max_price": {"prompt": 100}},
        )
        with patch.object(
            cloud, "post", side_effect=[OSError("offline"), ANSWER]
        ) as post:
            cloud.Router().complete(request)
        body = post.call_args_list[1].args[1]
        self.assertEqual(body["model"], "openrouter/free")
        self.assertNotIn("models", body)
        self.assertNotIn("plugins", body)
        self.assertTrue(all(v == 0 for v in body["provider"]["max_price"].values()))
        self.assertEqual(post.call_count, 2)

    def test_quota_http_error_falls_back_without_cloud_key(self):
        self.connected()
        with patch.object(
            cloud,
            "post",
            side_effect=[
                OSError("local offline"),
                HTTPError("cloud", 429, "quota", {}, None),
                OSError("local offline"),
            ],
        ) as post:
            router = cloud.Router()
            with self.assertRaises(OSError):
                router.complete(REQUEST)
            with self.assertRaises(OSError):
                router.complete(REQUEST)
        self.assertEqual(post.call_count, 3)
        self.assertTrue(
            post.call_args_list[2].args[0].startswith("http://127.0.0.1:11434/")
        )
        self.assertEqual(len(post.call_args_list[2].args), 2)

    def test_invalid_cloud_response_falls_back(self):
        self.connected()
        with patch.object(
            cloud, "post", side_effect=[{"error": "bad"}, ANSWER]
        ) as post:
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        self.assertEqual(post.call_count, 2)

    def test_permissive_profile_stays_local_even_when_cloud_connected(self):
        self.connected()
        (cloud.STATE / "model-choice.json").write_text('{"profile":"permissive"}')
        with (patch.object(cloud.Router, "local_candidates",
                           return_value=[cloud.PERMISSIVE_MODEL]),
              patch.object(cloud, "post", return_value=ANSWER) as post):
            cloud.Router().complete(REQUEST)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].startswith("http://127.0.0.1:11434/"))
        self.assertEqual(post.call_args.args[1]["model"], cloud.PERMISSIVE_MODEL)

    def test_free_cloud_only_overrides_an_old_manual_local_model_choice(self):
        cloud.routing_mode.save_scope(cloud.STATE, "free_cloud_only")
        cloud.save_provider_key("groq", "gsk_test_secret_value", True)
        (cloud.STATE / "model-choice.json").write_text('{"profile":"permissive"}')
        with (patch.object(cloud_providers, "complete", return_value=(ANSWER, 0.5)) as free,
              patch.object(cloud, "post") as local):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        free.assert_called_once()
        local.assert_not_called()

    def test_pc_offline_switch_overrides_cloud_only_and_keeps_local_inference(self):
        cloud.routing_mode.save_scope(cloud.STATE, "cloud_only")
        self.offline.stop()
        with (patch.object(cloud.pc_offline, "offline", return_value=True),
              patch.object(cloud.Router, "local_candidates", return_value=[cloud.LOCAL_MODEL]),
              patch.object(cloud_providers, "complete") as free,
              patch.object(cloud, "post", return_value=ANSWER) as local):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        free.assert_not_called()
        self.assertTrue(local.call_args.args[0].startswith("http://127.0.0.1:11434/"))

    def test_unsuitable_saved_manual_model_falls_back_to_eligible_local_model(self):
        self.connected()
        (cloud.STATE / "model-choice.json").write_text('{"profile":"permissive"}')
        with (patch.object(cloud.Router, "local_candidates",
                           return_value=[cloud.FAST_MODEL]) as candidates,
              patch.object(cloud, "post", return_value=ANSWER) as post):
            self.assertEqual(cloud.Router().complete(REQUEST), ANSWER)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[1]["model"], cloud.FAST_MODEL)
        self.assertEqual(candidates.call_count, 2)

    def test_manual_profile_rejects_model_without_hardware_and_tool_eligibility(self):
        with (patch.object(cloud.Router, "local_candidates", return_value=[]) as candidates,
              patch.object(cloud, "post") as post):
            with self.assertRaisesRegex(ValueError, "too slow for this PC"):
                cloud.select_profile("permissive")
        candidates.assert_called_once()
        post.assert_not_called()
        self.assertFalse((cloud.STATE / "model-choice.json").exists())

    def test_daily_budget_persists_across_router_restart(self):
        self.connected()
        import time

        (cloud.STATE / "quota.json").write_text(
            json.dumps(
                {"day": time.strftime("%Y-%m-%d", time.gmtime()), "attempts": 50}
            )
        )
        with patch.object(cloud, "post", return_value=ANSWER) as post:
            cloud.Router().complete(REQUEST)
        self.assertTrue(post.call_args.args[0].startswith("http://127.0.0.1:11434/"))

    def test_cloud_unreachable_and_local_fails_propagates_failure(self):
        self.connected()
        with patch.object(cloud, "post", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                cloud.Router().complete(REQUEST)

    def test_callback_rejects_wrong_state_and_path(self):
        for path in (
            "/connect?state=wrong&code=x",
            "/wrong?state=good&code=x",
            "/connect?state=good",
        ):
            with self.assertRaises(ValueError):
                cloud.callback_code(path, "/connect", "good")
        self.assertEqual(
            cloud.callback_code("/connect?state=good&code=once", "/connect", "good"),
            "once",
        )

    def test_browser_oauth_callback_pkce_and_encrypted_save(self):
        observed = {}
        threads = []

        def browser(url):
            params = parse_qs(urlsplit(url).query)
            observed.update(params)
            callback = (
                params["callback_url"][0]
                + "?code=test-once&state="
                + params["state"][0]
            )

            def visit():
                with urlopen(callback, timeout=10) as response:
                    observed["page"] = response.read().decode()

            thread = threading.Thread(target=visit)
            threads.append(thread)
            thread.start()
            return True

        def exchange(url, payload, **kwargs):
            self.assertEqual(url, cloud.API + "/auth/keys")
            actual = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(payload["code_verifier"].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            self.assertEqual(actual, observed["code_challenge"][0])
            self.assertEqual(payload["code"], "test-once")
            return {"key": "sk-or-test-oauth-key"}

        with (
            patch.object(cloud.webbrowser, "open", side_effect=browser),
            patch.object(cloud, "post", side_effect=exchange),
        ):
            cloud.connect_account()
        for thread in threads:
            thread.join(10)
        self.assertIn("Connected", observed["page"])
        self.assertEqual(cloud.credentials()["cloud_key"], "sk-or-test-oauth-key")

    def test_stream_tool_calls_have_indices_and_preserve_arguments(self):
        result = dict(
            ANSWER,
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"x"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        )
        cloud.valid_completion(result)
        call = list(cloud.stream_chunks(result))[0]["choices"][0]["delta"][
            "tool_calls"
        ][0]
        self.assertEqual(call["index"], 0)
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "x"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_contains_diagnostic_uses_only_bounded_host_schema_guidance():
    from jsonschema import Draft202012Validator
    hint = "Include a real input action and an observable outcome in one unit."
    secret = "PRIVATE_INSTANCE_AND_TOOL_OUTPUT"
    schema = {"type": "array", "contains": {"description": hint, "type": "object", "required": ["action"]}}
    error = next(Draft202012Validator(schema).iter_errors([{ "private": secret }]))
    error.message = secret  # Raw validator/tool diagnostics must not leak either.
    failure = cloud.ToolArgumentError("task_steps", error, {"private": secret})
    assert failure.problem == "Schema rule contains: " + hint + "."
    assert secret not in str(failure) and failure.arguments == {"private": secret}
    for description in (None, {}, "   ", "H" * 10000 + secret):
        condition = {"type": "integer"}
        if description is not None:
            condition["description"] = description
        error = next(Draft202012Validator({"type": "array", "contains": condition}).iter_errors([secret]))
        failure = cloud.ToolArgumentError("array_tool", error)
        assert secret not in str(failure) and len(failure.problem) < 400
        if isinstance(description, str) and description.strip():
            assert failure.problem == "Schema rule contains: " + "H" * 350 + "."
        else:
            assert "at least one item matching its declared contains condition" in failure.problem


def test_nested_contains_diagnostic_uses_local_host_hint_without_instance_values():
    from jsonschema import Draft202012Validator
    schema = {"type": "object", "properties": {"units": {"type": "array", "items": {
        "type": "object", "properties": {"cases": {"type": "array", "contains": {
            "description": "At least one case must observe a result.", "required": ["expect"]}}}}}}}
    error = next(Draft202012Validator(schema).iter_errors({"units": [{"cases": [{"set": {"secret": "PRIVATE_VALUE"}}]}]}))
    failure = cloud.ToolArgumentError("nested_tool", error)
    assert failure.path == ["units", 0, "cases"]
    assert failure.problem == "Schema rule contains: At least one case must observe a result.."
    assert "PRIVATE_VALUE" not in str(failure) and "secret" not in str(failure)


def test_contains_diagnostic_does_not_change_type_or_malformed_json_errors():
    import pytest
    from jsonschema import Draft202012Validator
    schema = {"type": "object", "properties": {"rows": {"type": "array", "contains": {
        "description": "HOST_CONTAINS_HINT", "type": "integer"}}}, "required": ["rows"]}
    error = next(Draft202012Validator(schema).iter_errors({"rows": "PRIVATE_WRONG_TYPE"}))
    failure = cloud.ToolArgumentError("rows_tool", error)
    assert failure.path == ["rows"]
    assert failure.problem == 'Schema rule type: expected "array"; received JSON string.'
    assert "HOST_CONTAINS_HINT" not in str(failure) and "PRIVATE_WRONG_TYPE" not in str(failure)
    request = {"tools": [{"type": "function", "function": {"name": "rows_tool", "parameters": schema}}]}
    response = {"choices": [{"message": {"tool_calls": [{"id": "bad-json", "function": {
        "name": "rows_tool", "arguments": "{ malformed JSON"}}]}}]}
    with pytest.raises(json.JSONDecodeError):
        cloud.valid_completion(response, request)


class SubscriptionAgentTests(unittest.TestCase):
    def test_subscription_status_is_separate_from_free_registry_and_disabled(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cloud_providers, 'AGENT_ROOT', Path(d),create=True):
            for provider in ('claude','grok'):
                value=cloud_providers.subscription_agent_status(provider)
                self.assertFalse(value['installed']);self.assertFalse(value['connected']);self.assertFalse(value['enabled'])
                self.assertNotIn(provider,cloud_providers.PROVIDERS)
                with self.assertRaises(ValueError):cloud_providers.enable_subscription_agent(provider,True)

    def test_subscription_builder_keeps_prompt_one_argument_and_approvals(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents',create=True):
            folder=Path(d)/'project';folder.mkdir()
            prompt='Fix this; $(whoami) & never run injected shell'
            for provider in ('claude','grok'):
                exe=cloud_providers._subscription_binary(provider);exe.parent.mkdir(parents=True,exist_ok=True);exe.write_bytes(b'MZfake')
                args=cloud_providers.build_subscription_task_command(provider,folder,prompt)
                self.assertIn(prompt,args)
                self.assertFalse(any(x in args for x in ('--yolo','--always-approve','--dangerously-skip-permissions','bypassPermissions')))
                self.assertIn('--max-turns',args)

    def test_subscription_env_cannot_fall_back_to_paid_api_credentials(self):
        with patch.dict('os.environ',{'ANTHROPIC_API_KEY':'secret','XAI_API_KEY':'secret','CLAUDE_CODE_OAUTH_TOKEN':'secret','OPENAI_API_KEY':'secret','KEEP_TEST':'yes'}):
            env=cloud_providers._subscription_environment()
        for key in ('ANTHROPIC_API_KEY','XAI_API_KEY','CLAUDE_CODE_OAUTH_TOKEN','OPENAI_API_KEY'):self.assertNotIn(key,env)
        self.assertEqual(env['KEEP_TEST'],'yes')


    def test_subscription_connect_runs_only_official_login_and_is_not_enabled(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cloud_providers,'AGENT_ROOT',Path(d)):
            for provider in ('claude','grok'):
                exe=cloud_providers._subscription_binary(provider);exe.parent.mkdir(parents=True,exist_ok=True);exe.write_bytes(b'MZfake')
                replies=[(0,'do not expose auth output','')] + ([(0,json.dumps({'authMethod':'claude.ai'}),'')] if provider=='claude' else [])
                with patch.object(cloud_providers,'_agent_process',side_effect=replies) as execute:
                    status=cloud_providers.connect_subscription_agent(provider)
                self.assertTrue(status['connected']);self.assertFalse(status['enabled'])
                first=execute.call_args_list[0].args[0]
                self.assertEqual(first[0],str(exe));self.assertEqual(first[-1],'login')
                if provider=='claude':self.assertEqual(execute.call_args_list[1].args[0][-2:],['auth','status'])
                self.assertNotIn('do not expose',json.dumps(status))
                self.assertTrue(cloud_providers.enable_subscription_agent(provider,True)['enabled'])
                self.assertFalse(cloud_providers.enable_subscription_agent(provider,False)['enabled'])

    def test_subscription_failed_or_api_login_never_records_connected(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cloud_providers,'AGENT_ROOT',Path(d)):
            for provider,replies in [('grok',[(1,'secret','secret')]),('claude',[(0,'',''),(0,json.dumps({'authMethod':'api_key'}),'')])]:
                exe=cloud_providers._subscription_binary(provider);exe.parent.mkdir(parents=True,exist_ok=True);exe.write_bytes(b'MZfake')
                with patch.object(cloud_providers,'_agent_process',side_effect=replies):
                    with self.assertRaises(RuntimeError):cloud_providers.connect_subscription_agent(provider)
                self.assertFalse(cloud_providers.subscription_agent_status(provider)['connected'])

    def test_subscription_task_respects_policy_and_reports_unknown_usage(self):
        with tempfile.TemporaryDirectory() as d, patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'),patch.object(cloud,'STATE',Path(d)/'Cloud'),patch.object(cloud.pc_offline,'offline',return_value=False):
            folder=Path(d)/'project';folder.mkdir()
            exe=cloud_providers._subscription_binary('grok');exe.parent.mkdir(parents=True);exe.write_bytes(b'MZfake')
            cloud_providers._save_agent_preferences('grok',{'login_completed':True,'enabled':True})
            for scope in ('local_only','free_cloud_only'):
                cloud.routing_mode.save_scope(cloud.STATE,scope)
                with patch.object(cloud_providers,'_agent_process') as execute:
                    with self.assertRaisesRegex(ValueError,'policy'):cloud_providers.run_subscription_task('grok',folder,'Build a game')
                execute.assert_not_called()
            cloud.routing_mode.save_scope(cloud.STATE,'both')
            with patch.object(cloud_providers,'_agent_process',return_value=(0,json.dumps({'result':'proposed changes'}),'')) as execute:
                result=cloud_providers.run_subscription_task('grok',folder,'Build a game')
            self.assertEqual(result['verification'],'agent_report_only');self.assertIsNone(result['usage'])
            self.assertEqual(result['exit_code'],0);self.assertEqual(execute.call_args.kwargs['timeout'],300)
            with patch.object(cloud.pc_offline,'offline',return_value=True),patch.object(cloud_providers,'_agent_process') as execute:
                with self.assertRaises(ValueError):cloud_providers.run_subscription_task('grok',folder,'task')
            execute.assert_not_called()
            import usage_history
            token=usage_history.CURRENT_TASK.set('a'*12)
            try:
                with patch.object(cloud,'task_route',return_value='local_only'),patch.object(cloud_providers,'_agent_process') as execute:
                    with self.assertRaises(ValueError):cloud_providers.run_subscription_task('grok',folder,'task')
                execute.assert_not_called()
            finally:usage_history.CURRENT_TASK.reset(token)

    def test_subscription_installer_checks_published_digest_before_extraction(self):
        import platform
        with tempfile.TemporaryDirectory() as d, patch.object(cloud_providers,'AGENT_ROOT',Path(d)),patch.object(platform,'machine',return_value='AMD64'):
            response=io.BytesIO(b'not the official archive');response.geturl=lambda:'https://registry.npmjs.org/@anthropic-ai/claude-code-win32-x64/-/claude-code-win32-x64-2.1.318.tgz'
            with patch.object(cloud_providers,'urlopen',return_value=response):
                with self.assertRaisesRegex(ValueError,'checksum'):cloud_providers._install_subscription_agent('claude')
            self.assertFalse(cloud_providers._subscription_binary('claude').exists())

    def test_subscription_process_output_and_deadline_are_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises((ValueError,TimeoutError)):
                cloud_providers._agent_process([sys.executable,'-c',"print('x'*10000)"],cwd=d,timeout=10,max_output=100)
            with self.assertRaises(TimeoutError):
                cloud_providers._agent_process([sys.executable,'-c','import time;time.sleep(10)'],cwd=d,timeout=.1)


    def test_subscription_actual_usage_counts_cache_once_and_unknown_is_not_recorded(self):
        import usage_history
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'),patch.object(cloud,'STATE',Path(d)/'Cloud'),patch.object(cloud.pc_offline,'offline',return_value=False),patch.object(usage_history,'DATABASE',Path(d)/'usage.db'):
            folder=Path(d)/'project';folder.mkdir()
            for provider in ('claude','grok'):
                exe=cloud_providers._subscription_binary(provider);exe.parent.mkdir(parents=True,exist_ok=True);exe.write_bytes(b'MZfake')
                cloud_providers._save_agent_preferences(provider,{'login_completed':True,'enabled':True})
            token=usage_history.CURRENT_TASK.set('b'*12)
            try:
                report={'model':'haiku','usage':{'input_tokens':2,'output_tokens':3,'cache_read_input_tokens':10,'cache_creation_input_tokens':4}}
                with patch.object(cloud_providers,'_agent_process',side_effect=[(0,json.dumps({'authMethod':'claude.ai'}),''),(0,json.dumps(report),'')]):
                    measured=cloud_providers.run_subscription_task('claude',folder,'task')
                self.assertEqual(measured['usage'],{'prompt_tokens':16,'completion_tokens':3,'total_tokens':19})
                with patch.object(cloud_providers,'_agent_process',return_value=(0,json.dumps({'result':'done'}),'')):
                    self.assertIsNone(cloud_providers.run_subscription_task('grok',folder,'task')['usage'])
                db=sqlite3.connect(usage_history.DATABASE)
                try:rows=db.execute('SELECT source,task_id,total_tokens FROM usage_events').fetchall()
                finally:db.close()
                self.assertEqual(rows,[('claude_subscription','b'*12,19)])
            finally:usage_history.CURRENT_TASK.reset(token)
        result={'usage':{'input_tokens':999,'output_tokens':999},'modelUsage':{'one':{'inputTokens':1,'outputTokens':2,'cacheReadInputTokens':3,'cacheCreationInputTokens':4},'two':{'inputTokens':5,'outputTokens':6}}}
        self.assertEqual(cloud_providers._subscription_usage('claude',result)['total_tokens'],21)
        self.assertIsNone(cloud_providers._subscription_usage('grok',{'usage':{'input_tokens':True,'output_tokens':5}}))

    def test_subscription_installer_publishes_only_checksum_verified_executable(self):
        import tarfile,hashlib,base64,platform
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as package:
            item=tarfile.TarInfo('package/claude.exe');data=b'MZofficial-test-executable';item.size=len(data);package.addfile(item,io.BytesIO(data))
            traversal=tarfile.TarInfo('../outside.exe');traversal.size=3;package.addfile(traversal,io.BytesIO(b'bad'))
        raw=archive.getvalue()
        spec={**cloud_providers.SUBSCRIPTION_AGENTS['claude'],'integrity':{'x64':base64.b64encode(hashlib.sha512(raw).digest()).decode()}}
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'),patch.dict(cloud_providers.SUBSCRIPTION_AGENTS,{'claude':spec}),patch.object(platform,'machine',return_value='AMD64'):
            response=io.BytesIO(raw);response.geturl=lambda:'https://registry.npmjs.org/@anthropic-ai/claude-code-win32-x64/-/claude-code-win32-x64-2.1.318.tgz'
            with patch.object(cloud_providers,'urlopen',return_value=response):cloud_providers._install_subscription_agent('claude')
            self.assertEqual(cloud_providers._subscription_binary('claude').read_bytes(),data)
            self.assertFalse((Path(d)/'outside.exe').exists())

    def test_subscription_process_cancellation_kills_owned_process_without_unrelated_processes(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(InterruptedError):
                cloud_providers._agent_process([sys.executable,'-c','import time;time.sleep(10)'],cwd=d,timeout=10,cancel=lambda:True)


    def test_subscription_profile_isolation_and_current_api_auth_rejection(self):
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'),patch.object(cloud,'STATE',Path(d)/'Cloud'),patch.object(cloud.pc_offline,'offline',return_value=False):
            folder=Path(d)/'project';folder.mkdir()
            with patch.dict('os.environ',{'CLAUDE_CONFIG_DIR':'unrelated-global-config','GROK_HOME':'unrelated-api-profile','ANTHROPIC_BASE_URL':'https://bad.invalid','XAI_BASE_URL':'https://bad.invalid'}):
                env=cloud_providers._subscription_environment('claude')
            self.assertEqual(env['CLAUDE_CONFIG_DIR'],str(cloud_providers.AGENT_ROOT/'profiles/claude'))
            self.assertEqual(env['GROK_HOME'],str(cloud_providers.AGENT_ROOT/'profiles/grok'))
            self.assertNotIn('ANTHROPIC_BASE_URL',env);self.assertNotIn('XAI_BASE_URL',env)
            exe=cloud_providers._subscription_binary('claude');exe.parent.mkdir(parents=True);exe.write_bytes(b'MZfake')
            cloud_providers._save_agent_preferences('claude',{'login_completed':True,'enabled':True})
            with patch.object(cloud_providers,'_agent_process',return_value=(0,json.dumps({'authMethod':'api_key_helper'}),'')) as execute:
                with self.assertRaisesRegex(ValueError,'not currently signed'):cloud_providers.run_subscription_task('claude',folder,'task')
            self.assertEqual(execute.call_count,1);self.assertEqual(execute.call_args.args[0][-2:],['auth','status'])
            self.assertEqual(execute.call_args.kwargs['provider'],'claude')

    def test_failed_reconnect_revokes_saved_plan_use_before_login(self):
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)):
            exe=cloud_providers._subscription_binary('grok');exe.parent.mkdir(parents=True);exe.write_bytes(b'MZfake')
            cloud_providers._save_agent_preferences('grok',{'login_completed':True,'enabled':True})
            with patch.object(cloud_providers,'_agent_process',return_value=(1,'','')):
                with self.assertRaises(RuntimeError):cloud_providers.connect_subscription_agent('grok')
            self.assertFalse(cloud_providers.subscription_agent_status('grok')['enabled'])
            self.assertFalse(cloud_providers.subscription_agent_status('grok')['connected'])


    def test_subscription_windows_command_limit_counts_utf16_and_never_truncates(self):
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'):
            folder=Path(d)/'project';folder.mkdir()
            for provider in ('claude','grok'):
                exe=cloud_providers._subscription_binary(provider);exe.parent.mkdir(parents=True,exist_ok=True);exe.write_bytes(b'MZfake')
                short='a'*20000
                self.assertIn(short,cloud_providers.build_subscription_task_command(provider,folder,short))
                for too_long in ('a'*24000,chr(0x1f680)*12000):
                    with self.assertRaisesRegex(ValueError,'UTF-16'):cloud_providers.build_subscription_task_command(provider,folder,too_long)


    def test_subscription_claude_never_loads_project_billing_or_hook_settings(self):
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'):
            folder=Path(d)/'project';folder.mkdir();(folder/'.claude').mkdir()
            settings={'env':{'ANTHROPIC_API_KEY':'NOT_A_REAL_KEY'},'apiKeyHelper':'must-not-execute','hooks':{'SessionStart':[{'command':'must-not-execute'}]}}
            for name in ('settings.json','settings.local.json'):(folder/'.claude'/name).write_text(json.dumps(settings))
            original={name:(folder/'.claude'/name).read_bytes() for name in ('settings.json','settings.local.json')}
            exe=cloud_providers._subscription_binary('claude');exe.parent.mkdir(parents=True);exe.write_bytes(b'MZfake')
            command=cloud_providers.build_subscription_task_command('claude',folder,'task')
            self.assertEqual(command[command.index('--setting-sources')+1],'user')
            self.assertEqual(json.loads(command[command.index('--settings')+1]),{'forceLoginMethod':'claudeai'})
            with patch.object(cloud_providers,'_agent_process',side_effect=[(0,'',''),(0,json.dumps({'authMethod':'claude.ai'}),'')]) as execute:
                cloud_providers.connect_subscription_agent('claude')
            for call in execute.call_args_list:
                args=call.args[0];self.assertEqual(args[args.index('--setting-sources')+1],'user')
            for name,data in original.items():self.assertEqual((folder/'.claude'/name).read_bytes(),data)

    def test_subscription_grok_refuses_provider_config_but_preserves_tool_permissions(self):
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'):
            folder=Path(d)/'project';folder.mkdir();(folder/'.grok').mkdir()
            exe=cloud_providers._subscription_binary('grok');exe.parent.mkdir(parents=True);exe.write_bytes(b'MZfake')
            path=folder/'.grok/config.toml'
            for content in ('[model."grok-4.7"]\nbase_url="https://api.invalid"\n', '[auth]\nauth_provider_command="must-not-execute"\n', '[endpoints]\nmodels_base_url="https://api.invalid"\n'):
                path.write_text(content)
                with self.assertRaisesRegex(ValueError,'override'):cloud_providers.build_subscription_task_command('grok',folder,'task')
                self.assertEqual(path.read_text(),content)
            path.write_text('[permission]\nallow=["Read"]\n')
            self.assertIn('grok-4.7',cloud_providers.build_subscription_task_command('grok',folder,'task'))
            profile=cloud_providers.AGENT_ROOT/'profiles/grok';profile.mkdir(parents=True)
            (profile/'config.toml').write_text('[models]\ndefault="paid-custom"\n')
            with self.assertRaisesRegex(ValueError,'override'):cloud_providers.build_subscription_task_command('grok',folder,'task')

    def test_subscription_grok_strips_external_auth_and_model_environment(self):
        overrides={'GROK_AUTH_PROVIDER_COMMAND':'must-not-execute','GROK_MODELS_BASE_URL':'https://bad.invalid','GROK_MODELS_LIST_URL':'https://bad.invalid','GROK_CODE_XAI_API_KEY':'NOT_REAL','GROK_AUTH':'NOT_REAL','GROK_HOME':'externalprofile'}
        with patch.dict('os.environ',overrides):
            env=cloud_providers._subscription_environment('grok')
        for name in overrides:
            if name!='GROK_HOME':self.assertNotIn(name,env)
        with tempfile.TemporaryDirectory() as d,patch('subprocess.Popen') as launch:
            launch.return_value.poll.return_value=0;launch.return_value.returncode=0
            cloud_providers._agent_process(['official-grok'],cwd=d,timeout=1,provider='grok',subscription_task=True)
        self.assertEqual(launch.call_args.kwargs['env']['GROK_DISABLE_API_KEY_AUTH'],'1')


    def test_subscription_coding_handoff_can_edit_without_broad_shell_approval(self):
        import glob
        with tempfile.TemporaryDirectory() as d,patch.object(cloud_providers,'AGENT_ROOT',Path(d)/'agents'):
            folder=Path(d)/'project';folder.mkdir()
            for provider in ('claude','grok'):
                exe=cloud_providers._subscription_binary(provider);exe.parent.mkdir(parents=True,exist_ok=True);exe.write_bytes(b'MZfake')
                command=cloud_providers.build_subscription_task_command(provider,folder,'Implement the requested project change')
                mode=command[command.index('--permission-mode')+1]
                self.assertEqual(mode,'acceptEdits' if provider=='claude' else 'default')
                if provider=='grok':self.assertEqual(command[command.index('--allow')+1],'Edit('+glob.escape(folder.resolve().as_posix())+'/**)')
                self.assertNotIn('--always-approve',command);self.assertNotIn('--yolo',command)
                self.assertNotIn('bypassPermissions',command)
                self.assertFalse(any(arg.startswith(('Bash(','MCPTool(','WebFetch(')) for arg in command))


def test_official_connection_helper_runs_without_console_or_terminal_input(tmp_path):
    import subprocess
    import sys
    import cloud_providers
    from unittest.mock import patch
    actual = subprocess.Popen
    with patch.object(subprocess, "Popen", wraps=actual) as launched:
        code, output, errors = cloud_providers._agent_process(
            [sys.executable, "-c", "print('browser sign-in helper')"], cwd=tmp_path, timeout=10)
    assert code == 0 and "browser sign-in helper" in output
    options = launched.call_args.kwargs
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] is not None and options["stderr"] is not None
    assert not options.get("shell", False)
    if sys.platform == "win32":
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_native_context_codec_preserves_tool_ids_schema_options_and_no_mutation():
    payload = {"model": "local", "max_tokens": 200, "reasoning_effort": "none",
               "temperature": 0.3, "top_p": 0.8, "seed": 2, "stop": ["END"],
               "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
               "messages": [{"role": "assistant", "content": None, "reasoning": "checked first",
                    "tool_calls": [{"id": "call-original", "type": "function", "function": {
                        "name": "lookup", "arguments": '{"q":"private"}'}}]},
                    {"role": "tool", "tool_call_id": "call-original", "content": "result"}],
               "response_format": {"type": "json_schema", "json_schema": {"name": "answer",
                    "strict": True, "schema": {"type": "object", "properties": {"x": {"type": "string"}}}}}}
    original = json.loads(json.dumps(payload))
    body = cloud.native_chat_request(payload, 65536)
    assert body["messages"][0]["tool_calls"][0]["id"] == "call-original"
    assert body["messages"][0]["tool_calls"][0]["function"]["arguments"] == {"q": "private"}
    assert body["messages"][1]["tool_name"] == "lookup"
    assert body["messages"][1]["tool_call_id"] == "call-original"
    assert body["messages"][0]["thinking"] == "checked first" and body["think"] is False
    assert body["format"] == payload["response_format"]["json_schema"]["schema"]
    assert body["tools"] == payload["tools"] and body["options"]["temperature"] == .3
    assert payload == original


def test_native_context_unsupported_features_keep_legacy_and_overflow_never_truncates():
    import pytest
    base = {"model": "local", "max_tokens": 30, "messages": [{"role": "user", "content": "hello"}]}
    for change in ({"tool_choice": "required"}, {"reasoning_effort": "high"}, {"n": 2},
                   {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]}]},
                   {"messages": [{"role": "developer", "content": "rules"}]}, {"max_tokens": None}):
        assert cloud.native_chat_request({**base, **change}, 65536) is None
    with pytest.raises(ValueError, match="not truncated"):
        cloud.native_chat_request({**base, "max_tokens": 65000}, 65536)
    assert cloud.native_chat_request(base, 3000)["options"]["num_ctx"] <= 3000


def test_native_context_normalization_preserves_reasoning_calls_and_unknown_counts():
    import pytest
    raw = {"model": "local", "done": True, "done_reason": "stop", "message": {
        "role": "assistant", "content": "", "thinking": "reason", "tool_calls": [
            {"id": "original", "function": {"name": "lookup", "arguments": {"x": 1}}},
            {"function": {"name": "lookup", "arguments": {"x": 2}}}]}}
    reply = cloud.native_chat_completion(raw)
    assert "usage" not in reply
    message = reply["choices"][0]["message"]
    assert message["reasoning"] == "reason" and message["tool_calls"][0]["id"] == "original"
    assert message["tool_calls"][1]["id"] != "original"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
    assert cloud.native_chat_completion({**raw, "eval_count": 0, "prompt_eval_count": 0})["usage"]["total_tokens"] == 0
    assert "usage" not in cloud.native_chat_completion({**raw, "eval_count": True, "prompt_eval_count": 1})
    with pytest.raises(ValueError):
        cloud.native_chat_completion({**raw, "done": False})
    with pytest.raises(ValueError):
        cloud.native_chat_completion({**raw, "message": {"role": "assistant", "tool_calls": [{"function": {"name": "x", "arguments": "bad"}}]}})


def test_native_context_counts_long_requests_without_product_cap_and_defaults_match_v1():
    body = cloud.native_chat_request({"model": "local", "max_tokens": 8192,
        "messages": [{"role": "user", "content": "hello " * 18000}], "stop": "END"}, 262144)
    assert body["options"]["num_ctx"] > 32768
    assert body["options"]["num_ctx"] <= 262144
    assert body["options"]["temperature"] == 1.0 and body["options"]["top_p"] == 1.0
    assert body["options"]["stop"] == ["END"]
    assert body["messages"][0]["content"] == "hello " * 18000


def test_native_context_nullable_sampling_matches_legacy_defaults():
    body = cloud.native_chat_request({"model": "local", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 20, "temperature": None, "top_p": None, "seed": None,
        "presence_penalty": None, "frequency_penalty": None}, 65536)
    assert body["options"]["temperature"] == 1.0 and body["options"]["top_p"] == 1.0
    assert not {"seed", "presence_penalty", "frequency_penalty"} & body["options"].keys()
