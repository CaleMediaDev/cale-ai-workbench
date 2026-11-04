"""Skill discovery never installs an uninspected or scanner-blocked result."""

import json
import unittest
from unittest.mock import patch

import skill_hub


class SkillHubTests(unittest.TestCase):
    def setUp(self):
        skill_hub.SEEN.clear()
        skill_hub.INSPECTED.clear()

    def test_search_inspect_install_in_order(self):
        identifier = "skills-sh/example/workflows/accessibility"
        payload = json.dumps([{
            "identifier": "skills-sh/example/workflows/unrelated",
            "name": "unrelated",
            "description": "No match",
            "source": "skills.sh",
        }, {
            "identifier": identifier,
            "name": "accessibility",
            "description": "Check local interface accessibility.",
            "source": "skills.sh",
            "trust_level": "community",
        }])
        with patch.object(skill_hub, "_run", side_effect=[payload, "Skill preview", "Installed"]) as run:
            with self.assertRaises(ValueError):
                skill_hub.install(identifier)
            matches = skill_hub.search("accessibility", limit=99)["matches"]
            self.assertEqual(matches[0]["identifier"], identifier)
            self.assertEqual(skill_hub.inspect(identifier)["preview"], "Skill preview")
            self.assertEqual(skill_hub.install(identifier)["result"], "Installed")
        self.assertEqual(run.call_args_list[0].args[0:2], ("search", "accessibility"))
        self.assertEqual(run.call_args_list[2].args, ("install", identifier, "--yes"))

    def test_search_skips_non_object_entries(self):
        identifier = "skills-sh/example/workflows/accessibility"
        payload = json.dumps([None, "invalid entry", [], {
            "identifier": identifier,
            "name": "accessibility",
            "description": "Check interface accessibility.",
        }])
        with patch.object(skill_hub, "_run", return_value=payload):
            result = skill_hub.search("accessibility", limit=5)
        self.assertEqual(
            [match["identifier"] for match in result["matches"]],
            [identifier],
        )
        self.assertEqual(set(skill_hub.SEEN), {identifier})
        self.assertEqual(skill_hub.INSPECTED, {})

    def test_failed_reinspection_invalidates_previous_inspection(self):
        identifier = "skills-sh/example/workflows/accessibility"
        for outcome, error in (
            (RuntimeError("Preview unavailable"), RuntimeError),
            ("x" * 30001, ValueError),
        ):
            with self.subTest(error=error.__name__):
                skill_hub.SEEN[identifier] = skill_hub.time.monotonic()
                with patch.object(
                    skill_hub, "_run", side_effect=["Skill preview", outcome]
                ) as run:
                    skill_hub.inspect(identifier)
                    self.assertIn(identifier, skill_hub.INSPECTED)
                    with self.assertRaises(error):
                        skill_hub.inspect(identifier)
                    self.assertNotIn(identifier, skill_hub.INSPECTED)
                    with self.assertRaisesRegex(ValueError, "Search and inspect"):
                        skill_hub.install(identifier)
                    self.assertEqual(run.call_count, 2)
                    self.assertTrue(
                        all(call.args == ("inspect", identifier) for call in run.call_args_list)
                    )

    def test_rejects_unsearched_urls_and_blocked_scans(self):
        with self.assertRaises(ValueError):
            skill_hub.inspect("https://example.com/SKILL.md")
        with self.assertRaises(ValueError):
            skill_hub.search("bad\nquery")
        with self.assertRaises(ValueError):
            skill_hub.search("ok", source="unreviewed")
        identifier = "skills-sh/example/workflows/accessibility"
        skill_hub.SEEN[identifier] = skill_hub.INSPECTED[identifier] = skill_hub.time.monotonic()
        with patch.object(skill_hub, "_run", side_effect=RuntimeError("Scan blocked")):
            with self.assertRaisesRegex(RuntimeError, "Scan blocked"):
                skill_hub.install(identifier)


if __name__ == "__main__":
    unittest.main()
