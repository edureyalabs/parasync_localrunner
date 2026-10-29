# sandbox/microvm/senses.py
"""
The agent's senses for the browser in this MicroVM: ONE resident watcher that stays connected to the already-running Chromium (over CDP, through Playwright's
connection) for the whole life of the session, so nothing that happens between two calls is lost. A script run by /browser/exec starts a new process
every time and forgets everything when it ends; this object does not.

It records, scrubbed (scrub.py) before anything leaves this machine:

  * NETWORK   every request that matters (pages, XHR/fetch, form posts, anything that failed), with what was sent, the status, how long it took and the
              SHAPE of a JSON reply. Static files (images, styles, fonts, scripts) are only counted.
  * CONSOLE   errors and warnings, uncaught page errors.
  * EVENTS    new tabs, closed tabs, address changes, browser dialogs (alert/confirm/prompt: handled and logged, so they can never freeze the page),
              downloads (saved into the working folder so they come back as files), websockets opening.
  * SETTLED   an honest "the page has stopped changing": no request in flight, no DOM change, document complete.
  * STRUCTURE `look()`: the page described through window.__ps (page_js.py), frames and open shadow roots included, with stable handles.
  * CHANGES   `act()`: one move (click, type, choose, ...) that waits for the page to settle and reports what appeared, disappeared and changed, which
              requests it caused, and anything that showed briefly (a toast).

Everything is generic: nothing here knows any site. Python 3.9 compatible (the MicroVM image's interpreter).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from typing import Any, Optional
from urllib.parse import urlsplit

import scrub
from page_js import PS_JS

log = logging.getLogger("parasync-senses")

MAX_EVENTS = 1500
STATIC_TYPES = {"image", "stylesheet", "font", "media", "texttrack", "manifest", "ping", "beacon", "other_static"}
NOT_BUSY_TYPES = {"websocket", "eventsource", "media", "ping", "beacon"}
LONG_POLL_AFTER_S = 5.0                  # a request still open after this long stops counting as "the page is busy"
BODY_PEEK_MAX_BYTES = 400_000
BODY_PEEK_TIMEOUT_S = 1.5
DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024
TRACKERS = re.compile(r"(google-analytics|googletagmanager|doubleclick|facebook\.net|facebook\.com/tr|hotjar|clarity\.ms|segment\.io|segment\.com|mixpanel|sentry\.io|"
                      r"newrelic|nr-data|datadoghq|fullstory|intercom\.io|hs-analytics|hsforms|adsrvr|criteo|taboola|outbrain|bat\.bing|linkedin\.com/px|favicon\.ico)", re.I)
MAX_FRAMES = 12
DIALOG_GRACE_S = 0.15


def _now() -> float:
    return time.monotonic()


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _path(url: str, page_host: str = "") -> str:
    """A request address for display: the path and query, with the host only when it differs from the page's own."""
    try:
        p = urlsplit(url)
        shown = p.path or "/"
        if p.query:
            shown += "?" + p.query
        host = (p.hostname or "").lower()
        return shown if (not host or host == page_host) else host + shown
    except Exception:
        return url[:140]


class Senses:
    def __init__(self, cdp_url: str = "http://localhost:9222", workdir: str = "/tmp/browser-workspace") -> None:
        self.cdp_url = cdp_url
        self.workdir = workdir
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._conn_lock: Optional[asyncio.Lock] = None
        self.op_lock: Optional[asyncio.Lock] = None
        self.events: "deque[dict]" = deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.t0 = _now()
        self._inflight: dict = {}
        self._entries: dict = {}
        self._last_net = _now()
        self.active: Any = None
        self._attached: set = set()
        self._frame_by_tag: dict = {}
        self._allowed: list = []
        self._route_handler: Any = None
        self.dialog_mode: Optional[str] = None          # 'accept' | 'dismiss' for confirm/prompt while an action runs; None = dismiss (Playwright's own default)
        self.prompt_text: Optional[str] = None
        self._tasks: set = set()
        self.downloads_dir = os.path.join(workdir, "downloads")

    # ── connection ───────────────────────────────────────────────────────────

    def _locks(self) -> None:
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
            self.op_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return bool(self._browser is not None and self._browser.is_connected())

    async def ensure(self) -> None:
        """Connects (or reconnects after the browser or the connection went away). Cheap when already connected."""
        self._locks()
        if self.connected:
            return
        async with self._conn_lock:  # type: ignore[union-attr]
            if self.connected:
                return
            await self._teardown()
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
            self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            await self._context.add_init_script(PS_JS)
            self._context.on("page", lambda p: self._spawn(self._on_new_page(p, opened=True)))
            self._attached = set()
            for page in list(self._context.pages):
                await self._attach(page)
                await self._install(page)
            self.active = self._context.pages[0] if self._context.pages else None
            self._route_handler = None
            await self._apply_allowed()

    async def _teardown(self) -> None:
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = None
        self._inflight.clear()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── event log ────────────────────────────────────────────────────────────

    def emit(self, kind: str, **fields: Any) -> dict:
        self.seq += 1
        event = {"seq": self.seq, "ms": int((_now() - self.t0) * 1000), "kind": kind}
        event.update(fields)
        self.events.append(event)
        return event

    def mark(self) -> int:
        return self.seq

    def tab_index(self, page: Any) -> int:
        try:
            return self._context.pages.index(page)
        except Exception:
            return -1

    # ── attaching to pages ───────────────────────────────────────────────────

    async def _on_new_page(self, page: Any, opened: bool) -> None:
        if page in self._attached:
            return
        await self._attach(page)
        if opened:
            self.active = page
            self.emit("tab_opened", tab=self.tab_index(page), url=scrub.url(page.url))
        try:
            await self._install(page)
        except Exception:
            pass

    async def _attach(self, page: Any) -> None:
        if page in self._attached:
            return
        self._attached.add(page)
        page.on("request", lambda r: self._on_request(page, r))
        page.on("response", lambda r: self._spawn(self._on_response(page, r)))
        page.on("requestfinished", lambda r: self._on_finished(r))
        page.on("requestfailed", lambda r: self._on_failed(r))
        page.on("console", lambda m: self._on_console(page, m))
        page.on("pageerror", lambda e: self._on_pageerror(page, e))
        page.on("dialog", lambda d: self._spawn(self._on_dialog(page, d)))
        page.on("download", lambda d: self._spawn(self._on_download(page, d)))
        page.on("websocket", lambda w: self._on_websocket(page, w))
        page.on("framenavigated", lambda f: self._on_navigated(page, f))
        page.on("close", lambda _p=None: self._on_close(page))

    def _on_close(self, page: Any) -> None:
        self._attached.discard(page)
        self.emit("tab_closed", url=scrub.url(getattr(page, "url", "") or ""))
        if self.active is page:
            pages = [p for p in (self._context.pages if self._context else []) if not p.is_closed()]
            self.active = pages[-1] if pages else None

    def _on_navigated(self, page: Any, frame: Any) -> None:
        try:
            if frame == page.main_frame:
                self.emit("navigated", tab=self.tab_index(page), url=scrub.url(frame.url))
        except Exception:
            pass

    async def _install(self, page: Any) -> None:
        for frame in list(page.frames)[:MAX_FRAMES]:
            try:
                await frame.evaluate(PS_JS)
            except Exception:
                pass

    # ── network ──────────────────────────────────────────────────────────────

    def _relevant(self, rtype: str, method: str, url: str) -> bool:
        if TRACKERS.search(url):
            return False
        if rtype in STATIC_TYPES or rtype in ("script", "stylesheet", "image", "font"):
            return False
        return True

    def _on_request(self, page: Any, request: Any) -> None:
        try:
            rtype = request.resource_type
            url = request.url
            if url.startswith(("data:", "blob:", "chrome-extension:")):
                return
            if rtype not in NOT_BUSY_TYPES:
                self._inflight[request] = _now()
                self._last_net = _now()
            if not self._relevant(rtype, request.method, url):
                return
            try:
                post = scrub.body(request.post_data, request.headers.get("content-type", "")) if request.method != "GET" else None
            except Exception:
                post = None
            entry = {"m": request.method, "type": rtype, "url": scrub.url(url), "post": post, "tab": self.tab_index(page), "t0": _now()}
            self._entries[request] = entry
            self.emit("net", entry=entry)
        except Exception:
            log.debug("request hook failed", exc_info=True)

    async def _on_response(self, page: Any, response: Any) -> None:
        try:
            request = response.request
            self._last_net = _now()
            entry = self._entries.get(request)
            if entry is None:
                return
            entry["status"] = response.status
            headers = response.headers or {}
            mime = (headers.get("content-type") or "").split(";")[0].strip().lower()
            entry["mime"] = mime
            if 300 <= response.status < 400 and headers.get("location"):
                entry["to"] = scrub.url(headers["location"])
            await self._peek(page, request, response, entry, mime, headers)
        except Exception:
            log.debug("response hook failed", exc_info=True)

    async def _peek(self, page: Any, request: Any, response: Any, entry: dict, mime: str, headers: dict) -> None:
        """A short look at a background reply: its shape if JSON, its first words if text. Only for requests the page made to its own site (or an allowed one)."""
        if request.resource_type not in ("xhr", "fetch", "document") or request.resource_type == "document" and request.method == "GET":
            return
        host = _host(request.url)
        page_host = _host(page.url)
        if self._allowed:
            if not any(host == h or host.endswith("." + h) for h in self._allowed):
                return
        elif host and page_host and host != page_host and not host.endswith("." + page_host) and not page_host.endswith("." + host):
            return
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > BODY_PEEK_MAX_BYTES or not any(k in mime for k in ("json", "text", "xml", "javascript", "html", "x-www-form")):
            return
        try:
            raw = await asyncio.wait_for(response.body(), BODY_PEEK_TIMEOUT_S)
        except Exception:
            return
        entry["bytes"] = len(raw)
        if len(raw) > BODY_PEEK_MAX_BYTES:
            return
        text = raw.decode("utf-8", errors="replace")
        if "json" in mime or text.lstrip()[:1] in "{[":
            try:
                import json as _json

                value = _json.loads(text)
                entry["shape"] = scrub.shape(value)
                entry["peek"] = scrub.body(text, "application/json", 260)
                return
            except ValueError:
                pass
        plain = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
        plain = re.sub(r"<[^>]+>", " ", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            entry["peek"] = scrub.text(plain[:220])
        elif "html" in mime:
            entry["peek"] = "(html fragment)"

    def _on_finished(self, request: Any) -> None:
        self._inflight.pop(request, None)
        self._last_net = _now()
        entry = self._entries.get(request)
        if entry is not None:
            entry["ms"] = int((_now() - entry.get("t0", _now())) * 1000)

    def _on_failed(self, request: Any) -> None:
        self._inflight.pop(request, None)
        self._last_net = _now()
        entry = self._entries.get(request)
        failure = ""
        try:
            failure = request.failure or ""
        except Exception:
            pass
        if entry is None and not TRACKERS.search(request.url) and request.resource_type not in STATIC_TYPES:
            entry = {"m": request.method, "type": request.resource_type, "url": scrub.url(request.url), "post": None, "t0": _now()}
            self.emit("net", entry=entry)
        if entry is not None:
            entry["failed"] = scrub.text(str(failure))[:120] or "failed"

    def _on_websocket(self, page: Any, ws: Any) -> None:
        try:
            self.emit("ws_open", url=scrub.url(ws.url))
            ws.on("close", lambda _w=None: self.emit("ws_close", url=scrub.url(ws.url)))
        except Exception:
            pass

    # ── console, errors, dialogs, downloads ──────────────────────────────────

    def _on_console(self, page: Any, message: Any) -> None:
        try:
            kind = message.type
            if kind not in ("error", "warning", "warn", "assert"):
                self.emit("log", n=1)
                return
            text = scrub.text(message.text or "")[:240]
            last = self.events[-1] if self.events else None
            if last and last.get("kind") == "console" and last.get("text") == text:
                last["n"] = last.get("n", 1) + 1
                return
            self.emit("console", level="error" if kind in ("error", "assert") else "warning", text=text, n=1)
        except Exception:
            pass

    def _on_pageerror(self, page: Any, error: Any) -> None:
        try:
            self.emit("pageerror", text=scrub.text(str(getattr(error, "message", None) or error))[:240])
        except Exception:
            pass

    async def _on_dialog(self, page: Any, dialog: Any) -> None:
        await asyncio.sleep(DIALOG_GRACE_S)           # a script that registered its own handler gets the first chance
        try:
            kind = dialog.type
            accept = kind in ("alert", "beforeunload") or self.dialog_mode == "accept"
            self.emit("dialog", type=kind, message=scrub.text(dialog.message or "")[:300], handled="accepted" if accept else "dismissed")
            if accept:
                await dialog.accept(self.prompt_text) if kind == "prompt" and self.prompt_text is not None else await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception:
            log.debug("dialog already handled", exc_info=True)

    async def _on_download(self, page: Any, download: Any) -> None:
        try:
            os.makedirs(self.downloads_dir, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9._ -]", "_", download.suggested_filename or "download")[:120] or "download"
            dest = os.path.join(self.downloads_dir, name)
            base, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):
                n += 1
                dest = "%s_%d%s" % (base, n, ext)
            await download.save_as(dest)
            size = os.path.getsize(dest)
            rel = os.path.relpath(dest, self.workdir).replace(os.sep, "/")
            if size > DOWNLOAD_MAX_BYTES:
                os.remove(dest)
                self.emit("download", file=rel, bytes=size, error="larger than 25 MB, not kept")
            else:
                self.emit("download", file=rel, bytes=size, url=scrub.url(download.url))
        except Exception as exc:
            self.emit("download", error=scrub.text(str(exc))[:160])

    # ── guard for Browser Access sessions ────────────────────────────────────

    async def set_allowed(self, hosts: Optional[list]) -> None:
        self._allowed = sorted({h.lower() for h in hosts or [] if h})
        if self._context is not None:
            await self._apply_allowed()

    async def _apply_allowed(self) -> None:
        if self._route_handler is not None:
            try:
                await self._context.unroute("**/*", self._route_handler)
            except Exception:
                pass
            self._route_handler = None
        if not self._allowed:
            return
        bases = list(self._allowed)

        def ok(url: str) -> bool:
            h = _host(url)
            return (not h) or any(h == b or h.endswith("." + b) for b in bases)

        async def handler(route: Any, request: Any) -> None:
            try:
                if request.is_navigation_request() and not ok(request.url):
                    self.emit("blocked", url=scrub.url(request.url))
                    await route.abort()
                    return
            except Exception:
                pass
            try:
                await route.continue_()
            except Exception:
                pass

        self._route_handler = handler
        await self._context.route("**/*", handler)

    def host_allowed(self, url: str) -> bool:
        if not self._allowed:
            return True
        h = _host(url)
        return (not h) or any(h == b or h.endswith("." + b) for b in self._allowed)

    # ── tabs ─────────────────────────────────────────────────────────────────

    def pages(self) -> list:
        return [p for p in (self._context.pages if self._context else []) if not p.is_closed()]

    def page_for(self, tab: Any = None) -> Any:
        pages = self.pages()
        if tab is not None:
            try:
                return pages[int(tab)]
            except (ValueError, IndexError, TypeError):
                raise ValueError("there is no tab %s (tabs: 0 to %d)" % (tab, len(pages) - 1))
        if self.active is not None and not self.active.is_closed():
            return self.active
        if not pages:
            raise ValueError("the browser has no open tab")
        return pages[-1]

    async def target_id(self, page: Any) -> str:
        """The browser's own id for a tab: the same from every connection, unlike the tab's position."""
        try:
            session = await self._context.new_cdp_session(page)
            try:
                info = await session.send("Target.getTargetInfo")
                return str(info["targetInfo"]["targetId"])
            finally:
                await session.detach()
        except Exception:
            return ""

    async def tabs(self) -> list:
        out = []
        active = self.page_for()
        for i, p in enumerate(self.pages()):
            try:
                title = await asyncio.wait_for(p.title(), 1.0)
            except Exception:
                title = ""
            out.append({"tab": i, "url": scrub.url(p.url)[:140], "title": scrub.text(title)[:70], "active": p is active})
        return out

    # ── busy / settled ───────────────────────────────────────────────────────

    def busy_requests(self) -> list:
        now = _now()
        return [r for r, t in self._inflight.items() if now - t < LONG_POLL_AFTER_S]

    async def settle(self, page: Any, timeout_ms: int = 8000, quiet_ms: int = 500) -> dict:
        started = _now()
        deadline = started + timeout_ms / 1000.0
        while True:
            busy = self.busy_requests()
            muts_quiet, ready = 10 ** 6, "unknown"
            try:
                q = await asyncio.wait_for(page.main_frame.evaluate("() => window.__ps ? window.__ps.quiet() : null"), 1.5)
                if q:
                    muts_quiet, ready = q["since"], q["ready"]
                else:
                    await self._install(page)
            except Exception:
                muts_quiet, ready = 0, "navigating"
            net_quiet = (_now() - self._last_net) * 1000
            if not busy and ready in ("complete", "unknown") and muts_quiet >= quiet_ms and net_quiet >= quiet_ms:
                return {"settled": True, "waited_ms": int((_now() - started) * 1000)}
            if _now() >= deadline:
                still = [scrub.url(r.url)[:100] for r in busy][:4]
                return {"settled": False, "waited_ms": int((_now() - started) * 1000), **({"still_loading": still} if still else {}), "ready": ready}
            await asyncio.sleep(0.1)

    # ── summarising what happened ────────────────────────────────────────────

    def summarize(self, since: int, page_host: str = "", limit: int = 20) -> dict:
        """What happened after event number `since`, compact."""
        net: list = []
        statics = 0
        out: dict = {}
        console: list = []
        happenings: list = []
        logs = 0
        for ev in list(self.events):
            if ev["seq"] <= since:
                continue
            kind = ev["kind"]
            if kind == "net":
                e = ev["entry"]
                row = {"m": e.get("m"), "u": _path(e.get("url", ""), page_host)}
                for k_src, k_dst in (("status", "s"), ("ms", "ms"), ("post", "post"), ("shape", "reply"), ("peek", "peek"), ("failed", "failed"), ("to", "to")):
                    if e.get(k_src) not in (None, "", {}):
                        row[k_dst] = e[k_src]
                if e.get("type") not in ("xhr", "fetch"):
                    row["t"] = e.get("type")
                if net and net[-1].get("m") == row["m"] and net[-1].get("u") == row["u"] and net[-1].get("post") == row.get("post"):
                    net[-1]["n"] = net[-1].get("n", 1) + 1
                    continue
                net.append(row)
            elif kind == "console":
                console.append({"level": ev["level"], "text": ev["text"], **({"n": ev["n"]} if ev.get("n", 1) > 1 else {})})
            elif kind == "pageerror":
                console.append({"level": "pageerror", "text": ev["text"]})
            elif kind == "log":
                logs += ev.get("n", 1)
            elif kind in ("dialog", "tab_opened", "tab_closed", "navigated", "download", "ws_open", "ws_close", "blocked"):
                happenings.append({k: v for k, v in ev.items() if k not in ("seq", "ms")})
        if net:
            out["requests"] = net[:limit]
            if len(net) > limit:
                out["requests_more"] = len(net) - limit
        if console:
            out["console"] = console[:8]
        if happenings:
            out["events"] = happenings[:12]
        if logs:
            out["log_lines"] = logs
        return out

    # ── structure ────────────────────────────────────────────────────────────

    def _frames(self, page: Any) -> list:
        frames = [f for f in page.frames if not f.is_detached()]
        main = page.main_frame
        ordered = [main] + [f for f in frames if f != main]
        return ordered[:MAX_FRAMES]

    async def snapshot_frames(self, page: Any, opts: dict) -> list:
        """One __ps.snapshot per frame: [(frame_index, frame, snapshot dict)]. Frames that cannot be read are skipped."""
        out = []
        for index, frame in enumerate(self._frames(page)):
            if index > 0 and (frame.url in ("about:blank", "") and not opts.get("blank_frames")):
                continue
            child = dict(opts)
            if index > 0:
                child["max"] = min(opts.get("max", 120), 40)
                child["noText"] = index > 3
            try:
                snap = await asyncio.wait_for(frame.evaluate("o => window.__ps.snapshot(o)", child), 5.0)
            except Exception:
                try:
                    await frame.evaluate(PS_JS)
                    snap = await asyncio.wait_for(frame.evaluate("o => window.__ps.snapshot(o)", child), 5.0)
                except Exception:
                    continue
            if not isinstance(snap, dict):
                continue
            self._frame_by_tag[snap.get("frame_tag")] = frame
            out.append((index, frame, snap))
        return out

    async def light(self, page: Any) -> dict:
        """{handle: (role, name, signature)} of every visible control in every frame, for before/after comparison, plus the main document's identity."""
        controls: dict = {}
        tables: dict = {}
        ident = None
        for index, frame, snap in await self.snapshot_frames(page, {"light": True, "max": 400}):
            if index == 0:
                ident = (snap.get("frame_tag"), snap.get("url"))
            for c in snap.get("controls", []):
                sig = {k: c[k] for k in ("value", "checked", "disabled", "expanded", "selected", "invalid") if k in c and c[k] not in (None, "")}
                controls[c["id"]] = (c.get("role"), c.get("name"), sig)
            for t in snap.get("tables", []) or []:
                tables[t["id"]] = t
        return {"controls": controls, "tables": tables, "ident": ident}

    async def mutation_marks(self, page: Any) -> dict:
        marks = {}
        for _i, frame, snap in await self.snapshot_frames(page, {"light": True, "max": 1}):
            try:
                marks[snap["frame_tag"]] = await frame.evaluate("() => window.__ps.mutSeq")
            except Exception:
                pass
        return marks

    async def mutations_since(self, page: Any, marks: dict) -> list:
        found: list = []
        for frame in self._frames(page):
            for tag, mark in marks.items():
                if self._frame_by_tag.get(tag) is frame:
                    try:
                        found += await frame.evaluate("m => window.__ps.mutations(m)", mark)
                    except Exception:
                        pass
        return found

    @staticmethod
    def brief_appearances(muts: list) -> list:
        """Text that appeared (and, if it did, went again) during an action: toasts, banners, spinners."""
        added: dict = {}
        removed: dict = {}
        for m in muts:
            text = m.get("text") or ""
            if not text:
                continue
            (added if m["k"] == "added" else removed).setdefault(text, m)
        out = []
        for text, m in added.items():
            gone = removed.get(text)
            what = m.get("cls") or m.get("tag") or ""
            row = {"text": text[:140], "what": what}
            if gone is not None and gone["t"] >= m["t"]:
                row["gone_after_ms"] = gone["t"] - m["t"]
            out.append(row)
        return out[:8]
