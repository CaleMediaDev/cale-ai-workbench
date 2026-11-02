"""The voice upgrade runs independently of the desktop window."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import natural_voice


class NaturalVoiceTests(unittest.TestCase):
    def test_existing_install_gets_engine_then_verified_weights(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media_python = root / "python.exe"
            media_python.touch()
            status = root / "voice-status.json"
            with (
                patch.object(natural_voice, "ROOT", root),
                patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice, "ready", side_effect=[False, True]),
                patch.object(natural_voice, "whisper_ready", return_value=True),
                patch.object(
                    natural_voice.subprocess,
                    "run",
                    side_effect=[
                        SimpleNamespace(returncode=1),
                        SimpleNamespace(returncode=0),
                        SimpleNamespace(returncode=0),
                    ],
                ) as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "complete")
            self.assertEqual(run.call_count, 3)
            self.assertIn("kokoro-onnx==0.6.1", run.call_args_list[1].args[0])
            self.assertIn("--download-voice", run.call_args_list[2].args[0])

    def test_missing_whisper_weights_are_restored_without_redownloading_voice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media_python = root / "python.exe"
            media_python.touch()
            status = root / "voice-status.json"
            with (
                patch.object(natural_voice, "ROOT", root),
                patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice, "ready", return_value=True),
                patch.object(natural_voice, "whisper_ready", side_effect=[False, True]),
                patch.object(natural_voice.subprocess, "run",
                             side_effect=[SimpleNamespace(returncode=0),
                                          SimpleNamespace(returncode=0)]) as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "complete")
            self.assertEqual(run.call_count, 2)
            self.assertIn("--download-whisper", run.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
