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


class CloudTests(unittest.TestCase):
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
        self.addCleanup(self.plan.stop)
        self.addCleanup(self.offline.stop)

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
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "loaded_local_models", return_value=[])):
            router = cloud.Router()
            self.assertIn(text_only, router.local_candidates("quick"))
            self.assertEqual(router.local_candidates("quick", require_vision=True),
                             [cloud.FAST_MODEL])

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
        large = cloud.CATALOG["permissive"]["alias"]

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

    def test_approved_recommended_35b_model_becomes_balanced_preference(self):
        self.candidates.stop()
        catalog = dict(cloud.CATALOG, newer={
            "alias": "cale-qwen36", "source": "qwen3.6:35b", "candidate": True,
        })
        sizes = {cloud.BALANCED_MODEL: "34.7B", "cale-qwen36": "35.5B"}

        def show(_url, payload, **_kwargs):
            if payload["model"] not in sizes:
                raise OSError("not installed")
            return {"capabilities": ["completion", "tools"],
                    "parameters": "num_ctx 65536\n",
                    "details": {"parameter_size": sizes[payload["model"]]}}

        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "candidate_approved", return_value=True),
              patch.object(cloud, "post", side_effect=show)):
            self.assertEqual(cloud.Router().local_candidates("balanced")[0], "cale-qwen36")
        with (patch.object(cloud, "CATALOG", catalog),
              patch.object(cloud, "refresh_catalog"),
              patch.object(cloud, "candidate_approved", side_effect=lambda item: not item.get("candidate")),
              patch.object(cloud, "post", side_effect=show)):
            self.assertEqual(cloud.Router().local_candidates("balanced")[0], cloud.BALANCED_MODEL)

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
        with patch.object(cloud, "post", return_value=ANSWER) as post:
            cloud.Router().complete(REQUEST)
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].startswith("http://127.0.0.1:11434/"))
        self.assertEqual(post.call_args.args[1]["model"], cloud.PERMISSIVE_MODEL)

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
