"""Updates must honor the model alias in an existing user's config."""

import tempfile
import unittest
from pathlib import Path

from model_identity import configured_main_alias, configured_permissive_alias


class ModelIdentityTests(unittest.TestCase):
    def test_existing_legacy_installation_keeps_its_model_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/config.yaml").write_text(
                "model:\n  provider: custom\n  default: qwen3-coder-local\n"
                "agent:\n  default: irrelevant\n",
                encoding="utf-8",
            )
            self.assertEqual(configured_main_alias(root), "qwen3-coder-local")
            self.assertEqual(
                configured_permissive_alias(root), "qwen3-coder-permissive"
            )

    def test_new_install_and_unrelated_nested_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(configured_main_alias(root), "cale-local")
            self.assertEqual(configured_permissive_alias(root), "cale-permissive")
            (root / "data").mkdir()
            (root / "data/config.yaml").write_text(
                "model:\n  provider: custom\nagent:\n  default: not-the-model\n",
                encoding="utf-8",
            )
            self.assertEqual(configured_main_alias(root), "cale-local")

    def test_invalid_alias_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/config.yaml").write_text(
                "model:\n  default: ../../not-a-model\n", encoding="utf-8"
            )
            self.assertEqual(configured_main_alias(root), "cale-local")


if __name__ == "__main__":
    unittest.main()
