"""Preview source selection and local URL boundary checks."""

import json
import tempfile
import unittest
from pathlib import Path

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
