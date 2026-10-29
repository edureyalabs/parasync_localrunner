# sandbox/microvm/scrub.py
"""
Hides secrets in everything the browser senses record (senses.py) BEFORE it leaves the browser machine. Network records carry login forms, session
tokens and API keys, so the rule is strict and blunt:

  * request and response HEADERS are never recorded at all (cookies, Authorization): only the content type is kept;
  * a form or JSON field whose NAME looks secret (password, token, cookie, session, csrf, otp, pin, card, key, ...) keeps its name and loses its value;
  * anything JWT-shaped, a "Bearer ..." value, or a long opaque token is replaced wherever it appears (URLs, bodies, page text, console lines);
  * a password input's value is never read by the page script.

bemain scrubs again with the session's own known values (browser_access/redaction.py); this is the first layer, and it needs no knowledge of the session.
Python 3.9 compatible (the MicroVM image's interpreter).
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HIDDEN = "[hidden]"
_SECRET_NAME = re.compile(
    r"(pass(word|wd)?|pwd|secret|token|auth|cookie|session|sess(id)?|csrf|xsrf|otp|passcode|\bpin\b|cvv|cvc|card|ssn|api[-_]?key|apikey|signature|credential|bearer|jwt|private)",
    re.I)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])")
MAX_JSON_DEPTH = 6


def is_secret_name(name: object) -> bool:
    return bool(_SECRET_NAME.search(str(name or "")))


def text(value: object) -> str:
    """Free text with token-shaped strings replaced."""
    s = value if isinstance(value, str) else str(value)
    s = _JWT.sub(HIDDEN, s)
    s = _BEARER.sub(lambda m: m.group(1) + " " + HIDDEN, s)
    return _OPAQUE.sub(HIDDEN, s)


def url(raw: str) -> str:
    """A URL with secret-named query values and fragment values hidden, and token-shaped strings replaced."""
    try:
        parts = urlsplit(raw)
        if not parts.query and not parts.fragment:
            return text(raw)
        query = urlencode([(k, HIDDEN if is_secret_name(k) else text(v)) for k, v in parse_qsl(parts.query, keep_blank_values=True)], safe="[]")
        fragment = HIDDEN if parts.fragment and "=" in parts.fragment and is_secret_name(parts.fragment) else parts.fragment
        return text(urlunsplit((parts.scheme, parts.netloc, parts.path, query, fragment)))
    except Exception:
        return text(raw)


def _walk(node: object, depth: int = 0) -> object:
    if depth > MAX_JSON_DEPTH:
        return "…"
    if isinstance(node, dict):
        return {k: (HIDDEN if is_secret_name(k) and not isinstance(v, (dict, list)) else _walk(v, depth + 1)) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, depth + 1) for v in node[:50]]
    if isinstance(node, str):
        return text(node)
    return node


def json_value(value: object) -> object:
    return _walk(value)


def fields(pairs: list) -> dict:
    """Form fields (name, value) as a dict, secret-named values hidden, repeated names collected."""
    out: dict = {}
    for name, value in pairs:
        shown = HIDDEN if is_secret_name(name) else text(value)
        if name in out:
            out[name] = (out[name] if isinstance(out[name], list) else [out[name]]) + [shown]
        else:
            out[name] = shown
    return out


def body(raw: object, content_type: str = "", limit: int = 700) -> object:
    """What was sent or received, safe to show: a dict of form fields, scrubbed JSON, or a short scrubbed string."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return "[binary %d bytes]" % len(raw)
    ctype = (content_type or "").lower()
    s = str(raw)
    if "multipart/form-data" in ctype:
        names = re.findall(r'name="([^"]+)"', s[:4000])
        return {"multipart_fields": [HIDDEN if is_secret_name(n) else n for n in names[:30]]}
    stripped = s.lstrip()
    if "json" in ctype or stripped[:1] in "{[":
        try:
            return _fit(_walk(json.loads(s)), limit)
        except ValueError:
            pass
    if "x-www-form-urlencoded" in ctype or ("=" in s and "<" not in s[:50] and "\n" not in s[:200]):
        try:
            return _fit(fields(parse_qsl(s, keep_blank_values=True)), limit)
        except Exception:
            pass
    return text(s[:limit])


def _fit(value: object, limit: int) -> object:
    """The value as it is if its JSON fits `limit` characters, otherwise a shortened JSON string."""
    try:
        dumped = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return text(str(value)[:limit])
    return value if len(dumped) <= limit else dumped[:limit] + "…"


def shape(value: object, depth: int = 0) -> str:
    """A short description of a JSON value's structure: `{rows: list(24), total: number}`. Values are never shown."""
    if isinstance(value, dict):
        if depth >= 3:
            return "{%d keys}" % len(value)
        items = list(value.items())[:12]
        inner = ", ".join("%s: %s" % (k, shape(v, depth + 1)) for k, v in items)
        return "{" + inner + (", …" if len(value) > 12 else "") + "}"
    if isinstance(value, list):
        return "list(%d)" % len(value) + (" of " + shape(value[0], depth + 1) if value and depth < 3 else "")
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "text"
