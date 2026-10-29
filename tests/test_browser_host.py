# tests/test_browser_host.py
"""
The browser host's protocol and lifetime, with a fake Chromium and a fake core (no browser needed): ready/response lines, what each op passes on to the
shared core, time-limit clamping, ping, a Chromium that dies, bad requests, close / end of input / idle exits, a start failure, a core that raises,
and how the real Chromium is started (loopback address, its own profile folder, a random port) without starting it. What a request really DOES in a
browser is the cloud's own code and is tested against a real Chromium in tests/test_browser_real.py and in parasync_sandbox.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runner"))
import browser_host as bh  # noqa: E402


class FakeSenses:
    def __init__(self):
        self.torn_down = False

    async def _teardown(self):
        self.torn_down = True


class FakeCore:
    def __init__(self):
        self.calls = []
        self.senses = FakeSenses()
        self.explode = False

    async def run_script(self, code, files, env_vars, timeout):
        self.calls.append(("exec", code, files, env_vars, timeout))
        if self.explode:
            raise RuntimeError("boom")
        return {"ok": True, "result": code.upper(), "stdout": "", "output_files": {}, "senses": {"console": []}}

    async def look(self, body):
        self.calls.append(("look", body))
        return {"ok": True, "report": "PAGE x", "output_files": {}}

    async def act(self, body):
        self.calls.append(("act", body))
        return {"ok": True, "report": "clicked", "output_files": {}}


class FakeChromium:
    cdp_url = "http://127.0.0.1:1"

    def __init__(self):
        self.up = True
        self.stopped = 0

    def alive(self):
        return self.up

    def stop(self):
        self.stopped += 1


@pytest.fixture(autouse=True)
def workroot(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "WORK_ROOT", str(tmp_path))
    cwd = os.getcwd()
    yield tmp_path
    os.chdir(cwd)


def run_session(requests: list, chromium=None, core=None, idle_seconds: float = 5, chromium_factory=None):
    chromium = chromium or FakeChromium()
    core = core or FakeCore()
    r, w = os.pipe()
    with os.fdopen(w, "wb") as writer:
        for request in requests:
            writer.write((json.dumps(request) + "\n").encode())
    out = io.StringIO()
    with os.fdopen(r, "rb") as reader:
        code = bh.serve("sess1", chromium_factory or (lambda sid, wd: chromium), lambda wd, url: core, stdin=reader, stdout=out, idle_seconds=idle_seconds)
    lines = [json.loads(line[len(bh.MARKER):]) for line in out.getvalue().splitlines() if line.startswith(bh.MARKER)]
    return lines, code, chromium, core


def outcome(lines: list, rid: str) -> dict:
    return next(m["outcome"] for m in lines if m.get("id") == rid)


def test_ready_says_the_senses_are_there_and_the_end_of_input_ends_the_session_and_the_browser():
    lines, code, chromium, core = run_session([])
    assert lines == [{"type": "ready", "senses": True}] and code == 0
    assert chromium.stopped == 1 and core.senses.torn_down                    # the browser goes with the app's pipe


def test_exec_look_and_act_go_to_the_shared_core_with_what_the_app_sent():
    lines, _code, _c, core = run_session([
        {"id": "1", "op": "exec", "code": "page.url", "files": {"a.txt": "eA=="}, "env_vars": {"K": "v"}, "timeout": 30},
        {"id": "2", "op": "look", "query": "search", "allowed_hosts": ["erp.test"], "id_ignored": 1},
        {"id": "3", "op": "act", "action": "click", "target": "k3x.1", "settle_ms": 100},
    ])
    assert outcome(lines, "1")["result"] == "PAGE.URL" and outcome(lines, "1")["senses"] == {"console": []}
    assert outcome(lines, "2")["report"] == "PAGE x" and outcome(lines, "3")["report"] == "clicked"
    assert core.calls[0] == ("exec", "page.url", {"a.txt": "eA=="}, {"K": "v"}, 30.0)
    assert core.calls[1] == ("look", {"query": "search", "allowed_hosts": ["erp.test"], "id_ignored": 1})          # the id and op are the protocol's, not the body
    assert core.calls[2] == ("act", {"action": "click", "target": "k3x.1", "settle_ms": 100})


def test_the_time_limit_is_clamped_and_defaults():
    lines, _c, _ch, core = run_session([{"id": "1", "op": "exec", "code": "x", "timeout": 99999}, {"id": "2", "op": "exec", "code": "y"}, {"id": "3", "op": "exec", "code": "z", "timeout": "soon"},
                                         {"id": "4", "op": "exec", "code": "w", "timeout": 0.01}])
    assert [c[4] for c in core.calls] == [bh.MAX_EXEC_TIMEOUT, bh.DEFAULT_EXEC_TIMEOUT, bh.DEFAULT_EXEC_TIMEOUT, 1.0]


def test_bad_requests_get_an_answer_and_do_not_end_the_session():
    lines, _c, _ch, core = run_session([{"id": "1", "op": "exec", "code": "  "}, {"id": "2", "op": "fly"}, {"id": "3", "op": "exec"}, {"id": "4", "op": "exec", "code": "ok"}])
    assert outcome(lines, "1") == {"ok": False, "error": "bad request"} and outcome(lines, "2") == {"ok": False, "error": "bad request"} and outcome(lines, "3")["error"] == "bad request"
    assert outcome(lines, "4")["ok"] is True and len(core.calls) == 1
    assert not any(m.get("type") == "fatal" for m in lines)


def test_garbage_lines_are_ignored():
    r, w = os.pipe()
    with os.fdopen(w, "wb") as writer:
        writer.write(b"not json\n[1,2]\n" + (json.dumps({"id": "1", "op": "ping"}) + "\n").encode())
    out = io.StringIO()
    with os.fdopen(r, "rb") as reader:
        bh.serve("s", lambda sid, wd: FakeChromium(), lambda wd, url: FakeCore(), stdin=reader, stdout=out, idle_seconds=5)
    assert '"outcome": {"ok": true}' in out.getvalue()


def test_ping_says_whether_chromium_answers():
    lines, _c, chromium, _core = run_session([{"id": "1", "op": "ping"}])
    assert outcome(lines, "1") == {"ok": True}
    dead = FakeChromium()
    dead.up = False
    lines, code, _c, _core = run_session([{"id": "1", "op": "ping"}], chromium=dead)
    assert outcome(lines, "1") == {"ok": False} and not any(m.get("type") == "fatal" for m in lines)


def test_a_dead_chromium_ends_the_session_with_a_clear_answer_and_a_fatal_line():
    dead = FakeChromium()
    dead.up = False
    lines, code, chromium, core = run_session([{"id": "1", "op": "exec", "code": "page.url"}], chromium=dead)
    assert "stopped responding" in outcome(lines, "1")["error"] and lines[-1]["type"] == "fatal" and code == 0
    assert core.calls == [] and chromium.stopped == 1


def test_a_core_that_raises_is_an_answer_not_the_end():
    core = FakeCore()
    core.explode = True
    lines, _c, _ch, _core = run_session([{"id": "1", "op": "exec", "code": "x"}, {"id": "2", "op": "ping"}], core=core)
    assert outcome(lines, "1")["ok"] is False and "RuntimeError: boom" in outcome(lines, "1")["error"] and outcome(lines, "2") == {"ok": True}


def test_close_answers_and_stops_the_browser():
    lines, code, chromium, _core = run_session([{"id": "1", "op": "close"}, {"id": "2", "op": "exec", "code": "never"}])
    assert outcome(lines, "1") == {"ok": True} and not any(m.get("id") == "2" for m in lines) and code == 0 and chromium.stopped == 1


def test_chromium_is_started_off_the_event_loop():
    """Playwright's synchronous API (used to find Chromium) refuses to run inside a running asyncio loop: it failed in CI inside the real image."""
    import asyncio

    seen = []

    def factory(sid, wd):
        try:
            asyncio.get_running_loop()
            seen.append("in the loop")
        except RuntimeError:
            seen.append("off the loop")
        return FakeChromium()

    lines, _code, _c, _core = run_session([], chromium_factory=factory)
    assert seen == ["off the loop"] and lines[0]["type"] == "ready"


def test_a_start_failure_is_one_fatal_line():
    def broken(sid, wd):
        raise RuntimeError("no chromium here")

    lines, code, _c, _core = run_session([], chromium_factory=broken)
    assert len(lines) == 1 and lines[0]["type"] == "fatal" and "could not be started" in lines[0]["error"] and "no chromium here" in lines[0]["error"] and code == 1


def test_an_idle_session_closes_itself():
    r, w = os.pipe()                                          # nothing is ever written and the pipe stays open
    out = io.StringIO()
    chromium = FakeChromium()
    reader = os.fdopen(r, "rb")
    try:
        code = bh.serve("idle1", lambda sid, wd: chromium, lambda wd, url: FakeCore(), stdin=reader, stdout=out, idle_seconds=0.3)
    finally:
        os.close(w)                                           # the reading thread sees the end and lets go of the reader before it is closed
        time.sleep(0.2)
        reader.close()
    messages = [json.loads(line[len(bh.MARKER):]) for line in out.getvalue().splitlines() if line.startswith(bh.MARKER)]
    assert code == 0 and messages[-1]["type"] == "fatal" and "idle" in messages[-1]["error"] and chromium.stopped == 1


def test_descendants_are_found_from_proc():
    import subprocess

    child = subprocess.Popen(["sleep", "30"])
    try:
        assert child.pid in bh._descendants(os.getpid())
    finally:
        child.kill()
        child.wait()


def test_chromium_is_started_on_loopback_with_its_own_profile_and_a_random_port(monkeypatch, tmp_path):
    started = []

    class FakeProc:
        pid = 99999

        def poll(self):
            return None

    monkeypatch.setattr(bh.subprocess, "Popen", lambda args, **kw: started.append((args, kw)) or FakeProc())
    a = bh.Chromium("/x/chrome", str(tmp_path / "p1"))
    b = bh.Chromium("/x/chrome", str(tmp_path / "p2"))
    args = started[0][0]
    assert args[0] == "/x/chrome" and "--remote-debugging-port=%d" % a.port in args and "--remote-debugging-address=127.0.0.1" in args
    assert "--user-data-dir=" + str(tmp_path / "p1") in args and "--headless=new" in args and a.cdp_url == "http://127.0.0.1:%d" % a.port
    assert started[0][1]["stdout"] == bh.subprocess.DEVNULL                   # nothing Chromium prints can reach the protocol channel
    assert a.port != b.port and os.path.isdir(str(tmp_path / "p1"))
