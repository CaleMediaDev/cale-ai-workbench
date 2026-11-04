"""Exercise Cale's native browser against a local page, without cloud access."""

import json
import tempfile
from types import SimpleNamespace
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

import native_agent
import native_browser


def test_javascript_error_capture_is_bounded_deduplicated_and_drained():
    class ObservedPage:
        def __init__(self):
            self.events = {}

        def on(self, event, callback):
            self.events[event] = callback

    class Error:
        def __init__(self, message):
            self.message = message

        def __str__(self):
            raise AssertionError("Do not collect error stacks")

    page = ObservedPage()
    try:
        native_browser._watch_page(page)
        report = page.events["pageerror"]
        report(Error("missing helper\n    at private stack"))
        report(Error("missing helper"))
        report(Error("x" * 1000))
        for number in range(20):
            report(Error(f"failure {number}"))
        result = json.loads(native_browser._response(page, {"title": "App"}))
        assert len(result["javascript_errors"]) == native_browser.MAX_PAGE_ERRORS
        assert result["javascript_errors"][0] == "missing helper"
        assert max(map(len, result["javascript_errors"])) <= native_browser.MAX_PAGE_ERROR_LENGTH
        assert result["javascript_errors_truncated"] is True
        assert "Untrusted" in result["javascript_errors_note"]
        assert json.loads(native_browser._response(page, {}))["javascript_errors"] == []
        report(Error("child frame failure"))
        page.events["framenavigated"](SimpleNamespace(parent_frame=page))
        assert native_browser._PAGE_ERRORS[page]["messages"]
        page.events["framenavigated"](SimpleNamespace(parent_frame=None))
        assert json.loads(native_browser._response(page, {}))["javascript_errors"] == []
        page.events["close"](page)
        assert page not in native_browser._PAGE_ERRORS
        assert "console" not in page.events
    finally:
        native_browser._PAGE_ERRORS.pop(page, None)


def test_local_button_javascript_error_is_observed_and_fixed_reload_is_clean(tmp_path):
    pytest.importorskip("playwright")
    source = tmp_path / "index.html"
    source.write_text('<title>App</title><input aria-label="Name">'
                      '<button onclick="escapeHtml()">Run</button><p id="result"></p>', encoding="utf-8")
    try:
        opened = native_agent.execute_tool(tmp_path, "browser_open_project", {"path": "index.html"})
        if opened.startswith("Tool error:") and "Executable doesn't exist" in opened:
            pytest.skip("Chromium runtime is not installed in this test environment")
        assert json.loads(opened)["javascript_errors"] == []
        controls = json.loads(native_browser.execute(tmp_path, "snapshot", {}))["controls"]
        name = next(row["ref"] for row in controls if row["label"] == "Name")
        button = next(row["ref"] for row in controls if row["label"] == "Run")
        native_browser.execute(tmp_path, "type", {"ref": name, "text": "private-field-sentinel"})
        clicked = native_agent.execute_tool(tmp_path, "browser_click", {"ref": button})
        result = json.loads(clicked)
        assert any("escapeHtml is not defined" in error for error in result["javascript_errors"])
        assert "private-field-sentinel" not in clicked
        assert "passed" not in result
        assert json.loads(native_browser.execute(tmp_path, "read", {}))["javascript_errors"] == []
        # Leave an undrained old-document error, then replace the broken source.
        page = native_browser._page(tmp_path)
        page.evaluate("setTimeout(() => {throw new Error('old document')}, 0)")
        page.wait_for_timeout(30)
        source.write_text('<title>Fixed</title><button onclick="document.querySelector(\'p\').textContent=\'Done\'">Run</button><p></p>', encoding="utf-8")
        assert json.loads(native_browser.execute(tmp_path, "open_project", {"path": "index.html"}))["javascript_errors"] == []
        controls = json.loads(native_browser.execute(tmp_path, "snapshot", {}))["controls"]
        assert json.loads(native_browser.execute(tmp_path, "click", {"ref": controls[0]["ref"]}))["javascript_errors"] == []
        assert json.loads(native_browser.execute(tmp_path, "read", {}))["text"] == "Run\n\nDone"
    finally:
        native_browser.close()


def test_existing_and_future_pages_observe_errors_and_context_close_cleans_state(tmp_path):
    pytest.importorskip("playwright")
    try:
        page = native_browser._page(tmp_path, local=True)
        context = page.context
        page.set_content("<body>Initial page</body>")
        page.evaluate("setTimeout(() => {throw new Error('initial failure')}, 0)")
        page.wait_for_timeout(30)
        assert json.loads(native_browser.execute(tmp_path, "read", {}))["javascript_errors"] == ["initial failure"]
        (tmp_path / "startup.html").write_text(
            '<body>Startup<script>throw new Error("startup failure")</script></body>', encoding="utf-8")
        assert json.loads(native_browser.execute(tmp_path, "open_project", {"path": "startup.html"}))["javascript_errors"] == ["startup failure"]
        future = context.new_page()
        future.set_content("<body>Future page</body>")
        future.evaluate("console.error('noisy widget'); setTimeout(() => {throw new Error('future failure')}, 0)")
        future.wait_for_timeout(30)
        assert json.loads(native_browser.execute(tmp_path, "read", {}))["javascript_errors"] == ["future failure"]
        future.close()
        assert future not in native_browser._PAGE_ERRORS
        assert page in native_browser._PAGE_ERRORS
        context.close()
        assert page not in native_browser._PAGE_ERRORS
        assert (str(tmp_path.resolve()), True) not in native_browser._CONTEXTS
    finally:
        native_browser.close()
    assert not native_browser._PAGE_ERRORS


def test_rejected_file_url_does_not_start_a_browser(tmp_path):
    with patch.object(native_browser, "_page", side_effect=AssertionError("browser started")):
        with pytest.raises(ValueError, match="full http"):
            native_browser.execute(tmp_path, "open", {"url": "file:///tmp/page.html"})


def test_local_project_open_rejects_escape_before_browser_launch(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    (tmp_path / "outside.html").write_text("outside", encoding="utf-8")
    (folder / "notes.txt").write_text("not HTML", encoding="utf-8")
    with patch.object(native_browser, "_page", side_effect=AssertionError("browser started")):
        for path in ("../outside.html", str(tmp_path / "outside.html"), "notes.txt", "missing.html"):
            with pytest.raises(ValueError):
                native_browser.execute(folder, "open_project", {"path": path})


def test_local_project_preview_is_offline_interactive_and_separate_from_online_profile(tmp_path):
    pytest.importorskip("playwright")
    requests = []

    class CountedPage(Page):
        def do_GET(self):
            requests.append(self.path)
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), CountedPage)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        native_browser.execute(tmp_path, "open", {"url": url})
        online_page = native_browser._page(tmp_path)
        (tmp_path / "game.html").write_text(
            '<!doctype html><title>Local arcade</title><body>'
            '<button onclick="document.getElementById(\'score\').textContent=\'1\'">Fire</button>'
            '<p id="score">0</p>'
            f'<script src="{url}should-be-blocked"></script></body>', encoding="utf-8")
        result = json.loads(native_agent.execute_tool(tmp_path, "browser_open_project", {"path": "game.html"}))
        assert result["title"] == "Local arcade"
        offline_page = native_browser._page(tmp_path)
        assert offline_page is not online_page
        assert "/should-be-blocked" not in requests
        snapshot = json.loads(native_browser.execute(tmp_path, "snapshot", {}))
        fire = next(item["ref"] for item in snapshot["controls"] if item["label"] == "Fire")
        native_browser.execute(tmp_path, "click", {"ref": fire})
        assert offline_page.locator("#score").inner_text() == "1"
        assert (tmp_path / ".local-ai/browser.png").is_file()
        assert (tmp_path / ".local-ai/project-browser-profile").is_dir()
        native_browser.execute(tmp_path, "open", {"url": url})
        assert native_browser._page(tmp_path) is online_page
    finally:
        native_browser.close()
        server.shutdown()
        server.server_close()


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/download":
            content = b"private project download"
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="example.txt"')
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if self.path == "/other":
            content = b"<html><title>Other page</title><body>Another page</body></html>"
        else:
            content = (b"<html><title>Browser check</title><body>"
                       b"<input aria-label='Name'><select aria-label='Color'>"
                       b"<option value='red'>Red</option><option value='blue'>Blue</option></select>"
                       b"<button onclick=\"document.getElementById('result').textContent="
                       b"document.querySelector('input').value + ' ' + document.querySelector('select').value\">"
                       b"Show</button><a href='/other'>Other</a><a href='/download'>Download</a>"
                       b"<p id='result'></p></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_):
        pass


def test_native_browser_can_fill_click_read_and_preview():
    pytest.importorskip("playwright")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ExitStack() as stack:
            temporary = stack.enter_context(tempfile.TemporaryDirectory())
            stack.callback(native_browser.close)
            folder = Path(temporary)
            url = f"http://127.0.0.1:{server.server_port}/"
            opened = native_agent.execute_tool(folder, "browser_open", {"url": url})
            if opened.startswith("Tool error:") and "Executable doesn't exist" in opened:
                pytest.skip("Chromium runtime is not installed in this test environment")
            assert json.loads(opened)["title"] == "Browser check"
            snapshot = json.loads(native_agent.execute_tool(folder, "browser_snapshot", {}))
            fields = {item["label"]: item["ref"] for item in snapshot["controls"]}
            native_agent.execute_tool(folder, "browser_type", {"ref": fields["Name"], "text": "Cale"})
            assert json.loads(native_agent.execute_tool(
                folder, "browser_select", {"ref": fields["Color"], "value": "blue"}
            ))["selected"] == ["blue"]
            native_agent.execute_tool(folder, "browser_click", {"ref": fields["Show"]})
            assert "Cale blue" in json.loads(native_agent.execute_tool(folder, "browser_read", {}))["text"]
            native_agent.execute_tool(folder, "browser_press", {"ref": fields["Name"], "key": "Tab"})
            saved = json.loads(native_agent.execute_tool(folder, "browser_download", {"ref": fields["Download"]}))
            assert saved["saved"] == "Downloads/example.txt"
            assert (folder / saved["saved"]).read_bytes() == b"private project download"
            import project_memory
            assert project_memory.search(folder, "private project download", kind="file")
            inspected = json.loads(native_agent.execute_tool(
                folder, "inspect_project_file", {"path": saved["saved"]}))
            assert inspected["text"] == "private project download"
            native_agent.execute_tool(folder, "browser_click", {"ref": fields["Other"]})
            assert "Another page" in json.loads(native_agent.execute_tool(folder, "browser_read", {}))["text"]
            native_agent.execute_tool(folder, "browser_back", {})
            assert "Browser check" in json.loads(native_agent.execute_tool(folder, "browser_read", {}))["title"]
            assert (folder / ".local-ai/browser.png").is_file()
            assert native_agent.execute_tool(folder, "browser_open", {"url": "file:///etc/passwd"}).startswith("Tool error:")
            native_browser.close()
    finally:
        native_browser.close()
        server.shutdown()
        server.server_close()


def test_snapshot_clears_refs_from_controls_that_become_hidden(tmp_path):
    pytest.importorskip("playwright")
    (tmp_path / "controls.html").write_text(
        '<!doctype html><title>Snapshot refs</title><body>'
        '<button id="first">First</button>'
        '<button id="second" onclick="this.textContent=\'Clicked\'">Second</button>'
        '</body>', encoding="utf-8")
    try:
        opened = native_agent.execute_tool(
            tmp_path, "browser_open_project", {"path": "controls.html"})
        if opened.startswith("Tool error:") and "Executable doesn't exist" in opened:
            pytest.skip("Chromium runtime is not installed in this test environment")
        assert json.loads(opened)["title"] == "Snapshot refs"
        first = json.loads(native_browser.execute(tmp_path, "snapshot", {}))
        assert [item["label"] for item in first["controls"]] == ["First", "Second"]
        page = native_browser._page(tmp_path)
        page.locator("#first").evaluate("el => el.style.display = 'none'")
        second = json.loads(native_browser.execute(tmp_path, "snapshot", {}))
        assert [item["label"] for item in second["controls"]] == ["Second"]
        ref = second["controls"][0]["ref"]
        assert ref == first["controls"][0]["ref"]
        assert page.locator("#first").get_attribute("data-cale-ref") is None
        assert page.locator(f'[data-cale-ref="{ref}"]').count() == 1
        native_browser.execute(tmp_path, "click", {"ref": ref})
        assert page.locator("#second").inner_text() == "Clicked"
    finally:
        native_browser.close()


def test_closed_project_context_is_replaced_on_next_open(tmp_path):
    pytest.importorskip("playwright")
    (tmp_path / "page.html").write_text(
        '<!doctype html><title>Recovered browser</title><body>Ready</body>',
        encoding="utf-8")
    try:
        opened = native_agent.execute_tool(
            tmp_path, "browser_open_project", {"path": "page.html"})
        if opened.startswith("Tool error:") and "Executable doesn't exist" in opened:
            pytest.skip("Chromium runtime is not installed in this test environment")
        assert json.loads(opened)["title"] == "Recovered browser"
        old_context = native_browser._page(tmp_path).context
        old_context.close()

        reopened = json.loads(native_browser.execute(
            tmp_path, "open_project", {"path": "page.html"}))

        assert reopened["title"] == "Recovered browser"
        assert native_browser._page(tmp_path).context is not old_context
        assert native_browser._ACTIVE_LOCAL[str(tmp_path.resolve())] is True
    finally:
        native_browser.close()


def test_download_filename_collision_search_continues_past_999(tmp_path):
    destination = tmp_path / "Downloads"
    destination.mkdir()
    original = destination / "report.txt"
    original.write_text("original", encoding="utf-8")
    for number in range(2, 1000):
        (destination / f"report-{number}.txt").write_text("saved", encoding="utf-8")

    target = native_browser._download_target(tmp_path, "report.txt")

    assert target == destination / "report-1000.txt"
    assert not target.exists()
    assert original.read_text(encoding="utf-8") == "original"
    assert (destination / "report-999.txt").read_text(encoding="utf-8") == "saved"


def test_download_filename_does_not_reuse_dangling_symlink(tmp_path):
    destination = tmp_path / "Downloads"
    destination.mkdir()
    missing = tmp_path / "outside-downloads.txt"
    existing = destination / "report.txt"
    try:
        existing.symlink_to(missing)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable in this test environment")
    assert existing.is_symlink()
    assert not existing.exists()

    target = native_browser._download_target(tmp_path, "report.txt")

    assert target == destination / "report-2.txt"
    target.write_text("downloaded", encoding="utf-8")
    assert existing.is_symlink()
    assert existing.readlink() == missing
    assert not missing.exists()
    assert target.read_text(encoding="utf-8") == "downloaded"


@pytest.mark.parametrize("character", ["\U0001f600", "\u754c"])
def test_download_multibyte_filename_can_be_saved_and_collision_resolved(tmp_path, character):
    filename = character * 120 + ".txt"

    first = native_browser._download_target(tmp_path, filename)
    assert len(first.name.encode("utf-8")) <= 120
    assert first.name
    first.write_bytes(b"first download")

    second = native_browser._download_target(tmp_path, filename)
    assert second != first
    assert second.parent == first.parent == tmp_path / "Downloads"
    second.write_bytes(b"second download")
    assert first.read_bytes() == b"first download"
    assert second.read_bytes() == b"second download"


def test_download_directory_symlink_cannot_redirect_outside_project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = folder / "Downloads"
    try:
        destination.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are unavailable in this test environment")

    with pytest.raises(ValueError, match="inside the task folder"):
        target = native_browser._download_target(folder, "report.txt")
        target.write_bytes(b"downloaded")

    assert destination.is_symlink()
    assert list(outside.iterdir()) == []


def test_download_filename_stays_in_project_and_does_not_overwrite(tmp_path):
    first = native_browser._download_target(tmp_path, "../secret.txt")
    assert first.parent == tmp_path / "Downloads"
    first.write_text("saved", encoding="utf-8")
    second = native_browser._download_target(tmp_path, "../secret.txt")
    assert second != first
    assert first.read_text(encoding="utf-8") == "saved"
