# tests/test_browser_real.py
"""
A real browser session end to end: browser_host.py started as its own process with the real Playwright launcher, two scripts in the SAME
session (page state must persist between them), a screenshot coming back as an output file, then close. Skipped where Playwright or Chromium
is not installed; CI runs it inside the built image, which is the point.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys

import pytest

HOST = os.path.join(os.path.dirname(__file__), "..", "runner", "browser_host.py")
MARKER = "__PARASYNC_BROWSER__"


def _chromium_available() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


# CI sets PARASYNC_REQUIRE_BROWSER=1 inside the built image: there a missing browser is a FAILURE, never a quiet skip.
REQUIRED = os.environ.get("PARASYNC_REQUIRE_BROWSER") == "1"
pytestmark = pytest.mark.skipif(not REQUIRED and not _chromium_available(), reason="Playwright with Chromium is not installed here")


def test_a_session_keeps_its_page_between_scripts_and_returns_a_screenshot(tmp_path):
    env = {**os.environ, "PARASYNC_BROWSER_WORK_ROOT": str(tmp_path), "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")}
    requests = [
        {"id": "1", "op": "exec", "code": "page.goto('data:text/html,<title>hello</title><h1 id=x>first</h1>')\nresult = page.title()"},
        {"id": "2", "op": "exec", "code": "page.screenshot(path='shot.png')\nresult = page.inner_text('#x')"},
        {"id": "3", "op": "close"},
    ]
    proc = subprocess.run(
        [sys.executable, HOST, "real1"], input="".join(json.dumps(r) + "\n" for r in requests), text=True,
        capture_output=True, env=env, timeout=120,
    )
    messages = [json.loads(line[len(MARKER):]) for line in proc.stdout.splitlines() if line.startswith(MARKER)]
    assert messages[0] == {"type": "ready"}, proc.stderr[-800:]
    by_id = {m["id"]: m["outcome"] for m in messages if m.get("type") == "response"}
    assert by_id["1"]["result"] == "hello"
    assert by_id["2"]["result"] == "first"                      # the second script saw the page the first one opened
    assert base64.b64decode(by_id["2"]["output_files"]["shot.png"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert by_id["3"] == {"ok": True}
    assert proc.returncode == 0


def test_a_script_that_outlives_its_limit_leaves_a_working_browser_that_is_still_signed_in(tmp_path):
    """The failure seen on real PCs (2026-10-25): a long in-page job ran past its time limit and every later call, even page.title(), then hung.
    The session must come back by itself with the same cookies and the same page."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "sid=abc123; Path=/")
            self.end_headers()
            self.wfile.write(b"<title>erp</title><h1 id=x>list page</h1>")

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/erp/list"
    env = {**os.environ, "PARASYNC_BROWSER_WORK_ROOT": str(tmp_path), "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")}
    requests = [
        {"id": "1", "op": "exec", "code": f"page.goto('{url}')\nresult = page.title()"},
        {"id": "2", "op": "exec", "timeout": 3, "code": "page.evaluate('new Promise(r => setTimeout(r, 60000))')\nresult = 'never'"},
        {"id": "3", "op": "exec", "code": "result = {'title': page.title(), 'url': page.url, 'cookies': [c['name'] + '=' + c['value'] for c in context.cookies()]}"},
        {"id": "4", "op": "close"},
    ]
    try:
        proc = subprocess.run(
            [sys.executable, HOST, "real2"], input="".join(json.dumps(r) + "\n" for r in requests), text=True,
            capture_output=True, env=env, timeout=180,
        )
    finally:
        server.shutdown()
    messages = [json.loads(line[len(MARKER):]) for line in proc.stdout.splitlines() if line.startswith(MARKER)]
    assert messages[0] == {"type": "ready"}, proc.stderr[-800:]
    by_id = {m["id"]: m["outcome"] for m in messages if m.get("type") == "response"}
    assert by_id["1"]["result"] == "erp"
    assert by_id["2"]["ok"] is False and "timed out" in by_id["2"]["error"]
    assert not any(m.get("type") == "fatal" for m in messages)          # the session was not given up on
    after = by_id["3"]
    assert after["ok"] is True, after
    assert after["result"]["title"] == "erp" and after["result"]["url"] == url          # back on the page it was on
    assert "sid=abc123" in after["result"]["cookies"]                                    # and still signed in
    assert by_id["4"] == {"ok": True}
