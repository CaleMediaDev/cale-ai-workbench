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

    def test_unreadable_file_does_not_abort_remaining_folder_index(self):
        folder = self.root / "selected"
        folder.mkdir()
        vanished = folder / "a-vanished.txt"
        kept = folder / "b-kept.txt"
        vanished.write_text("old value", encoding="utf-8")
        kept.write_text("useful memory", encoding="utf-8")
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == vanished:
                raise FileNotFoundError("file changed during scan")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", stat):
            result = user_file_memory.ingest(self.state, [str(folder)])
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(user_file_memory.search(self.state, "useful")[0]["path"], str(kept))

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

    def test_precise_search_requires_all_query_terms(self):
        acme = self.root / "acme-invoice.txt"
        other = self.root / "other-invoice.txt"
        acme.write_text("Acme invoice total is 42.", encoding="utf-8")
        other.write_text("Other invoice total is 99.", encoding="utf-8")
        user_file_memory.ingest(self.state, [str(acme), str(other)])
        hits = user_file_memory.search(self.state, "acme invoice", match_all=True)
        self.assertEqual([row["path"] for row in hits], [str(acme)])

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

    def test_media_version_detects_in_place_bytes_even_with_unchanged_metadata(self):
        from types import SimpleNamespace

        path = self.root / "clip.mov"
        path.write_bytes(b"a" * 12_288)
        stable = SimpleNamespace(
            st_dev=1, st_ino=2, st_size=12_288, st_mtime_ns=3, st_ctime_ns=4
        )
        with patch.object(user_file_memory.os, "fstat", return_value=stable):
            before = user_file_memory._file_version(path)
            with path.open("r+b") as stream:
                stream.seek(6144)
                stream.write(b"changed")
            after = user_file_memory._file_version(path)
        self.assertNotEqual(before, after)

    def test_document_version_detects_edit_between_old_sample_points(self):
        from types import SimpleNamespace

        stable = SimpleNamespace(
            st_dev=1, st_ino=2, st_size=32_768, st_mtime_ns=3, st_ctime_ns=4
        )
        for name in ("report.pdf", "photo.png"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(b"a" * 32_768)
                with patch.object(user_file_memory.os, "fstat", return_value=stable):
                    before = user_file_memory._file_version(path)
                    with path.open("r+b") as stream:
                        stream.seek(8192)
                        stream.write(b"changed")
                    after = user_file_memory._file_version(path)
                self.assertNotEqual(before, after)

    def test_changed_image_replaces_stale_search_description(self):
        path = self.root / "receipt.png"
        path.write_bytes(b"a" * 12_288)
        with patch.object(user_file_memory.file_chat, "image_description",
                          side_effect=["forest receipt", "desert receipt"]) as describe:
            user_file_memory.ingest(self.state, [str(path)])
            before = path.stat()
            with path.open("r+b") as stream:
                stream.seek(6144)
                stream.write(b"changed")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
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
        # No UI background worker is running in this test. Search itself must
        # find a later addition to the folder the person selected.
        self.assertEqual(user_file_memory.search(self.state, "information")[0]["path"],
                         str(later))

    def test_matching_search_does_not_rescan_entire_selected_folder(self):
        folder = self.root / "selected"
        folder.mkdir()
        note = folder / "note.txt"
        note.write_text("orchid planning note", encoding="utf-8")
        user_file_memory.remember_roots(self.state, [str(folder)])
        user_file_memory.ingest(self.state, [str(folder)])
        with patch.object(user_file_memory, "_files", side_effect=AssertionError("full scan")):
            self.assertEqual(user_file_memory.search(self.state, "orchid")[0]["path"],
                             str(note))
        later = folder / "later.txt"
        later.write_text("sunflower new file", encoding="utf-8")
        self.assertEqual(user_file_memory.search(self.state, "sunflower")[0]["path"],
                         str(later))

    def test_quick_search_defers_new_video_analysis_to_background(self):
        folder = self.root / "selected"
        folder.mkdir()
        video = folder / "long-recording.mp4"
        video.write_bytes(b"video")
        user_file_memory.remember_roots(self.state, [str(folder)])
        with patch.object(user_file_memory, "_media_call",
                          side_effect=AssertionError("video analysis blocked chat")):
            self.assertEqual(user_file_memory.search(
                self.state, "meeting notes", scan_on_miss=False), [])
        self.assertTrue((self.state / user_file_memory.REFRESH_REQUEST).is_file())
        with patch.object(user_file_memory, "_media_call", return_value={
            "sampled_frames": [{"timestamp_seconds": 0,
                                "description": "Meeting notes on a whiteboard"}],
            "text": "",
        }):
            user_file_memory.ingest(self.state, [str(folder)])
        self.assertEqual(user_file_memory.search(
            self.state, "meeting notes", scan_on_miss=False)[0]["path"], str(video))

    def test_quick_search_hides_changed_image_without_slow_reanalysis(self):
        image = self.root / "receipt.png"
        image.write_bytes(b"first")
        with patch.object(user_file_memory.file_chat, "image_description",
                          return_value="Forest receipt"):
            user_file_memory.ingest(self.state, [str(image)])
        image.write_bytes(b"changed")
        with patch.object(user_file_memory.file_chat, "image_description",
                          side_effect=AssertionError("image analysis blocked chat")):
            self.assertEqual(user_file_memory.search(
                self.state, "forest", scan_on_miss=False), [])
        self.assertTrue((self.state / user_file_memory.REFRESH_REQUEST).is_file())

    def test_search_does_not_discover_new_file_outside_selected_folder(self):
        selected = self.root / "selected"
        selected.mkdir()
        user_file_memory.remember_roots(self.state, [str(selected)])
        (self.root / "other-notes.md").write_text("unselected detail", encoding="utf-8")
        self.assertEqual(user_file_memory.search(self.state, "unselected"), [])

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

    def test_null_image_description_clears_stale_text_and_allows_retry(self):
        image = self.root / "a-image.png"
        note = self.root / "b-note.txt"
        image.write_bytes(b"original image")
        with patch.object(user_file_memory.file_chat, "image_description",
                          return_value="obsolete receipt"):
            user_file_memory.ingest(self.state, [str(image)])
        image.write_bytes(b"changed image fixture")
        note.write_text("retained notebook", encoding="utf-8")
        with patch.object(user_file_memory.file_chat, "image_description",
                          side_effect=[None, "recovered receipt"]) as describe:
            result = user_file_memory.ingest(self.state, [str(image), str(note)])
            self.assertEqual(result, {"indexed": 1, "unchanged": 0, "skipped": 1})
            with user_file_memory._connection(self.state) as conn:
                failed = conn.execute(
                    "SELECT version,content FROM files WHERE path=?", (str(image),)
                ).fetchone()
                self.assertEqual(tuple(failed), ("", ""))
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM file_fts WHERE path=?", (str(image),)
                ).fetchone()[0], 0)
            self.assertEqual(
                user_file_memory.search(self.state, "notebook")[0]["path"], str(note)
            )
            self.assertEqual(
                user_file_memory.search(self.state, "recovered")[0]["path"], str(image)
            )
        self.assertEqual(describe.call_count, 2)
        self.assertEqual(user_file_memory.search(self.state, "obsolete"), [])

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

    def test_non_object_media_response_keeps_batch_and_retry_working(self):
        from types import SimpleNamespace

        python = self.root / "Media/python/Scripts/python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"")
        recording = self.root / "a-recording.wav"
        note = self.root / "b-note.txt"
        recording.write_bytes(b"audio fixture")
        note.write_text("retained notebook", encoding="utf-8")
        responses = [
            SimpleNamespace(returncode=0, stdout="null\n"),
            SimpleNamespace(returncode=0, stdout='{"text": "recovered transcript"}\n'),
        ]
        with (
            patch.object(user_file_memory.file_chat, "ROOT", self.root),
            patch.object(user_file_memory.subprocess, "run", side_effect=responses) as worker,
        ):
            result = user_file_memory.ingest(self.state, [str(recording), str(note)])
            self.assertEqual(result, {"indexed": 1, "unchanged": 0, "skipped": 1})
            with user_file_memory._connection(self.state) as conn:
                failed = conn.execute(
                    "SELECT version,content FROM files WHERE path=?", (str(recording),)
                ).fetchone()
                self.assertEqual(tuple(failed), ("", ""))
            self.assertEqual(
                user_file_memory.search(self.state, "notebook")[0]["path"], str(note)
            )
            self.assertEqual(
                user_file_memory.search(self.state, "recovered")[0]["path"], str(recording)
            )
        self.assertEqual(worker.call_count, 2)

    def test_null_audio_transcript_hides_stale_text_and_allows_retry(self):
        recording = self.root / "a-recording.wav"
        note = self.root / "b-note.txt"
        recording.write_bytes(b"original audio")
        with patch.object(user_file_memory, "_media_call",
                          return_value={"text": "obsolete transcript"}):
            user_file_memory.ingest(self.state, [str(recording)])
        recording.write_bytes(b"changed audio fixture")
        note.write_text("retained notebook", encoding="utf-8")
        with patch.object(user_file_memory, "_media_call", side_effect=[
            {"text": None}, {"text": "recovered transcript"},
        ]) as worker:
            result = user_file_memory.ingest(self.state, [str(recording), str(note)])
            self.assertEqual(result, {"indexed": 1, "unchanged": 0, "skipped": 1})
            with user_file_memory._connection(self.state) as conn:
                failed = conn.execute(
                    "SELECT version,content FROM files WHERE path=?", (str(recording),)
                ).fetchone()
                self.assertEqual(tuple(failed), ("", ""))
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM file_fts WHERE path=?", (str(recording),)
                ).fetchone()[0], 0)
            self.assertEqual(
                user_file_memory.search(self.state, "notebook")[0]["path"], str(note)
            )
            self.assertEqual(
                user_file_memory.search(self.state, "recovered")[0]["path"], str(recording)
            )
        self.assertEqual(worker.call_count, 2)

    def test_null_video_audio_retains_visual_memory_and_remaining_batch(self):
        video = self.root / "a-walkthrough.mp4"
        note = self.root / "b-note.txt"
        video.write_bytes(b"video fixture")
        note.write_text("retained notebook", encoding="utf-8")
        with patch.object(user_file_memory, "_media_call", side_effect=[
            {"sampled_frames": [
                {"timestamp_seconds": 0.0, "description": "A blue dashboard."}
            ]},
            {"text": None},
        ]) as media:
            result = user_file_memory.ingest(self.state, [str(video), str(note)])
        self.assertEqual(result, {"indexed": 2, "unchanged": 0, "skipped": 0})
        self.assertEqual(media.call_count, 2)
        self.assertIn("blue dashboard", user_file_memory.get(self.state, str(video))["content"])
        self.assertEqual(
            user_file_memory.search(self.state, "notebook")[0]["path"], str(note)
        )

    def test_null_video_frames_clear_stale_memory_and_allow_retry(self):
        video = self.root / "a-walkthrough.mp4"
        note = self.root / "b-note.txt"
        video.write_bytes(b"original video")
        with patch.object(user_file_memory, "_media_call", side_effect=[
            {"sampled_frames": [
                {"timestamp_seconds": 0, "description": "obsolete dashboard"}
            ]},
            {"text": ""},
        ]):
            user_file_memory.ingest(self.state, [str(video)])
        video.write_bytes(b"changed video fixture")
        note.write_text("retained notebook", encoding="utf-8")
        with patch.object(user_file_memory, "_media_call", side_effect=[
            {"sampled_frames": None},
            {"sampled_frames": [
                {"timestamp_seconds": 0, "description": "recovered dashboard"}
            ]},
            {"text": ""},
        ]) as worker:
            result = user_file_memory.ingest(self.state, [str(video), str(note)])
            self.assertEqual(result, {"indexed": 1, "unchanged": 0, "skipped": 1})
            with user_file_memory._connection(self.state) as conn:
                failed = conn.execute(
                    "SELECT version,content FROM files WHERE path=?", (str(video),)
                ).fetchone()
                self.assertEqual(tuple(failed), ("", ""))
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM file_fts WHERE path=?", (str(video),)
                ).fetchone()[0], 0)
            self.assertEqual(
                user_file_memory.search(self.state, "notebook")[0]["path"], str(note)
            )
            self.assertEqual(
                user_file_memory.search(self.state, "recovered")[0]["path"], str(video)
            )
        self.assertEqual(worker.call_count, 3)
        self.assertEqual(user_file_memory.search(self.state, "obsolete"), [])

    def test_null_video_frame_entry_clears_stale_memory_and_allows_retry(self):
        video = self.root / "a-walkthrough.mp4"
        note = self.root / "b-note.txt"
        video.write_bytes(b"original video")
        with patch.object(user_file_memory, "_media_call", side_effect=[
            {"sampled_frames": [
                {"timestamp_seconds": 0, "description": "obsolete dashboard"}
            ]},
            {"text": ""},
        ]):
            user_file_memory.ingest(self.state, [str(video)])
        video.write_bytes(b"changed video fixture")
        note.write_text("retained notebook", encoding="utf-8")
        with patch.object(user_file_memory, "_media_call", side_effect=[
            {"sampled_frames": [None]},
            {"sampled_frames": [
                {"timestamp_seconds": 0, "description": "recovered dashboard"}
            ]},
            {"text": ""},
        ]) as worker:
            result = user_file_memory.ingest(self.state, [str(video), str(note)])
            self.assertEqual(result, {"indexed": 1, "unchanged": 0, "skipped": 1})
            with user_file_memory._connection(self.state) as conn:
                failed = conn.execute(
                    "SELECT version,content FROM files WHERE path=?", (str(video),)
                ).fetchone()
                self.assertEqual(tuple(failed), ("", ""))
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM file_fts WHERE path=?", (str(video),)
                ).fetchone()[0], 0)
            self.assertEqual(
                user_file_memory.search(self.state, "notebook")[0]["path"], str(note)
            )
            self.assertEqual(
                user_file_memory.search(self.state, "recovered")[0]["path"], str(video)
            )
        self.assertEqual(worker.call_count, 3)
        self.assertEqual(user_file_memory.search(self.state, "obsolete"), [])

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
