"""Persistent, project-scoped memory and full-text indexing behavior."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import project_memory as memory


class ProjectMemoryTests(unittest.TestCase):
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

    def test_code_scan_is_bounded_and_excludes_secrets(self):
        (self.folder / "app.py").write_text("def make_invoice(): return 42", encoding="utf-8")
        (self.folder / ".env").write_text("SECRET_TOKEN=private", encoding="utf-8")
        (self.folder / "credentials.json").write_text('{"token":"private"}', encoding="utf-8")
        (self.folder / "node_modules").mkdir()
        (self.folder / "node_modules" / "module.js").write_text("hidden_package_marker", encoding="utf-8")
        result = memory.index(self.folder, include_code=True)
        self.assertEqual(result["indexed"], 1)
        self.assertTrue(memory.search(self.folder, "invoice", kind="file"))
        self.assertFalse(memory.search(self.folder, "private"))
        self.assertFalse(memory.search(self.folder, "hidden_package_marker"))

    def test_project_scan_and_explicit_index_reject_nested_secret_folder(self):
        private = self.folder / "secrets"
        private.mkdir()
        source = private / "ordinary-notes.md"
        source.write_text("hidden-project-key", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        self.assertFalse(memory.search(self.folder, "hidden-project-key"))
        with self.assertRaises(ValueError):
            memory.index_file(self.folder, source)

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

    def test_old_identifier_does_not_match_new_identifier_by_shared_word(self):
        source = self.folder / "app.py"
        source.write_text("def old_widget(): return 1", encoding="utf-8")
        memory.index(self.folder, include_code=True)
        source.write_text("def new_widget(): return 2", encoding="utf-8")
        self.assertFalse(memory.search(self.folder, "old_widget", kind="file"))
        self.assertTrue(memory.search(self.folder, "new_widget", kind="file"))

    def test_memory_is_isolated_and_source_cannot_escape_project(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        (other / "outside.txt").write_text("outside", encoding="utf-8")
        saved = memory.save(self.folder, "decision", "Use SQLite", "Keep fast lookup local.")
        self.assertEqual(memory.search(other, "SQLite"), [])
        with self.assertRaises(ValueError):
            memory.get(other, saved["id"])
        with self.assertRaises(ValueError):
            memory.save(self.folder, "note", "Bad source", "No", source_path=str(other / "outside.txt"))
        with self.assertRaises(ValueError):
            memory.save(self.folder, "file", "Fake", "No")

    def test_search_treats_query_as_text_not_fts_syntax(self):
        memory.save(self.folder, "component", "Search API", "The database stores app components.")
        self.assertTrue(memory.search(self.folder, '"Search" OR component*'))
        self.assertEqual(memory.search(self.folder, "unrelatedword"), [])

    def test_tool_scope_requires_registered_exact_project_folder(self):
        import toolbox_mcp

        state = Path(self.temp.name) / "LongTasks"
        state.mkdir()
        with closing(sqlite3.connect(state / "projects.db")) as conn:
            conn.execute("CREATE TABLE projects(folder TEXT)")
            conn.execute("INSERT INTO projects(folder) VALUES (?)", (str(self.folder),))
            conn.commit()
        with patch.object(toolbox_mcp, "ROOT", Path(self.temp.name)):
            self.assertEqual(toolbox_mcp.registered_project(str(self.folder)), self.folder)
            other = Path(self.temp.name) / "other"
            other.mkdir()
            with self.assertRaises(ValueError):
                toolbox_mcp.registered_project(str(other))


if __name__ == "__main__":
    unittest.main(verbosity=2)
