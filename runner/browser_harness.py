# sandbox/browser_harness.py
"""
The Playwright script-execution harness shared by both browser automation backends
(browser_runner.py's Daytona sandboxes, docker_browser_runner.py's Docker
containers) — extracted so the two backends can never silently drift into producing
different response shapes for what bemain treats as one identical /browser/exec
contract. Both backends' harness process only ever assumes a CDP endpoint reachable
at `CDP_URL` inside whatever sandbox/container it's running in — the harness itself
has no idea which backend launched it.

Same result-marker/`result`-variable convention as daytona_runner.py's own script
harness. Sync Playwright API deliberately, not async — avoids asyncio-in-a-
short-lived-process complexity for what is, per call, just "connect, do a few page
actions, disconnect."
"""
from __future__ import annotations

RESULT_MARKER = "__PARASYNC_RESULT__"
CDP_URL = "http://localhost:9222"

# `page`/`context`/`browser` are pre-bound into the exec namespace before the
# user's script runs, connected to the ALREADY-RUNNING Chromium via CDP rather
# than launching a fresh one.
_SCRIPT_HARNESS = '''
import sys, json
from playwright.sync_api import sync_playwright

_ns = {{}}
_pw = sync_playwright().start()

try:
    _browser = _pw.chromium.connect_over_cdp({cdp_url!r})
    _context = _browser.contexts[0] if _browser.contexts else _browser.new_context()
    _want = {active_target!r}
    _page = None
    if _want and len(_context.pages) > 1:
        # The order of tabs differs between connections, so the tab is found by its browser target id, not by its position.
        for _p in _context.pages:
            try:
                _cs = _context.new_cdp_session(_p)
                _tid = _cs.send("Target.getTargetInfo")["targetInfo"]["targetId"]
                _cs.detach()
            except Exception:
                continue
            if _tid == _want:
                _page = _p
                break
    if _page is None:
        _page = _context.pages[0] if _context.pages else _context.new_page()
    _ns["browser"] = _browser
    _ns["context"] = _context
    _ns["page"] = _page
except Exception as exc:
    print("{marker}" + json.dumps({{"ok": False, "error": "failed to connect to the browser session: " + type(exc).__name__ + ": " + str(exc)}}))
    _pw.stop()
    sys.exit(0)

try:
    exec(compile({user_code!r}, "<browser_script>", "exec"), _ns)
except Exception as exc:
    print("{marker}" + json.dumps({{"ok": False, "error": type(exc).__name__ + ": " + str(exc)}}))
    _pw.stop()
    sys.exit(0)

if "result" in _ns:
    try:
        json.dumps(_ns["result"])
        print("{marker}" + json.dumps({{"ok": True, "result": _ns["result"]}}))
    except TypeError:
        print("{marker}" + json.dumps({{"ok": False, "error": "`result` is not JSON-serializable"}}))
else:
    print("{marker}" + json.dumps({{"ok": True, "result": None}}))

_pw.stop()
'''

# python3, not curl — neither the Daytona debian_slim image nor the Docker
# python:3.12-slim image has curl preinstalled, and there's no reason to make
# either image heavier just to check a port. Exit code 0 only once the CDP
# endpoint genuinely answers.
READY_CHECK_CMD = (
    "python3 -c \"import urllib.request,sys; "
    "sys.exit(0 if urllib.request.urlopen('%s/json/version', timeout=2).getcode()==200 else 1)\"" % CDP_URL
)


def render_harness(user_code: str, active_target: str = "", cdp_url: str = "") -> str:
    """`active_target`: the browser target id of the tab `page` is bound to (the tab the agent's senses last worked in). Empty = the first tab, as before.
    `cdp_url`: where this session's Chromium listens (default CDP_URL: the MicroVM's fixed port; a PC runs several sessions, each on its own port)."""
    return _SCRIPT_HARNESS.format(cdp_url=cdp_url or CDP_URL, user_code=user_code, marker=RESULT_MARKER, active_target=str(active_target or ""))


def parse_harness_output(out_text: str) -> dict | None:
    """Returns the parsed {"ok":..., "result"|"error":...} dict from a harness
    run's captured stdout, or None if the marker never appeared (execution
    produced no result at all — a crash before the harness's own try/except
    could even run, or the process was killed)."""
    import json as _json

    idx = out_text.rfind(RESULT_MARKER)
    if idx == -1:
        return None
    result_line = out_text[idx + len(RESULT_MARKER):].strip().splitlines()[0]
    parsed = _json.loads(result_line)
    parsed["stdout"] = out_text[:idx]
    return parsed
