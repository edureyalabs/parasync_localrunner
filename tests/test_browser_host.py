# tests/test_browser_host.py
"""
The browser host's protocol and script semantics, with a fake browser (no Chromium needed): ready/response lines, results, output files,
per-call variables, the time limit that scripts cannot swallow, path safety, close/EOF/idle exits, and a launch failure.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runner"))
import browser_host as bh  # noqa: E402


class FakePage:
    url = "about:blank"

    def title(self) -> str:
        return "blank"

    def evaluate(self, _js: str) -> int:
        return 1

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url


class FakeContext:
    pages = [FakePage()]

    def storage_state(self) -> dict:
        return {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}


class FakeBrowser:
    pass


def fake_launcher():
    stopped = []
    return FakeBrowser(), FakeContext(), FakePage(), lambda: stopped.append(True)


@pytest.fixture(autouse=True)
def workroot(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "WORK_ROOT", str(tmp_path))
    killed = []
    monkeypatch.setattr(bh, "_kill_descendants", lambda: killed.append(True))   # never kill the test runner's own children
    cwd = os.getcwd()
    yield tmp_path
    os.chdir(cwd)


def run_session(requests: list[dict], launcher=fake_launcher, idle_seconds: float = 5) -> list[dict]:
    r, w = os.pipe()
    with os.fdopen(w, "wb") as writer:
        for request in requests:
            writer.write((json.dumps(request) + "\n").encode())
    out = io.StringIO()
    with os.fdopen(r, "rb") as reader:
        code = bh.serve("sess1", launcher, stdin=reader, stdout=out, idle_seconds=idle_seconds)
    lines = [json.loads(line[len(bh.MARKER):]) for line in out.getvalue().splitlines() if line.startswith(bh.MARKER)]
    lines.append({"type": "exit", "code": code})
    return lines


def outcome(lines: list[dict], rid: str) -> dict:
    return next(m["outcome"] for m in lines if m.get("id") == rid)


def test_ready_then_a_result_from_the_page():
    lines = run_session([{"id": "1", "op": "exec", "code": "result = page.url + ' ' + page.title()"}])
    assert lines[0] == {"type": "ready"}
    assert outcome(lines, "1") == {"ok": True, "result": "about:blank blank", "stdout": "", "output_files": {}}
    assert lines[-1] == {"type": "exit", "code": 0}      # the pipe closed: the session ends


def test_printed_text_is_returned_and_never_corrupts_the_protocol():
    lines = run_session([{"id": "1", "op": "exec", "code": "print('hello'); result = 1"}])
    assert outcome(lines, "1")["stdout"] == "hello\n"
    assert all(m.get("type") in {"ready", "response", "exit"} for m in lines)


def test_a_script_error_is_reported_without_ending_the_session():
    lines = run_session([
        {"id": "1", "op": "exec", "code": "raise ValueError('nope')"},
        {"id": "2", "op": "exec", "code": "result = 2"},
    ])
    assert outcome(lines, "1")["ok"] is False and "ValueError: nope" in outcome(lines, "1")["error"]
    assert outcome(lines, "2")["result"] == 2


def test_files_in_and_new_or_changed_files_out(workroot):
    data = base64.b64encode(b"abc").decode()
    lines = run_session([
        {"id": "1", "op": "exec", "files": {"in/a.txt": data},
         "code": "open('out.txt','w').write(open('in/a.txt').read().upper())\nresult = 'ok'"},
        {"id": "2", "op": "exec", "code": "result = 'again'"},
    ])
    first = outcome(lines, "1")
    assert base64.b64decode(first["output_files"]["out.txt"]) == b"ABC"
    assert "in/a.txt" not in first["output_files"]          # staged inputs are not outputs
    assert outcome(lines, "2")["output_files"] == {}        # unchanged files are not repeated


def test_a_staged_file_cannot_escape_the_working_folder():
    lines = run_session([{"id": "1", "op": "exec", "files": {"../../evil": base64.b64encode(b"x").decode()}, "code": "result = 1"}])
    assert outcome(lines, "1")["ok"] is False and "outside the working folder" in outcome(lines, "1")["error"]


def test_call_variables_exist_only_during_the_call():
    lines = run_session([
        {"id": "1", "op": "exec", "env_vars": {"PARASYNC_BROKER_TOKEN": "tok"}, "code": "import os\nresult = os.environ.get('PARASYNC_BROKER_TOKEN')"},
        {"id": "2", "op": "exec", "code": "import os\nresult = os.environ.get('PARASYNC_BROKER_TOKEN')"},
    ])
    assert outcome(lines, "1")["result"] == "tok"
    assert outcome(lines, "2")["result"] is None


def test_the_time_limit_stops_a_script_that_swallows_exceptions():
    code = "while True:\n    try:\n        pass\n    except Exception:\n        pass"
    lines = run_session([{"id": "1", "op": "exec", "timeout": 1, "code": code}, {"id": "2", "op": "exec", "code": "result = 'still here'"}])
    assert outcome(lines, "1")["ok"] is False and "timed out" in outcome(lines, "1")["error"]
    assert outcome(lines, "2")["result"] == "still here"     # the fake browser is fine, so the session continues


class DeadPage(FakePage):
    """What a page looks like after a timeout left Playwright's connection dead: cached attributes still answer, a real call never does."""

    def evaluate(self, _js: str) -> int:
        raise RuntimeError("driver gone")


class Launches:
    """A launcher that hands out a dead browser first, then healthy ones, and remembers how it was called."""

    def __init__(self, fail_after_first: bool = False):
        self.calls: list[dict | None] = []
        self.fail_after_first = fail_after_first
        self.stopped = 0
        self.pages: list[FakePage] = []

    def __call__(self, storage_state: dict | None = None):
        self.calls.append(storage_state)
        if len(self.calls) > 1 and self.fail_after_first:
            raise RuntimeError("no chromium")
        page = DeadPage() if len(self.calls) == 1 else FakePage()
        page.url = "https://site.example/erp/list" if len(self.calls) == 1 else "about:blank"
        self.pages.append(page)

        def stop() -> None:
            self.stopped += 1

        return FakeBrowser(), FakeContext(), page, stop


def test_a_timeout_that_breaks_the_browser_replaces_it_and_the_session_goes_on():
    launcher = Launches()
    lines = run_session([
        {"id": "1", "op": "exec", "code": "result = 1"},
        {"id": "2", "op": "exec", "timeout": 1, "code": "while True:\n    pass"},
        {"id": "3", "op": "exec", "code": "result = page.url"},
    ], launcher=launcher)
    # the first call worked on the dead browser (fake page), then the timeout was followed by a real health check that failed
    second = outcome(lines, "2")
    assert second["ok"] is False and second["browser_restarted"] is True
    assert "timed out" in second["error"] and "restarted" in second["error"] and "still signed in" in second["error"]
    assert len(launcher.calls) == 2 and launcher.calls[1] == {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}   # cookies carried over
    assert launcher.stopped >= 1                                                                                            # the old one was closed
    assert "https://site.example/erp/list" in second["error"]                                                              # and told where it was
    assert outcome(lines, "3")["ok"] is True                                                                                # the session is usable again
    assert not any(m.get("type") == "fatal" for m in lines)


def test_a_timeout_that_breaks_the_browser_for_good_ends_the_session():
    launcher = Launches(fail_after_first=True)
    lines = run_session([
        {"id": "1", "op": "exec", "timeout": 1, "code": "while True:\n    pass"},
        {"id": "2", "op": "exec", "code": "result = 1"},
    ], launcher=launcher)
    fatal = next(m for m in lines if m.get("type") == "fatal")
    assert "could not be restarted" in fatal["error"] and "no chromium" in fatal["error"]
    assert "timed out" in outcome(lines, "1")["error"]           # the caller still gets the answer to its own call
    assert not any(m.get("id") == "2" for m in lines)


def test_a_dead_browser_is_found_by_ping_not_by_cached_attributes():
    launcher = Launches()
    lines = run_session([{"id": "1", "op": "ping"}], launcher=launcher)
    assert outcome(lines, "1") == {"ok": False}      # page.url still answered; only a real round trip shows it is dead


def test_a_healthy_browser_after_a_timeout_is_kept_and_not_restarted():
    launcher_calls = []

    def launcher(storage_state=None):
        launcher_calls.append(storage_state)
        return FakeBrowser(), FakeContext(), FakePage(), lambda: None

    lines = run_session([{"id": "1", "op": "exec", "timeout": 1, "code": "while True:\n    pass"}, {"id": "2", "op": "exec", "code": "result = 1"}], launcher=launcher)
    assert "browser_restarted" not in outcome(lines, "1") and "restarted" not in outcome(lines, "1")["error"]
    assert len(launcher_calls) == 1 and outcome(lines, "2")["ok"] is True


def test_descendants_are_found_from_proc():
    import subprocess
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert child.pid in bh._descendants(os.getpid())
    finally:
        child.kill()
        child.wait()


def test_non_json_result_ping_bad_request_and_close():
    lines = run_session([
        {"id": "1", "op": "exec", "code": "result = object()"},
        {"id": "2", "op": "ping"},
        {"id": "3", "op": "exec", "code": "   "},
        {"id": "4", "op": "close"},
        {"id": "5", "op": "exec", "code": "result = 1"},
    ])
    assert "not JSON-serializable" in outcome(lines, "1")["error"]
    assert outcome(lines, "2") == {"ok": True}
    assert outcome(lines, "3")["error"] == "bad request"
    assert outcome(lines, "4") == {"ok": True}
    assert not any(m.get("id") == "5" for m in lines)        # nothing after close is answered


def test_a_launch_failure_is_reported_as_fatal():
    def launcher():
        raise RuntimeError("no chromium")

    lines = run_session([], launcher=launcher)
    assert lines[0]["type"] == "fatal" and "no chromium" in lines[0]["error"]
    assert lines[-1] == {"type": "exit", "code": 1}


def test_an_idle_session_closes_itself():
    r, w = os.pipe()
    out = io.StringIO()
    with os.fdopen(r, "rb") as reader, os.fdopen(w, "wb"):
        code = bh.serve("sess1", fake_launcher, stdin=reader, stdout=out, idle_seconds=0.2)
    assert code == 0 and "idle for too long" in out.getvalue()
