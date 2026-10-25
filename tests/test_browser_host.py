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


class FakeContext:
    pages = [FakePage()]


class FakeBrowser:
    pass


def fake_launcher():
    stopped = []
    return FakeBrowser(), FakeContext(), FakePage(), lambda: stopped.append(True)


@pytest.fixture(autouse=True)
def workroot(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "WORK_ROOT", str(tmp_path))
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


def test_a_timeout_that_breaks_the_browser_closes_the_session():
    class Broken(FakeContext):
        @property
        def pages(self):
            raise RuntimeError("driver gone")

    def launcher():
        return FakeBrowser(), Broken(), FakePage(), lambda: None

    lines = run_session([{"id": "1", "op": "exec", "timeout": 1, "code": "while True:\n    pass"},
                         {"id": "2", "op": "exec", "code": "result = 1"}], launcher=launcher)
    assert any(m.get("type") == "fatal" for m in lines)
    assert not any(m.get("id") == "2" for m in lines)


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
