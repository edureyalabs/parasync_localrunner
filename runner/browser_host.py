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

A script that runs past its time limit is stopped by a signal, and that signal can land inside Playwright's own plumbing and leave the whole
connection dead (page.title() then never answers again; found on real PCs 2026-10-25, where every later call burned its full time limit until
the agent closed the browser). So after a timeout the host checks the browser with a real round trip, and when it is dead it replaces it in
place: a new Chromium with the same cookies and local storage, back on the page it was on. The session survives and stays signed in; only the
page's unsaved state is lost, and the timed-out call's answer says so.
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
import threading
import traceback
from contextlib import contextmanager
from typing import Any, Callable, IO

MARKER = "__PARASYNC_BROWSER__"
IDLE_SECONDS = 45 * 60
DEFAULT_EXEC_TIMEOUT = 120
MAX_EXEC_TIMEOUT = 600
WORK_ROOT = os.environ.get("PARASYNC_BROWSER_WORK_ROOT", "/tmp/parasync-browser")
CHROMIUM_ARGS = ["--disable-dev-shm-usage", "--disable-gpu"]


STOP_GRACE_SECONDS = 5
PROBE_SECONDS = 5
RELOAD_TIMEOUT_MS = 12_000   # probe 5 s + close 5 s + start ~3 s + this = inside the app's 30 s grace after a script's own limit


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


def _descendants(pid: int) -> list[int]:
    """Every process below `pid` (found in /proc; no extra package). The browser driver and Chromium are among them."""
    children: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as handle:
                ppid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(name))
    found: list[int] = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def _kill_descendants() -> None:
    for pid in reversed(_descendants(os.getpid())):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


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

    def __init__(self, session_id: str, launcher: Callable[..., tuple[Any, Any, Any, Callable[[], None]]]):
        self.session_id = session_id
        self.launcher = launcher
        self.workdir = os.path.join(WORK_ROOT, session_id, "work")
        os.makedirs(self.workdir, exist_ok=True)
        os.chdir(self.workdir)
        self.state: dict | None = None      # cookies and local storage as of the last call that left the browser healthy (memory only)
        self.last_url: str | None = None
        self.browser, self.context, self.page, self._stop = launcher()

    def stop(self) -> None:
        self._abandon()

    def _abandon(self) -> None:
        """Closes the browser, and kills whatever is left of it when closing does not work (a dead connection cannot close anything)."""
        closed = {"ok": False}

        def close() -> None:
            try:
                self._stop()
                closed["ok"] = True
            except BaseException:
                pass

        worker = threading.Thread(target=close, daemon=True)
        worker.start()
        worker.join(STOP_GRACE_SECONDS)
        if not closed["ok"]:
            _kill_descendants()

    def alive(self) -> bool:
        """A real round trip to the page. Reading `page.url` or `context.pages` is not one: Playwright keeps those in memory, so they still
        answer after the connection to Chromium is dead."""
        try:
            with _time_limit(PROBE_SECONDS):
                self.page.evaluate("1")
            return True
        except BaseException:
            return False

    def remember(self) -> None:
        """Keeps the login and the page address, so a browser that has to be replaced can come back where it was."""
        try:
            with _time_limit(PROBE_SECONDS):
                self.state = self.context.storage_state()
                self.last_url = self.page.url
        except BaseException:
            pass

    def recover(self) -> str:
        """Replaces a browser that stopped answering. Returns what to tell the caller; raises when no new browser can be started."""
        url = self.last_url
        self._abandon()
        if self.state is not None:
            self.browser, self.context, self.page, self._stop = self.launcher(storage_state=self.state)
        else:
            self.browser, self.context, self.page, self._stop = self.launcher()
        if url and url.startswith(("http://", "https://")):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=RELOAD_TIMEOUT_MS)
                return (
                    f"The browser had stopped responding, so it was restarted. You are still signed in (cookies and local storage kept) "
                    f"and the page was reloaded at {url}; anything on the page that was not saved is gone, and `page` is the new tab."
                )
            except Exception:
                return (
                    f"The browser had stopped responding, so it was restarted. You are still signed in (cookies and local storage kept) "
                    f"but the page could not be reloaded; go to {url} again."
                )
        return "The browser had stopped responding, so it was restarted (it is on a blank tab)."

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


def serve(session_id: str, launcher: Callable[..., tuple[Any, Any, Any, Callable[[], None]]], stdin: IO[bytes] | None = None, stdout: IO[str] | None = None, idle_seconds: float = IDLE_SECONDS) -> int:
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
            if outcome.pop("timed_out", False):
                if not session.alive():
                    # The time limit interrupted Playwright mid-call and left it unusable. Replace the browser (same login, same page)
                    # instead of leaving a session that answers nothing; only when that fails too is the session over.
                    try:
                        outcome["error"] = f"{outcome.get('error', 'browser script timed out')}. {session.recover()}"
                        outcome["browser_restarted"] = True
                    except BaseException as exc:
                        emit(out, {"type": "response", "id": rid, "outcome": outcome})
                        emit(out, {"type": "fatal", "error": f"the browser stopped responding after a timeout and could not be restarted: {type(exc).__name__}: {exc}"})
                        return 0
                else:
                    session.remember()
            else:
                session.remember()
            emit(out, {"type": "response", "id": rid, "outcome": outcome})
    finally:
        session.stop()


def playwright_launcher(storage_state: dict | None = None) -> tuple[Any, Any, Any, Callable[[], None]]:
    """The real launcher: synchronous Playwright, headless Chromium, an in-memory profile (`storage_state`: the cookies and local storage of
    the browser this one replaces)."""
    # WSL does not carry the image's Docker ENV into the running system, so say where the browser lives.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
        context = browser.new_context(accept_downloads=True, storage_state=storage_state) if storage_state else browser.new_context(accept_downloads=True)
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
