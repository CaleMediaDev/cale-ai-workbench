"""Exercise Cale's native browser against a local page, without cloud access."""

import json
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

import native_agent
import native_browser


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        content = (b"<html><title>Browser check</title><body>"
                   b"<input aria-label='Name'><button onclick=\"document.getElementById('result').textContent="
                   b"document.querySelector('input').value\">Show</button><p id='result'></p></body></html>")
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
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            url = f"http://127.0.0.1:{server.server_port}/"
            opened = native_agent.execute_tool(folder, "browser_open", {"url": url})
            if opened.startswith("Tool error:") and "Executable doesn't exist" in opened:
                pytest.skip("Chromium runtime is not installed in this test environment")
            assert json.loads(opened)["title"] == "Browser check"
            snapshot = json.loads(native_agent.execute_tool(folder, "browser_snapshot", {}))
            fields = {item["label"]: item["ref"] for item in snapshot["controls"]}
            native_agent.execute_tool(folder, "browser_type", {"ref": fields["Name"], "text": "Cale"})
            native_agent.execute_tool(folder, "browser_click", {"ref": fields["Show"]})
            assert "Cale" in json.loads(native_agent.execute_tool(folder, "browser_read", {}))["text"]
            assert (folder / ".local-ai/browser.png").is_file()
            assert native_agent.execute_tool(folder, "browser_open", {"url": "file:///etc/passwd"}).startswith("Tool error:")
            native_browser.close()
    finally:
        native_browser.close()
        server.shutdown()
        server.server_close()
