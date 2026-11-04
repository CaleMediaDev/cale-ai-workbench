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

    def test_search_matches_non_ascii_topic(self):
        identifier = "skills-sh/example/translation"
        payload = json.dumps([
            {
                "identifier": "skills-sh/example/unrelated",
                "name": "Unrelated",
                "description": "Check interface accessibility.",
            },
            {
                "identifier": identifier,
                "name": "日本語",
                "description": "Japanese translation assistance.",
            },
        ])
        with patch.object(skill_hub, "_run", return_value=payload):
            result = skill_hub.search("日本語")
        self.assertEqual(
            [match["identifier"] for match in result["matches"]],
            [identifier],
        )
        self.assertEqual(set(skill_hub.SEEN), {identifier})
        self.assertEqual(skill_hub.INSPECTED, {})

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

    def test_search_rejects_identifiers_from_another_registry(self):
        identifier = "skills-sh/example/accessibility"
        other_identifier = "github/example/accessibility"
        payload = json.dumps([
            {
                "identifier": other_identifier,
                "name": "accessibility",
                "source": "skills-sh",
            },
            {
                "identifier": identifier,
                "name": "accessibility",
                "source": "skills.sh",
            },
        ])
        with patch.object(skill_hub, "_run", return_value=payload):
            result = skill_hub.search("accessibility", source="skills-sh")
        self.assertEqual(result["source"], "skills-sh")
        self.assertEqual(
            [match["identifier"] for match in result["matches"]],
            [identifier],
        )
        self.assertEqual(set(skill_hub.SEEN), {identifier})
        self.assertEqual(skill_hub.INSPECTED, {})

    def test_rejects_dot_only_identifier_segments(self):
        valid_identifier = "skills-sh/example.org/accessibility.v2"
        invalid_identifiers = [
            f"skills-sh/{segment}/accessibility"
            for segment in (".", "..")
        ] + [
            f"skills-sh/example/{segment}/accessibility"
            for segment in (".", "..")
        ] + [
            f"skills-sh/example/{segment}"
            for segment in (".", "..")
        ]
        payload = json.dumps([
            {"identifier": identifier, "name": "accessibility"}
            for identifier in invalid_identifiers + [valid_identifier]
        ])
        with patch.object(skill_hub, "_run", return_value=payload):
            result = skill_hub.search("accessibility", limit=5)
        self.assertEqual(result["matches"], [])
        self.assertEqual(skill_hub.SEEN, {})

        with patch.object(skill_hub, "_run", return_value=json.dumps([
            {"identifier": valid_identifier, "name": "accessibility"}
        ])):
            result = skill_hub.search("accessibility")
        self.assertEqual(
            [match["identifier"] for match in result["matches"]],
            [valid_identifier],
        )

        with patch.object(skill_hub, "_run") as run:
            for identifier in invalid_identifiers:
                with self.subTest(identifier=identifier):
                    now = skill_hub.time.monotonic()
                    skill_hub.SEEN[identifier] = now
                    skill_hub.INSPECTED[identifier] = now
                    with self.assertRaises(ValueError):
                        skill_hub.inspect(identifier)
                    with self.assertRaises(ValueError):
                        skill_hub.install(identifier)
            run.assert_not_called()

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

    def test_install_rejects_expired_search_with_recent_inspection(self):
        identifier = "skills-sh/example/workflows/accessibility"
        payload = json.dumps([{
            "identifier": identifier,
            "name": "accessibility",
        }])
        with patch.object(skill_hub.time, "monotonic", return_value=100) as clock:
            with patch.object(
                skill_hub, "_run", side_effect=[payload, "Skill preview"]
            ) as run:
                skill_hub.search("accessibility")
                clock.return_value = 100 + skill_hub.MAX_AGE - 1
                skill_hub.inspect(identifier)
                clock.return_value = 100 + skill_hub.MAX_AGE + 1
                self.assertLess(
                    clock.return_value - skill_hub.INSPECTED[identifier],
                    skill_hub.MAX_AGE,
                )
                with self.assertRaisesRegex(ValueError, "Search and inspect"):
                    skill_hub.install(identifier)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.args, ("inspect", identifier))

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
