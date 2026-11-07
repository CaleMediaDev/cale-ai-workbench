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

    def test_non_html_symlink_target_does_not_block_static_html_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            (folder / "notes.txt").write_text("not a preview", encoding="utf-8")
            (folder / "dist").mkdir()
            fallback = folder / "dist/index.html"
            fallback.write_text("<html>preview</html>", encoding="utf-8")
            try:
                (folder / "index.html").symlink_to("notes.txt")
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"Symlinks unavailable: {error}")
            self.assertEqual(preview.source(folder), fallback.as_uri())

    def test_unresolvable_project_root_returns_no_preview(self):
        for error in (
            PermissionError("Cannot search project directory"),
            RuntimeError("Symlink loop at project root"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(Path, "resolve", side_effect=error):
                    self.assertEqual(preview.source("project"), "")

    def test_invalid_project_root_types_return_no_preview(self):
        for invalid_root in (None, 17):
            with self.subTest(root=invalid_root):
                self.assertEqual(preview.source(invalid_root), "")

    def test_project_root_with_embedded_nul_returns_no_preview(self):
        with tempfile.TemporaryDirectory() as temporary:
            invalid_root = str(Path(temporary) / "project") + "\x00"
            self.assertEqual(preview.source(invalid_root), "")

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

    def test_external_declaration_symlink_falls_back_to_static_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            folder = root / "project"
            folder.mkdir()
            fallback = folder / "index.html"
            fallback.write_text("hello", encoding="utf-8")
            (folder / ".local-ai").mkdir()
            external = root / "preview.json"
            external.write_text(
                json.dumps({"url": "http://localhost:3000"}), encoding="utf-8"
            )
            try:
                (folder / ".local-ai/preview.json").symlink_to(external)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"Symlinks unavailable: {error}")
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


class CreateConversationTests(unittest.TestCase):
    """Exercise the actual nested UI code without launching app services/databases."""

    @staticmethod
    def source_nodes():
        import ast
        tree = ast.parse((Path(__file__).resolve().parents[1] / "long_tasks.py").read_text(encoding="utf-8"))
        return ast, list(ast.walk(tree))

    def function(self, name, environment):
        import copy
        ast, nodes = self.source_nodes()
        node = copy.deepcopy(next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == name))
        node.body = [n for n in node.body if not isinstance(n, ast.Nonlocal)]
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "<create-function>", "exec"), environment)
        return environment[name]

    def test_finished_project_changes_queue_note_without_creating_another_task(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        project = {"id": "finished-game", "title": "Space shooter", "status": "accepted"}
        request = Mock()
        reply = Mock()
        environment = dict(
            pending_followup=None, donation_service=SimpleNamespace(activity=lambda: None),
            first_launch_readiness=lambda _: (True, ""), STATE=None,
            attachments=[], chat_context=lambda _: [], save_chat=Mock(), show_chat=Mock(),
            chat_assistant=SimpleNamespace(attach_request=lambda text, _: text,
                FILE_MEMORY_REFERENCE=SimpleNamespace(search=lambda _: False),
                PERSONAL_CONTACT=SimpleNamespace(search=lambda _: False),
                private_history_for=lambda *_: False),
            projects=lambda: [project], request=request, assistant_reply=reply, render_cards=Mock(),
        )
        send = self.function("submit_chat_text", environment)
        self.assertTrue(send("Add boss fights", use_attachments=False, project_id=project["id"]))
        request.assert_called_once_with(project["id"], "note", "Add boss fights")
        request.reset_mock()
        self.assertFalse(send("Add enemies", use_attachments=False, project_id="removed-game"))
        request.assert_not_called()

    def test_create_composer_edits_preview_and_new_project_starts_build(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        prompt = Mock()
        prompt.get.return_value = "Add shields"
        send = Mock(return_value=True)
        environment = dict(create_prompt=prompt, first_launch_readiness=lambda _: (True, ""),
            STATE=None, create_status=Mock(), preview_task_id="existing-game", submit_chat_text=send,
            create_kind=SimpleNamespace(get=lambda: "Game"), create_project_selection=Mock())
        submit = self.function("submit_creation", environment)
        submit()
        send.assert_called_once_with("Add shields", use_attachments=False, intent="task", project_id="existing-game")
        send.reset_mock()
        environment["preview_task_id"] = None
        submit()
        send.assert_called_once_with("Build a game: Add shields", use_attachments=False, intent="task", project_id=None)

    def test_chat_and_send_remain_visible_at_minimum_window_size(self):
        import tkinter as tk
        from tkinter import ttk
        ast, nodes = self.source_nodes()
        owner = next(n for n in nodes if isinstance(n, ast.FunctionDef)
            and any(isinstance(c, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "create_chat" for t in c.targets) for c in n.body))
        start = next(i for i, n in enumerate(owner.body) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "create_chat" for t in n.targets))
        end = next(i for i, n in enumerate(owner.body) if isinstance(n, ast.FunctionDef) and n.name == "submit_creation")
        button_start = next(i for i, n in enumerate(owner.body) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "create_build_button" for t in n.targets))
        root = tk.Tk()
        try:
            root.geometry("1050x740")
            body = ttk.Frame(root)
            body.pack(fill="both", expand=True, pady=(150, 20))
            body.columnconfigure(0, weight=1, minsize=270)
            body.columnconfigure(1, weight=3, minsize=480)
            body.rowconfigure(0, weight=1)
            environment = dict(tk=tk, ttk=ttk, create_body=body, card="#151923", text="#eeeeee",
                field="#111111", chat_mirrors=[], submit_creation=lambda: None)
            code = owner.body[start:end] + owner.body[button_start:button_start+2]
            exec(compile(ast.fix_missing_locations(ast.Module(body=code, type_ignores=[])), "<create-layout>", "exec"), environment)
            preview = tk.Label(body, text="Game preview", width=60, height=30)
            preview.grid(row=0, column=1, sticky="nsew")
            # A complete build report previously pushed both chat and input out.
            environment["create_status"].set("Finished game report " * 1000)
            environment["create_conversation"].configure(state="normal")
            environment["create_conversation"].insert("end", "Build complete.\n" * 100)
            root.update()
            chat = environment["create_chat"]
            for name in ("create_prompt", "create_build_button", "create_history"):
                widget = environment[name]
                self.assertTrue(widget.winfo_ismapped(), name)
                self.assertGreater(widget.winfo_height(), 20, name)
                self.assertLessEqual(widget.winfo_y() + widget.winfo_height(), chat.winfo_height(), name)
            first, last = environment["create_conversation"].yview()
            self.assertLess(last - first, 1, "History must remain scrollable")
        finally:
            root.destroy()
