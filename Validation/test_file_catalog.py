"""The whole-PC catalog must stay local and recover from changed files."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import file_catalog


class FileCatalogTests(unittest.TestCase):
    def test_recent_complete_catalog_is_reused_after_restart(self):
        snapshot = {"status": "complete", "updated": 1000.0}
        self.assertEqual(file_catalog.refresh_delay(snapshot, 86400, now=4600), 82800)
        self.assertEqual(file_catalog.refresh_delay(snapshot, 86400, now=90000), 0)
        self.assertEqual(file_catalog.refresh_delay({**snapshot, "status": "working"}, now=1001), 0)
        self.assertEqual(file_catalog.refresh_delay({"status": "complete"}, now=1001), 0)

    def test_low_space_keeps_previous_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "drive"
            state = Path(temp) / "state"
            root.mkdir()
            kept = root / "kept.txt"
            kept.write_text("contents never indexed", encoding="utf-8")
            file_catalog.scan_computer(state, [root])
            with patch.object(file_catalog.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
                result = file_catalog.scan_computer(state, [root])
            self.assertEqual(result["status"], "waiting_for_space")
            self.assertEqual(file_catalog.search(state, "kept")[0]["path"], str(kept))

    def test_low_space_mid_scan_stops_before_pruning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "drive"
            state = Path(temp) / "state"
            root.mkdir()
            prior = root / "prior.txt"
            prior.write_text("old", encoding="utf-8")
            file_catalog.scan_computer(state, [root])
            prior.unlink()
            for number in range(12):
                (root / f"new-{number}.txt").write_text("new", encoding="utf-8")
            with patch.object(file_catalog, "BATCH", 1), patch.object(
                file_catalog.shutil, "disk_usage",
                side_effect=[SimpleNamespace(free=file_catalog.MIN_FREE_BYTES), SimpleNamespace(free=0)],
            ):
                result = file_catalog.scan_computer(state, [root])
            self.assertEqual(result["status"], "waiting_for_space")
            with file_catalog.database(state) as conn:
                self.assertIsNotNone(
                    conn.execute("SELECT id FROM entries WHERE path=?", (str(prior),)).fetchone()
                )

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
            self.assertFalse(file_catalog.first_scan_pending(state))
            self.assertTrue((state / file_catalog.FIRST_SCAN_NAME).is_file())

    def test_first_scan_only_holds_first_run(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            self.assertTrue(file_catalog.first_scan_pending(state))
            self.assertTrue(file_catalog.first_scan_pending(
                state, {"status": "working", "files": 5}
            ))
            self.assertFalse(file_catalog.first_scan_pending(
                state, {"status": "complete"}
            ))  # An older installation has no completion marker.
            self.assertTrue((state / file_catalog.FIRST_SCAN_NAME).is_file())
            (state / file_catalog.FIRST_SCAN_NAME).unlink()
            self.assertFalse(file_catalog.first_scan_pending(
                state, {"status": "error"}
            ))  # A failed scan must not lock the whole app.
            (state / file_catalog.FIRST_SCAN_NAME).write_text("complete")
            self.assertFalse(file_catalog.first_scan_pending(
                state, {"status": "working"}
            ))  # Daily rescans continue in the background.

    def test_natural_phrase_returns_closest_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "drive"
            state = Path(temp) / "state"
            root.mkdir()
            target = root / "budget-2024.xlsx"
            target.write_bytes(b"spreadsheet contents")
            (root / "spreadsheet-notes.txt").write_text("notes", encoding="utf-8")
            file_catalog.scan_computer(state, [root])
            results = file_catalog.search(state, "my budget spreadsheet from 2024")
            self.assertEqual(results[0]["path"], str(target))
            self.assertEqual(file_catalog.search(state, "my budget")[0]["path"], str(target))
            self.assertEqual(file_catalog.search(state, "my budget spreadsheet from 2025"), [])

    def test_filler_word_does_not_hide_compound_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "drive"
            state = Path(temp) / "state"
            root.mkdir()
            target = root / "file_catalog.py"
            target.write_text("module", encoding="utf-8")
            (root / "catalog.py").write_text("other", encoding="utf-8")
            file_catalog.scan_computer(state, [root])
            self.assertEqual(file_catalog.search(state, "my file_catalog.py")[0]["path"], str(target))


if __name__ == "__main__":
    unittest.main()
