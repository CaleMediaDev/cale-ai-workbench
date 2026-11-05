"""Bundled skills install locally without overwriting a person's changes."""

import tempfile
import unittest
from unittest import mock
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

    def test_preserves_local_file_obstructing_skills_root(self):
        destination = self.root / "data/skills"
        destination.parent.mkdir()
        destination.write_text("My local notes", encoding="utf-8")
        expected = {
            "installed": 0,
            "updated": 0,
            "preserved": len(skill_catalog.SKILLS) + len(skill_catalog.SHARED_REFERENCES),
        }

        for _ in range(2):
            self.assertEqual(skill_catalog.sync(self.root), expected)
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_text(encoding="utf-8"), "My local notes")

    def test_preserves_local_dangling_skill_symlink(self):
        skill_catalog.sync(self.root)
        installed = self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md"
        local_target = self.root / "local-skill.md"
        installed.unlink()
        try:
            installed.symlink_to(local_target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {"installed": 0, "updated": 0, "preserved": 1})
        self.assertTrue(installed.is_symlink())
        self.assertEqual(installed.readlink(), local_target)
        self.assertFalse(local_target.exists())

    def test_preserves_local_directory_and_continues_sync(self):
        skill_catalog.sync(self.root)
        installed = self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md"
        installed.unlink()
        installed.mkdir()
        local_file = installed / "local-notes.md"
        local_file.write_text("My local notes", encoding="utf-8")
        source = self.root / "vendor/agent-skills/observability-and-instrumentation/SKILL.md"
        source.write_text("Updated upstream", encoding="utf-8")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {"installed": 0, "updated": 1, "preserved": 1})
        self.assertTrue(installed.is_dir())
        self.assertEqual(local_file.read_text(encoding="utf-8"), "My local notes")
        updated = self.root / "data/skills/software-development/observability-and-instrumentation/SKILL.md"
        self.assertEqual(updated.read_text(encoding="utf-8"), "Updated upstream")

    def test_preserves_obstructed_parent_and_continues_sync(self):
        skill_catalog.sync(self.root)
        installed = self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md"
        installed.unlink()
        folder = installed.parent
        folder.rmdir()
        folder.write_text("My local notes", encoding="utf-8")
        source = self.root / "vendor/agent-skills/observability-and-instrumentation/SKILL.md"
        source.write_text("Updated upstream", encoding="utf-8")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {"installed": 0, "updated": 1, "preserved": 1})
        self.assertEqual(folder.read_text(encoding="utf-8"), "My local notes")
        updated = self.root / "data/skills/software-development/observability-and-instrumentation/SKILL.md"
        self.assertEqual(updated.read_text(encoding="utf-8"), "Updated upstream")

    def test_preserves_symlink_parent_without_probing_inaccessible_target(self):
        skill_catalog.sync(self.root)
        installed = self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md"
        installed.unlink()
        folder = installed.parent
        folder.rmdir()
        private = self.root / "private"
        private.mkdir()
        try:
            folder.symlink_to(private, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        self.addCleanup(private.chmod, 0o700)
        private.chmod(0)
        try:
            installed.is_symlink()
        except PermissionError:
            pass
        else:
            self.skipTest("Directory permissions are not enforced in this environment")
        source = self.root / "vendor/agent-skills/observability-and-instrumentation/SKILL.md"
        source.write_text("Updated upstream", encoding="utf-8")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {"installed": 0, "updated": 1, "preserved": 1})
        self.assertTrue(folder.is_symlink())
        self.assertEqual(folder.readlink(), private)
        updated = self.root / "data/skills/software-development/observability-and-instrumentation/SKILL.md"
        self.assertEqual(updated.read_text(encoding="utf-8"), "Updated upstream")

    def test_preserves_local_file_at_staging_path(self):
        skill_catalog.sync(self.root)
        installed = self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md"
        staging = installed.with_name(installed.name + ".cale-updating")
        staging.write_text("My local notes", encoding="utf-8")
        source = self.root / "vendor/agent-skills/frontend-ui-engineering/SKILL.md"
        source.write_text("Updated upstream", encoding="utf-8")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {"installed": 0, "updated": 1, "preserved": 0})
        self.assertEqual(installed.read_text(encoding="utf-8"), "Updated upstream")
        self.assertTrue(staging.is_file())
        self.assertEqual(staging.read_text(encoding="utf-8"), "My local notes")
        self.assertEqual(
            set(installed.parent.iterdir()), {installed, staging}
        )

    def test_preserves_local_file_at_marker_staging_path(self):
        skill_catalog.sync(self.root)
        staging = self.root / "data/.cale-skill-files.json.tmp"
        staging.write_text("My local notes", encoding="utf-8")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {"installed": 0, "updated": 0, "preserved": 0})
        self.assertEqual(staging.read_text(encoding="utf-8"), "My local notes")
        source = self.root / "vendor/agent-skills/frontend-ui-engineering/SKILL.md"
        source.write_text("Updated upstream", encoding="utf-8")
        result = skill_catalog.sync(self.root)
        self.assertEqual(result, {"installed": 0, "updated": 1, "preserved": 0})
        installed = self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md"
        self.assertEqual(installed.read_text(encoding="utf-8"), "Updated upstream")
        self.assertEqual(staging.read_text(encoding="utf-8"), "My local notes")
        self.assertEqual(
            set(staging.parent.iterdir()),
            {self.root / "data/skills", self.root / "data/.cale-skill-files.json", staging},
        )

    def test_skips_symlinked_bundle_root(self):
        bundle = self.root / "vendor/agent-skills"
        external = self.root / "external-bundle"
        bundle.rename(external)
        skill = external / "frontend-ui-engineering/SKILL.md"
        reference = external / "references" / skill_catalog.SHARED_REFERENCES[0]
        for source in (skill, reference):
            source.write_text("Not reviewed bundle content", encoding="utf-8")
        try:
            bundle.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {"installed": 0, "updated": 0, "preserved": 0})
        self.assertEqual(list((self.root / "data/skills").iterdir()), [])
        self.assertTrue(bundle.is_symlink())
        self.assertEqual(bundle.readlink(), external)
        for source in (skill, reference):
            self.assertEqual(
                source.read_text(encoding="utf-8"), "Not reviewed bundle content"
            )

    def test_skips_symlinked_bundled_skill_directory(self):
        name = "frontend-ui-engineering"
        source = self.root / "vendor/agent-skills" / name
        external = self.root / "external-skill"
        source.rename(external)
        (external / "SKILL.md").write_text(
            "Not reviewed bundle content", encoding="utf-8"
        )
        try:
            source.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {
            "installed": len(skill_catalog.SKILLS) + len(skill_catalog.SHARED_REFERENCES) - 1,
            "updated": 0,
            "preserved": 0,
        })
        self.assertFalse(
            (self.root / "data/skills/creative" / name).exists()
        )
        self.assertTrue(source.is_symlink())
        self.assertEqual(
            (external / "SKILL.md").read_text(encoding="utf-8"),
            "Not reviewed bundle content",
        )
        self.assertTrue(
            (self.root / "data/skills/software-development/context-engineering/SKILL.md").is_file()
        )

    def test_skips_symlinked_bundled_reference_directory(self):
        references = self.root / "vendor/agent-skills/references"
        external = self.root / "external-references"
        references.rename(external)
        for name in skill_catalog.SHARED_REFERENCES:
            (external / name).write_text("Not reviewed bundle content", encoding="utf-8")
        try:
            references.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {
            "installed": len(skill_catalog.SKILLS),
            "updated": 0,
            "preserved": 0,
        })
        self.assertFalse((self.root / "data/skills/references").exists())
        self.assertTrue(references.is_symlink())
        for name in skill_catalog.SHARED_REFERENCES:
            self.assertEqual(
                (external / name).read_text(encoding="utf-8"),
                "Not reviewed bundle content",
            )
        self.assertTrue(
            (self.root / "data/skills/software-development/context-engineering/SKILL.md").is_file()
        )

    def test_skips_symlinked_bundled_reference(self):
        name = skill_catalog.SHARED_REFERENCES[0]
        source = self.root / "vendor/agent-skills/references" / name
        external = self.root / "external-reference.md"
        external.write_text("Not reviewed bundle content", encoding="utf-8")
        source.unlink()
        try:
            source.symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")

        result = skill_catalog.sync(self.root)

        self.assertEqual(result, {
            "installed": len(skill_catalog.SKILLS) + len(skill_catalog.SHARED_REFERENCES) - 1,
            "updated": 0,
            "preserved": 0,
        })
        self.assertFalse((self.root / "data/skills/references" / name).exists())
        self.assertTrue(source.is_symlink())
        self.assertEqual(external.read_text(encoding="utf-8"), "Not reviewed bundle content")
        other = skill_catalog.SHARED_REFERENCES[1]
        self.assertEqual(
            (self.root / "data/skills/references" / other).read_text(encoding="utf-8"),
            "Shared reference",
        )

    def test_skips_bundled_file_symlinks_without_probing_targets(self):
        bundle = self.root / "vendor/agent-skills"
        skill = bundle / "frontend-ui-engineering/SKILL.md"
        reference = bundle / "references" / skill_catalog.SHARED_REFERENCES[0]
        external = self.root / "external.md"
        external.write_text("Not reviewed bundle content", encoding="utf-8")
        for source in (skill, reference):
            source.unlink()
            try:
                source.symlink_to(external)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Symlinks unavailable: {exc}")

        original_is_file = Path.is_file

        def guarded_is_file(path):
            if path in (skill, reference):
                raise PermissionError("Symlink target is inaccessible")
            return original_is_file(path)

        with mock.patch.object(Path, "is_file", guarded_is_file):
            result = skill_catalog.sync(self.root)

        self.assertEqual(result, {
            "installed": len(skill_catalog.SKILLS) + len(skill_catalog.SHARED_REFERENCES) - 2,
            "updated": 0,
            "preserved": 0,
        })
        self.assertFalse(
            (self.root / "data/skills/creative/frontend-ui-engineering/SKILL.md").exists()
        )
        self.assertFalse(
            (self.root / "data/skills/references" / reference.name).exists()
        )
        self.assertTrue(
            (self.root / "data/skills/software-development/context-engineering/SKILL.md").is_file()
        )
        self.assertEqual(external.read_text(encoding="utf-8"), "Not reviewed bundle content")

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
