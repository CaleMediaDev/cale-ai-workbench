"""Exercise Cale's native browser against a local page, without cloud access."""

import json
import tempfile
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

import native_agent
import native_browser


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


def test_download_filename_stays_in_project_and_does_not_overwrite(tmp_path):
    first = native_browser._download_target(tmp_path, "../secret.txt")
    assert first.parent == tmp_path / "Downloads"
    first.write_text("saved", encoding="utf-8")
    second = native_browser._download_target(tmp_path, "../secret.txt")
    assert second != first
    assert first.read_text(encoding="utf-8") == "saved"
