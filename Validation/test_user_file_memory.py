"""Selected documents are indexed locally and remain source-backed."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

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

    def test_get_refreshes_only_requested_file(self):
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("first memory", encoding="utf-8")
        second.write_text("second memory", encoding="utf-8")
        user_file_memory.ingest(self.state, [str(first), str(second)])
        actual_version = user_file_memory._file_version
        seen = []

        def record_version(source):
            seen.append(source)
            return actual_version(source)

        with patch.object(user_file_memory, "_file_version", side_effect=record_version):
            self.assertIn("first memory", user_file_memory.get(self.state, str(first))["content"])
        self.assertEqual(seen, [first])

    def test_get_does_not_index_an_unselected_file(self):
        path = self.root / "never-selected.txt"
        path.write_text("private notes", encoding="utf-8")
        with self.assertRaises(ValueError):
            user_file_memory.get(self.state, str(path))
        self.assertEqual(user_file_memory.search(self.state, "private"), [])

    def test_replaced_selected_file_link_does_not_index_target(self):
        selected = self.root / "selected.txt"
        target = self.root / "target.txt"
        selected.write_text("old selected text", encoding="utf-8")
        target.write_text("unselected secret text", encoding="utf-8")
        user_file_memory.ingest(self.state, [str(selected)])
        selected.unlink()
        try:
            selected.symlink_to(target)
        except OSError:
            self.skipTest("File symlinks are not enabled on this Windows account")
        self.assertEqual(user_file_memory.search(self.state, "secret"), [])
        self.assertEqual(user_file_memory.search(self.state, "old"), [])
        with self.assertRaises(ValueError):
            user_file_memory.get(self.state, str(selected))
        self.assertEqual(user_file_memory.search(self.state, "secret"), [])

    def test_link_guard_removes_cached_text_even_without_symlink_privilege(self):
        selected = self.root / "selected.txt"
        selected.write_text("cached detail", encoding="utf-8")
        user_file_memory.ingest(self.state, [str(selected)])
        original_is_link = Path.is_symlink

        def simulated_link(path):
            return path == selected or original_is_link(path)

        with patch.object(Path, "is_symlink", simulated_link):
            self.assertEqual(user_file_memory.search(self.state, "cached"), [])
            with self.assertRaises(ValueError):
                user_file_memory.get(self.state, str(selected))

    def test_same_size_edit_with_restored_mtime_refreshes_memory(self):
        path = self.root / "notes.txt"
        path.write_text("forest", encoding="utf-8")
        user_file_memory.ingest(self.state, [str(path)])
        before = path.stat()
        path.write_text("desert", encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(user_file_memory.search(self.state, "forest"), [])
        self.assertEqual(user_file_memory.search(self.state, "desert")[0]["path"], str(path))

    def test_replaced_image_with_same_size_and_mtime_is_reanalyzed(self):
        path = self.root / "receipt.png"
        path.write_bytes(b"first")
        with patch.object(user_file_memory.file_chat, "image_description",
                          side_effect=["forest receipt", "desert receipt"]) as describe:
            user_file_memory.ingest(self.state, [str(path)])
            original = path.stat()
            replacement = self.root / "replacement.png"
            replacement.write_bytes(b"other")
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            os.replace(replacement, path)
            self.assertEqual(user_file_memory.search(self.state, "forest"), [])
            self.assertEqual(user_file_memory.search(self.state, "desert")[0]["path"], str(path))
        self.assertEqual(describe.call_count, 2)

    def test_search_refreshes_selected_file_beyond_twentieth(self):
        folder = self.root / "selected"
        folder.mkdir()
        for number in range(25):
            (folder / f"note-{number:02}.txt").write_text("forest", encoding="utf-8")
        self.assertEqual(user_file_memory.ingest(self.state, [str(folder)])["indexed"], 25)
        last = folder / "note-24.txt"
        last.write_text("desert", encoding="utf-8")
        self.assertEqual(user_file_memory.search(self.state, "desert")[0]["path"], str(last))

    def test_selected_folder_indexes_past_old_two_hundred_file_cutoff(self):
        folder = self.root / "selected"
        folder.mkdir()
        for number in range(225):
            (folder / f"note-{number:03}.txt").write_text(
                f"Batch {number} content", encoding="utf-8"
            )
        result = user_file_memory.ingest(self.state, [str(folder)])
        self.assertEqual(result["indexed"], 225)
        self.assertEqual(
            user_file_memory.search(self.state, "Batch 224")[0]["path"],
            str(folder / "note-224.txt"),
        )

    def test_explicit_roots_are_saved_for_background_resume(self):
        folder = self.root / "selected"
        folder.mkdir()
        (folder / "notes.md").write_text("Recoverable notes", encoding="utf-8")
        self.assertEqual(user_file_memory.remember_roots(self.state, [str(folder)]), 1)
        self.assertEqual(user_file_memory.remembered_roots(self.state), [str(folder)])
        self.assertEqual(user_file_memory.ingest(
            self.state, user_file_memory.remembered_roots(self.state)
        )["indexed"], 1)
        self.assertEqual(user_file_memory.search(self.state, "Recoverable")[0]["path"],
                         str(folder / "notes.md"))
        later = folder / "added-later.md"
        later.write_text("New information after selection", encoding="utf-8")
        self.assertEqual(user_file_memory.ingest(
            self.state, user_file_memory.remembered_roots(self.state)
        )["indexed"], 1)
        self.assertEqual(user_file_memory.search(self.state, "information")[0]["path"],
                         str(later))

    def test_failed_extraction_keeps_selected_file_for_retry(self):
        path = self.root / "notes.txt"
        path.write_text("recoverable", encoding="utf-8")
        actual_text = user_file_memory._text
        attempts = 0

        def read_once(source):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("The offline reader is still installing")
            return actual_text(source)

        with patch.object(user_file_memory, "_text", side_effect=read_once):
            self.assertEqual(user_file_memory.ingest(self.state, [str(path)])["skipped"], 1)
            self.assertEqual(user_file_memory.search(self.state, "recoverable")[0]["path"], str(path))
        self.assertEqual(attempts, 2)

    def test_existing_index_migrates_and_rechecks_file_version(self):
        self.state.mkdir()
        path = self.root / "legacy.txt"
        path.write_text("updated fact", encoding="utf-8")
        with closing(sqlite3.connect(self.state / "user-files.sqlite3")) as conn:
            conn.execute("""CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL, content TEXT NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            conn.execute("INSERT INTO files(path,size,modified_ns,content) VALUES(?,?,?,?)",
                         (str(path), path.stat().st_size, path.stat().st_mtime_ns, "old fact"))
            conn.commit()
        self.assertEqual(user_file_memory.search(self.state, "updated")[0]["path"], str(path))

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

    def test_nested_secret_folder_is_not_indexed_or_recalled(self):
        folder = self.root / "selected"
        private = folder / "credentials"
        private.mkdir(parents=True)
        source = private / "ordinary-notes.txt"
        source.write_text("hidden-account-detail", encoding="utf-8")
        (folder / "public-notes.txt").write_text("public-project-detail", encoding="utf-8")
        self.assertEqual(user_file_memory.ingest(self.state, [str(folder)])["indexed"], 1)
        self.assertEqual(user_file_memory.search(self.state, "hidden"), [])
        self.assertEqual(len(user_file_memory.search(self.state, "public")), 1)
        # Old indexes made before this guard must also disappear from search.
        with user_file_memory._connection(self.state) as conn:
            conn.execute("INSERT INTO files(path,size,modified_ns,version,content) VALUES(?,?,?,?,?)",
                         (str(source), source.stat().st_size, source.stat().st_mtime_ns,
                          "old", "hidden-account-detail"))
            conn.execute("INSERT INTO file_fts(path,filename,content) VALUES(?,?,?)",
                         (str(source), source.name, "hidden-account-detail"))
        self.assertEqual(user_file_memory.search(self.state, "hidden"), [])
        with self.assertRaises(ValueError):
            user_file_memory.get(self.state, str(source))

    def test_empty_or_unreadable_revision_does_not_leave_old_memory(self):
        path = self.root / "report.txt"
        path.write_text("The budget is 42.", encoding="utf-8")
        user_file_memory.ingest(self.state, [str(path)])
        path.write_text("", encoding="utf-8")
        self.assertEqual(user_file_memory.search(self.state, "budget"), [])
        path.write_text("The budget is 43.", encoding="utf-8")
        user_file_memory.ingest(self.state, [str(path)])
        path.write_bytes(b"\x00binary")
        self.assertEqual(user_file_memory.search(self.state, "budget"), [])

    def test_image_and_recording_use_local_extractors(self):
        image = self.root / "receipt.png"
        recording = self.root / "meeting.wav"
        image.write_bytes(b"image fixture")
        recording.write_bytes(b"audio fixture")
        with (
            patch.object(user_file_memory.file_chat, "image_description",
                         return_value="Receipt total is $18.25") as describe,
            patch.object(user_file_memory, "_media_call",
                         return_value={"text": "Please send the proposal tomorrow."}) as media,
        ):
            result = user_file_memory.ingest(self.state, [str(image), str(recording)])
        self.assertEqual(result["indexed"], 2)
        describe.assert_called_once()
        media.assert_called_once_with("transcribe_audio", recording)
        self.assertEqual(user_file_memory.search(self.state, "receipt")[0]["path"], str(image))
        self.assertEqual(user_file_memory.search(self.state, "proposal")[0]["path"], str(recording))

    def test_video_memory_labels_sampled_frames(self):
        video = self.root / "walkthrough.mp4"
        video.write_bytes(b"video fixture")
        result = {
            "sampled_frames": [{"timestamp_seconds": 0.0, "description": "A blue dashboard."}],
            "audio": {"text": "Open the settings panel."},
        }
        with patch.object(user_file_memory, "_media_call",
                          side_effect=[result, {"text": "Open the settings panel."}]) as media:
            self.assertEqual(user_file_memory.ingest(self.state, [str(video)])["indexed"], 1)
        self.assertEqual(media.call_count, 2)
        indexed = user_file_memory.get(self.state, str(video))["content"]
        self.assertIn("sampled video frames", indexed)
        self.assertIn("blue dashboard", indexed)
        self.assertIn("Open the settings panel", indexed)


if __name__ == "__main__":
    unittest.main()
