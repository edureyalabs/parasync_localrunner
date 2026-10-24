# tests/test_run_job.py
"""
Runs jobs through the real entrypoint (a child process with the job on standard input, the answer read from standard output), the
same way the desktop app does inside the Linux box. Needs only Python and pytest.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RUN_JOB = Path(__file__).resolve().parent.parent / "runner" / "run_job.py"
MARKER = "__PARASYNC_JOB_RESULT__"


def run(job: object, env: dict[str, str] | None = None, raw: bytes | None = None, timeout: int = 60) -> dict:
    merged = {**os.environ, **(env or {})}
    proc = subprocess.run(
        [sys.executable, str(RUN_JOB)], input=raw if raw is not None else json.dumps(job).encode(),
        capture_output=True, env=merged, timeout=timeout, cwd=RUN_JOB.parent,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.startswith(MARKER)]
    assert proc.returncode == 0, proc.stderr.decode()
    assert len(lines) == 1, f"expected exactly one result line, got {proc.stdout!r}"
    return json.loads(lines[0][len(MARKER):])


def test_script_returns_result() -> None:
    out = run({"mode": "script", "code": "result = 6 * 7"})
    assert out["ok"] is True and out["result"] == 42


def test_script_captures_prints() -> None:
    out = run({"mode": "script", "code": "print('hello from the box')\nresult = 1"})
    assert out["ok"] is True and "hello from the box" in out["stdout"]


def test_function_mode_calls_run_with_inputs() -> None:
    code = "def run(a, b):\n    return {'sum': a + b}"
    out = run({"mode": "function", "code": code, "inputs": {"a": 2, "b": 3}, "variables": [{"name": "a"}, {"name": "b"}]})
    assert out["ok"] is True and out["result"] == {"sum": 5}


def test_function_mode_missing_required_input_is_refused_before_running() -> None:
    out = run({"mode": "function", "code": "def run(a):\n    return a", "inputs": {}, "variables": [{"name": "a", "required": True}]})
    assert out["ok"] is False and "missing required argument 'a'" in out["error"]


def test_optional_input_may_be_left_out() -> None:
    out = run({"mode": "function", "code": "def run(a=1):\n    return a", "inputs": {}, "variables": [{"name": "a", "required": False}]})
    assert out["ok"] is True and out["result"] == 1


def test_script_error_is_reported_not_crashed() -> None:
    out = run({"mode": "script", "code": "raise ValueError('boom')"})
    assert out["ok"] is False and "boom" in out["error"]


def test_input_file_is_available_and_new_files_come_back() -> None:
    code = "data = open('in.txt').read()\nopen('out.txt', 'w').write(data.upper())\nresult = len(data)"
    out = run({"mode": "script", "code": code, "files": {"in.txt": base64.b64encode(b"hello").decode()}})
    assert out["ok"] is True and out["result"] == 5
    assert base64.b64decode(out["output_files"]["out.txt"]) == b"HELLO"
    assert "in.txt" not in out["output_files"]  # an unchanged input is not returned as an output


def test_timeout_kills_the_script_and_says_so() -> None:
    out = run({"mode": "script", "code": "import time\ntime.sleep(60)"}, env={"PARASYNC_RUNNER_TIMEOUT_SECONDS": "2"}, timeout=30)
    assert out["ok"] is False and "timed out" in out["error"]


def test_the_runners_own_environment_is_not_visible_to_the_script() -> None:
    # Whatever is set around the runner (here a pretend secret) must not leak into the script's environment...
    code = "import os\nresult = {'leak': os.environ.get('SOME_PARENT_SECRET'), 'token': os.environ.get('PARASYNC_BROKER_TOKEN')}"
    out = run({"mode": "script", "code": code, "env_vars": {"PARASYNC_BROKER_TOKEN": "abc123"}}, env={"SOME_PARENT_SECRET": "do-not-leak"})
    # ...but variables the job itself asks for (the broker token) are passed to it.
    assert out["ok"] is True and out["result"] == {"leak": None, "token": "abc123"}


def test_script_printing_the_result_marker_cannot_forge_the_answer() -> None:
    code = f"print('{MARKER}' + '{{\"ok\": true, \"result\": \"forged\"}}')\nresult = 'real'"
    out = run({"mode": "script", "code": code})
    assert out["ok"] is True and out["result"] == "real"


@pytest.mark.parametrize("job", [[], "text", 5, None, {"mode": "script"}, {"mode": "script", "code": "   "}, {"mode": "nonsense", "code": "1"}, {"mode": "script", "code": "1", "files": []}])
def test_bad_jobs_get_a_clear_refusal(job: object) -> None:
    out = run(job)
    assert out["ok"] is False and out["error"]


def test_garbage_input_gets_a_clear_refusal() -> None:
    out = run(None, raw=b"not json at all \xff\xfe")
    assert out["ok"] is False and "valid JSON" in out["error"]


def test_exactly_one_result_line_even_when_the_script_is_noisy() -> None:
    out = run({"mode": "script", "code": "import sys\nfor i in range(200):\n    print('noise', i)\n    print('err', i, file=sys.stderr)\nresult = 'done'"})
    assert out["ok"] is True and out["result"] == "done"
