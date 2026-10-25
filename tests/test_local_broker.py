# tests/test_local_broker.py
"""
The local broker: a script may USE a secret that only this PC holds, never read it. Covers the placeholder substitution, who may be sent
a secret (https only, the secret's own host list, public addresses only), redaction of whatever comes back, the rule against mixing a local
and a cloud secret in one call, passing everything else to the cloud's broker, and a real TLS round trip to a pinned address.
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import http.server
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "runner"))
import local_broker as lb  # noqa: E402

TOKEN = "t" * 40


def state(cloud=None) -> lb.BrokerState:
    return lb.BrokerState(TOKEN, {
        "API_KEY": {"value": "sk-live-0123456789", "hosts": ["api.example.com"]},
        "OTHER": {"value": "other-secret-value", "hosts": ["other.example.com"]},
    }, cloud)


@pytest.fixture
def sent(monkeypatch):
    """Replaces the real outgoing request; records what would have been sent."""
    calls: list[dict] = []

    def fake(url, method, headers, body, hosts):
        calls.append({"url": url, "method": method, "headers": headers, "body": body, "hosts": hosts})
        return 200, json.dumps({"echo": headers.get("Authorization"), "ok": True}).encode()

    monkeypatch.setattr(lb, "https_request", fake)
    return calls


def relay(st, **body):
    return lb.handle(st, "/broker/relay", {"token": TOKEN, **body})


def test_the_token_must_match(sent) -> None:
    status, out = lb.handle(state(), "/broker/relay", {"token": "wrong", "url": "https://api.example.com/x"})
    assert status == 403 and "invalid" in out["detail"]
    assert sent == []
    assert lb.handle(state(), "/broker/relay", {"url": "https://api.example.com/x"})[0] == 403


def test_a_named_secret_is_put_into_the_request_and_blanked_from_the_answer(sent) -> None:
    status, out = relay(state(), url="https://api.example.com/v1?k={{secret:API_KEY}}", method="post",
                        headers={"Authorization": "Bearer {{secret:API_KEY}}"}, json_body={"key": "{{secret:API_KEY}}", "n": 1})
    assert status == 200
    call = sent[0]
    assert call["url"] == "https://api.example.com/v1?k=sk-live-0123456789"
    assert call["method"] == "POST"
    assert call["headers"]["Authorization"] == "Bearer sk-live-0123456789"
    assert json.loads(call["body"]) == {"key": "sk-live-0123456789", "n": 1}
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["hosts"] == ["api.example.com"]
    assert out == {"status_code": 200, "body": {"echo": "Bearer [REDACTED]", "ok": True}}, "the value the server echoed back is blanked"
    assert "sk-live-0123456789" not in json.dumps(out)


def test_the_plain_placeholder_uses_secret_name(sent) -> None:
    status, out = relay(state(), secret_name="API_KEY", url="https://api.example.com/x", headers={"Authorization": "Bearer {{secret}}"})
    assert status == 200 and sent[0]["headers"]["Authorization"] == "Bearer sk-live-0123456789"
    status, out = relay(state(), url="https://api.example.com/x", headers={"Authorization": "Bearer {{secret}}"})
    assert status in (400, 404), "a plain placeholder without secret_name is refused"


def test_a_secret_is_only_sent_to_its_own_hosts(sent) -> None:
    status, out = relay(state(), url="https://evil.example.net/x", headers={"Authorization": "{{secret:API_KEY}}"})
    assert status == 403 and "allowed list" in out["detail"]
    status, out = relay(state(), url="https://other.example.com/x", headers={"Authorization": "{{secret:API_KEY}}"})
    assert status == 403, "another secret's host does not count"
    no_hosts = lb.BrokerState(TOKEN, {"API_KEY": {"value": "sk-live-0123456789", "hosts": []}}, None)
    assert relay(no_hosts, url="https://api.example.com/x", headers={"A": "{{secret:API_KEY}}"})[0] == 403, "no list means no use"
    assert sent == []


def test_a_call_cannot_mix_a_local_and_a_cloud_secret(sent) -> None:
    status, out = relay(state(), url="https://api.example.com/x", headers={"A": "{{secret:API_KEY}}", "B": "{{secret:CLOUD_ONE}}"})
    assert status == 400 and "two calls" in out["detail"]
    assert sent == []


class FakeCloud(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *_a):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeCloud.seen.append((self.path, body))
        data = json.dumps({"status_code": 200, "body": "from the cloud sk-live-0123456789"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_calls_that_name_no_local_secret_go_to_the_cloud_broker_with_its_token() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCloud)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cloud = {"url": f"http://127.0.0.1:{server.server_address[1]}", "token": "cloud-token-123456789"}
        FakeCloud.seen = []
        st = state(cloud)
        status, out = relay(st, url="https://api.example.com/x", headers={"A": "{{secret:CLOUD_ONE}}"})
        assert status == 200
        assert FakeCloud.seen[0][0] == "/broker/relay" and FakeCloud.seen[0][1]["token"] == "cloud-token-123456789", "the script's token is replaced"
        assert "sk-live-0123456789" not in json.dumps(out), "even the cloud's answer is scrubbed of local values"
        status, out = lb.handle(st, "/broker/signed/aws", {"token": TOKEN, "action": "athena_run_query", "arguments": {"sql": "select 1"}})
        assert status == 200 and FakeCloud.seen[1][0] == "/broker/signed/aws" and FakeCloud.seen[1][1]["action"] == "athena_run_query"
        assert lb.handle(st, "/not-broker/x", {"token": TOKEN})[0] == 404
        assert lb.handle(st, "/broker/../etc", {"token": TOKEN})[0] == 404
    finally:
        server.shutdown()
    assert lb.handle(state(None), "/broker/signed/aws", {"token": TOKEN})[0] == 404, "no cloud broker for this run"


def test_private_addresses_are_never_sent_a_secret(monkeypatch) -> None:
    monkeypatch.setattr(lb.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("192.168.1.1", 443))])
    with pytest.raises(lb.BrokerError) as e:
        lb.https_request("https://api.example.com/x", "GET", {}, None, ["api.example.com"])
    assert e.value.status == 403 and "private" in e.value.detail
    # a name that points to one public AND one private address is refused too
    monkeypatch.setattr(lb.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(lb.BrokerError):
        lb.public_address("api.example.com")
    for bad in ("127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"):
        monkeypatch.setattr(lb.socket, "getaddrinfo", lambda *a, _b=bad, **k: [(2, 1, 6, "", (_b, 443))])
        with pytest.raises(lb.BrokerError):
            lb.public_address("x.example.com")
    monkeypatch.setattr(lb.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    assert lb.public_address("api.example.com") == "8.8.8.8"


def test_only_https_without_credentials_in_the_address() -> None:
    for url in ("http://api.example.com/x", "ftp://api.example.com/x", "https://user:pw@api.example.com/x", "https:///x"):
        with pytest.raises(lb.BrokerError) as e:
            lb.https_request(url, "GET", {}, None, ["api.example.com"])
        assert e.value.status in (400, 403)


@pytest.mark.skipif(shutil.which('openssl') is None, reason='the openssl program is not in this environment')
def test_a_real_tls_round_trip_goes_to_the_checked_address_and_verifies_the_name(tmp_path, monkeypatch) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=api.example.com", "-addext", "subjectAltName=DNS:api.example.com"], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    received: list = []

    class Api(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization"), self.headers.get("Host")))
            data = b'{"hello": "world"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.HTTPServer(("127.0.0.1", 0), Api)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        # the name resolves to a "public" address as far as the broker is concerned, but the connection goes to the pinned address: the test server
        monkeypatch.setattr(lb, "public_address", lambda host: "127.0.0.1")
        trusting = ssl.create_default_context(cafile=str(cert))
        monkeypatch.setattr(lb.ssl, "create_default_context", lambda *a, **k: trusting)
        status, data = lb.https_request(f"https://api.example.com:{port}/p?x=1", "GET", {"Authorization": "Bearer abc"}, None, ["api.example.com"])
        assert status == 200 and json.loads(data) == {"hello": "world"}
        assert received == [("/p?x=1", "Bearer abc", f"api.example.com:{port}")]
        # a certificate for a different name must fail
        wrong = ssl.create_default_context(cafile=str(cert))
        wrong.check_hostname = True
        monkeypatch.setattr(lb.ssl, "create_default_context", lambda *a, **k: wrong)
        with pytest.raises(lb.BrokerError):
            lb.https_request(f"https://api.example.org:{port}/p", "GET", {}, None, ["api.example.org"])
    finally:
        server.shutdown()


def test_the_server_answers_over_loopback_http(sent) -> None:
    st = state(None)
    server, port = lb.start(st)
    try:
        def post(path, payload):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        assert post("/broker/relay", {"token": "nope"})[0] == 403
        assert post("/broker/relay", {"token": TOKEN, "url": "https://api.example.com/x", "headers": {"A": "{{secret:API_KEY}}"}})[0] == 200
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
