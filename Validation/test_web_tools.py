"""Business contact lookup must return live page evidence, not search snippets."""

import unittest
from unittest.mock import patch

import web_tools


class BusinessContactTests(unittest.TestCase):
    def test_contact_from_matching_live_page(self):
        search = {
            "results": [
                {"url": "https://unrelated.example/", "title": "Other company"},
                {
                    "url": "https://puremichiganlawncare.example/",
                    "title": "Pure Michigan Lawn Care",
                },
            ]
        }
        page = {
            "source_url": "https://puremichiganlawncare.example/",
            "title": "Pure Michigan Lawn Care",
            "retrieved_at": "2026-10-31T00:00:00Z",
            "contact_links": ["tel:+15172581313", "mailto:office@example.com"],
            "text": "Call (517) 258-1313 for a quote.",
        }
        with (
            patch.object(web_tools, "search_web", return_value=search),
            patch.object(web_tools, "read_webpage", return_value=page) as read,
        ):
            result = web_tools.find_business_contact("Pure Michigan Lawn Care")
        read.assert_called_once_with("https://puremichiganlawncare.example/")
        self.assertEqual(result["sources"][0]["phone_links"], ["tel:+15172581313"])
        self.assertEqual(result["sources"][0]["source_url"], page["source_url"])

    def test_no_live_phone_link_is_not_reported_as_verified(self):
        search = {
            "results": [
                {"url": "https://business.example/", "title": "Business phone 123"}
            ]
        }
        page = {
            "source_url": "https://business.example/",
            "title": "Business",
            "retrieved_at": "2026-10-31T00:00:00Z",
            "contact_links": [],
            "text": "No contact page",
        }
        with (
            patch.object(web_tools, "search_web", return_value=search),
            patch.object(web_tools, "read_webpage", return_value=page),
        ):
            result = web_tools.find_business_contact("Business")
        self.assertEqual(result["sources"], [])
        self.assertIn("No matching", result["error"])


if __name__ == "__main__":
    unittest.main()
