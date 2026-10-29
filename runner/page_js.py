# sandbox/microvm/page_js.py
"""
The script injected into every page and frame (senses.py installs it as a context init script and also evaluates it once in pages that were already
loaded). It defines `window.__ps` (idempotent), which:

  * gives every interesting element a short handle (`k3x.12`) that stays valid until that element leaves the page (a new document starts new handles);
  * describes the page's structure in one call (`snapshot`): controls with their real name, state and value, whether something covers them, forms,
    tables, dialogs, alerts, the visible text. Open shadow roots are walked; frames are described by the caller, one `snapshot` per frame;
  * finds controls by what they are called (`find`), for actions that name a control instead of using a handle;
  * watches the DOM (`mutations`): nodes that appear or disappear, with their text, so a toast that shows and goes between two calls is still seen;
  * reports whether the page is still changing (`quiet`).

Nothing here writes to the page except the handle bookkeeping (kept in JS memory, no DOM attributes). Password values are never read; values of fields
with secret-looking names are hidden. Plain JavaScript, no dependencies.
"""

VERSION = 2

PS_JS = r'''
(() => {
  if (window.__ps && window.__ps.v === 2) return true;
  const ps = { v: 2, tag: Math.random().toString(36).slice(2, 5), n: 0, refs: new Map(), ids: new WeakMap(), mut: [], mutTotal: 0, lastMut: Date.now(), t0: Date.now() };
  const SECRET = /(pass(word|wd)?|pwd|secret|token|auth|cookie|session|csrf|xsrf|otp|passcode|\bpin\b|cvv|cvc|card|ssn|api[-_]?key|signature|credential)/i;
  const clip = (s, n) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n) + '…' : s; };
  const textOf = (el, n) => clip(el.innerText || el.textContent || '', n || 80);

  ps.idOf = (el) => {
    let id = ps.ids.get(el);
    if (!id) { id = ps.tag + '.' + (++ps.n); ps.ids.set(el, id); ps.refs.set(id, new WeakRef(el)); }
    return id;
  };
  ps.byId = (id) => {
    const r = ps.refs.get(id); const el = r && r.deref();
    return el && el.isConnected ? el : null;
  };
  ps.has = (id) => !!ps.byId(id);

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity !== 0;
  };
  const inView = (r) => r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;

  const nearLabel = (el) => {
    const cell = el.closest && el.closest('td,th');
    if (cell && cell.previousElementSibling) { const t = textOf(cell.previousElementSibling, 60); if (t) return t; }
    let p = el.previousSibling;
    while (p) {
      if (p.nodeType === 3 && p.textContent.trim()) return clip(p.textContent, 60);
      if (p.nodeType === 1) { if (!p.matches('input,select,textarea,button,br')) { const t = textOf(p, 60); if (t) return t; } break; }
      p = p.previousSibling;
    }
    return '';
  };
  const nameOf = (el) => {
    let n = el.getAttribute('aria-label'); if (n && n.trim()) return clip(n, 80);
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => { const e = document.getElementById(i); return e ? textOf(e, 60) : ''; }).join(' ').trim(); if (t) return clip(t, 80); }
    if (el.labels && el.labels.length) { const t = Array.from(el.labels).map(l => textOf(l, 60)).join(' ').trim(); if (t) return clip(t, 80); }
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && ['submit', 'button', 'reset', 'image'].includes(el.type)) return clip(el.value || el.alt || el.type, 80);
    if (tag === 'img') return clip(el.alt || '', 80);
    const isField = tag === 'input' || tag === 'select' || tag === 'textarea';
    if (!isField) { const own = textOf(el, 80); if (own) return own; }
    const attr = el.title || el.placeholder || el.getAttribute('alt') || '';
    if (attr) return clip(attr, 80);
    if (isField) { const near = nearLabel(el); if (near) return near; }
    if (!isField) { const own = textOf(el, 80); if (own) return own; }
    return clip(el.name || el.id || '', 80);
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role'); if (explicit) return explicit.split(' ')[0];
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox' || t === 'radio' || t === 'file' || t === 'range') return t;
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return 'clickable';
  };

  const NATIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[contenteditable=""],[contenteditable="true"],[role=button],[role=link],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=checkbox],[role=radio],[role=switch],[role=option],[role=combobox],[role=textbox],[role=searchbox],[role=slider],[role=treeitem],[onclick],[tabindex]:not([tabindex="-1"])';
  const collect = (root, out, shadow, budget) => {
    const all = root.querySelectorAll('*');
    for (let i = 0; i < all.length && budget.n < 30000; i++) {
      const el = all[i]; budget.n++;
      out.push({ el, shadow });
      if (el.shadowRoot) collect(el.shadowRoot, out, true, budget);
    }
  };
  const uniqueSel = (el) => {
    try {
      const tag = el.tagName.toLowerCase();
      const tries = [];
      if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) tries.push('#' + el.id);
      if (el.getAttribute('name')) tries.push(tag + '[name="' + el.getAttribute('name').replace(/"/g, '\\"') + '"]');
      if (el.getAttribute('data-testid')) tries.push('[data-testid="' + el.getAttribute('data-testid') + '"]');
      for (const s of tries) { if (document.querySelectorAll(s).length === 1) return s; }
      const parts = []; let cur = el; let depth = 0;
      while (cur && cur.nodeType === 1 && cur !== document.documentElement && depth < 6) {
        if (cur.id && /^[A-Za-z][\w-]*$/.test(cur.id) && document.querySelectorAll('#' + cur.id).length === 1) { parts.unshift('#' + cur.id); break; }
        const sibs = cur.parentElement ? Array.from(cur.parentElement.children).filter(c => c.tagName === cur.tagName) : [cur];
        parts.unshift(cur.tagName.toLowerCase() + (sibs.length > 1 ? ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')' : ''));
        cur = cur.parentElement; depth++;
      }
      return parts.join(' > ');
    } catch (e) { return null; }
  };
  const blockedBy = (el, r) => {
    if (!inView(r)) return null;
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
    const root = el.getRootNode();
    const top = (root && root.elementFromPoint ? root : document).elementFromPoint(x, y);
    if (!top || top === el || el.contains(top) || top.contains(el)) return null;
    return top.tagName.toLowerCase() + (top.id ? '#' + top.id : '') + (textOf(top, 24) ? ' "' + textOf(top, 24) + '"' : '');
  };
  const describe = (item, o, i) => {
    const el = item.el; const r = el.getBoundingClientRect(); const tag = el.tagName.toLowerCase();
    const role = roleOf(el); const t = (el.type || '').toLowerCase();
    const rec = { id: ps.idOf(el), role, name: nameOf(el), vis: inView(r) ? 'view' : 'off' };
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (t && tag === 'input') rec.type = t;
      const nm = el.getAttribute('name'); if (nm) rec.field = nm;
      if (t === 'password') rec.value = ''; else if (SECRET.test(nm || el.id || '')) rec.value = el.value ? '[hidden]' : '';
      else if (tag === 'select') { const sel = el.options && el.options[el.selectedIndex]; rec.value = sel ? clip(sel.text, 60) : ''; }
      else if (t !== 'file') rec.value = clip(el.value, 80);
      if (tag === 'select') rec.options = Array.from(el.options).slice(0, 40).map(op => clip(op.text, 40));
      if (el.placeholder) rec.placeholder = clip(el.placeholder, 40);
    }
    if (el.href && tag === 'a') rec.href = el.getAttribute('href') && el.getAttribute('href').startsWith('javascript') ? 'javascript:' : clip(el.href, 140);
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') rec.disabled = true;
    if (el.readOnly) rec.readonly = true;
    if (el.required) rec.required = true;
    if (t === 'checkbox' || t === 'radio' || el.getAttribute('aria-checked')) rec.checked = el.checked === true || el.getAttribute('aria-checked') === 'true';
    ['expanded', 'selected', 'pressed'].forEach(a => { const v = el.getAttribute('aria-' + a); if (v) rec[a] = v === 'true'; });
    if (el.willValidate && el.validity && !el.validity.valid && el.value) rec.invalid = clip(el.validationMessage, 60);
    if (document.activeElement === el) rec.focused = true;
    if (item.shadow) rec.shadow = true;
    if (!o.light) {
      const b = blockedBy(el, r); if (b) rec.blocked_by = b;
      if (!item.shadow) { const s = uniqueSel(el); if (s) rec.sel = s; }
      if (el.form) rec.form = el.form.id || el.form.getAttribute('name') || 'form';
    }
    return rec;
  };
  const interactive = (list, o) => {
    const out = [];
    const seen = new Set();
    const scope = o.scope ? ps.byId(o.scope) : null;
    const pointerOk = list.length < 9000;
    for (const item of list) {
      const el = item.el;
      if (scope && !scope.contains(el)) continue;
      let ok = el.matches(NATIVE);
      if (!ok && pointerOk && el.children.length === 0 && textOf(el, 4)) {
        const cs = getComputedStyle(el);
        const pcs = el.parentElement ? getComputedStyle(el.parentElement) : null;
        ok = cs.cursor === 'pointer' && !(pcs && pcs.cursor === 'pointer' && el.parentElement.matches(NATIVE));
        if (ok && el.closest('a[href],button,[role=button]')) ok = false;
      }
      if (!ok) continue;
      if (el.tagName === 'LABEL') continue;
      if (!visible(el)) { out.hidden = (out.hidden || 0) + 1; continue; }
      if (seen.has(el)) continue; seen.add(el);
      out.push(item);
    }
    return out;
  };

  ps.snapshot = (o) => {
    o = o || {};
    const list = []; collect(document, list, false, { n: 0 });
    const items = interactive(list, o);
    const total = items.length;
    let recs = items.map((it, i) => describe(it, o, i));
    const q = (o.query || '').toLowerCase();
    if (q) recs = recs.filter(r => (r.name + ' ' + (r.value || '') + ' ' + (r.href || '') + ' ' + (r.field || '') + ' ' + (r.placeholder || '')).toLowerCase().includes(q));
    if (o.only === 'inputs') recs = recs.filter(r => ['textbox', 'select', 'checkbox', 'radio', 'file', 'range', 'combobox', 'searchbox', 'slider'].includes(r.role));
    else if (o.only === 'buttons') recs = recs.filter(r => r.role === 'button' || r.role === 'clickable' || r.role === 'tab' || r.role.startsWith('menuitem'));
    else if (o.only === 'links') recs = recs.filter(r => r.role === 'link');
    const tablesOf = () => Array.from(document.querySelectorAll('table')).filter(t => !t.querySelector('table') && t.rows.length >= 2 && !t.querySelector('input,select,textarea,button') && visible(t)).slice(0, 8).map(t => {
      const rows = Array.from(t.rows); const cellsOf = (r) => Array.from(r.cells).slice(0, 10).map(c => clip(c.innerText, 36));
      let hdrIndex = rows.findIndex(r => r.querySelector('th')); if (hdrIndex < 0) hdrIndex = 0;
      return { id: ps.idOf(t), headers: cellsOf(rows[hdrIndex]), rows: rows.length - hdrIndex - 1, sample: rows.slice(hdrIndex + 1, hdrIndex + 3).map(cellsOf) };
    });
    const viewFirst = recs.filter(r => r.vis === 'view').concat(recs.filter(r => r.vis !== 'view'));
    const max = o.max || 120;
    const res = { frame_tag: ps.tag, url: location.href, title: document.title, ready: document.readyState, total_controls: total, hidden_controls: items.hidden || 0, controls: o.light ? recs : viewFirst.slice(0, max), more: Math.max(recs.length - max, 0) };
    if (o.light) { res.tables = tablesOf().map(t => ({ id: t.id, headers: t.headers, rows: t.rows })); return res; }
    res.tables = tablesOf();
    res.scroll = { y: Math.round(scrollY), height: document.documentElement.scrollHeight, view: innerHeight };
    if (o.only === 'text' || !o.only || o.only === 'tables' || o.only === 'forms') {
      res.headings = Array.from(document.querySelectorAll('h1,h2,h3')).filter(visible).slice(0, 8).map(h => textOf(h, 80)).filter(Boolean);
      res.dialogs = Array.from(document.querySelectorAll('[role=dialog],[role=alertdialog],dialog[open],[aria-modal=true]')).filter(visible).slice(0, 3).map(d => ({ id: ps.idOf(d), name: nameOf(d) || clip(d.getAttribute('aria-label'), 60), text: textOf(d, 220) }));
      res.alerts = Array.from(document.querySelectorAll('[role=alert],[role=status],[aria-live=assertive],.alert,.error,.errors,.toast,.notification,.notice,.warning,.success,.message,.msg,.flash')).filter(visible).slice(0, 6).map(a => textOf(a, 180)).filter(Boolean);
      res.forms = Array.from(document.forms).filter(f => visible(f) || Array.from(f.elements).some(e => e.type !== 'hidden' && visible(e))).slice(0, 8).map(f => {
        let action = ''; try { const u = new URL(f.action, location.href); action = u.pathname; } catch (e) {}
        return { id: f.id || f.getAttribute('name') || '', method: (f.method || 'get').toUpperCase(), action, fields: Array.from(f.elements).filter(e => e.name && e.type !== 'hidden' && e.type !== 'submit').slice(0, 24).map(e => e.name) };
      });
      const spinner = Array.from(document.querySelectorAll('[aria-busy=true],[class*=spinner],[class*=loading],[class*=loader],[class*=busy],[class*=progress]')).filter(visible);
      res.busy_ui = spinner.length > 0;
    }
    const body = (document.body && document.body.innerText) || '';
    res.text_chars = body.length;
    res.text = o.noText ? '' : clip(body, o.text || 1500);
    res.frames = Array.from(document.querySelectorAll('iframe,frame')).slice(0, 12).map(f => ({ name: f.name || f.id || '', src: clip(f.getAttribute('src') || '', 100), visible: visible(f) }));
    res.focused = document.activeElement && document.activeElement !== document.body ? ps.idOf(document.activeElement) : null;
    return res;
  };

  ps.find = (spec) => {
    const list = []; collect(document, list, false, { n: 0 });
    const items = interactive(list, {});
    const name = String(spec.name || '').toLowerCase().trim(); const role = spec.role ? String(spec.role).toLowerCase() : null;
    const scored = [];
    for (const it of items) {
      const el = it.el; const r = roleOf(el); const nm = nameOf(el).toLowerCase();
      if (role && r !== role && !(role === 'input' && r === 'textbox') && !(role === 'dropdown' && r === 'select')) continue;
      let s = 0;
      if (!name) s = 1; else if (nm === name) s = 4; else if (nm.startsWith(name)) s = 3; else if (nm.includes(name)) s = 2;
      else { const f = (el.getAttribute('name') || el.id || '').toLowerCase(); if (f && f === name) s = 3; else if (el.placeholder && el.placeholder.toLowerCase().includes(name)) s = 2; }
      if (s) scored.push({ s, id: ps.idOf(el), role: r, name: nameOf(el), vis: inView(el.getBoundingClientRect()) ? 'view' : 'off' });
    }
    scored.sort((a, b) => b.s - a.s);
    return scored.slice(0, 8);
  };

  ps.brief = (id) => {
    const el = ps.byId(id); if (!el) return null;
    return { role: roleOf(el), name: nameOf(el), tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase() };
  };
  ps.why = (id) => {
    const el = ps.byId(id); if (!el) return { gone: true };
    const r = el.getBoundingClientRect();
    const info = { visible: visible(el), in_viewport: inView(r) };
    const b = blockedBy(el, r); if (b) info.blocked_by = b;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') info.disabled = true;
    if (el.readOnly) info.readonly = true;
    return info;
  };
  ps.quiet = () => ({ muts: ps.mutTotal, since: Date.now() - ps.lastMut, ready: document.readyState });
  ps.mutations = (since) => ps.mut.filter(m => m.n > since);

  const SKIP = new Set(['SCRIPT', 'STYLE', 'LINK', 'META', 'NOSCRIPT', 'HEAD', 'TITLE']);
  const note = (kind, n) => {
    if (SKIP.has(n.tagName)) return;
    const text = clip(n.innerText || n.textContent || '', 160);
    if (!text && !/^(IMG|IFRAME|DIALOG)$/.test(n.tagName)) return;
    const last = ps.mut[ps.mut.length - 1];
    if (last && last.k === kind && last.text === text && Date.now() - last.t < 300) return;
    const cls = typeof n.className === 'string' ? n.className.split(/\s+/).filter(Boolean).slice(0, 2).join('.') : '';
    ps.mut.push({ n: ++ps.mutSeq, t: Date.now(), k: kind, tag: n.tagName.toLowerCase(), id: n.id || '', cls: clip(cls, 30), role: n.getAttribute && n.getAttribute('role') || '', text });
    if (ps.mut.length > 80) ps.mut.splice(0, ps.mut.length - 80);
  };
  ps.mutSeq = 0;
  try {
    const obs = new MutationObserver((list) => {
      ps.mutTotal += list.length; ps.lastMut = Date.now();
      for (const m of list) {
        if (m.type !== 'childList') continue;
        m.addedNodes.forEach(n => { if (n.nodeType === 1) note('added', n); });
        m.removedNodes.forEach(n => { if (n.nodeType === 1) note('removed', n); });
      }
    });
    obs.observe(document, { childList: true, subtree: true });
  } catch (e) {}
  window.__ps = ps;
  return true;
})()
'''
