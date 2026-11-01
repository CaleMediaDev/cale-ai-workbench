"""Selected documents are indexed locally and remain source-backed."""

import tempfile
import unittest
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
