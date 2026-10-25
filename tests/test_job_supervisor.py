# tests/test_job_supervisor.py
"""
The supervisor that starts a job given secrets to keep away from its own script. The script's program runs for real as a child process here
(as the current user instead of `runner`), talking to the broker over loopback; only the broker's outgoing HTTPS request is replaced.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runner"))
import job_supervisor as js  # noqa: E402
import local_broker as lb  # noqa: E402

MARKER = js.MARKER
RUN_JOB = os.path.join(os.path.dirname(__file__), "..", "runner", "run_job.py")


class FakeStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


@pytest.fixture
def harness(monkeypatch, capsys):
    monkeypatch.setattr(js, "CHILD", ["timeout", "--signal=KILL"])
    monkeypatch.setattr(js, "SCRIPT", RUN_JOB)
    monkeypatch.setattr(js, "refuse_dumps", lambda: None)

    outgoing: list[dict] = []

    def fake_https(url, method, headers, body, hosts):
        outgoing.append({"url": url, "headers": headers})
        return 200, json.dumps({"seen": headers.get("Authorization")}).encode()

    monkeypatch.setattr(lb, "https_request", fake_https)

    def run(envelope, limit="20") -> tuple[int, dict | None]:
        monkeypatch.setattr(sys, "stdin", FakeStdin(json.dumps(envelope).encode()))
        code = js.main(["job_supervisor.py", limit])
        lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith(MARKER)]
        return code, (json.loads(lines[-1][len(MARKER):]) if lines else None)

    run.outgoing = outgoing  # type: ignore[attr-defined]
    return run


SCRIPT = """
import os, json, urllib.request
req = urllib.request.Request(os.environ['PARASYNC_BROKER_URL'] + '/broker/relay',
    data=json.dumps({'token': os.environ['PARASYNC_BROKER_TOKEN'], 'url': 'https://api.example.com/v1', 'headers': {'Authorization': 'Bearer {{secret:API_KEY}}'}}).encode(),
    method='POST', headers={'Content-Type': 'application/json'})
answer = json.loads(urllib.request.urlopen(req, timeout=20).read())
result = {'answer': answer, 'env_has_value': any('sk-live-0123456789' in v for v in os.environ.values()),
          'available': os.environ.get('PARASYNC_AVAILABLE_SECRETS'), 'env_keys': sorted(k for k in os.environ if k.startswith('PARASYNC_'))}
"""


def envelope(code=SCRIPT, **over):
    return {"job": {"mode": "script", "code": code, "agent_id": "t", "env_vars": {}, **over},
            "broker": {"secrets": [{"name": "API_KEY", "value": "sk-live-0123456789", "hosts": ["api.example.com"]}], "cloud": None}}


def test_the_script_uses_the_secret_through_the_broker_and_never_holds_it(harness) -> None:
    code, out = harness(envelope())
    assert code == 0 and out and out["ok"] is True, out
    result = out["result"]
    assert result["answer"]["body"] == {"seen": "Bearer [REDACTED]"}, "the broker put the value in and blanked it from the answer"
    assert harness.outgoing[0]["headers"]["Authorization"] == "Bearer sk-live-0123456789", "the real request carried the secret"
    assert result["env_has_value"] is False, "the value is nowhere in the script's environment"
    assert result["available"] == "API_KEY"
    assert "PARASYNC_BROKER_URL" in result["env_keys"] and "PARASYNC_BROKER_TOKEN" in result["env_keys"]


def test_the_cloud_brokers_address_is_replaced_by_the_local_one(harness) -> None:
    code, out = harness(envelope(env_vars={"PARASYNC_BROKER_URL": "https://bemain.example.com", "PARASYNC_BROKER_TOKEN": "cloud-token-xyz", "PARASYNC_AVAILABLE_SECRETS": "CLOUD_ONE"}))
    assert out and out["ok"] is True
    assert out["result"]["available"] == "API_KEY,CLOUD_ONE"
    assert out["result"]["answer"]["status_code"] == 200, "the call went to the local broker, not the cloud address"


def test_a_malformed_envelope_is_refused_before_anything_starts(harness) -> None:
    for bad in (
        {"job": {}, "broker": {"secrets": [{"name": "lower", "value": "abcd1234", "hosts": []}]}},
        {"job": {}, "broker": {"secrets": [{"name": "OK_NAME", "value": "", "hosts": []}]}},
        {"job": {}, "broker": {"secrets": [{"name": "OK_NAME", "value": "abcd1234", "hosts": "api.example.com"}]}},
        {"job": {}, "broker": {"secrets": [], "cloud": {"url": "http://insecure", "token": "x"}}},
        {"job": [], "broker": {}},
        {"nope": 1},
    ):
        code, out = harness(bad)
        assert code == 0 and out and out["ok"] is False and "malformed" in out["error"], bad


def test_a_script_that_runs_past_the_limit_is_stopped(harness) -> None:
    code, out = harness(envelope(code="import time\ntime.sleep(30)\nresult = 1"), limit="1")
    assert code == 137 and out is None, "no result line: the app reads 137 as 'did not finish in time'"


def test_a_script_error_comes_back_as_the_normal_error_answer(harness) -> None:
    code, out = harness(envelope(code="raise RuntimeError('boom')"))
    assert code == 0 and out and out["ok"] is False and "boom" in out["error"]
