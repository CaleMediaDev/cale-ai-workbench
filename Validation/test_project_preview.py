"""Preview source selection and local URL boundary checks."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import project_preview as preview


class ProjectPreviewTests(unittest.TestCase):
    def test_web_project_is_selected_but_question_is_not(self):
        self.assertTrue(
            preview.wants_preview(
                {"status": "working", "title": "Build app", "brief": ""}
            )
        )
        self.assertFalse(
            preview.wants_preview(
                {"status": "accepted", "title": "Build app", "brief": ""}
            )
        )
        self.assertFalse(
            preview.wants_preview(
                {"status": "working", "title": "Find a number", "brief": ""}
            )
        )

    def test_wants_preview_handles_none_fields(self):
        self.assertFalse(
            preview.wants_preview(
                {"status": "working", "title": None, "brief": None}
            )
        )
        self.assertTrue(
            preview.wants_preview(
                {"status": "working", "title": "Build app", "brief": None}
            )
        )
        self.assertTrue(
            preview.wants_preview(
                {"status": "working", "title": None, "brief": "web app"}
            )
        )

    def test_non_text_fields_are_ignored_without_losing_valid_project_text(self):
        for invalid in (17, True, ["web app"], {"title": "web app"}):
            with self.subTest(value=invalid):
                self.assertFalse(preview.wants_preview({"status": "working", "title": invalid, "brief": invalid}))
                self.assertTrue(preview.wants_preview({"status": "working", "title": invalid, "brief": "Build dashboard"}))
                self.assertTrue(preview.wants_preview({"status": "working", "title": "game", "brief": invalid}))

    def test_only_explicit_loopback_servers_are_allowed(self):
        self.assertEqual(
            preview.safe_loopback_url("http://127.0.0.1:3000/"),
            "http://127.0.0.1:3000/",
        )
        self.assertEqual(
            preview.safe_loopback_url("https://localhost:8443"),
            "https://localhost:8443",
        )
        for url in (
            "https://example.com:443",
            "http://localhost",
            "http://name@localhost:3000",
            "file:///C:/x",
            "http://127.0.0.2:3000",
        ):
            self.assertEqual(preview.safe_loopback_url(url), "", url)

    def test_malformed_ipv6_authority_falls_back_to_static_html(self):
        self.assertEqual(
            preview.safe_loopback_url("http://[::1]:3000/"),
            "http://[::1]:3000/",
        )
        malformed = "http://[::1]invalid:3000/"
        self.assertEqual(preview.safe_loopback_url(malformed), "")
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            fallback = folder / "index.html"
            fallback.write_text("hello", encoding="utf-8")
            (folder / ".local-ai").mkdir()
            (folder / ".local-ai/preview.json").write_text(
                json.dumps({"url": malformed}), encoding="utf-8"
            )
            self.assertEqual(preview.source(folder), fallback.as_uri())

    def test_symlink_loop_does_not_block_static_html_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            (folder / "dist").mkdir()
            fallback = folder / "dist/index.html"
            fallback.write_text("hello", encoding="utf-8")
            try:
                (folder / "index.html").symlink_to("index.html")
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"Symlinks unavailable: {error}")
            self.assertEqual(preview.source(folder), fallback.as_uri())

    def test_unreadable_declaration_check_falls_back_to_static_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            fallback = folder / "index.html"
            fallback.write_text("hello", encoding="utf-8")
            declaration = folder / ".local-ai/preview.json"
            original_is_file = Path.is_file

            def is_file(path):
                if path == declaration:
                    raise PermissionError("Cannot search declaration directory")
                return original_is_file(path)

            with patch.object(Path, "is_file", new=is_file):
                self.assertEqual(preview.source(folder), fallback.as_uri())

    def test_deeply_nested_declaration_falls_back_to_static_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            fallback = folder / "index.html"
            fallback.write_text("hello", encoding="utf-8")
            (folder / ".local-ai").mkdir()
            depth = sys.getrecursionlimit() + 100
            (folder / ".local-ai/preview.json").write_text(
                "[" * depth + "0" + "]" * depth, encoding="utf-8"
            )
            self.assertEqual(preview.source(folder), fallback.as_uri())

    def test_declaration_and_static_html_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "index.html").write_text("hello", encoding="utf-8")
            self.assertEqual(preview.source(folder), (folder / "index.html").as_uri())
            (folder / ".local-ai").mkdir()
            (folder / ".local-ai/preview.json").write_text(
                json.dumps({"url": "http://localhost:3000"}), encoding="utf-8"
            )
            self.assertEqual(preview.source(folder), "http://localhost:3000")
            (folder / ".local-ai/preview.json").write_text(
                json.dumps({"url": "https://example.com:443"}), encoding="utf-8"
            )
            self.assertEqual(preview.source(folder), (folder / "index.html").as_uri())
            self.assertEqual(
                preview.browser_screenshot_path(folder),
                folder / ".local-ai/browser.png",
            )


if __name__ == "__main__":
    unittest.main()


def test_malformed_authority_rejected_when_legacy_parser_returns_it():
    from urllib.parse import SplitResult
    malformed = "http://[::1]invalid:3000/"
    # Older urllib implementations returned these hostname/port fields.
    # Require the app boundary to reject the complete malformed authority
    # even when its parser dependency does not reject it first.
    parsed = SplitResult("http", "[::1]invalid:3000", "/", "", "")
    assert parsed.hostname == "::1" and parsed.port == 3000
    with patch.object(preview, "urlsplit", return_value=parsed):
        assert preview.safe_loopback_url(malformed) == ""
