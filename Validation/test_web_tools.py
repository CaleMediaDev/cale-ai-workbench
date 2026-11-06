"""Business contact lookup must return live page evidence, not search snippets."""

import os
import unittest
from unittest.mock import patch

import web_tools


class BusinessContactTests(unittest.TestCase):
    def test_web_reader_uses_bundled_curl_when_windows_omits_it(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

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

    def test_reader_parses_mixed_case_html_content_type(self):
        body = (
            '<html><head><title>Business contact</title></head><body>'
            '<p>Call our office.</p><a href="tel:+15172581313">Phone</a>'
            '</body></html>'
        )
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/contact", body, "Text/HTML; charset=UTF-8", 200),
        ):
            page = web_tools.read_webpage("https://example.org/contact")
        self.assertEqual(page["title"], "Business contact")
        self.assertEqual(page["contact_links"], ["tel:+15172581313"])
        self.assertIn("Call our office.", page["text"])
        self.assertNotIn("<", page["text"])

    def test_reader_preserves_mixed_case_contact_schemes(self):
        body = (
            '<a href="TEL:+15172581313">Phone</a>'
            '<a href="MaIlTo:Office@RealBusiness.com">Email</a>'
        )
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/contact", body, "text/html", 200),
        ):
            page = web_tools.read_webpage("https://example.org/contact")
        self.assertEqual(page["contact_links"], [
            "TEL:+15172581313", "MaIlTo:Office@RealBusiness.com",
        ])
        self.assertEqual(web_tools.public_emails(page), ["office@realbusiness.com"])

    def test_reader_ignores_malformed_link_and_keeps_page_content(self):
        body = (
            '<title>Contact details</title>'
            '<p>Before <a href="https://[broken">broken link</a> after.</p>'
            '<a href="/contact">Contact page</a>'
            '<a href="tel:+15172581313">Phone</a>'
        )
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/details", body, "text/html", 200),
        ):
            page = web_tools.read_webpage("https://example.org/details")
        self.assertEqual(page["title"], "Contact details")
        self.assertIn("Before broken link after.", page["text"])
        self.assertEqual(page["links"], [
            {"url": "https://example.org/contact", "title": "Contact page"},
        ])
        self.assertEqual(page["contact_links"], ["tel:+15172581313"])

    def test_reader_preserves_trailing_ampersand(self):
        body = '<p>Research &amp; development</p>Contact &'
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/details", body, "text/html", 200),
        ):
            page = web_tools.read_webpage("https://example.org/details")
        self.assertEqual(page["text"], "Research & development\nContact &")

    def test_reader_preserves_table_cell_text_boundaries(self):
        body = (
            '<table><tr><th>Contact</th><th>Details</th></tr>'
            '<tr><td>Phone</td><td>5172581313</td></tr></table>'
        )
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/contact", body, "text/html", 200),
        ):
            page = web_tools.read_webpage("https://example.org/contact")
        self.assertEqual(page["text"], "Contact\nDetails\nPhone\n5172581313")
        self.assertEqual(web_tools.visible_phone_numbers(page["text"]), ["5172581313"])

    def test_reader_preserves_lower_level_heading_text_boundaries(self):
        for tag in ("h4", "h5", "h6"):
            with self.subTest(tag=tag):
                body = f'Contact details<{tag}>Phone</{tag}>5172581313'
                with patch.object(
                    web_tools, "fetch",
                    return_value=("https://example.org/contact", body, "text/html", 200),
                ):
                    page = web_tools.read_webpage("https://example.org/contact")
                self.assertEqual(page["text"], "Contact details\nPhone\n5172581313")
                self.assertEqual(
                    web_tools.visible_phone_numbers(page["text"]), ["5172581313"]
                )

    def test_reader_preserves_description_list_text_boundaries(self):
        body = '<dl><dt>Phone</dt><dd>5172581313</dd></dl>Contact us'
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/contact", body, "text/html", 200),
        ):
            page = web_tools.read_webpage("https://example.org/contact")
        self.assertEqual(page["text"], "Phone\n5172581313\nContact us")
        self.assertEqual(web_tools.visible_phone_numbers(page["text"]), ["5172581313"])

    def test_reader_preserves_closing_block_text_boundaries(self):
        body = (
            '<p>First <strong>paragraph</strong>.</p>Trailing text'
            '<div>Second block.</div>More text'
        )
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/details", body, "text/html", 200),
        ):
            page = web_tools.read_webpage("https://example.org/details")
        self.assertEqual(
            page["text"],
            "First paragraph.\nTrailing text\nSecond block.\nMore text",
        )

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

    def test_exact_find_maps_casefolded_offsets_to_original_text(self):
        body = "NEEDLE " + "ß" * 16000 + "Needle at the end."
        expected_match = body.index("Needle")
        with patch.object(
            web_tools, "fetch",
            return_value=("https://example.org/details", body, "text/plain", 200),
        ):
            for start in (7, expected_match):
                with self.subTest(start_char=start):
                    page = web_tools.read_webpage(
                        "https://example.org/details", find="needle", start_char=start
                    )
                    self.assertTrue(page["find_matched"])
                    self.assertEqual(page["start_char"], expected_match - 1000)
                    self.assertIn("Needle at the end.", page["text"])
                    self.assertEqual(page["text"], body[expected_match - 1000:])

    def test_search_skips_malformed_duckduckgo_redirect_target(self):
        html = (
            '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2F%5Bbroken">'
            'Malformed result</a>'
            '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.gov%2Fstory">'
            'Official story</a>'
        )
        with patch.object(
            web_tools, "fetch",
            side_effect=[RuntimeError("HTTP 429"), (
                "https://lite.duckduckgo.com/lite/?q=test", html,
                "text/html", 200,
            )],
        ):
            result = web_tools.search_web("test")
        self.assertEqual(result["search_engine"], "DuckDuckGo Lite")
        self.assertEqual(result["results"], [
            {"title": "Official story", "url": "https://example.gov/story"},
        ])

    def test_search_only_unwraps_actual_duckduckgo_hosts(self):
        lookalike = "https://notduckduckgo.com/story?uddg=https%3A%2F%2Fother.example%2F"
        html = (
            f'<a href="{lookalike}">Original story</a>'
            '<a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.gov%2Fstory">'
            'Official story</a>'
            '<a href="https://lite.duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fstory">'
            'Another story</a>'
        )
        with patch.object(
            web_tools, "fetch",
            return_value=("https://search.brave.com/search?q=test", html, "text/html", 200),
        ):
            result = web_tools.search_web("test")
        self.assertEqual(result["results"], [
            {"title": "Original story", "url": lookalike},
            {"title": "Official story", "url": "https://example.gov/story"},
            {"title": "Another story", "url": "https://example.org/story"},
        ])

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

    def test_visible_phone_limit_counts_distinct_numbers(self):
        numbers = [f"(517) 555-010{i}" for i in range(10)]
        text = (f"Phone: {numbers[0]}\n" * 8) + "\n".join(
            f"Phone: {number}" for number in numbers[1:]
        )
        self.assertEqual(web_tools.visible_phone_numbers(text), numbers[:8])

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
