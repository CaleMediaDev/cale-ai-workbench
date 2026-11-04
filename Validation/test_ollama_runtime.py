"""Native Ollama migration uses a pinned installer and waits for live work."""

import hashlib
import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ollama_runtime


class OllamaRuntimeTests(unittest.TestCase):
    def test_only_official_pinned_installer_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "downloads.json").write_text(
                json.dumps(
                    {
                        "ollama_setup": {
                            "url": "https://github.com/ollama/ollama/releases/download/v0.40.13/OllamaSetup.exe",
                            "sha256": "a" * 64,
                        }
                    }
                )
            )
            with patch.object(ollama_runtime, "ROOT", root):
                self.assertEqual(ollama_runtime.pinned_installer()[1], "a" * 64)
                (root / "downloads.json").write_text(
                    json.dumps(
                        {
                            "ollama_setup": {
                                "url": "https://example.com/other.exe",
                                "sha256": "a" * 64,
                            }
                        }
                    )
                )
                with self.assertRaises(ValueError):
                    ollama_runtime.pinned_installer()

    def test_download_rejects_wrong_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installer = root / "OllamaSetup.exe"
            with (
                patch.object(ollama_runtime, "INSTALLER", installer),
                patch.object(ollama_runtime, "STATUS", root / "status.json"),
                patch.object(
                    ollama_runtime.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(free=20 * 2**30),
                ),
                patch.object(
                    ollama_runtime, "urlopen", return_value=BytesIO(b"bad bytes")
                ),
            ):
                with self.assertRaises(ValueError):
                    ollama_runtime.download_installer(
                        "https://github.com/ollama/ollama/releases/download/v0.40.13/OllamaSetup.exe",
                        hashlib.sha256(b"good bytes").hexdigest(),
                    )
            self.assertFalse(installer.exists())

    def test_interrupted_download_removes_partial_and_preserves_installer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installer = root / "OllamaSetup.exe"
            installer.write_bytes(b"previous installer")
            response = BytesIO()
            with (
                patch.object(ollama_runtime, "INSTALLER", installer),
                patch.object(ollama_runtime, "STATUS", root / "status.json"),
                patch.object(
                    ollama_runtime.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(free=20 * 2**30),
                ),
                patch.object(ollama_runtime, "urlopen", return_value=response),
                patch.object(
                    response,
                    "read",
                    side_effect=[b"partial download", OSError("connection lost")],
                ),
            ):
                with self.assertRaisesRegex(OSError, "connection lost"):
                    ollama_runtime.download_installer(
                        "https://github.com/ollama/ollama/releases/download/v0.40.13/OllamaSetup.exe",
                        hashlib.sha256(b"complete installer").hexdigest(),
                    )
            self.assertFalse(installer.with_suffix(".partial").exists())
            self.assertEqual(installer.read_bytes(), b"previous installer")

    def test_skips_migration_completed_before_lock_acquisition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundled = root / "Toolbox/ollama/ollama.exe"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            native = root / "native.exe"

            def acquire_lock(*args):
                # Another worker completed after the initial eligibility check.
                native.touch()

            with (
                patch.object(ollama_runtime, "ROOT", root),
                patch.object(
                    ollama_runtime, "STATUS", root / "LongTasks/ollama-upgrade.json"
                ),
                patch.object(ollama_runtime, "native_path", return_value=native),
                patch.object(
                    ollama_runtime.msvcrt, "locking", side_effect=acquire_lock
                ),
                patch.object(ollama_runtime, "busy", return_value=False) as busy,
                patch.object(
                    ollama_runtime,
                    "pinned_installer",
                    side_effect=RuntimeError("Unexpected installer lookup"),
                ) as pinned_installer,
                patch.object(ollama_runtime, "save_download_status") as save_status,
            ):
                ollama_runtime.run()
                busy.assert_not_called()
                pinned_installer.assert_not_called()
                save_status.assert_not_called()

    def test_migration_only_for_portable_installs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundled = root / "Toolbox/ollama/ollama.exe"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            native = root / "native.exe"
            with (
                patch.object(ollama_runtime, "ROOT", root),
                patch.object(ollama_runtime, "native_path", return_value=native),
            ):
                self.assertTrue(ollama_runtime.migration_needed())
                native.touch()
                self.assertFalse(ollama_runtime.migration_needed())


if __name__ == "__main__":
    unittest.main()
