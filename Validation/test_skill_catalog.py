"""Bundled skills install locally without overwriting a person's changes."""

import tempfile
import unittest
from pathlib import Path

import skill_catalog


class SkillCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bundle = self.root / "vendor/agent-skills"
        for name in skill_catalog.SKILLS:
            folder = bundle / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Test workflow.\n---\n\nOriginal",
                encoding="utf-8",
            )
        references = bundle / "references"
        references.mkdir()
        for name in skill_catalog.SHARED_REFERENCES:
            (references / name).write_text("Shared reference", encoding="utf-8")

    def test_installs_idempotently_and_updates_only_unchanged_files(self):
        first = skill_catalog.sync(self.root)
        self.assertEqual(first["installed"], len(skill_catalog.SKILLS) + len(skill_catalog.SHARED_REFERENCES))
        self.assertEqual(skill_catalog.sync(self.root)["installed"], 0)
        source = self.root / "vendor/agent-skills/context-engineering/SKILL.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nNew upstream guidance", encoding="utf-8")
        changed = skill_catalog.sync(self.root)
        self.assertEqual(changed["updated"], 1)
        installed = self.root / "data/skills/software-development/context-engineering/SKILL.md"
        self.assertIn("New upstream", installed.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "data/skills/references/security-checklist.md").is_file())

    def test_preserves_local_skill_edits(self):
        skill_catalog.sync(self.root)
        installed = self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md"
        installed.write_text("My own instructions", encoding="utf-8")
        source = self.root / "vendor/agent-skills/frontend-ui-engineering/SKILL.md"
        source.write_text("Updated upstream", encoding="utf-8")
        result = skill_catalog.sync(self.root)
        self.assertEqual(result["preserved"], 1)
        self.assertEqual(installed.read_text(encoding="utf-8"), "My own instructions")


if __name__ == "__main__":
    unittest.main()
