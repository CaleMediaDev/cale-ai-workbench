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
    def test_thinking_model_probe_reaches_the_tool_call(self):
        response = {
            "message": {
                "tool_calls": [
                    {"function": {"name": "multiply", "arguments": {"a": 7, "b": 13}}}
                ]
            }
        }
        with patch.object(library, "post", return_value=response) as post:
            self.assertTrue(library.probe_tool_use("cale-qwen36", "none"))
        request = post.call_args.args[1]
        self.assertIs(request["think"], False)
        self.assertEqual(request["options"]["num_predict"], 512)

    def test_recommendation_can_specify_tool_reasoning(self):
        candidate = json.loads((ROOT / "models/recommendations.json").read_text())
        with patch.object(library, "CATALOG", {"main": {"alias": "base"}}):
            accepted = library.validated_recommendations(candidate)
        self.assertEqual(accepted["qwen36"]["tool_reasoning_effort"], "none")

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
            patch.object(library, "model_ready", side_effect=[False, True]),
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=40 * 2**30),
            ),
            patch.object(library, "install", side_effect=fake_install),
        ):
            library.auto_install(folder, refresh=False)
            library.auto_install(folder, refresh=False)
            state = json.loads((Path(folder) / "optional-model-small.json").read_text())
            self.assertEqual(state["status"], "complete")
            worker = json.loads((Path(folder) / "auto-model-worker.json").read_text())
            self.assertEqual(worker["catalog"], library.catalog_signature())
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
            patch.object(library, "model_ready", return_value=False),
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=10 * 2**30),
            ),
            patch.object(library, "install") as install,
        ):
            library.auto_install(folder, refresh=False)
            state = json.loads((Path(folder) / "optional-model-small.json").read_text())
            self.assertEqual(state["status"], "waiting_storage")
            install.assert_not_called()

    def test_stale_complete_status_is_reinstalled(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {
                "label": "Small",
                "alias": "small-local",
                "download_gb": 8,
                "ram_gb": 24,
            },
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(library, "model_ready", return_value=False),
            patch.object(library, "install") as install,
            patch.object(
                library.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=40 * 2**30),
            ),
        ):
            status = Path(folder) / "optional-model-small.json"
            library.save_download_status(status, "complete", "Old result", 100)
            library.auto_install(folder, refresh=False)
            install.assert_called_once()
            self.assertEqual(json.loads(status.read_text())["status"], "complete")

    def test_existing_model_needs_no_download_or_extra_space(self):
        catalog = {
            "main": {"alias": "base"},
            "small": {
                "label": "Small",
                "alias": "small-local",
                "download_gb": 8,
                "ram_gb": 24,
            },
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=32),
            patch.object(library, "model_ready", return_value=True),
            patch.object(library, "install") as install,
            patch.object(library.shutil, "disk_usage") as disk_usage,
        ):
            library.auto_install(folder, refresh=False)
            install.assert_not_called()
            disk_usage.assert_not_called()
            self.assertEqual(
                json.loads((Path(folder) / "optional-model-small.json").read_text())[
                    "status"
                ],
                "complete",
            )

    def test_custom_ollama_store_controls_disk_check(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(library.os.environ, {"OLLAMA_MODELS": folder}):
                self.assertEqual(library.model_store_path(), Path(folder))

    def test_manual_experimental_model_is_not_auto_downloaded(self):
        catalog = {
            "main": {"alias": "base"},
            "experimental": {
                "label": "Experimental",
                "alias": "experimental-local",
                "download_gb": 8,
                "ram_gb": 24,
                "auto": False,
            },
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(library, "CATALOG", catalog),
            patch.object(library, "total_ram_gb", return_value=128),
            patch.object(library, "install") as install,
        ):
            library.auto_install(folder, refresh=False)
            install.assert_not_called()
            self.assertFalse(
                (Path(folder) / "optional-model-experimental.json").exists()
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
