# runner/browser_host.py
"""
One browser session = one process of this program, kept alive by the desktop app for the session's whole life:

    wsl.exe -d ParasyncRunner -u root --exec sh -c '<lock interop>; exec runuser -u runner -- ... python3 /opt/runner/browser_host.py <session_id>'

It starts headless Chromium once, on a free port of this box's loopback address, and then answers the requests the app writes to its standard input, one
JSON object per line. Everything a request does is browser_core.py, a verbatim copy of the cloud's (scripts/sync_browser.py): a script runs in its own process
against that Chromium, `look` describes the page, `act` does one move and reports what it caused, and the resident watcher (senses.py) records the network,
console, dialogs, tabs and downloads between calls. So a browser here behaves like a browser in the cloud MicroVM. What is this file's own: starting and
stopping Chromium, the line protocol, the session's lifetime.

The debugging port is the price of that sameness. It is chosen at random per session, bound to 127.0.0.1 of the box, and exists only while the session does.

Requests (stdin), each a single line:
  {"id": "...", "op": "exec", "code": "...", "files": {"rel/path": "<base64>"}, "env_vars": {...}, "timeout": 120}
  {"id": "...", "op": "look", ...the cloud's /browser/look body...}
  {"id": "...", "op": "act", "action": "click", ...the cloud's /browser/act body...}
  {"id": "...", "op": "ping"}
  {"id": "...", "op": "close"}
Replies (stdout), each a single line starting with the marker so that anything else printed is ignored:
  __PARASYNC_BROWSER__{"type": "ready", "senses": true}
  __PARASYNC_BROWSER__{"type": "response", "id": "...", "outcome": {...}}
  __PARASYNC_BROWSER__{"type": "fatal", "error": "..."}            (then the process exits)
An outcome has the shape the cloud endpoint returns: exec {"ok", "result" | "error", "stdout", "output_files", "senses"}, look and act {"ok", "report", "page",
"settled", ..., "output_files"}. The process exits when the app closes the pipe, on "close", when Chromium dies, or after IDLE_SECONDS without a request.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from typing import IO, Any, Callable, Optional

MARKER = "__PARASYNC_BROWSER__"
IDLE_SECONDS = 45 * 60
DEFAULT_EXEC_TIMEOUT = 120
MAX_EXEC_TIMEOUT = 600
START_SECONDS = 40
WORK_ROOT = os.environ.get("PARASYNC_BROWSER_WORK_ROOT", "/tmp/parasync-browser")
CHROMIUM_ARGS = ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-first-run", "--no-default-browser-check"]
STOP_GRACE_SECONDS = 5


def emit(out: IO[str], message: dict) -> None:
    out.write(MARKER + json.dumps(message) + "\n")
    out.flush()


# ── Chromium ─────────────────────────────────────────────────────────────────

def _descendants(pid: int) -> list:
    """Every process below `pid` (found in /proc; no extra package)."""
    children: dict = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as handle:
                ppid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(name))
    found: list = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Chromium:
    """A Chromium process of its own with a remote-debugging port on loopback and a throw-away profile folder."""

    def __init__(self, executable: str, profile: str, port: Optional[int] = None) -> None:
        self.port = port or _free_port()
        self.profile = profile
        self.cdp_url = "http://127.0.0.1:%d" % self.port
        os.makedirs(profile, exist_ok=True)
        args = [executable, *CHROMIUM_ARGS, "--remote-debugging-port=%d" % self.port, "--remote-debugging-address=127.0.0.1", "--user-data-dir=" + profile, "about:blank"]
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_ready(self, seconds: float = START_SECONDS) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Chromium exited while starting (code %s)" % self.proc.returncode)
            try:
                urllib.request.urlopen(self.cdp_url + "/json/version", timeout=1)
                return
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("Chromium did not answer on its debugging port in %ds" % int(seconds))

    def alive(self) -> bool:
        """A real round trip, not just 'the process exists': a hung Chromium does not answer."""
        if self.proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(self.cdp_url + "/json/version", timeout=3)
            return True
        except Exception:
            return False

    def stop(self) -> None:
        below = _descendants(self.proc.pid)
        try:
            self.proc.terminate()
            self.proc.wait(STOP_GRACE_SECONDS)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        for pid in reversed(below):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)


def real_chromium(session_id: str, workdir: str) -> Chromium:
    """The real thing: the Chromium Playwright installed in this box (/opt/pw-browsers), started and waited for."""
    # WSL does not carry the image's Docker ENV into the running system, so say where the browser lives.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    from playwright.sync_api import sync_playwright

    executable = os.environ.get("PARASYNC_CHROMIUM_PATH") or ""          # a development / test override; a PC always uses the one Playwright installed
    if not executable:
        with sync_playwright() as pw:
            executable = pw.chromium.executable_path
    chromium = Chromium(executable, os.path.join(WORK_ROOT, session_id, "profile"))
    try:
        chromium.wait_ready()
    except BaseException:
        chromium.stop()
        raise
    return chromium


def real_core(workdir: str, cdp_url: str) -> Any:
    from browser_core import BrowserCore

    return BrowserCore(workdir, cdp_url, DEFAULT_EXEC_TIMEOUT, python=sys.executable or "python3")


# ── the line protocol ────────────────────────────────────────────────────────

def _read_lines(stdin: IO[bytes], loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue") -> None:
    """Runs in a thread: one queue item per line, None at the end of input. Quiet when the session is already over (the loop is closed)."""
    def post(item: Any) -> bool:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
            return True
        except RuntimeError:
            return False

    try:
        while True:
            line = stdin.readline()
            if not line or not post(line):
                break
    except Exception:
        pass
    post(None)


async def _handle(core: Any, request: dict) -> dict:
    op = request.get("op")
    if op == "exec":
        code = request.get("code")
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "bad request"}
        try:
            wanted = float(request.get("timeout") or DEFAULT_EXEC_TIMEOUT)
        except (TypeError, ValueError):
            wanted = float(DEFAULT_EXEC_TIMEOUT)
        timeout = float(min(max(wanted, 1), MAX_EXEC_TIMEOUT))
        return await core.run_script(code, request.get("files") or {}, request.get("env_vars") or {}, timeout)
    body = {k: v for k, v in request.items() if k not in ("id", "op")}
    if op == "look":
        return await core.look(body)
    if op == "act":
        return await core.act(body)
    return {"ok": False, "error": "bad request"}


async def serve_async(session_id: str, chromium_factory: Callable[[str, str], Any], core_factory: Callable[[str, str], Any],
                      stdin: IO[bytes], out: IO[str], idle_seconds: float = IDLE_SECONDS) -> int:
    workdir = os.path.join(WORK_ROOT, session_id, "work")
    os.makedirs(workdir, exist_ok=True)
    os.chdir(workdir)
    loop = asyncio.get_event_loop()
    chromium = None
    core = None
    try:
        try:
            # Off the event loop: finding Chromium asks Playwright's synchronous API, which refuses to run inside a running loop, and starting it waits seconds.
            chromium = await loop.run_in_executor(None, chromium_factory, session_id, workdir)
            core = core_factory(workdir, chromium.cdp_url)
        except Exception as exc:
            emit(out, {"type": "fatal", "error": "the browser could not be started: %s: %s" % (type(exc).__name__, exc)})
            return 1
        queue: "asyncio.Queue" = asyncio.Queue()
        threading.Thread(target=_read_lines, args=(stdin, loop, queue), daemon=True).start()
        emit(out, {"type": "ready", "senses": True})
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), idle_seconds)
            except asyncio.TimeoutError:
                emit(out, {"type": "fatal", "error": "the browser session was idle for too long and was closed"})
                return 0
            if line is None:
                return 0                      # the app closed the pipe (quit, crash, sign-out): the browser goes with it
            try:
                request = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(request, dict):
                continue
            op, rid = request.get("op"), request.get("id")
            if op == "close":
                emit(out, {"type": "response", "id": rid, "outcome": {"ok": True}})
                return 0
            alive = chromium.alive()
            if op == "ping":
                emit(out, {"type": "response", "id": rid, "outcome": {"ok": alive}})
                continue
            if not alive:
                emit(out, {"type": "response", "id": rid, "outcome": {"ok": False, "error": "the browser stopped responding", "stdout": "", "output_files": {}}})
                emit(out, {"type": "fatal", "error": "the browser stopped responding and the session ended"})
                return 0
            try:
                outcome = await _handle(core, request)
            except Exception as exc:
                outcome = {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300]), "stdout": "", "output_files": {}}
            emit(out, {"type": "response", "id": rid, "outcome": outcome})
    finally:
        if core is not None:
            try:
                await core.senses._teardown()
            except Exception:
                pass
        if chromium is not None:
            chromium.stop()


def serve(session_id: str, chromium_factory: Callable[[str, str], Any] = real_chromium, core_factory: Callable[[str, str], Any] = real_core,
          stdin: Optional[IO[bytes]] = None, stdout: Optional[IO[str]] = None, idle_seconds: float = IDLE_SECONDS) -> int:
    return asyncio.run(serve_async(session_id, chromium_factory, core_factory, stdin or sys.stdin.buffer, stdout or sys.stdout, idle_seconds))


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].replace("-", "").isalnum():
        sys.stderr.write("usage: browser_host.py <session_id>\n")
        return 2
    # Playwright's driver and Chromium must never write into our protocol channel.
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    try:
        return serve(sys.argv[1], stdout=protocol)
    except Exception:
        emit(protocol, {"type": "fatal", "error": traceback.format_exc(limit=3)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
