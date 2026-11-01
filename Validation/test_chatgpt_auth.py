"""Offline checks for the ChatGPT OAuth route's request contract and privacy."""

import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import chatgpt_auth as c


class ChatGPTAuthTests(unittest.TestCase):
    def test_streamed_output_survives_empty_completed_output(self):
        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def __iter__(self):
                events = [
                    {"type": "response.output_item.done", "output_index": 0,
                     "item": {"type": "message", "content": [
                         {"type": "output_text", "text": "Cale connected"}]}},
                    {"type": "response.completed", "response": {
                        "id": "resp_1", "model": "gpt-test", "output": [],
                        "usage": {"input_tokens": 2, "output_tokens": 3,
                                  "total_tokens": 5}}},
                ]
                return iter(("data: " + json.dumps(event) + "\n").encode()
                            for event in events)

        with (patch.object(c, "access_token", return_value="opaque"),
              patch.object(c, "available_models", return_value=["gpt-test"]),
              patch.object(c, "urlopen", return_value=Stream())):
            result = c.complete({"messages": [{"role": "user", "content": "Hi"}]})
        self.assertEqual(result["choices"][0]["message"]["content"], "Cale connected")
        self.assertEqual(result["usage"]["total_tokens"], 5)

    def test_response_payload_has_only_supported_core_fields(self):
        payload = c.response_payload(
            {
                "messages": [
                    {"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "Find a file"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "search_files",
                                    "arguments": '{"path":"."}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "found"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "search_files",
                            "description": "Search local files",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "temperature": 0.7,
                "max_tokens": 10000,
            },
            "gpt-test",
        )
        self.assertEqual(payload["instructions"], "You are helpful.")
        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][2]["type"], "function_call_output")
        self.assertEqual(payload["store"], False)
        self.assertEqual(payload["stream"], True)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_output_tokens", payload)

    def test_completed_response_preserves_tool_calls(self):
        result = c.response_to_completion(
            {
                "id": "resp_1",
                "model": "gpt-test",
                "usage": {"input_tokens": 42, "output_tokens": 8, "total_tokens": 50},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "search_files",
                        "arguments": '{"path":"."}',
                    }
                ],
            }
        )
        message = result["choices"][0]["message"]
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(result["usage"]["total_tokens"], 50)
        self.assertEqual(result["usage"]["prompt_tokens"], 42)

    def test_plan_usage_requires_explicit_enable(self):
        with patch.object(
            c,
            "load",
            return_value={
                "scopes": ["chatgpt.tokens.use.direct"],
                "access_token": "opaque",
                "enabled": False,
            },
        ):
            self.assertFalse(c.status()["enabled"])
            with self.assertRaises(ValueError):
                c.access_token()


if __name__ == "__main__":
    unittest.main(verbosity=2)
