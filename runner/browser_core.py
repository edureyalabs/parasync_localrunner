# sandbox/microvm/browser_core.py
"""
Everything a browser session does for its caller, with no web framework and no fixed port: run a Playwright script against the session's Chromium, look at
the page, do one move on it, and hand back the files that appeared. Two programs use this one module, so the browser behaves the same wherever it runs:

  * microvm_app.py   (the AWS MicroVM, FastAPI endpoints /browser/exec, /browser/look, /browser/act), and
  * parasync_localrunner/runner/browser_host.py   (a person's own PC, a line protocol on stdin/stdout) which receives a verbatim copy of this file and of
    senses.py, driver.py, scrub.py, page_js.py and browser_harness.py (parasync_localrunner/scripts/sync_browser.py).

The caller starts Chromium (with a remote-debugging port on loopback) and gives this module the address. Nothing here knows how the request arrived or which
machine it is on. Python 3.9 compatible (the MicroVM image's and the PC box's interpreter). Changing this file changes both places: re-sync the local runner.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import subprocess
from typing import Any, Dict, Optional

from browser_harness import parse_harness_output, render_harness
from driver import Driver
from senses import Senses

log = logging.getLogger("parasync-browser-core")

HARNESS_FILENAME = "__parasync_browser_harness__.py"
DEFAULT_EXEC_TIMEOUT_SECONDS = 120
LOOK_TIMEOUT_SECONDS = 60
ACT_TIMEOUT_SECONDS = 120


class BrowserCore:
    def __init__(self, workdir: str, cdp_url: str, exec_timeout: int = DEFAULT_EXEC_TIMEOUT_SECONDS, python: str = "python3") -> None:
        self.workdir = workdir
        self.cdp_url = cdp_url
        self.exec_timeout = exec_timeout
        self.python = python
        # The resident watcher (senses.py) and the look/act driver (driver.py) share ONE connection to the Chromium above. Connected lazily on the first call (a
        # connection cannot survive an image snapshot) and reconnected by itself if it drops.
        self.senses = Senses(cdp_url, workdir)
        self.driver = Driver(self.senses)
        # File name -> content hash of everything already handed back, so a file made by a script, a click or a download between calls is delivered once.
        self.delivered: Dict[str, str] = {}
        os.makedirs(workdir, exist_ok=True)

    # ── files ────────────────────────────────────────────────────────────────

    def hash_tree(self) -> dict:
        found: dict = {}
        for root, _dirs, filenames in os.walk(self.workdir):
            for fname in filenames:
                if fname == HARNESS_FILENAME and root == self.workdir:
                    continue
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, self.workdir).replace(os.sep, "/")
                try:
                    with open(abs_path, "rb") as f:
                        found[rel_path] = (hashlib.sha256(f.read()).hexdigest(), abs_path)
                except OSError:
                    continue
        return found

    def stage_files(self, files: Optional[dict]) -> None:
        os.makedirs(self.workdir, exist_ok=True)
        root = os.path.normpath(self.workdir)
        for rel_path, b64content in (files or {}).items():
            dest = os.path.normpath(os.path.join(self.workdir, rel_path))
            if dest != root and not dest.startswith(root + os.sep):
                continue
            os.makedirs(os.path.dirname(dest) or self.workdir, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(base64.b64decode(b64content))
        for rel_path, (digest, _abs) in self.hash_tree().items():
            if rel_path in (files or {}):
                self.delivered[rel_path] = digest           # a file the caller handed in is not an output

    def collect_outputs(self) -> dict:
        """Files that are new or changed since they were last handed back (made by a script, an action, or a download between calls)."""
        out: dict = {}
        for rel_path, (digest, abs_path) in self.hash_tree().items():
            if self.delivered.get(rel_path) == digest:
                continue
            try:
                with open(abs_path, "rb") as f:
                    out[rel_path] = base64.b64encode(f.read()).decode()
                self.delivered[rel_path] = digest
            except OSError:
                continue
        return out

    # ── the senses ───────────────────────────────────────────────────────────

    async def senses_ready(self) -> bool:
        """Connects the watcher; never raises (the senses are an addition: a browser call must still work without them)."""
        try:
            await self.senses.ensure()
            return True
        except Exception:
            log.warning("senses could not connect", exc_info=True)
            return False

    # ── a script ─────────────────────────────────────────────────────────────

    async def run_script(self, code: str, files: Optional[dict] = None, env_vars: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        """Runs a Playwright script in its own process against this session's Chromium and returns {ok, result|error, stdout, output_files, senses}. A script
        that overruns its time is killed alone: the browser, its tabs and its login are untouched."""
        if not (code or "").strip():
            return {"ok": False, "error": "no code provided"}
        limit = float(timeout) if timeout else float(self.exec_timeout)
        self.stage_files(files)
        watching = await self.senses_ready()
        mark = self.senses.mark() if watching else 0
        active_target = ""
        if watching and len(self.senses.pages()) > 1:
            try:
                active_target = await self.senses.target_id(self.senses.page_for(None))
            except Exception:
                active_target = ""

        harness = render_harness(code, active_target, self.cdp_url)
        harness_path = os.path.join(self.workdir, HARNESS_FILENAME)
        with open(harness_path, "w", encoding="utf-8") as f:
            f.write(harness)

        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            **{str(k): str(v) for k, v in (env_vars or {}).items()},
        }

        # subprocess.run() blocks: run it off the event loop so the watcher keeps recording while the script works.
        def _run_sync() -> tuple:
            try:
                proc = subprocess.run([self.python, HARNESS_FILENAME], cwd=self.workdir, env=env, capture_output=True, text=True, timeout=limit)
                return proc.stdout, proc.stderr, False
            except subprocess.TimeoutExpired as exc:
                out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                err = (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                return out, err, True

        out_text, err_text, timed_out = await asyncio.get_event_loop().run_in_executor(None, _run_sync)

        try:
            os.remove(harness_path)
        except OSError:
            pass
        output_files = self.collect_outputs()
        senses_part = None
        if watching:
            try:
                senses_part = self.senses.summarize(mark, limit=8)
            except Exception:
                senses_part = None

        if timed_out:
            return {"ok": False, "error": "browser script timed out after %ds and was killed" % int(limit), "stdout": out_text, "output_files": output_files, "senses": senses_part}
        parsed = parse_harness_output(out_text)
        if parsed is None:
            detail = out_text.strip() or err_text.strip() or "no output"
            return {"ok": False, "error": "execution produced no result: %s" % detail, "stdout": out_text, "output_files": output_files, "senses": senses_part}
        parsed["output_files"] = output_files
        parsed["senses"] = senses_part
        return parsed

    # ── look and act ─────────────────────────────────────────────────────────

    async def look(self, body: dict) -> dict:
        """Describes the page the agent is on right now (driver.py)."""
        try:
            self.stage_files(body.get("files") or {})
            result = await asyncio.wait_for(self.driver.look(body), LOOK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "look took too long (the page may be stuck loading)"}
        except Exception as exc:
            log.warning("look failed", exc_info=True)
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
        result["output_files"] = self.collect_outputs()
        return result

    async def act(self, body: dict) -> dict:
        """Does one thing in the browser and reports what it caused (driver.py)."""
        try:
            self.stage_files(body.get("files") or {})
            result = await asyncio.wait_for(self.driver.act(body), ACT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "the action took too long and was given up on; the browser is still open, run look to see where it is"}
        except Exception as exc:
            log.warning("act failed", exc_info=True)
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:300])}
        result["output_files"] = self.collect_outputs()
        return result
