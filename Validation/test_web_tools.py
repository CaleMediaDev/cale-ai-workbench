"""Business contact lookup must return live page evidence, not search snippets."""

import unittest
from unittest.mock import patch

import web_tools


class BusinessContactTests(unittest.TestCase):
    def test_search_falls_back_after_brave_rate_limit(self):
        html = (
            '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.gov%2Fstory">'
            'Official story</a>'
        )
        with patch.object(
            web_tools, "fetch",
            side_effect=[RuntimeError("HTTP 429"), (
                "https://lite.duckduckgo.com/lite/?q=test", html,
                "text/html", 200,
            )],
        ) as fetch:
            result = web_tools.search_web("test")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["search_engine"], "DuckDuckGo Lite")
        self.assertEqual(result["results"][0]["url"], "https://example.gov/story")

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

    def test_contact_page_on_same_business_site_beats_earlier_directory(self):
        search = {"results": [
            {"url": "https://directory.example/pure-michigan", "title": "Pure Michigan Lawn Care listing"},
            {"url": "https://puremichiganlawncare.com/", "title": "Pure Michigan Lawn Care"},
            {"url": "https://puremichiganlawncare.com/contact", "title": "Contact Pure Michigan Lawn Care"},
        ]}

        def read(url):
            return {
                "source_url": url,
                "title": "Pure Michigan Lawn Care",
                "retrieved_at": "2026-10-31T00:00:00Z",
                "contact_links": [] if url.endswith(".com/") else ["tel:+15172581313"],
                "text": "Call (517) 258-1313" if not url.endswith(".com/") else "Welcome",
            }

        with (
            patch.object(web_tools, "search_web", return_value=search),
            patch.object(web_tools, "read_webpage", side_effect=read) as reader,
        ):
            result = web_tools.find_business_contact("Pure Michigan Lawn Care")
        self.assertEqual(result["sources"][0]["source_url"], "https://puremichiganlawncare.com/contact")
        self.assertTrue(result["sources"][0]["name_matching_domain"])
        self.assertEqual(reader.call_count, 3)


if __name__ == "__main__":
    unittest.main()
