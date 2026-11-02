"""Business contact lookup must return live page evidence, not search snippets."""

import unittest
import os
from unittest.mock import patch

import web_tools


class BusinessContactTests(unittest.TestCase):
    def test_web_reader_uses_bundled_curl_when_windows_omits_it(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as folder:
            root = Path(folder)
            bundled = root / "data/tools/git-2.53.0+3-win32-x64/mingw64/bin/curl.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"bundled curl")
            with patch.dict(os.environ, {"SystemRoot": str(root / "no-windows-curl")}):
                self.assertEqual(web_tools.curl_executable(root), str(bundled))
                bundled.unlink()
                with self.assertRaisesRegex(RuntimeError, "web downloader is missing"):
                    web_tools.curl_executable(root)

    def test_business_name_does_not_match_a_similar_company(self):
        self.assertFalse(web_tools.business_name_matches(
            "Pure Michigan Lawn Care", "Pure Precision Lawn Care",
            "https://pureprecisionlawncare.com/contact-us.html",
        ))
        self.assertTrue(web_tools.business_name_matches(
            "Pure Michigan Lawn Care", "Pure Michigan Lawn Care listing",
            "https://directory.example/listing",
        ))

    def test_public_emails_ignores_placeholder_and_examples(self):
        page = {
            "contact_links": ["mailto:Office%40RealBusiness.com?subject=Hello", "tel:+15551234567"],
            "text": "[email protected] Use sample@example.com for demos.",
        }
        self.assertEqual(web_tools.public_emails(page), ["office@realbusiness.com"])

    def test_business_email_uses_live_mailto_source(self):
        search = {"results": [{
            "url": "https://puremichiganlawncare.com/contact",
            "title": "Pure Michigan Lawn Care contact",
        }]}
        page = {
            "source_url": "https://puremichiganlawncare.com/contact",
            "title": "Pure Michigan Lawn Care",
            "retrieved_at": "2026-10-31T00:00:00Z",
            "contact_links": ["mailto:hello@puremichiganlawncare.com"],
            "text": "Contact Pure Michigan Lawn Care.",
        }
        with (
            patch.object(web_tools, "search_web", return_value=search),
            patch.object(web_tools, "read_webpage", return_value=page) as read,
        ):
            result = web_tools.find_business_email("Pure Michigan Lawn Care")
        read.assert_called_once_with(
            "https://puremichiganlawncare.com/contact",
            question="Pure Michigan Lawn Care public email address",
        )
        self.assertEqual(result["sources"][0]["email_addresses"], ["hello@puremichiganlawncare.com"])

    def test_question_selects_late_live_page_passage(self):
        body = "Opening notes.\n" + "Ordinary material.\n" * 1400
        body += "The current warranty code is W-4817.\n"
        body += "Ordinary material.\n" * 1400
        with patch.object(
            web_tools, "fetch", return_value=("https://example.org/details", body, "text/plain", 200)
        ):
            page = web_tools.read_webpage(
                "https://example.org/details", question="What is the current warranty code?"
            )
        self.assertIn("W-4817", page["text"])
        self.assertIn("Selected excerpts from a longer page", page["text"])
        self.assertLessEqual(len(page["text"]), 3500)
        self.assertTrue(page["text_truncated"])
        self.assertIsNone(page["next_start_char"])

    def test_exact_find_still_supports_paginated_reading(self):
        body = "Intro. " + "Filler. " * 3000 + "NEEDLE at the end."
        with patch.object(
            web_tools, "fetch", return_value=("https://example.org/details", body, "text/plain", 200)
        ):
            page = web_tools.read_webpage(
                "https://example.org/details", find="NEEDLE", question="What is the needle?"
            )
        self.assertIn("NEEDLE", page["text"])
        self.assertNotIn("Selected excerpts", page["text"])

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

    def test_contact_lookup_reports_unreadable_live_pages(self):
        search = {"results": [{"url": "https://business.example/contact", "title": "Business contact"}]}
        with (
            patch.object(web_tools, "search_web", return_value=search),
            patch.object(web_tools, "read_webpage", side_effect=RuntimeError("TLS error")),
        ):
            result = web_tools.find_business_contact("Business")
        self.assertEqual(result["lookup_status"], "pages_unavailable")
        self.assertEqual(result["sources"], [])

    def test_contact_lookup_reports_failed_live_search(self):
        with patch.object(web_tools, "search_web", return_value={"results": [], "error": "search failed"}):
            result = web_tools.find_business_contact("Business")
        self.assertEqual(result["lookup_status"], "search_unavailable")

    def test_labeled_visible_phone_without_tel_link_is_found(self):
        search = {"results": [{
            "url": "https://puremichiganlawncare.example/contact",
            "title": "Pure Michigan Lawn Care contact",
        }]}
        page = {
            "source_url": "https://puremichiganlawncare.example/contact",
            "title": "Pure Michigan Lawn Care",
            "retrieved_at": "2026-11-01T00:00:00Z",
            "contact_links": [],
            "text": "Phone: (517) 258-1313\nFax: (517) 555-0100\nReference 5172589999",
        }
        with (
            patch.object(web_tools, "search_web", return_value=search),
            patch.object(web_tools, "read_webpage", return_value=page),
        ):
            result = web_tools.find_business_contact("Pure Michigan Lawn Care")
        self.assertEqual(result["sources"][0]["phone_links"], [])
        self.assertEqual(result["sources"][0]["visible_numbers"], ["(517) 258-1313"])

    def test_unlabeled_number_is_not_treated_as_contact(self):
        self.assertEqual(web_tools.visible_phone_numbers("Reference: 5172581313"), [])
        self.assertEqual(web_tools.visible_phone_numbers("Fax: (517) 258-1313"), [])

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
