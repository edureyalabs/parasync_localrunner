# runner/job_supervisor.py
"""
Starts ONE job that was given secrets to keep away from its own script, and runs the local broker for it.

    wsl.exe -d ParasyncRunner -u root --exec sh -c '<lock interop>; exec timeout --signal=KILL <limit+20> python3 /opt/runner/job_supervisor.py <limit>'

The desktop app writes one JSON object to standard input: {"job": <the usual job>, "broker": {"secrets": [{"name", "value", "hosts"}], "cloud": {...}|null}}.
This program runs as root. It reads the secrets into its own memory only (never an environment variable, a file or a command line), starts the
broker on 127.0.0.1, and then starts the script as the unprivileged `runner` user with the broker's address and a one-time token as the only
things it is given. The script is another user, so it cannot read this process's memory, environment or files; all it can do is ask the broker
to make a request for it. The answer is the script's own marked result line, passed through unchanged.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import secrets as _secrets
import signal
import subprocess
import sys

import local_broker

MARKER = "__PARASYNC_JOB_RESULT__"
NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
MAX_SECRETS = 20
MAX_VALUE_CHARS = 64 * 1024
CHILD = ["runuser", "-u", "runner", "--", "env", "-u", "WSL_INTEROP", "timeout", "--signal=KILL"]
SCRIPT = "/opt/runner/run_job.py"


def emit(result: dict) -> None:
    sys.stdout.write(MARKER + json.dumps(result) + "\n")
    sys.stdout.flush()


def refuse_dumps() -> None:
    """Best effort: nobody (not even a debugger run as the script's user) may take a memory dump of this process."""
    try:
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)       # PR_SET_DUMPABLE = 4
    except Exception:
        pass


def parse(envelope: object) -> tuple[dict, local_broker.BrokerState | None, str | None]:
    """(job, broker state, error). The envelope comes from the desktop app, but it is still checked: this process holds the secrets."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("job"), dict) or not isinstance(envelope.get("broker"), dict):
        return {}, None, "the job envelope is malformed"
    job = envelope["job"]
    raw = envelope["broker"]
    items = raw.get("secrets")
    if not isinstance(items, list) or len(items) > MAX_SECRETS:
        return job, None, "the secrets list is malformed"
    secrets: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not NAME.match(item["name"]):
            return job, None, "a secret name is malformed"
        value, hosts = item.get("value"), item.get("hosts")
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE_CHARS:
            return job, None, "a secret value is malformed"
        if not isinstance(hosts, list) or len(hosts) > 20 or not all(isinstance(h, str) and len(h) <= 253 for h in hosts):
            return job, None, "a host list is malformed"
        secrets[item["name"]] = {"value": value, "hosts": [h.lower() for h in hosts]}
    cloud = raw.get("cloud")
    if cloud is not None and (not isinstance(cloud, dict) or not isinstance(cloud.get("url"), str) or not isinstance(cloud.get("token"), str) or not cloud["url"].startswith("https://")):
        return job, None, "the cloud broker is malformed"
    return job, local_broker.BrokerState(_secrets.token_urlsafe(32), secrets, cloud), None


def child_job(job: dict, port: int, token: str, names: list[str]) -> dict:
    """The job the script's own program receives: the broker's address and token replace the cloud broker's, and no secret value is in it."""
    env = dict(job.get("env_vars") or {})
    available = {n for n in (env.get("PARASYNC_AVAILABLE_SECRETS") or "").split(",") if n} | set(names)
    env["PARASYNC_BROKER_URL"] = f"http://127.0.0.1:{port}"
    env["PARASYNC_BROKER_TOKEN"] = token
    env["PARASYNC_AVAILABLE_SECRETS"] = ",".join(sorted(available))
    return {**job, "env_vars": env}


def main(argv: list[str]) -> int:
    try:
        limit = max(1, min(int(argv[1]), 900))
    except (IndexError, ValueError):
        emit({"ok": False, "error": "no time limit was given"})
        return 0
    refuse_dumps()
    try:
        envelope = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        emit({"ok": False, "error": "the job was not valid JSON"})
        return 0
    job, state, problem = parse(envelope)
    if problem or state is None:
        emit({"ok": False, "error": problem or "the job envelope is malformed"})
        return 0

    server, port = local_broker.start(state)
    try:
        payload = child_job(job, port, state.token, sorted(state.secrets))
        child = subprocess.Popen(
            CHILD + [str(limit), "python3", SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            out, _ = child.communicate(json.dumps(payload).encode(), timeout=limit + 15)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            return 137                                  # the app reads 137 as "did not finish in time"
        line = next((l for l in reversed(out.decode("utf-8", errors="replace").splitlines()) if l.startswith(MARKER)), None)
        if line is None:
            return 137 if child.returncode in (137, -9) else 1
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
