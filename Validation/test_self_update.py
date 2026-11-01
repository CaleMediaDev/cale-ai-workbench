"""Release updates protect user data and require a published digest."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import self_update


class SelfUpdateTests(unittest.TestCase):
    def test_candidate_needs_digest_and_newer_version(self):
        asset = {
            "name": "Cale-AI-Workbench-0.5.0.zip",
            "digest": "sha256:" + "a" * 64,
            "size": 123,
            "browser_download_url": "https://github.com/CaleMediaDev/cale-ai-workbench/releases/download/v0.5.0/Cale-AI-Workbench-0.5.0.zip",
        }
        release = {"tag_name": "v0.5.0", "assets": [asset]}
        self.assertEqual(
            self_update.release_candidate([release], (0, 4, 1))[0], (0, 5, 0)
        )
        self.assertIsNone(self_update.release_candidate([release], (0, 5, 0)))
        self.assertIsNone(
            self_update.release_candidate(
                [{"tag_name": "v0.5.0", "assets": [{**asset, "digest": ""}]}], (0, 4, 1)
            )
        )

    def test_manifest_rejects_project_or_secret_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "install-files.json", json.dumps(["Projects/secret.txt"])
                )
                archive.writestr("Projects/secret.txt", "bad")
            with ZipFile(archive_path) as archive:
                with self.assertRaises(ValueError):
                    self_update.validated_manifest(archive)

    def test_apply_preserves_user_project_and_makes_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".cale-ai-install").write_text("Cale AI Workbench 0.4.0")
            (root / "long_tasks.py").write_text("old")
            (root / "Projects").mkdir()
            (root / "Projects/work.txt").write_text("my work")
            archive_path = root / "release.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("install-files.json", json.dumps(["long_tasks.py"]))
                archive.writestr("long_tasks.py", "new")
            with (
                patch.object(self_update, "ROOT", root),
                patch.object(self_update, "active_tasks", return_value=False),
            ):
                backup = self_update.apply(archive_path, (0, 5, 0))
            self.assertEqual((root / "long_tasks.py").read_text(), "new")
            self.assertEqual((root / "Projects/work.txt").read_text(), "my work")
            self.assertEqual((backup / "long_tasks.py").read_text(), "old")
            self.assertIn("0.5.0", (root / ".cale-ai-install").read_text())

    def test_design_tools_migrate_without_replacing_existing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "data/config.yaml"
            config.parent.mkdir()
            original = "platform_toolsets:\n  cli:\n  - mcp-toolbox\nmcp_servers:\n  toolbox: {}\nskills:\n  disabled: []\n"
            config.write_text(original, encoding="utf-8")
            backup = root / "backup"
            self_update.add_design_tools_to_config(root, backup)
            updated = config.read_text(encoding="utf-8")
            self.assertIn("  - mcp-design\n", updated)
            self.assertIn("  design:\n", updated)
            self.assertIn("  toolbox: {}\n", updated)
            self.assertEqual((backup / "data/config.yaml").read_text(), original)
            self.assertIsNone(self_update.add_design_tools_to_config(root, backup))

    def test_existing_32k_model_config_is_upgraded_without_other_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "data/config.yaml"
            config.parent.mkdir()
            original = (
                "model:\n  default: qwen3-coder-local\n  context_length: 32768\n"
                "fallback_providers: []\nplatform_toolsets:\n  cli:\n  - mcp-toolbox\n"
                "  - mcp-design\nmcp_servers:\n  design: {}\nskills:\n  disabled: []\n"
            )
            config.write_text(original, encoding="utf-8")
            backup = root / "backup"
            self_update.add_design_tools_to_config(root, backup)
            updated = config.read_text(encoding="utf-8")
            self.assertIn("context_length: 65536", updated)
            self.assertIn("default: qwen3-coder-local", updated)
            self.assertEqual((backup / "data/config.yaml").read_text(), original)


if __name__ == "__main__":
    unittest.main()
