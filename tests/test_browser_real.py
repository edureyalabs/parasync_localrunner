# tests/test_browser_real.py
"""
A real browser session end to end: browser_host.py started as its own process with a real Chromium, scripts in the SAME session (page state persists
between them), look and act (what the page is, what a click caused), files handed back once, a script that overruns killed alone, a screenshot, then close.
Skipped where Playwright or Chromium is not installed; CI runs it inside the built image, which is the point.
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
    if os.environ.get("PARASYNC_CHROMIUM_PATH"):
        return os.path.exists(os.environ["PARASYNC_CHROMIUM_PATH"])
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


import http.server
import threading


class Site(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/file.csv"):
            body, ctype, extra = b"a,b\n1,2\n", "text/csv", {"Content-Disposition": 'attachment; filename="report.csv"'}
        elif self.path.startswith("/api"):
            body, ctype, extra = b'{"items": [1, 2, 3]}', "application/json", {}
        else:
            body = (b"<title>erp</title><h1>Orders</h1><button id=b onclick=\"fetch('/api?token=abcdef123456').then(r=>r.json()).then(j=>{document.getElementById('o').textContent='got '+j.items.length})\">Load orders</button>"
                    b"<span id=o></span> <a href='/file.csv'>Download csv</a><script>console.error('page boot error')</script>")
            ctype, extra = "text/html", {"Set-Cookie": "sid=abc123; Path=/"}
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _session(tmp_path, requests, timeout=180):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    env = {**os.environ, "PARASYNC_BROWSER_WORK_ROOT": str(tmp_path), "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")}
    try:
        proc = subprocess.run([sys.executable, HOST, "real1"], input="".join(json.dumps(r(url) if callable(r) else r) + "\n" for r in requests), text=True, capture_output=True, env=env, timeout=timeout)
    finally:
        server.shutdown()
    messages = [json.loads(line[len(MARKER):]) for line in proc.stdout.splitlines() if line.startswith(MARKER)]
    assert messages and messages[0] == {"type": "ready", "senses": True}, proc.stderr[-1500:]
    return proc, messages, {m["id"]: m["outcome"] for m in messages if m.get("type") == "response"}, url


def test_a_session_keeps_its_page_between_scripts_and_returns_a_screenshot(tmp_path):
    proc, messages, by_id, _url = _session(tmp_path, [
        {"id": "1", "op": "exec", "code": "page.goto('data:text/html,<title>hello</title><h1 id=x>first</h1>')\nresult = page.title()"},
        {"id": "2", "op": "exec", "code": "page.screenshot(path='shot.png')\nresult = page.inner_text('#x')"},
        {"id": "3", "op": "close"},
    ])
    assert by_id["1"]["result"] == "hello" and by_id["2"]["result"] == "first"                      # the second script saw the page the first one opened
    assert base64.b64decode(by_id["2"]["output_files"]["shot.png"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert by_id["3"] == {"ok": True} and proc.returncode == 0


def test_look_and_act_work_like_the_clouds_and_the_script_results_carry_what_the_page_did(tmp_path):
    proc, messages, by_id, url = _session(tmp_path, [
        lambda u: {"id": "1", "op": "exec", "code": f"page.goto('{u}')\nresult = page.title()"},
        {"id": "2", "op": "look"},
        {"id": "3", "op": "act", "action": "click", "target": {"name": "Load orders"}},
        {"id": "4", "op": "act", "action": "click", "target": {"name": "Download csv"}},
        {"id": "5", "op": "look"},
        {"id": "6", "op": "exec", "code": "result = {'cookies': [c['name'] for c in context.cookies()], 'url': page.url}"},
        {"id": "7", "op": "close"},
    ])
    assert by_id["1"]["result"] == "erp" and by_id["1"]["senses"] is not None
    assert 'button "Load orders"' in by_id["2"]["report"] and by_id["2"]["ok"] is True
    clicked = by_id["3"]["report"]
    assert by_id["3"]["ok"] and "GET /api" in clicked and "abcdef123456" not in clicked and "items: list(3)" in clicked      # the request it caused, with the secret-looking value hidden
    assert by_id["4"]["ok"] and any(name.endswith("report.csv") for name in by_id["4"]["output_files"])                    # a download comes back as a file
    assert by_id["5"]["output_files"] == {} and "sid" in by_id["6"]["result"]["cookies"]                                  # ... exactly once; the login cookie is in the session
    assert by_id["7"] == {"ok": True} and proc.returncode == 0


def test_a_script_that_outlives_its_limit_is_killed_alone_and_the_browser_keeps_its_page_and_login(tmp_path):
    """The failure seen on real PCs (2026-10-25): a long in-page job ran past its limit and every later call hung. The script is its own process now, so
    the browser, the page and the cookies are untouched."""
    proc, messages, by_id, url = _session(tmp_path, [
        lambda u: {"id": "1", "op": "exec", "code": f"page.goto('{u}erp/list')\nresult = page.title()"},
        {"id": "2", "op": "exec", "timeout": 3, "code": "page.evaluate('new Promise(r => setTimeout(r, 60000))')\nresult = 'never'"},
        {"id": "3", "op": "exec", "code": "result = {'title': page.title(), 'url': page.url, 'cookies': [c['name'] + '=' + c['value'] for c in context.cookies()]}"},
        {"id": "4", "op": "look"},
        {"id": "5", "op": "close"},
    ])
    assert by_id["1"]["result"] == "erp"
    assert by_id["2"]["ok"] is False and "timed out" in by_id["2"]["error"]
    assert not any(m.get("type") == "fatal" for m in messages)
    after = by_id["3"]
    assert after["ok"] is True and after["result"]["title"] == "erp" and after["result"]["url"].endswith("/erp/list") and "sid=abc123" in after["result"]["cookies"]
    assert by_id["4"]["ok"] and "Orders" in by_id["4"]["report"] and by_id["5"] == {"ok": True}
