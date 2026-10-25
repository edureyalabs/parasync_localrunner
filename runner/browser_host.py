# runner/browser_host.py
"""
One browser session = one process of this program, kept alive by the desktop app for the session's whole life:

    wsl.exe -d ParasyncRunner -u root --exec sh -c '<lock interop>; exec runuser -u runner -- ... python3 /opt/runner/browser_host.py <session_id>'

It launches headless Chromium once (an in-memory profile, nothing is written to disk) and then answers requests the app writes to its standard
input, one JSON object per line. Nothing listens on a port: Playwright talks to Chromium over a pipe, and the app talks to this program over
stdin/stdout, so no other program on the PC can attach to the browser.

Requests (stdin), each a single line:
  {"id": "...", "op": "exec", "code": "...", "files": {"rel/path": "<base64>"}, "env_vars": {...}, "timeout": 120}
  {"id": "...", "op": "ping"}
  {"id": "...", "op": "close"}
Replies (stdout), each a single line starting with the marker so that anything else printed is ignored:
  __PARASYNC_BROWSER__{"type": "ready"}
  __PARASYNC_BROWSER__{"type": "response", "id": "...", "outcome": {...}}
  __PARASYNC_BROWSER__{"type": "fatal", "error": "..."}            (then the process exits)
An "exec" outcome has the same shape the cloud's /browser/exec returns: {"ok", "result" | "error", "stdout", "output_files": {path: base64}}.

An exec script runs here with `browser`, `context` and `page` pre-bound (the synchronous Playwright API, exactly like the cloud harness) and a
`result` variable is returned if it is JSON-serialisable. Files the script writes into the working folder come back as output_files (new or changed
since before the call). The process exits when the app closes the pipe, on "close", or after IDLE_SECONDS without a request.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import select
import signal
import sys
import traceback
from contextlib import contextmanager
from typing import Any, Callable, IO

MARKER = "__PARASYNC_BROWSER__"
IDLE_SECONDS = 45 * 60
DEFAULT_EXEC_TIMEOUT = 120
MAX_EXEC_TIMEOUT = 600
WORK_ROOT = os.environ.get("PARASYNC_BROWSER_WORK_ROOT", "/tmp/parasync-browser")
CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--disable-gpu"]


class ExecTimeout(BaseException):
    """BaseException on purpose: a script's own `except Exception:` must not swallow the time limit."""


def emit(out: IO[str], message: dict) -> None:
    out.write(MARKER + json.dumps(message) + "\n")
    out.flush()


def _hash_tree(root: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(folder, name)
            try:
                with open(path, "rb") as handle:
                    hashes[os.path.relpath(path, root).replace(os.sep, "/")] = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
    return hashes


def _safe_join(root: str, rel: str) -> str | None:
    """A staged file must stay inside the working folder."""
    full = os.path.normpath(os.path.join(root, rel))
    return full if full == root or full.startswith(root + os.sep) else None


@contextmanager
def _time_limit(seconds: float):
    def on_alarm(_signum, _frame):
        raise ExecTimeout()

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def _environment(extra: dict[str, str]):
    """The call's own variables (the broker URL and token) exist only while the script runs."""
    previous = {key: os.environ.get(key) for key in extra}
    os.environ.update({str(k): str(v) for k, v in extra.items()})
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class Session:
    """The browser, its one context and its first page, plus the working folder scripts run in."""

    def __init__(self, session_id: str, launcher: Callable[[], tuple[Any, Any, Any, Callable[[], None]]]):
        self.session_id = session_id
        self.workdir = os.path.join(WORK_ROOT, session_id, "work")
        os.makedirs(self.workdir, exist_ok=True)
        os.chdir(self.workdir)
        self.browser, self.context, self.page, self._stop = launcher()

    def stop(self) -> None:
        try:
            self._stop()
        except Exception:
            pass

    def alive(self) -> bool:
        """A cheap call that fails when the browser or the Playwright connection is broken."""
        try:
            with _time_limit(8):
                _ = self.context.pages
                _ = self.page.url
            return True
        except BaseException:
            return False

    def execute(self, code: str, files: dict[str, str], env_vars: dict[str, str], timeout: float) -> dict:
        for rel, b64 in (files or {}).items():
            dest = _safe_join(self.workdir, rel)
            if dest is None:
                return {"ok": False, "error": f"refused a file path outside the working folder: {rel}", "stdout": "", "output_files": {}}
            os.makedirs(os.path.dirname(dest) or self.workdir, exist_ok=True)
            with open(dest, "wb") as handle:
                handle.write(base64.b64decode(b64))
        before = _hash_tree(self.workdir)

        namespace: dict[str, Any] = {"browser": self.browser, "context": self.context, "page": self.page}
        captured = io.StringIO()
        real_stdout, real_stderr = sys.stdout, sys.stderr
        outcome: dict[str, Any]
        timed_out = False
        sys.stdout = sys.stderr = captured
        try:
            with _environment(env_vars or {}), _time_limit(timeout):
                exec(compile(code, "<browser_script>", "exec"), namespace)
            if "result" in namespace:
                try:
                    json.dumps(namespace["result"])
                    outcome = {"ok": True, "result": namespace["result"]}
                except TypeError:
                    outcome = {"ok": False, "error": "`result` is not JSON-serializable"}
            else:
                outcome = {"ok": True, "result": None}
        except ExecTimeout:
            timed_out = True
            outcome = {"ok": False, "error": f"browser script timed out after {int(timeout)}s and was stopped"}
        except SystemExit:
            outcome = {"ok": False, "error": "the script called exit()"}
        except Exception as exc:
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            sys.stdout, sys.stderr = real_stdout, real_stderr

        outcome["stdout"] = captured.getvalue()
        after = _hash_tree(self.workdir)
        output_files: dict[str, str] = {}
        for rel, digest in after.items():
            if before.get(rel) == digest:
                continue
            with open(os.path.join(self.workdir, rel), "rb") as handle:
                output_files[rel] = base64.b64encode(handle.read()).decode()
        outcome["output_files"] = output_files
        outcome["timed_out"] = timed_out
        return outcome


class LineReader:
    """Lines from a file descriptor with a time limit. Unbuffered on purpose: select() on a buffered Python reader can miss lines that
    already sit in its buffer, which would strand a request that arrived together with another one."""

    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = b""
        self.eof = False

    def readline(self, timeout: float) -> bytes | None:
        """A line, b"" at end of input, or None when nothing arrived within `timeout` seconds."""
        while b"\n" not in self.buffer:
            if self.eof:
                rest, self.buffer = self.buffer, b""
                return rest
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                return None
            chunk = os.read(self.fd, 1 << 20)
            if not chunk:
                self.eof = True
                continue
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line + b"\n"


def serve(session_id: str, launcher: Callable[[], tuple[Any, Any, Any, Callable[[], None]]], stdin: IO[bytes] | None = None, stdout: IO[str] | None = None, idle_seconds: float = IDLE_SECONDS) -> int:
    out = stdout or sys.stdout
    source = LineReader((stdin or sys.stdin.buffer).fileno())
    try:
        session = Session(session_id, launcher)
    except Exception as exc:
        emit(out, {"type": "fatal", "error": f"the browser could not be started: {type(exc).__name__}: {exc}"})
        return 1
    emit(out, {"type": "ready"})
    try:
        while True:
            line = source.readline(idle_seconds)
            if line is None:
                emit(out, {"type": "fatal", "error": "the browser session was idle for too long and was closed"})
                return 0
            if not line:
                return 0                      # the app closed the pipe (quit, crash, sign-out): the browser goes with it
            try:
                request = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            op, rid = request.get("op"), request.get("id")
            if op == "close":
                emit(out, {"type": "response", "id": rid, "outcome": {"ok": True}})
                return 0
            if op == "ping":
                emit(out, {"type": "response", "id": rid, "outcome": {"ok": session.alive()}})
                continue
            if op != "exec" or not isinstance(request.get("code"), str) or not request["code"].strip():
                emit(out, {"type": "response", "id": rid, "outcome": {"ok": False, "error": "bad request"}})
                continue
            timeout = float(min(max(request.get("timeout") or DEFAULT_EXEC_TIMEOUT, 1), MAX_EXEC_TIMEOUT))
            outcome = session.execute(request["code"], request.get("files") or {}, request.get("env_vars") or {}, timeout)
            if outcome.pop("timed_out", False) and not session.alive():
                # The time limit interrupted Playwright mid-call and left it unusable: say so instead of limping on.
                emit(out, {"type": "response", "id": rid, "outcome": outcome})
                emit(out, {"type": "fatal", "error": "the browser stopped responding after a timeout and was closed"})
                return 0
            outcome.pop("timed_out", None)
            emit(out, {"type": "response", "id": rid, "outcome": outcome})
    finally:
        session.stop()


def playwright_launcher() -> tuple[Any, Any, Any, Callable[[], None]]:
    """The real launcher: synchronous Playwright, headless Chromium, an in-memory profile."""
    # WSL does not carry the image's Docker ENV into the running system, so say where the browser lives.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
    except BaseException:
        pw.stop()
        raise

    def stop() -> None:
        try:
            browser.close()
        finally:
            pw.stop()

    return browser, context, page, stop


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].replace("-", "").isalnum():
        sys.stderr.write("usage: browser_host.py <session_id>\n")
        return 2
    # Playwright's driver and Chromium must never write into our protocol channel.
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    try:
        return serve(sys.argv[1], playwright_launcher, stdout=protocol)
    except Exception:
        emit(protocol, {"type": "fatal", "error": traceback.format_exc(limit=3)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
