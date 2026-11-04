"""Check that context follows model support and machine capacity."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import context_window


class ContextWindowTests(unittest.TestCase):
    def test_large_machine_uses_full_native_window(self):
        info = {"model_info": {"qwen.context_length": 262144}}
        self.assertEqual(
            context_window.desired_context(info, 128 * 2**30, 52 * 2**30),
            262144,
        )

    def test_small_machine_stays_within_memory_budget(self):
        info = {"model_info": {"qwen.context_length": 262144}}
        self.assertEqual(
            context_window.desired_context(info, 24 * 2**30, 7 * 2**30),
            65536,
        )

    def test_memory_below_minimum_context_budget_is_rejected(self):
        info = {"model_info": {"qwen.context_length": 262144}}
        for ram_gib in (8, 16, 22):
            with self.subTest(ram_gib=ram_gib):
                with self.assertRaisesRegex(ValueError, "Insufficient memory"):
                    context_window.desired_context(
                        info, ram_gib * 2**30, 7 * 2**30
                    )

    def test_exact_minimum_context_memory_budget_is_accepted(self):
        info = {"model_info": {"qwen.context_length": 262144}}
        self.assertEqual(
            context_window.desired_context(info, 23 * 2**30, 7 * 2**30),
            65536,
        )

    def test_architecture_ceiling_is_respected(self):
        info = {"model_info": {"qwen.context_length": 131072}}
        self.assertEqual(
            context_window.desired_context(info, 128 * 2**30, 7 * 2**30),
            131072,
        )

    def test_auxiliary_architecture_cannot_raise_native_ceiling(self):
        info = {
            "parameters": "num_ctx 524288\n",
            "model_info": {
                "general.architecture": "qwen",
                "qwen.context_length": 65536,
                "clip.context_length": 262144,
            },
        }
        self.assertEqual(context_window.native_context(info), 65536)
        self.assertEqual(
            context_window.desired_context(info, 128 * 2**30, 7 * 2**30),
            65536,
        )

    def test_non_power_of_two_native_window_is_used_when_memory_fits(self):
        info = {"model_info": {"qwen.context_length": 98304}}
        self.assertEqual(
            context_window.desired_context(info, 64 * 2**30, 7 * 2**30),
            98304,
        )

    def test_non_power_of_two_native_window_still_obeys_memory_budget(self):
        info = {"model_info": {"qwen.context_length": 98304}}
        self.assertEqual(
            context_window.desired_context(info, 24 * 2**30, 7 * 2**30),
            65536,
        )

    def test_model_below_hermes_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            context_window.desired_context(
                {"model_info": {"qwen.context_length": 32768}}, 128 * 2**30, 7 * 2**30
            )

    def test_sync_updates_main_model_regardless_of_section_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            config = root / "data/config.yaml"
            original = (
                "other:\n  context_length: 65536\n"
                "fallback_providers:\n"
                "  - model: cale-fast\n"
                "    context_length: 32768\n"
                "model:\n  context_length: 65536\n"
                "next_section:\n  context_length: 65536\n"
            )
            config.write_text(original, encoding="utf-8")
            info = {
                "parameters": "num_ctx 262144\n",
                "model_info": {"qwen.context_length": 262144},
            }
            with (patch.object(context_window, "post", return_value=info) as post,
                  patch.object(context_window, "physical_ram_bytes", return_value=128 * 2**30),
                  patch.object(context_window, "model_bytes", return_value=52 * 2**30)):
                self.assertEqual(context_window.sync(root, "main"), 262144)
            self.assertEqual(
                config.read_text(encoding="utf-8"),
                original.replace(
                    "model:\n  context_length: 65536\n",
                    "model:\n  context_length: 262144\n",
                ),
            )
            post.assert_called_once_with("/api/show", {"model": "main"})

    def test_sync_grows_only_main_alias_and_hermes_together(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            config = root / "data/config.yaml"
            config.write_text("model:\n  context_length: 65536\nfallback_providers: []\n")
            calls = []

            def fake_post(path, body):
                calls.append((path, body))
                if path == "/api/show":
                    if body["model"] != "main":
                        raise OSError("not installed")
                    return {
                        "parameters": "num_ctx 65536\n",
                        "model_info": {"qwen.context_length": 262144},
                    }
                return {"status": "success"}

            with (patch.object(context_window, "post", side_effect=fake_post),
                  patch.object(context_window, "physical_ram_bytes", return_value=128 * 2**30),
                  patch.object(context_window, "model_bytes", return_value=52 * 2**30)):
                self.assertEqual(context_window.sync(root, "main"), 262144)
            self.assertIn("context_length: 262144", config.read_text())
            self.assertTrue(any(path == "/api/create" and body["parameters"]["num_ctx"] == 262144
                                for path, body in calls))
            self.assertFalse(any(body.get("model") == "cale-fast" for _, body in calls))


if __name__ == "__main__":
    unittest.main()
