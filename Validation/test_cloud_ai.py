"""Checks use fake upstreams and temporary DPAPI files, never a real cloud account."""

import base64
import hashlib
import json
import sys
import tempfile
import threading
import unittest
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

    def test_provider_adapter_uses_fixed_endpoint_and_normalizes_cohere(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return json.dumps({"id": "co-test", "model": "command-r-08-2024",
                    "message": {"content": [{"type": "text", "text": "hello"}]},
                    "usage": {"billed_units": {"input_tokens": 3,
                                               "output_tokens": 1}}}).encode()

        with patch.object(cloud_providers, "urlopen", return_value=Response()) as fetch:
            result, elapsed = cloud_providers.complete("cohere", "private-test-key", REQUEST)
        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        self.assertEqual(result["usage"]["prompt_tokens"], 3)
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

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = patch.object(cloud, "STATE", Path(self.temp.name))
        self.state.start()
        self.root = patch.object(cloud, "ROOT", Path(self.temp.name))
        self.root.start()
        self.candidates = patch.object(
            cloud.Router, "local_candidates", return_value=[cloud.LOCAL_MODEL]
        )
        self.candidates.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.state.stop)
        self.addCleanup(self.root.stop)
        self.addCleanup(self.candidates.stop)

    def connected(self):
        data = cloud.credentials()
        data["cloud_key"] = "sk-or-test-secret"
        cloud.save_credentials(data)

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
        alias = cloud.CATALOG["permissive"]["alias"]

        def response(url, payload, **kwargs):
            if url.endswith("/api/show"):
                self.assertEqual(payload["model"], alias)
                return {"capabilities": ["completion", "tools"]}
            return REFUSAL if payload["model"] == cloud.LOCAL_MODEL else ANSWER

        with patch.object(cloud, "post", side_effect=response) as post:
            result = cloud.Router().complete(REQUEST)
        self.assertEqual(result, ANSWER)
        self.assertEqual(post.call_args.args[1]["model"], alias)

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
            self.assertEqual(post.call_args.args[1]["model"], large)

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
            patch.object(cloud, "DERESTRICTED_PROFILES", ("gptoss", "permissive")),
            patch.object(cloud, "post", side_effect=response),
        ):
            self.assertEqual(cloud.Router().complete(request), ANSWER)
        self.assertEqual(seen, [cloud.LOCAL_MODEL, cloud.PERMISSIVE_MODEL])

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
            patch.object(cloud, "post", side_effect=[
                {"capabilities": ["completion"]}, ANSWER,
            ]) as post,
        ):
            result = cloud.Router().complete(REQUEST)
        self.assertEqual(result, ANSWER)
        self.assertEqual(post.call_args.args[1]["model"], cloud.PERMISSIVE_MODEL)

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
              patch.object(cloud, "candidate_approved", return_value=True)):
            router = cloud.Router()
            self.assertEqual(router.local_candidates("quick")[0], cloud.FAST_MODEL)
            self.assertEqual(
                router.local_candidates("balanced")[0], cloud.BALANCED_MODEL
            )
            self.assertEqual(router.local_candidates("deep")[0], cloud.LOCAL_MODEL)
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
            self.assertEqual(cloud.Router().local_candidates("quick"), [cloud.LOCAL_MODEL])

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
