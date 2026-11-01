"""Adaptive routing uses local outcome history, not task contents."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import routing_memory as memory


class RoutingMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        database = patch.object(memory, "DATABASE", Path(self.temp.name) / "routing.db")
        database.start()
        self.addCleanup(database.stop)

    def test_paid_plan_expands_after_success_and_conserves_after_limit(self):
        now = 1_800_000_000
        with patch.object(memory.time, "time", return_value=now):
            for _ in range(15):
                memory.record("chatgpt", "deep", "success")
            abundant = memory.pressure("chatgpt")
            self.assertTrue(memory.use_paid("balanced", 40, abundant, now))
            self.assertFalse(memory.use_paid("quick", 5, abundant, now))
            memory.record("chatgpt", "deep", "limit")
            constrained = memory.pressure("chatgpt")
            self.assertFalse(memory.use_paid("balanced", 5, constrained, now + 4000))
            self.assertFalse(memory.use_paid("deep", 5, constrained, now + 100))
            self.assertTrue(memory.use_paid("deep", 5, constrained, now + 4000))

    def test_free_limits_reserve_remaining_cloud_for_demanding_work(self):
        now = 1_800_000_000
        with patch.object(memory.time, "time", return_value=now):
            memory.record("groq", "balanced", "limit")
            memory.record("openrouter", "deep", "limit")
            free = memory.pressure("free")
        self.assertEqual(free["limits"], 2)
        self.assertFalse(memory.use_free("balanced", 30, free))
        self.assertTrue(memory.use_free("balanced", 10, free))
        self.assertTrue(memory.use_free("deep", 30, free))

    def test_client_budget_limit_is_recorded_only_once_per_day(self):
        now = 1_800_000_000
        with patch.object(memory.time, "time", return_value=now):
            memory.record_budget_limit("openrouter")
            memory.record_budget_limit("openrouter")
            self.assertEqual(memory.pressure("openrouter")["limits"], 1)

    def test_limit_classifier_does_not_confuse_auth_or_network_errors(self):
        self.assertEqual(memory.failure_kind(HTTPError("url", 429, "", {}, None)), "limit")
        self.assertEqual(memory.failure_kind(HTTPError("url", 401, "", {}, None)), "error")
        self.assertEqual(memory.failure_kind(RuntimeError("usage_limit_reached")), "limit")
        self.assertEqual(memory.failure_kind(OSError("network disconnected")), "error")


if __name__ == "__main__":
    unittest.main()
