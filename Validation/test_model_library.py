"""Check automatic model selection without downloading any model weights."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_library as library


class AutoModelTests(unittest.TestCase):
    def test_only_models_that_fit_ram_and_disk_are_downloaded(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {"label": "Small", "download_gb": 8, "ram_gb": 24},
            "large": {"label": "Large", "download_gb": 50, "ram_gb": 96},
        }
        calls = []

        def fake_install(profile, progress):
            calls.append(profile)
            progress("downloading 8.0 / 8.0 GB")

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=40 * 2**30),
            ),
            patch.object(library, "install", side_effect=fake_install),
        ):
            library.auto_install(folder)
            library.auto_install(folder)
            state = json.loads((Path(folder) / "optional-model-small.json").read_text())
            self.assertEqual(state["status"], "complete")
            self.assertEqual(calls, ["small"])
            self.assertFalse((Path(folder) / "optional-model-large.json").exists())

    def test_low_disk_defers_download(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {"label": "Small", "download_gb": 8, "ram_gb": 24},
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=10 * 2**30),
            ),
            patch.object(library, "install") as install,
        ):
            library.auto_install(folder)
            state = json.loads((Path(folder) / "optional-model-small.json").read_text())
            self.assertEqual(state["status"], "waiting_storage")
            install.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
