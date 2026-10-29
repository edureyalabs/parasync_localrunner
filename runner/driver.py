# sandbox/microvm/driver.py
"""
`look` and `act`: the two ways the agent uses its senses (senses.py) without writing a script.

  look  describes the page now: address, tabs, dialogs and alerts, forms, controls (each with a handle, its real name, value, state, and whether something
        covers it), tables, the visible text, plus what the network and console did since the last report.
  act   does ONE thing (click, fill, choose, press, scroll, go, wait, switch tab, upload, fill several fields) and then waits for the page to settle and
        reports what it caused: controls that appeared, disappeared or changed, text that showed briefly (a toast), the requests it fired, errors.

Both return a `report` (compact text meant for the model) plus small structured parts. Generic: nothing here knows a site. Python 3.9 compatible.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Optional

import scrub
from senses import Senses, _host

HANDLE_RE = re.compile(r"^[a-z0-9]{3}\.\d+$")
DEFAULT_ACTION_TIMEOUT_MS = 6000
MAX_REPORT_CONTROLS = 60
VERBS = ("click", "dblclick", "hover", "fill", "type", "select", "check", "uncheck", "press", "scroll", "go", "back", "forward", "reload", "wait",
         "tab", "close_tab", "upload", "fill_many")


class ActionError(Exception):
    """A problem the agent can act on; the message says what to do."""


# ── rendering ────────────────────────────────────────────────────────────────

def _short(text: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def _url_short(url: str, n: int = 90) -> str:
    return _short(url, n)


def render_control(c: dict, frame_no: int = 0) -> str:
    bits = ["[%s] %s" % (c["id"], c.get("role", "?"))]
    if c.get("type") and c["type"] not in ("text", "button", "submit"):
        bits[0] += ":" + c["type"]
    if c.get("name"):
        bits.append('"%s"' % _short(c["name"], 70))
    if c.get("field"):
        bits.append("name=%s" % c["field"])
    if "value" in c and c["value"] != "" and c.get("role") not in ("button", "link"):
        bits.append('value="%s"' % _short(c["value"], 60))
    if c.get("placeholder") and not c.get("name"):
        bits.append('placeholder="%s"' % _short(c["placeholder"], 40))
    if c.get("options"):
        opts = c["options"]
        bits.append("options: " + " | ".join(_short(o, 28) for o in opts[:14]) + (" | … (%d)" % len(opts) if len(opts) > 14 else ""))
    if c.get("href"):
        bits.append("→ " + _short(c["href"], 70))
    for flag in ("disabled", "readonly", "required", "focused"):
        if c.get(flag):
            bits.append(flag)
    for flag in ("checked", "expanded", "selected", "pressed"):
        if flag in c:
            bits.append(flag if c[flag] else "not-" + flag)
    if c.get("invalid"):
        bits.append("invalid: " + _short(c["invalid"], 50))
    if c.get("blocked_by"):
        bits.append("BLOCKED BY " + _short(c["blocked_by"], 50))
    if c.get("vis") == "off":
        bits.append("offscreen")
    if frame_no:
        bits.append("in frame %d" % frame_no)
    if c.get("shadow"):
        bits.append("shadow")
    if c.get("sel") and len(c["sel"]) <= 60:
        bits.append("sel=" + c["sel"])
    return " ".join(bits)


def render_events(events: dict) -> list:
    lines = []
    for e in events.get("events", []):
        kind = e.get("kind")
        if kind == "dialog":
            lines.append('DIALOG %s "%s" → %s' % (e.get("type"), _short(e.get("message"), 120), e.get("handled")))
        elif kind == "tab_opened":
            lines.append("NEW TAB %s opened: %s" % (e.get("tab"), _url_short(e.get("url", ""))))
        elif kind == "tab_closed":
            lines.append("TAB CLOSED: %s" % _url_short(e.get("url", "")))
        elif kind == "navigated":
            lines.append("NAVIGATED tab %s → %s" % (e.get("tab"), _url_short(e.get("url", ""))))
        elif kind == "download":
            lines.append("DOWNLOAD %s (%s bytes)%s" % (e.get("file"), e.get("bytes"), " ERROR: " + e["error"] if e.get("error") else ""))
        elif kind == "blocked":
            lines.append("BLOCKED navigation to a site this login does not allow: %s" % _url_short(e.get("url", "")))
        elif kind in ("ws_open", "ws_close"):
            lines.append("WEBSOCKET %s %s" % ("opened" if kind == "ws_open" else "closed", _url_short(e.get("url", ""))))
    reqs = events.get("requests") or []
    if reqs:
        lines.append("REQUESTS (%d%s):" % (len(reqs), "+%d more" % events["requests_more"] if events.get("requests_more") else ""))
        for r in reqs:
            row = "  %s %s" % (r.get("m"), _short(r.get("u"), 110))
            if r.get("n", 1) > 1:
                row += " ×%d" % r["n"]
            if r.get("post"):
                row += " sent " + _short(r["post"] if isinstance(r["post"], str) else str(r["post"]), 160)
            if r.get("failed"):
                row += " → FAILED " + r["failed"]
            else:
                if r.get("s") is not None:
                    row += " → %s" % r["s"]
                if r.get("to"):
                    row += " to " + _short(r["to"], 60)
                if r.get("reply"):
                    row += " " + _short(r["reply"], 130)
                elif r.get("peek"):
                    row += ' "%s"' % _short(r["peek"], 110)
                if r.get("ms") is not None:
                    row += " %dms" % r["ms"]
            lines.append(row)
    for c in events.get("console", []):
        lines.append("CONSOLE %s: %s%s" % (c.get("level"), _short(c.get("text"), 200), " ×%d" % c["n"] if c.get("n") else ""))
    return lines


def render_page_block(snaps: list, opts: dict) -> list:
    """The lines describing the page from its frames' snapshots (main frame first)."""
    lines: list = []
    main = snaps[0][2] if snaps else {}
    for d in main.get("dialogs", []) or []:
        lines.append('DIALOG-ON-PAGE [%s] "%s": %s' % (d["id"], _short(d.get("name"), 50), _short(d.get("text"), 200)))
    for a in (main.get("alerts") or [])[:5]:
        lines.append('ALERT/NOTICE: "%s"' % _short(a, 160))
    if main.get("busy_ui"):
        lines.append("A loading indicator is visible on the page.")
    if main.get("headings"):
        lines.append("HEADINGS: " + " | ".join(_short(h, 60) for h in main["headings"][:6]))
    for f in main.get("forms", []) or []:
        lines.append("FORM %s %s %s fields: %s" % (f.get("id") or "(unnamed)", f.get("method"), f.get("action") or "", ", ".join(f.get("fields", [])[:16])))
    shown = 0
    cap = opts.get("max", MAX_REPORT_CONTROLS)
    control_lines: list = []
    total = 0
    for index, _frame, snap in snaps:
        total += snap.get("total_controls", 0)
        label = "" if index == 0 else (" (frame %d: %s)" % (index, _short(snap.get("url", ""), 60)))
        for c in snap.get("controls", []):
            if shown >= cap:
                break
            control_lines.append(render_control(c, index))
            shown += 1
        if label and snap.get("controls"):
            control_lines.append("— frame %d is %s" % (index, _short(snap.get("url", ""), 80)))
    hidden = sum(s[2].get("hidden_controls", 0) for s in snaps)
    more = max(total - shown, 0)
    head = "CONTROLS (%d shown of %d on the page%s)" % (shown, total, ", %d hidden ones left out" % hidden if hidden else "")
    if more:
        head += "; ask with query=\"...\", scope=<handle>, only=inputs|buttons|links, or max=%d to see the rest" % min(total, 300)
    lines.append(head + ":" if control_lines else "No visible controls.")
    lines.extend(control_lines)
    for t in main.get("tables", []) or []:
        lines.append('TABLE [%s] %d rows, columns: %s' % (t["id"], t.get("rows", 0), " | ".join(_short(h, 24) for h in t.get("headers", []))))
        for row in t.get("sample", [])[:2]:
            lines.append("   e.g. " + " | ".join(_short(c, 24) for c in row))
    text = main.get("text") or ""
    if text:
        lines.append("TEXT (%d of %d characters): %s" % (len(text), main.get("text_chars", len(text)), _short(text, opts.get("text", 1500))))
    return lines


# ── the driver ───────────────────────────────────────────────────────────────

class Driver:
    def __init__(self, senses: Senses) -> None:
        self.s = senses
        self.last_report_seq = 0

    # -- helpers -----------------------------------------------------------

    def _page_block(self, page: Any, title: str) -> dict:
        return {"url": scrub.url(page.url)[:200], "title": scrub.text(title)[:100], "tab": self.s.tab_index(page), "tabs": len(self.s.pages())}

    async def _title(self, page: Any) -> str:
        try:
            return await asyncio.wait_for(page.title(), 1.0)
        except Exception:
            return ""

    async def _resolve(self, page: Any, target: Any) -> tuple:
        """(frame, element handle, description) for a handle string, a {name, role, index} object, or a plain name."""
        if target is None or target == "":
            raise ActionError("this action needs a target: a handle from look (like k3x.12) or {\"name\": \"Search\", \"role\": \"button\"}")
        if isinstance(target, str) and HANDLE_RE.match(target.strip()):
            handle = target.strip()
            tag = handle.split(".")[0]
            frames = [self.s._frame_by_tag.get(tag)] if self.s._frame_by_tag.get(tag) is not None else []
            frames += [f for f in self.s._frames(page) if f not in frames]
            for frame in frames:
                if frame is None or frame.is_detached():
                    continue
                try:
                    jsh = await frame.evaluate_handle("id => window.__ps && window.__ps.byId(id)", handle)
                    el = jsh.as_element()
                except Exception:
                    continue
                if el is not None:
                    try:
                        brief = await frame.evaluate("id => window.__ps.brief(id)", handle) or {}
                    except Exception:
                        brief = {}
                    return frame, el, "[%s] %s \"%s\"" % (handle, brief.get("role", "element"), _short(brief.get("name"), 50)), brief
            raise ActionError("handle %s is not on the page any more (the page changed or navigated). Run look to get fresh handles." % handle)
        spec = {"name": target} if isinstance(target, str) else dict(target)
        index = spec.pop("index", None)
        found: list = []
        for fi, frame in enumerate(self.s._frames(page)):
            try:
                for c in await frame.evaluate("s => window.__ps.find(s)", spec):
                    found.append((fi, frame, c))
            except Exception:
                continue
        if not found:
            raise ActionError("no control named %s on the page. Run look (query=\"...\") to see what is there." % _short(spec.get("name") or spec, 60))
        found.sort(key=lambda x: (-x[2]["s"], x[2]["vis"] != "view", x[0]))
        best = [f for f in found if f[2]["s"] == found[0][2]["s"]]
        if len(best) > 1 and index is None:
            names = "; ".join('[%s] %s "%s"%s' % (c["id"], c["role"], _short(c["name"], 40), "" if c["vis"] == "view" else " offscreen") for _fi, _f, c in best[:6])
            raise ActionError("%d controls match %s: %s. Use the handle of the one you mean, or add \"index\": 0 to take the first." % (len(best), _short(spec.get("name"), 40), names))
        fi, frame, c = best[int(index or 0)] if index is not None and int(index) < len(best) else best[0]
        jsh = await frame.evaluate_handle("id => window.__ps.byId(id)", c["id"])
        el = jsh.as_element()
        if el is None:
            raise ActionError("the control disappeared while it was being found; try again")
        try:
            brief = await frame.evaluate("id => window.__ps.brief(id)", c["id"]) or {}
        except Exception:
            brief = {"role": c["role"], "name": c["name"]}
        return frame, el, "[%s] %s \"%s\"" % (c["id"], c["role"], _short(c["name"], 50)), brief

    async def _why(self, frame: Any, handle: str) -> dict:
        try:
            return await frame.evaluate("id => window.__ps.why(id)", handle) or {}
        except Exception:
            return {}

    # -- look --------------------------------------------------------------

    async def look(self, req: dict) -> dict:
        await self.s.ensure()
        await self.s.set_allowed(req.get("allowed_hosts"))
        async with self.s.op_lock:  # type: ignore[union-attr]
            page = self.s.page_for(req.get("tab"))
            opts = {k: req[k] for k in ("query", "scope", "only", "max", "text") if req.get(k) not in (None, "")}
            opts["max"] = int(opts.get("max") or MAX_REPORT_CONTROLS)
            opts["text"] = int(opts.get("text") or 1500)
            if req.get("settle", True):
                settled = await self.s.settle(page, int(req.get("settle_ms") or 3000))
            else:
                settled = {"settled": None}
            snaps = await self.s.snapshot_frames(page, opts)
            title = snaps[0][2].get("title") if snaps else await self._title(page)
            tabs = await self.s.tabs()
            events = self.s.summarize(self.last_report_seq, _host(page.url), limit=10)
            self.last_report_seq = self.s.mark()
            lines = ["PAGE %s \"%s\" · tab %d of %d%s" % (scrub.url(page.url)[:140], _short(scrub.text(title or ""), 70), self.s.tab_index(page), len(tabs),
                                                          "" if settled.get("settled") in (True, None) else " · STILL LOADING")]
            if snaps:
                sc = snaps[0][2].get("scroll") or {}
                if sc:
                    lines[0] += " · scrolled %d of %d" % (sc.get("y", 0), max(sc.get("height", 0) - sc.get("view", 0), 0))
            if len(tabs) > 1:
                lines.append("TABS: " + " | ".join('%d%s %s' % (t["tab"], "*" if t["active"] else "", _short(t["title"] or t["url"], 40)) for t in tabs))
            if settled.get("settled") is False and settled.get("still_loading"):
                lines.append("Still loading: " + ", ".join(settled["still_loading"]))
            lines += render_page_block(snaps, opts)
            lines += render_events(events)
            out: dict = {"ok": True, "report": "\n".join(lines), "page": self._page_block(page, title or ""), "settled": settled}
            if req.get("screenshot"):
                out["screenshot"] = await self._screenshot(page)
            return out

    async def _screenshot(self, page: Any) -> Optional[str]:
        try:
            folder = os.path.join(self.s.workdir, "screens")
            os.makedirs(folder, exist_ok=True)
            name = "look_%d.png" % self.s.mark()
            await page.screenshot(path=os.path.join(folder, name), timeout=8000)
            return "screens/" + name
        except Exception:
            return None

    # -- act ---------------------------------------------------------------

    async def act(self, req: dict) -> dict:
        action = str(req.get("action") or "").strip().lower()
        if action not in VERBS:
            return {"ok": False, "error": "unknown action '%s'. Actions: %s" % (action, ", ".join(VERBS))}
        await self.s.ensure()
        await self.s.set_allowed(req.get("allowed_hosts"))
        async with self.s.op_lock:  # type: ignore[union-attr]
            try:
                return await self._act(action, req)
            except ActionError as exc:
                return {"ok": False, "action": action, "error": str(exc)}
            except Exception as exc:
                return {"ok": False, "action": action, "error": "%s: %s" % (type(exc).__name__, scrub.text(str(exc))[:400])}

    async def _act(self, action: str, req: dict) -> dict:
        s = self.s
        page = s.page_for(req.get("tab"))
        timeout = int(req.get("timeout_ms") or DEFAULT_ACTION_TIMEOUT_MS)
        s.dialog_mode = req.get("dialogs") if req.get("dialogs") in ("accept", "dismiss") else None
        s.prompt_text = req.get("prompt_text")
        before = await s.light(page)
        marks = await s.mutation_marks(page)
        start_seq = s.mark()
        did = ""
        detail: dict = {}
        try:
            did, detail = await self._perform(action, req, page, timeout)
        finally:
            s.dialog_mode = None
            s.prompt_text = None
        await asyncio.sleep(0.05)
        page = s.page_for(None) if action in ("tab", "close_tab") or s.active is not page else page
        settled = await s.settle(page, int(req.get("settle_ms") or 8000)) if req.get("settle", True) else {"settled": None}
        after = await s.light(page)
        muts = await s.mutations_since(page, marks)
        events = s.summarize(start_seq, _host(page.url), limit=12)
        s.last_report_seq = s.mark()
        title = await self._title(page)
        navigated = (before.get("ident") != after.get("ident")) or any(e.get("kind") == "navigated" for e in events.get("events", []))
        lines = ["DID " + did]
        if detail.get("note"):
            lines.append(detail["note"])
        lines.append("PAGE %s \"%s\" · tab %d%s" % (scrub.url(page.url)[:140], _short(scrub.text(title), 70), s.tab_index(page),
                                                     " · settled in %.1fs" % (settled["waited_ms"] / 1000.0) if settled.get("settled") else " · NOT SETTLED (still loading or changing)" if settled.get("settled") is False else ""))
        if settled.get("settled") is False and settled.get("still_loading"):
            lines.append("Still loading: " + ", ".join(settled["still_loading"]))
        brief = req.get("look", "changes")
        if navigated or brief == "full":
            snaps = await s.snapshot_frames(page, {"max": 40 if navigated else MAX_REPORT_CONTROLS, "text": 500 if navigated else 1200})
            lines.append("NEW PAGE:" if navigated else "NOW:")
            lines += render_page_block(snaps, {"max": 40 if navigated else MAX_REPORT_CONTROLS, "text": 500 if navigated else 1200})
        elif brief != "none":
            lines += self._changes(before, after, muts)
        lines += render_events(events)
        out = {"ok": True, "action": action, "report": "\n".join(lines), "page": self._page_block(page, title), "settled": settled, "navigated": navigated}
        out.update({k: v for k, v in detail.items() if k in ("fields", "value")})
        return out

    def _changes(self, before: dict, after: dict, muts: list) -> list:
        b, a = before.get("controls", {}), after.get("controls", {})
        added = [(h, a[h]) for h in a if h not in b]
        removed = [(h, b[h]) for h in b if h not in a]
        changed = [(h, b[h], a[h]) for h in a if h in b and b[h][2] != a[h][2]]
        bt, at = before.get("tables", {}), after.get("tables", {})
        new_tables = [t for h, t in at.items() if h not in bt]
        grown = [(bt[h], t) for h, t in at.items() if h in bt and bt[h].get("rows") != t.get("rows")]
        lines: list = []
        shown_any = False
        if added:
            shown_any = True
            lines.append("APPEARED (%d):" % len(added))
            lines += ['  + [%s] %s "%s"' % (h, v[0], _short(v[1], 60)) for h, v in added[:25]]
            if len(added) > 25:
                lines.append("  + … %d more" % (len(added) - 25))
        if removed:
            shown_any = True
            lines.append("GONE (%d): " % len(removed) + "; ".join('%s "%s"' % (v[0], _short(v[1], 30)) for _h, v in removed[:10]) + (" …" if len(removed) > 10 else ""))
        if changed:
            shown_any = True
            lines.append("CHANGED (%d):" % len(changed))
            for h, old, new in changed[:15]:
                diffs = ["%s %s → %s" % (k, _short(old[2].get(k, "(none)"), 30), _short(new[2].get(k, "(none)"), 30)) for k in sorted(set(old[2]) | set(new[2])) if old[2].get(k) != new[2].get(k)]
                lines.append('  ~ [%s] %s "%s": %s' % (h, new[0], _short(new[1], 40), "; ".join(diffs)))
        if new_tables or grown:
            shown_any = True
            for t in new_tables[:4]:
                lines.append('TABLE APPEARED [%s] %d rows, columns: %s' % (t["id"], t.get("rows", 0), " | ".join(_short(c, 22) for c in t.get("headers", []))))
            for old, new in grown[:4]:
                lines.append('TABLE [%s] rows %s → %s' % (new["id"], old.get("rows"), new.get("rows")))
        brief = Senses.brief_appearances(muts)
        if brief:
            shown_any = True
            lines.append("SHOWN ON PAGE DURING THE ACTION:")
            for m in brief:
                lines.append('  "%s"%s%s' % (_short(m["text"], 120), " (%s)" % m["what"] if m.get("what") else "", " gone after %dms" % m["gone_after_ms"] if m.get("gone_after_ms") is not None else ""))
        if not shown_any:
            lines.append("NOTHING VISIBLE CHANGED on the page.")
        return lines

    async def _perform(self, action: str, req: dict, page: Any, timeout: int) -> tuple:
        s = self.s
        if action == "go":
            url = str(req.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                raise ActionError("go needs a full http(s) address in `url`")
            if not s.host_allowed(url):
                raise ActionError("this login only allows its own sites; %s is not one of them" % _host(url))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                if "ERR_ABORTED" in str(exc) or "interrupted by another navigation" in str(exc):
                    return "go %s (the navigation was cut short: a redirect or a download)" % scrub.url(url)[:100], {}
                raise
            return "go " + scrub.url(url)[:120], {}
        if action in ("back", "forward", "reload"):
            fn = {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action]
            await fn(wait_until="domcontentloaded", timeout=30000)
            return action, {}
        if action == "tab":
            index = req.get("index", req.get("tab_index"))
            if index is None:
                raise ActionError("tab needs `index`: the tab number from the TABS line of look")
            target = s.page_for(index)
            s.active = target
            await target.bring_to_front()
            return "switch to tab %s" % index, {}
        if action == "close_tab":
            target = s.page_for(req.get("index"))
            await target.close()
            return "close tab", {}
        if action == "wait":
            return await self._wait(req, page)
        if action == "scroll":
            return await self._scroll(req, page, timeout)
        if action == "press" and not req.get("target"):
            key = str(req.get("key") or "").strip()
            if not key:
                raise ActionError("press needs `key`, for example Enter or Control+A")
            await page.keyboard.press(key)
            return "press " + key, {}
        if action == "fill_many":
            return await self._fill_many(req, page, timeout)
        frame, el, desc, brief = await self._resolve(page, req.get("target"))
        handle = desc[1:desc.index("]")]
        try:
            if action in ("click", "dblclick"):
                await el.scroll_into_view_if_needed(timeout=timeout)
                await el.click(timeout=timeout, force=bool(req.get("force")), click_count=2 if action == "dblclick" else 1, button=req.get("button") or "left")
            elif action == "hover":
                await el.hover(timeout=timeout)
            elif action == "fill":
                await el.fill("" if req.get("value") is None else str(req.get("value")), timeout=timeout)
            elif action == "type":
                await el.focus()
                if req.get("clear"):
                    await el.fill("", timeout=timeout)
                await el.press_sequentially(str(req.get("text") if req.get("text") is not None else req.get("value") or ""), delay=15)
            elif action == "select":
                await self._select(el, req.get("value"), timeout)
            elif action in ("check", "uncheck"):
                await el.set_checked(action == "check", timeout=timeout, force=bool(req.get("force")))
            elif action == "press":
                await el.press(str(req.get("key") or "Enter"), timeout=timeout)
            elif action == "upload":
                await self._upload(el, req)
        except ActionError:
            raise
        except Exception as exc:
            why = await self._why(frame, handle)
            hint = ""
            if why.get("blocked_by"):
                hint = " It is covered by %s: close or dismiss that first." % why["blocked_by"]
            elif why.get("disabled"):
                hint = " It is disabled."
            elif why.get("visible") is False:
                hint = " It is not visible."
            raise ActionError("%s on %s failed: %s.%s" % (action, desc, _short(str(exc).split("\n")[0], 220), hint))
        extra = _short(req.get("value") if req.get("value") is not None else req.get("text") or req.get("key") or "", 40)
        masked = "[hidden]" if scrub.is_secret_name(brief.get("name")) or (brief.get("type") == "password") else extra
        return "%s %s%s" % (action, desc, (" with " + repr(masked)) if extra else ""), {}

    async def _select(self, el: Any, value: Any, timeout: int) -> None:
        values = value if isinstance(value, list) else [value]
        values = ["" if v is None else str(v) for v in values]
        try:
            await el.select_option(label=values, timeout=timeout)
        except Exception:
            try:
                await el.select_option(value=values, timeout=timeout)
            except Exception as exc:
                options = await el.evaluate("e => Array.from(e.options || []).slice(0, 30).map(o => o.text.trim())")
                raise ActionError("no option %s in this list. Options: %s" % (values, " | ".join(map(str, options)))) from exc

    async def _upload(self, el: Any, req: dict) -> None:
        paths = req.get("value") if isinstance(req.get("value"), list) else [req.get("value")]
        real = []
        for p in paths:
            full = os.path.normpath(os.path.join(self.s.workdir, str(p or "")))
            if not full.startswith(os.path.normpath(self.s.workdir)) or not os.path.isfile(full):
                raise ActionError("upload file '%s' is not in the browser's working folder; list it in `files` of this call" % p)
            real.append(full)
        await el.set_input_files(real)

    async def _fill_many(self, req: dict, page: Any, timeout: int) -> tuple:
        fields = req.get("fields")
        if not isinstance(fields, list) or not fields:
            raise ActionError("fill_many needs `fields`: a list of {\"target\": ..., \"value\": ...}")
        done = []
        for i, f in enumerate(fields[:30]):
            try:
                frame, el, desc, brief = await self._resolve(page, f.get("target"))
                role = brief.get("role")
                value = f.get("value")
                if role == "select" or brief.get("tag") == "select":
                    await self._select(el, value, timeout)
                elif role in ("checkbox", "radio") or brief.get("type") in ("checkbox", "radio"):
                    await el.set_checked(bool(value) and str(value).lower() not in ("false", "0", "no", "off"), timeout=timeout)
                else:
                    await el.fill("" if value is None else str(value), timeout=timeout)
                shown = "[hidden]" if scrub.is_secret_name(brief.get("name")) or brief.get("type") == "password" else _short(value, 30)
                done.append("%s = %s" % (desc, shown))
            except ActionError as exc:
                raise ActionError("field %d of %d (%s) failed: %s. Fields set before it: %s" % (i + 1, len(fields), _short(f.get("target"), 40), exc, "; ".join(done) or "none"))
            except Exception as exc:
                raise ActionError("field %d of %d (%s) failed: %s. Fields set before it: %s" % (i + 1, len(fields), _short(f.get("target"), 40), _short(str(exc).split("\n")[0], 160), "; ".join(done) or "none"))
        submit = req.get("submit")
        note = None
        if submit:
            if str(submit).lower() == "enter":
                await page.keyboard.press("Enter")
                note = "then pressed Enter"
            else:
                _f, sub, sdesc, _b = await self._resolve(page, submit)
                await sub.click(timeout=timeout)
                note = "then clicked " + sdesc
        return "fill %d fields" % len(done), {"fields": done, "note": "Set: " + "; ".join(done) + ("; " + note if note else "")}

    async def _scroll(self, req: dict, page: Any, timeout: int) -> tuple:
        if req.get("target"):
            _frame, el, desc, _b = await self._resolve(page, req.get("target"))
            await el.scroll_into_view_if_needed(timeout=timeout)
            return "scroll to " + desc, {}
        direction = str(req.get("direction") or "down").lower()
        amount = int(req.get("amount") or 600)
        if direction == "top":
            await page.evaluate("() => window.scrollTo(0, 0)")
        elif direction == "bottom":
            await page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
        else:
            await page.evaluate("d => window.scrollBy(0, d)", -amount if direction == "up" else amount)
        return "scroll " + direction, {}

    async def _wait(self, req: dict, page: Any) -> tuple:
        limit = min(int(req.get("timeout_ms") or 10000), 30000)
        if req.get("text"):
            needle = str(req["text"])
            try:
                await page.wait_for_function("t => (document.body.innerText || '').toLowerCase().includes(t.toLowerCase())", arg=needle, timeout=limit)
            except Exception:
                raise ActionError("the text %r did not appear within %.0fs" % (_short(needle, 40), limit / 1000.0))
            return "wait for text %r" % _short(needle, 40), {}
        if req.get("gone_text"):
            needle = str(req["gone_text"])
            try:
                await page.wait_for_function("t => !(document.body.innerText || '').toLowerCase().includes(t.toLowerCase())", arg=needle, timeout=limit)
            except Exception:
                raise ActionError("the text %r was still on the page after %.0fs" % (_short(needle, 40), limit / 1000.0))
            return "wait until %r is gone" % _short(needle, 40), {}
        if req.get("url_contains"):
            part = str(req["url_contains"])
            try:
                await page.wait_for_url(lambda u: part in u, timeout=limit)
            except Exception:
                raise ActionError("the address did not contain %r within %.0fs (it is %s)" % (part, limit / 1000.0, scrub.url(page.url)[:100]))
            return "wait for address containing %r" % part, {}
        await asyncio.sleep(min(int(req.get("ms") or 1000), 15000) / 1000.0)
        return "wait %dms" % min(int(req.get("ms") or 1000), 15000), {}
