"""The whole-PC catalog must stay local and recover from changed files."""

import tempfile
import unittest
from pathlib import Path

import file_catalog


class FileCatalogTests(unittest.TestCase):
    def test_personal_folders_scan_first(self):
        home = Path("C:/Users/person")
        self.assertGreater(
            file_catalog.folder_priority(home / "Documents", home),
            file_catalog.folder_priority(Path("C:/Windows"), home),
        )
        self.assertGreater(
            file_catalog.folder_priority(home, home),
            file_catalog.folder_priority(Path("C:/Windows"), home),
        )

    def test_scan_search_and_prune(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "drive"
            state = Path(temp) / "state"
            folder = root / "Family Photos"
            folder.mkdir(parents=True)
            picture = folder / "beach trip.jpg"
            picture.write_bytes(b"private photo bytes")
            result = file_catalog.scan_computer(state, [root])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["files"], 1)
            self.assertEqual(file_catalog.search(state, "beach trip")[0]["path"], str(picture))
            self.assertEqual(file_catalog.search(state, "Family Photos")[0]["kind"], "folder")
            self.assertEqual(file_catalog.search(state, "private photo bytes"), [])
            picture.unlink()
            self.assertEqual(file_catalog.search(state, "beach trip"), [])
            replacement = folder / "sunset.png"
            replacement.write_bytes(b"other bytes")
            file_catalog.scan_computer(state, [root])
            self.assertEqual(file_catalog.search(state, "sunset")[0]["path"], str(replacement))
            self.assertEqual(file_catalog.status(state)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
