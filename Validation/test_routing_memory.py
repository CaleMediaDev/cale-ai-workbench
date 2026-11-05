"""Adaptive routing uses local outcome history, not task contents."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import routing_memory


class RoutingMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        database = patch.object(routing_memory, "DATABASE", Path(self.temp.name) / "routing.db")
        database.start()
        self.addCleanup(database.stop)

    def test_paid_plan_expands_after_success_and_conserves_after_limit(self):
        now = 1_800_000_000
        with patch.object(routing_memory.time, "time", return_value=now):
            for _ in range(15):
                routing_memory.record("chatgpt", "deep", "success")
            abundant = routing_memory.pressure("chatgpt")
            self.assertTrue(routing_memory.use_paid("balanced", 40, abundant, now))
            self.assertFalse(routing_memory.use_paid("quick", 5, abundant, now))
            routing_memory.record("chatgpt", "deep", "limit")
            constrained = routing_memory.pressure("chatgpt")
            self.assertFalse(routing_memory.use_paid("balanced", 5, constrained, now + 4000))
            self.assertFalse(routing_memory.use_paid("deep", 5, constrained, now + 100))
            self.assertTrue(routing_memory.use_paid("deep", 5, constrained, now + 4000))

    def test_free_limits_reserve_remaining_cloud_for_demanding_work(self):
        now = 1_800_000_000
        with patch.object(routing_memory.time, "time", return_value=now):
            routing_memory.record("groq", "balanced", "limit")
            routing_memory.record("openrouter", "deep", "limit")
            free = routing_memory.pressure("free")
        self.assertEqual(free["limits"], 2)
        self.assertFalse(routing_memory.use_free("balanced", 30, free))
        self.assertTrue(routing_memory.use_free("balanced", 10, free))
        self.assertTrue(routing_memory.use_free("deep", 30, free))

    def test_client_budget_limit_is_recorded_only_once_per_day(self):
        now = 1_800_000_000
        with patch.object(routing_memory.time, "time", return_value=now):
            routing_memory.record_budget_limit("openrouter")
            routing_memory.record_budget_limit("openrouter")
            self.assertEqual(routing_memory.pressure("openrouter")["limits"], 1)

    def test_limit_classifier_does_not_confuse_auth_or_network_errors(self):
        self.assertEqual(routing_memory.failure_kind(HTTPError("url", 429, "", {}, None)), "limit")
        self.assertEqual(routing_memory.failure_kind(HTTPError("url", 401, "", {}, None)), "error")
        self.assertEqual(routing_memory.failure_kind(RuntimeError("usage_limit_reached")), "limit")
        self.assertEqual(routing_memory.failure_kind(OSError("network disconnected")), "error")





# Project memory contribution regressions share this small memory-focused suite.
"""Bounded memory contribution surface: storage, freshness and search behavior.

These existing regressions moved from test_project_memory.py so the explicit
public donation job fits every lane without discarding coverage or raising the
source-context limit. The broader suite remains in its original module.
"""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import project_memory as memory


class ProjectMemoryContributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "project"
        self.folder.mkdir()


    def test_database_creates_automatically_and_persists_typed_memory(self):
        path = Path(memory.initialize(self.folder))
        self.assertTrue(path.is_file())
        saved = memory.save(
            self.folder, "skill", "Ship a desktop app",
            "Build, run behavior tests, package, and record the release.",
            tags="release tests",
        )
        self.assertEqual(memory.get(self.folder, saved["id"])["body"],
                         "Build, run behavior tests, package, and record the release.")
        self.assertEqual(memory.search(self.folder, "release", kind="skill")[0]["id"], saved["id"])
        memory.save(self.folder, "skill", "Ship an app", "Build, test, and package.",
                    memory_id=saved["id"])
        self.assertEqual(memory.get(self.folder, saved["id"])["title"], "Ship an app")
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


    def test_file_index_refreshes_content_and_removes_deleted_files(self):
        source = self.folder / "README.md"
        source.write_text("The app uses offline recipe search.", encoding="utf-8")
        self.assertEqual(memory.index(self.folder)["indexed"], 1)
        self.assertEqual(memory.index(self.folder)["unchanged"], 1)
        self.assertEqual(memory.search(self.folder, "recipe")[0]["source_path"], "README.md")
        old = source.stat()
        source.write_text("The app uses offline budget search.", encoding="utf-8")
        os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertTrue(memory.search(self.folder, "budget"))
        self.assertFalse(memory.search(self.folder, "recipe"))
        source.unlink()
        self.assertFalse(memory.search(self.folder, "budget"))


    def test_recalled_code_file_refreshes_after_edit_and_deletion(self):
        source = self.folder / "app.py"
        source.write_text("def old_recipe(): return 1", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        identifier = "file:app.py"
        self.assertTrue(memory.search(self.folder, "old_recipe", kind="file"))
        before = source.stat()
        source.write_text("def new_budget(): return 2", encoding="utf-8")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIn("new_budget", memory.get(self.folder, identifier)["body"])
        self.assertFalse(memory.search(self.folder, "old_recipe", kind="file"))
        self.assertTrue(memory.search(self.folder, "new_budget", kind="file"))
        source.write_text("def fresh_invoice(): return 3", encoding="utf-8")
        self.assertFalse(memory.search(self.folder, "new_budget", kind="file"))
        self.assertIn("fresh_invoice", memory.get(self.folder, identifier)["body"])
        source.unlink()
        with self.assertRaisesRegex(ValueError, "source file was removed"):
            memory.get(self.folder, identifier)
        self.assertFalse(memory.search(self.folder, "fresh_invoice", kind="file"))


    def test_search_preserves_unicode_terms_without_casefold_expansion(self):
        saved = memory.save(self.folder, "note", "Travel directions",
                            "Follow the Straße to the station.")
        for query in ("Straße", "straße"):
            with self.subTest(query=query):
                found = memory.search(self.folder, query, kind="note")
                self.assertEqual([row["id"] for row in found], [saved["id"]])


    def test_fallback_filters_single_term_hits_before_result_limit(self):
        for number in range(6):
            memory.save(self.folder, "note", f"Alpha {number}", "Short entry.")
        saved = memory.save(
            self.folder, "note", "Relevant entry",
            "alpha beta " + "background " * 1000,
        )

        found = memory.search(self.folder, "alpha beta gamma", kind="note", limit=1)

        self.assertEqual([row["id"] for row in found], [saved["id"]])


    def test_search_treats_query_as_text_not_fts_syntax(self):
        memory.save(self.folder, "component", "Search API", "The database stores app components.")
        self.assertTrue(memory.search(self.folder, '"Search" OR component*'))
        self.assertEqual(memory.search(self.folder, "unrelatedword"), [])



def test_common_pair_survives_high_ranked_rare_single_term_hits(tmp_path):
    import project_memory

    # Rare single-term notes can outrank a longer, eligible two-term match.
    for number in range(20):
        project_memory.save(tmp_path, "note", f"Alpha {number}", "Short entry.")
        project_memory.save(tmp_path, "note", f"Beta {number}", "Short entry.")
    for number in range(6):
        project_memory.save(tmp_path, "note", f"Gamma {number}", "Short entry.")
    wanted = project_memory.save(tmp_path, "note", "Relevant entry", "alpha beta " + "background " * 1000)
    found = project_memory.search(tmp_path, "alpha beta gamma", kind="note", limit=1)
    assert [row["id"] for row in found] == [wanted["id"]]



if __name__ == "__main__":
    unittest.main()
