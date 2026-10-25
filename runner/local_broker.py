# runner/local_broker.py
"""
The local broker: lets a model-written script USE a secret that only this PC holds without ever being able to READ it.

It is the same idea as the cloud's secretless broker (bemain/broker.py). The script gets a URL and a one-time token instead of the secret and
makes a request THROUGH the broker; the broker puts the real value into the outgoing request and returns only the response:

    POST {PARASYNC_BROKER_URL}/broker/relay
    {"token": ..., "url": "https://api.example.com/x", "method": "POST", "headers": {"Authorization": "Bearer {{secret:API_KEY}}"}, "json_body": {...}}

This module runs inside job_supervisor.py, a process owned by root. The script runs as another user, so it cannot read the broker's memory,
environment or files; it can only talk to the broker over 127.0.0.1 with its token. Rules, each one prevents a real failure:
  * Only https, only to a host on THAT secret's allow list (the list comes from the cloud's record of the secret; no list means no use).
  * The host must resolve to public addresses only: a script must not use a secret to reach the PC's own network (router, printers, the
    Windows host). The request is made to the address that was checked, not to a second lookup of the name (no DNS tricks).
  * Redirects are never followed (a redirect could carry the secret header to another host).
  * Every local secret's value is blanked from whatever comes back.
  * A call that names no local secret is passed to the cloud's broker with the cloud token (cloud-held secrets, signed API actions). A call
    that mixes a local and a cloud secret is refused; it must be two calls.
Standard library only, so the Linux image needs nothing new.
"""
from __future__ import annotations

import hmac
import http.client
import http.server
import ipaddress
import json
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
MIN_REDACT_LENGTH = 4
PLACEHOLDER = "[REDACTED]"
SECRET_PLACEHOLDER = "{{secret}}"
NAMED_PLACEHOLDER = re.compile(r"\{\{secret:([A-Za-z0-9_.\-]{1,100})\}\}")
SIGNED_OR_OTHER_PATH = re.compile(r"^/broker/[A-Za-z0-9_./\-]{1,200}$")


class BrokerError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class BrokerState:
    """Everything one run's broker knows. Lives in this process's memory only."""

    def __init__(self, token: str, secrets: dict[str, dict], cloud: dict | None) -> None:
        self.token = token
        self.secrets = secrets          # name -> {"value": str, "hosts": [str]}
        self.cloud = cloud              # {"url": "https://...", "token": "..."} or None

    def values(self) -> list[str]:
        return [s["value"] for s in self.secrets.values()]


# ── placeholders ─────────────────────────────────────────────────────────────

def named_in(obj: Any) -> set[str]:
    if isinstance(obj, str):
        return set(NAMED_PLACEHOLDER.findall(obj))
    if isinstance(obj, dict):
        out: set[str] = set()
        for v in obj.values():
            out |= named_in(v)
        return out
    if isinstance(obj, list):
        out = set()
        for v in obj:
            out |= named_in(v)
        return out
    return set()


def substitute(obj: Any, values: dict[str, str], single: str | None) -> Any:
    if isinstance(obj, str):
        obj = NAMED_PLACEHOLDER.sub(lambda m: values[m.group(1)], obj)
        return obj.replace(SECRET_PLACEHOLDER, single) if single is not None else obj
    if isinstance(obj, dict):
        return {k: substitute(v, values, single) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, values, single) for v in obj]
    return obj


def redact(obj: Any, needles: list[str]) -> Any:
    if isinstance(obj, str):
        for n in needles:
            if n in obj:
                obj = obj.replace(n, PLACEHOLDER)
        return obj
    if isinstance(obj, dict):
        return {k: redact(v, needles) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, needles) for v in obj]
    return obj


def needles_of(values: list[str]) -> list[str]:
    return sorted({v for v in values if isinstance(v, str) and len(v) >= MIN_REDACT_LENGTH}, key=len, reverse=True)


# ── the outgoing request ────────────────────────────────────────────────────────

def public_address(host: str) -> str:
    """The one address to connect to: every address the name resolves to must be public (a name that also points inside is refused)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BrokerError(502, f"could not look up {host}: {exc}") from exc
    chosen: str | None = None
    for info in infos:
        address = info[4][0]
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise BrokerError(403, f"{host} points to a private address, which a secret may not be sent to")
        if chosen is None:
            chosen = address
    if chosen is None:
        raise BrokerError(502, f"could not look up {host}")
    return chosen


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to an address that was already checked, but still verifies the certificate for the real host name."""

    def __init__(self, host: str, ip: str, port: int, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def https_request(url: str, method: str, headers: dict[str, str], body: bytes | None, hosts: list[str]) -> tuple[int, bytes]:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise BrokerError(400, "only https addresses can be used with a secret")
    if parts.username or parts.password or not host:
        raise BrokerError(400, "that address is not allowed")
    if host not in {h.lower() for h in hosts}:
        raise BrokerError(403, f"host '{host}' is not on the allowed list for this secret; the owner must add it before this call can be relayed")
    ip = public_address(host)
    conn = _PinnedHTTPSConnection(host, ip, parts.port or 443, REQUEST_TIMEOUT_SECONDS)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise BrokerError(502, "the response was too large")
        return resp.status, data          # a redirect status comes back as it is: never followed
    except BrokerError:
        raise
    except Exception as exc:
        raise BrokerError(502, str(exc) or type(exc).__name__) from exc
    finally:
        conn.close()


def relay(state: BrokerState, body: dict) -> dict:
    """The /broker/relay call for local secrets. Same request and response shape as the cloud's."""
    url = body.get("url")
    if not isinstance(url, str) or not url:
        raise BrokerError(400, "url is required")
    headers_in = body.get("headers") or {}
    if not isinstance(headers_in, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers_in.items()):
        raise BrokerError(400, "headers must be an object of text values")
    json_body = body.get("json_body")
    method = str(body.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise BrokerError(400, "unsupported method")
    secret_name = body.get("secret_name")
    named = named_in(url) | named_in(headers_in) | named_in(json_body)
    uses_plain = any(SECRET_PLACEHOLDER in text for text in (url, *headers_in.values(), json.dumps(json_body, default=str)))
    if uses_plain and not secret_name:
        raise BrokerError(400, "`{{secret}}` needs `secret_name`; use `{{secret:NAME}}` to name the secret in place")
    wanted = sorted(named | ({secret_name} if secret_name else set()))
    values: dict[str, str] = {}
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for name in wanted:
        entry = state.secrets.get(name)
        if entry is None:
            raise BrokerError(400, f"secret '{name}' was not registered for this call")
        values[name] = entry["value"]
        hosts = [h.lower() for h in entry.get("hosts") or []]
        if not hosts or host not in hosts:
            raise BrokerError(403, f"host '{host}' is not on the allowed list for secret '{name}'; the owner must add it before this call can be relayed")
    single = values[secret_name] if secret_name else None

    final_url = substitute(url, values, single)
    headers = substitute(headers_in, values, single)
    payload: bytes | None = None
    if json_body is not None:
        payload = json.dumps(substitute(json_body, values, single)).encode()
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"
    status, data = https_request(final_url, method, headers, payload, [host])
    text = data.decode("utf-8", errors="replace")
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = text
    return redact({"status_code": status, "body": parsed}, needles_of(state.values()))


# ── passing a call on to the cloud's broker ──────────────────────────────────────

def forward_to_cloud(state: BrokerState, path: str, body: dict) -> tuple[int, dict]:
    if state.cloud is None:
        raise BrokerError(404, "this broker call is not available for this run")
    if not SIGNED_OR_OTHER_PATH.match(path) or ".." in path:
        raise BrokerError(404, "unknown broker call")
    sent = dict(body)
    sent["token"] = state.cloud["token"]
    request = urllib.request.Request(
        state.cloud["url"].rstrip("/") + path, data=json.dumps(sent).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=120) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            status = resp.status
    except urllib.error.HTTPError as err:
        raw = err.read(MAX_RESPONSE_BYTES + 1)
        status = err.code
    except Exception as exc:
        raise BrokerError(502, f"the cloud broker could not be reached: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        data = {"detail": "the cloud broker sent something unexpected"}
    return status, redact(data, needles_of(state.values())) if isinstance(data, (dict, list)) else {"detail": "unexpected"}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[override]
        return None


_NoRedirect = _NoRedirectHandler


def handle(state: BrokerState, path: str, body: dict) -> tuple[int, dict]:
    """The whole broker, with no sockets: (HTTP status, JSON answer). Tested directly."""
    token = body.get("token")
    if not isinstance(token, str) or not hmac.compare_digest(token.encode(), state.token.encode()):
        return 403, {"detail": "invalid or expired broker token"}
    try:
        if path == "/broker/relay":
            named = named_in(body.get("url")) | named_in(body.get("headers") or {}) | named_in(body.get("json_body"))
            if body.get("secret_name"):
                named.add(str(body["secret_name"]))
            uses_plain = SECRET_PLACEHOLDER in json.dumps([body.get("url"), body.get("headers"), body.get("json_body")], default=str)
            local = {n for n in named if n in state.secrets}
            other = named - local
            if local and other:
                raise BrokerError(400, "one call cannot use both a secret kept on this PC and one kept in the cloud; make two calls")
            if local or (uses_plain and body.get("secret_name") in state.secrets):
                return 200, relay(state, body)
        return forward_to_cloud(state, path, body)
    except BrokerError as err:
        return err.status, {"detail": err.detail}


class _Handler(http.server.BaseHTTPRequestHandler):
    state: BrokerState

    def log_message(self, *_args: Any) -> None:       # nothing is logged: a log line could hold a URL with a secret in it
        return

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:                         # noqa: N802 (name fixed by the base class)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send(413 if length > MAX_REQUEST_BYTES else 400, {"detail": "bad request size"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self._send(400, {"detail": "the body must be JSON"})
            return
        if not isinstance(body, dict):
            self._send(400, {"detail": "the body must be a JSON object"})
            return
        status, payload = handle(self.state, urllib.parse.urlsplit(self.path).path, body)
        self._send(status, payload)

    def do_GET(self) -> None:                          # noqa: N802
        self._send(404, {"detail": "not found"})


def start(state: BrokerState) -> tuple[http.server.ThreadingHTTPServer, int]:
    """Listens on the loopback address of the Linux box only, on a free port. Returns the server and the port."""
    handler = type("Handler", (_Handler,), {"state": state})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])
