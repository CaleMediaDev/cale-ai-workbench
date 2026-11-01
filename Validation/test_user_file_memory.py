"""Selected documents are indexed locally and remain source-backed."""

import tempfile
import unittest
from pathlib import Path

import user_file_memory


class UserFileMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"

    def test_selected_file_refreshes_and_deleted_file_disappears(self):
        path = self.root / "notes.txt"
        path.write_text("Michigan lawn care invoices use blue folders.", encoding="utf-8")
        self.assertEqual(user_file_memory.ingest(self.state, [str(path)])["indexed"], 1)
        hits = user_file_memory.search(self.state, "invoices")
        self.assertEqual(hits[0]["path"], str(path))
        self.assertIn("blue", user_file_memory.get(self.state, str(path))["content"])
        path.write_text("Michigan lawn care invoices use green folders.", encoding="utf-8")
        self.assertIn("green", user_file_memory.get(self.state, str(path))["content"])
        path.unlink()
        self.assertEqual(user_file_memory.search(self.state, "invoices"), [])
        with self.assertRaises(ValueError):
            user_file_memory.get(self.state, str(path))

    def test_folder_walk_skips_secrets_and_dependencies(self):
        folder = self.root / "selected"
        folder.mkdir()
        (folder / "plan.md").write_text("Aster project design notes", encoding="utf-8")
        (folder / ".env").write_text("secret-stuff", encoding="utf-8")
        (folder / "credentials.json").write_text("private-stuff", encoding="utf-8")
        (folder / "node_modules").mkdir()
        (folder / "node_modules" / "package.js").write_text("hidden-stuff", encoding="utf-8")
        result = user_file_memory.ingest(self.state, [str(folder)])
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(len(user_file_memory.search(self.state, "Aster")), 1)
        self.assertEqual(user_file_memory.search(self.state, "private"), [])
        self.assertEqual(user_file_memory.search(self.state, "hidden"), [])


if __name__ == "__main__":
    unittest.main()
