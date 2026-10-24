# runner/run_job.py
"""
Runs ONE job and exits. The desktop app starts this inside the sealed Linux box for each tool call:

    wsl.exe -d ParasyncRunner -u runner -- timeout --signal=KILL <seconds> python3 /opt/runner/run_job.py

and writes the job to its standard input. The job is the exact payload the cloud sandbox's /execute takes (code, mode, entrypoint,
variables, inputs, agent_id, files, packages, env_vars, needs_internet_access), so bemain builds one payload and either place can run it.
The answer is one line on standard output, `__PARASYNC_JOB_RESULT__` followed by JSON (the cloud's response shape). Anything else the
script or pip prints is inside that JSON or on standard error, never mixed into the line. No port is opened: there is nothing for
another program on the PC to connect to.
"""
from __future__ import annotations

import asyncio
import json
import sys

from runner import execute

MARKER = "__PARASYNC_JOB_RESULT__"
MODES = {"function", "script"}


def emit(result: dict) -> None:
    sys.stdout.write(MARKER + json.dumps(result) + "\n")
    sys.stdout.flush()


def validate(job: object) -> str | None:
    """Same checks the cloud harness makes before running anything. Returns a message, or None when the job is fine."""
    if not isinstance(job, dict):
        return "the job must be a JSON object"
    code = job.get("code")
    if not isinstance(code, str) or not code.strip():
        return "no code provided"
    mode = job.get("mode", "function")
    if mode not in MODES:
        return f"unknown mode '{mode}'"
    for field, kind in (("inputs", dict), ("files", dict), ("env_vars", dict), ("packages", list), ("variables", list)):
        if field in job and not isinstance(job[field], kind):
            return f"'{field}' must be a {kind.__name__}"
    if mode == "function":
        inputs = job.get("inputs") or {}
        for variable in job.get("variables") or []:
            if isinstance(variable, dict) and variable.get("required", True) and variable.get("name") not in inputs:
                return f"missing required argument '{variable.get('name')}'"
    return None


def main() -> int:
    try:
        job = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        emit({"ok": False, "error": "the job was not valid JSON"})
        return 0

    problem = validate(job)
    if problem:
        emit({"ok": False, "error": problem})
        return 0

    try:
        result = asyncio.run(execute(
            job["code"], job.get("inputs") or {}, mode=job.get("mode", "function"), agent_id=str(job.get("agent_id", "unknown")),
            files=job.get("files") or {}, packages=job.get("packages") or [], env_vars=job.get("env_vars") or {},
            needs_internet_access=bool(job.get("needs_internet_access", False)),
        ))
    except Exception as exc:  # the runner itself failed: say so plainly, never leave the caller waiting for an answer
        emit({"ok": False, "error": f"the local runner failed: {type(exc).__name__}: {exc}", "runner_failure": True})
        return 0
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
