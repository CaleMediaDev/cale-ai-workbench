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
    def test_empty_voice_file_triggers_repair_before_completion(self):
        names = ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
        for empty_name in names:
            with self.subTest(empty_file=empty_name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                media_python = root / "python.exe"
                media_python.touch()
                voice = root / "voice"
                voice.mkdir()
                for name in names:
                    (voice / name).write_bytes(b"" if name == empty_name else b"weights")
                status = root / "voice-status.json"
                fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)

                def run_command(command, **kwargs):
                    if "--download-voice" in command:
                        for name in names:
                            (voice / name).write_bytes(b"repaired weights")
                    return SimpleNamespace(returncode=0)

                with (
                    patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                    patch.object(natural_voice, "ROOT", root),
                    patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                    patch.object(natural_voice, "STATUS", status),
                    patch.object(natural_voice, "VOICE", voice),
                    patch.object(natural_voice, "whisper_ready", return_value=True),
                    patch.object(natural_voice.subprocess, "run", side_effect=run_command) as run,
                ):
                    self.assertFalse(natural_voice.ready())
                    natural_voice.install()
                    self.assertTrue(natural_voice.ready())
                self.assertEqual(run.call_count, 2)
                self.assertIn("--download-voice", run.call_args_list[1].args[0])
                self.assertEqual(json.loads(status.read_text())["status"], "complete")

    def test_missing_media_python_replaces_stale_working_status_with_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            status = root / "voice-status.json"
            status.write_text(json.dumps({"status": "working"}))
            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "MEDIA_PYTHON", root / "missing-python.exe"),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice.subprocess, "run") as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertIn("bundled media Python executable is missing", status.read_text())
            run.assert_not_called()

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
                        SimpleNamespace(returncode=0),
                    ],
                ) as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "complete")
            self.assertEqual(run.call_count, 4)
            self.assertIn("kokoro-onnx==0.6.1", run.call_args_list[1].args[0])
            self.assertEqual(run.call_args_list[2].args[0],
                             [str(media_python), "-c", "import kokoro_onnx"])
            self.assertIn("--download-voice", run.call_args_list[3].args[0])

    def test_successful_pip_does_not_hide_persistent_engine_import_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media_python = root / "python.exe"
            media_python.touch()
            status = root / "voice-status.json"
            fake_msvcrt = SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None)
            import_command = [str(media_python), "-c", "import kokoro_onnx"]
            with (
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
                patch.object(natural_voice, "MEDIA_PYTHON", media_python),
                patch.object(natural_voice, "STATUS", status),
                patch.object(natural_voice, "ready", return_value=True),
                patch.object(natural_voice, "whisper_ready", return_value=True),
                patch.object(
                    natural_voice.subprocess,
                    "run",
                    side_effect=[
                        SimpleNamespace(returncode=1),
                        SimpleNamespace(returncode=0),
                        natural_voice.subprocess.CalledProcessError(1, import_command),
                    ],
                ) as run,
            ):
                natural_voice.install()
            self.assertEqual(json.loads(status.read_text())["status"], "error")
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args_list[2].args[0], import_command)
            self.assertTrue(run.call_args_list[2].kwargs["check"])

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
