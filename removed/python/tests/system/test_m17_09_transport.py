"""M17-09 有界传输与 CONNECT。断言打在真实代理和本地上游上。"""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from mao.base import SystemClock
from mao.egress import AllowRule, EgressPlan, EgressProxy, ModelProxy, SecretStore, register_job_cap
from mao.errors import FactoryError
from mao.transport import load_transport, resolve_service_location
from tests.acceptance.m2.world import EXAMPLE, open_factory

_CERT = """-----BEGIN CERTIFICATE-----
MIIDJTCCAg2gAwIBAgIUHGnciNQnEUvV7HNPf+VcKImhzKEwDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJMTI3LjAuMC4xMB4XDTI2MTExNzAyNTMyOVoXDTM2MTEx
NDAyNTMyOVowFDESMBAGA1UEAwwJMTI3LjAuMC4xMIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEAswRJFWn++eTyaEgCuyZZtoGVCXFeTyq8hUAnG1mfcOLr
WJ5o1H9ZN2ImK6oR1z9coZ4K+cwn3PvayIVgSg0QNwgwuDkX9sMHmyjuBlzuHGU6
zkwa3IBNzN7nx1PhEHi2QHUD3Ri8QwlnuHv2Y9ebc+wdmtgCREwU4Uhx6hGhyJYB
bLUc1cvJmZVAHOgIrk7ZUtEBpthm02Mc4c+0EoTF6Cjv/P2AH0Tl54zrwPWylIbO
dUvlw1bVEqKF3cHpr95RHjoRLcvN1QbDPxwgusGYWFMML08zARCRYzkvKQvepD9t
8MQ2UKvbaqRBi4BDhpizmGMG2RU9ZwnE6XEQYhmK6QIDAQABo28wbTAdBgNVHQ4E
FgQUio63YUdmLHc4u7zfoU4WFh+melYwHwYDVR0jBBgwFoAUio63YUdmLHc4u7zf
oU4WFh+melYwDwYDVR0TAQH/BAUwAwEB/zAaBgNVHREEEzARhwR/AAABggkxMjcu
MC4wLjEwDQYJKoZIhvcNAQELBQADggEBAAdkhNADGDeukvUaIsdBVd2XxWVlTdbv
W+fiq0MW1yT9fJo5HWreGBc2yaw7Cg3iOxFl6cgYB0u9KuQ18CFJxzyOJx2YbTUC
7/4bWqbTTZZw4uhGkaFZ8ug7DDWl9+t3XpG1Mx/B202qi6zqcp/al0g3HQEGS5pw
AAZEfVRVrPbCD0nBEmLmMXzGmv+jWB609QGrI1iuSVKhQRqKI5UX+f97qE5H3I6o
lEkX8ktBQF5fmbvN9NYsTUDlFG0m+Dri3UNNNVrvEJKO4Fj/4mAGzm7lYOXlRFpG
vNUol5KvUhQu2s0wsa3YVKXh75GDDwL4NxMcOR7jYajr23GCZMi0wcc=
-----END CERTIFICATE-----
"""
_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCzBEkVaf755PJo
SAK7Jlm2gZUJcV5PKryFQCcbWZ9w4utYnmjUf1k3YiYrqhHXP1yhngr5zCfc+9rI
hWBKDRA3CDC4ORf2wwebKO4GXO4cZTrOTBrcgE3M3ufHU+EQeLZAdQPdGLxDCWe4
e/Zj15tz7B2a2AJETBThSHHqEaHIlgFstRzVy8mZlUAc6AiuTtlS0QGm2GbTYxzh
z7QShMXoKO/8/YAfROXnjOvA9bKUhs51S+XDVtUSooXdwemv3lEeOhEty83VBsM/
HCC6wZhYUwwvTzMBEJFjOS8pC96kP23wxDZQq9tqpEGLgEOGmLOYYwbZFT1nCcTp
cRBiGYrpAgMBAAECggEAJqfEvj6Co+RXocP27wGTerDVhVH5GuAwEBfvudyq7J3b
Uun12fjbw3VKp8AUFSYl8vIlDZaTfwjVT9pZFfRnLXLzzjvp3Et1z3/RkgcsFW6Y
eaZ+YGHGKvhxFR4cKcnfFOEPogTu1gzoXDJMXHfYpfn01JRTPPsuwQy7KF+BT0YU
A5t3mXx23xQVHI7ieNzTYQnpRqSuS/D6bFmlT5Wcrij0aheJYY6ipGz69klmvhno
AzTjuhtd2qWk5eTx2KZBiFW0LTQD2sOOaPQTYY5HayOir1WdRyUDl1/D+fOChKU9
i0ylavBLFi7L99jTp4+nEKaqAFRRIFeBJsA8pd9tNwKBgQDskK+EAvQ0JQtKM3+A
LIINn8eYk40XMjBHdTwhqYll/e0VocC+NfNhF71hwoFEle/yNdFZoFkGrWn7V68L
4luuzAxkD9Hhb8xzjHWTzXOZPAyGDDMUZehHmJ1JPhdGixoiA2LvEEhEFL+eo6i2
7wfj2JMsHqf4V85txbI8U1/23wKBgQDBuUV6V8Zmoz0soZ5Qaiaph6bcFx75QcmE
NA2alSQSgM2X5J9Ugr0yFdWuv3fSLTalmJ5k08hqGGuEjaf+4zm1zWAzhvLk+Tba
XcEzMstZ8QxuZ1b5IxkdlOS40Mww1oQFbhPZcac4UYbIbj65p/oMLFtHrNOUC+H+
hXgpabmfNwKBgQDK+U2PIBW10/7+2HbSW93uoKRaFmfdejOQbFd+rn90Yk4U3GeL
DLFZIkxc382YMmnhUSniE8GvCAXCITbRJJPzw6HyDNzQ5+LtVZvgiExiMvKv5THC
k7mcDKcPm9XOsjVm767oXfkTaRYMoHF3HFqRtVZ5aYhHfddhulm2hvb00wKBgAxB
XkXZG7IOjaYvK0bHEF8VN9cgl1MJeGgmqt2mpbuN7nO3gOTlyHZMr1H4xunC9nwP
ZaXXSAx+Mv8VvxT+5v27WmSoGuGlanTyDXDhu7+a4kBxxSzJraMTckzG2MQWLg/x
4MuWxw9nCPHRF6LDH6x5eeTU6CWrgsaJRwrHp3iNAoGBAKHJ7CgeM2+sC9069gzo
7Aeq9CEZvCpVhdC1j839/we7/MQEvP+Ue4ulGOaarwDq+/GKuaQmlUb7h57htsa9
8Nk/m+55XdY85GqZ1R1MBFsATx6drVIN7ZndW/M2NUCh8tml/0w6ZTEShp2nD+M8
1VNcuPlkd7k6aX+cy9m5Siw1
-----END PRIVATE KEY-----
"""


def test_model_deadlines_and_partial_response(tmp_path: Path) -> None:
    """慢连接记 not_sent；写出后的残缺响应不能当成成功。"""
    world = _model_world(
        tmp_path,
        connect_s=0.2,
        idle_s=0.3,
        total_s=2.0,
        upstream={"url": "http://127.0.0.1:1/v1", "requests": 0},
    )
    started = time.monotonic()
    status, body = _post(world, operation="slow-connect")
    elapsed = time.monotonic() - started
    assert status == 504, body
    assert body["detail"]["outcome"] == "not_sent"
    assert body["detail"]["retryable"] is True
    assert body["detail"]["phase"] == "connect"
    assert elapsed < 1.5
    partial = _stall_after(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\nshort")
    world["upstream"] = partial
    _point_upstream(world, partial)
    status, body = _post(world, operation="partial")
    assert status == 504, body
    assert body["detail"]["outcome"] == "outcome_unknown"
    assert body["detail"]["retryable"] is False
    assert partial["requests"] == 1
    allowed = resolve_service_location(
        "/v1/messages", base_url=partial["url"], allowed_origins=[_origin(partial["url"])]
    )
    assert allowed.startswith(partial["url"].split("/v1")[0])
    try:
        resolve_service_location(
            "file:///tmp/x", base_url=partial["url"], allowed_origins=[_origin(partial["url"])]
        )
    except FactoryError as exc:
        assert exc.code == "SERVICE_LOCATION_DENIED"
    else:
        raise AssertionError("file URL was accepted")
    world["proxy"].close()


def test_chunked_sse_usage_is_metered(tmp_path: Path) -> None:
    """分块的 responses 流去掉长度帧后仍能计量，不能记成无法解析。"""
    event = json.dumps(
        {
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 4, "output_tokens": 2, "cost_usd": 0.01}},
        }
    ).encode()
    piece = b"data: " + event + b"\n\n"
    body = f"{len(piece):x}\r\n".encode() + piece + b"\r\n0\r\n\r\n"
    raw = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
    ) + body
    upstream = _serve_raw(raw)
    world = _model_world(tmp_path, connect_s=1, idle_s=2, total_s=3, upstream=upstream)
    (world["ctx"].root / "egress" / "prices.json").write_text(
        json.dumps({"*": {"input_per_mtok": 1000.0, "output_per_mtok": 1000.0}}),
        encoding="utf-8",
    )
    req = Request(
        f"http://127.0.0.1:{world['port']}/v1/responses",
        data=b'{"model":"x","stream":true}',
        method="POST",
        headers={"X-Factory-Job": "j-transport", "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        forwarded = resp.read()
    assert b"response.completed" in forwarded
    assert world["proxy"].spent("j-transport") > 0
    assert world["ctx"].db.query("SELECT code FROM budget_stops") == []
    world["proxy"].close()


def test_total_deadline_not_extended_by_progress(tmp_path: Path) -> None:
    """持续有字节到达也不能把 total 往后推。"""
    drip = _drip(interval_s=0.05, seconds=1.2)
    world = _model_world(tmp_path, connect_s=0.3, idle_s=0.35, total_s=0.35, upstream=drip)
    started = time.monotonic()
    status, body = _post(world, operation="drip")
    elapsed = time.monotonic() - started
    assert status == 504, body
    assert body["detail"]["outcome"] == "outcome_unknown"
    assert elapsed < 0.9, elapsed
    assert drip["chunks"] >= 1
    world["proxy"].close()


def test_timeout_unknown_keeps_cost_and_prevents_resubmit(tmp_path: Path) -> None:
    """已发送后的超时保留费用上界，并且不会再向上游重发。"""
    partial = _stall_after(b"HTTP/1.1 200 OK\r\nContent-Length: 80\r\n\r\nnope")
    world = _model_world(tmp_path, connect_s=0.4, idle_s=0.25, total_s=1.0, upstream=partial, cap=1.5)
    status, body = _post(world, operation="op-unknown")
    assert status == 504 and body["detail"]["outcome"] == "outcome_unknown"
    held = world["ctx"].db.query(
        "SELECT kind, amount_usd FROM budget_entries WHERE operation_id = ?",
        ("op-unknown",),
    )
    assert held and held[0]["kind"] == "reserve" and float(held[0]["amount_usd"]) == 1.5
    attempt = world["ctx"].db.query(
        "SELECT outcome, error_code FROM transport_attempts WHERE operation_id = ?",
        ("op-unknown",),
    )
    assert attempt[0]["outcome"] == "outcome_unknown"
    status, again = _post(world, operation="op-unknown")
    assert status == 409, again
    assert again["detail"]["retryable"] is False
    assert partial["requests"] == 1
    world["proxy"].close()


def test_connect_buffered_fragmented_and_empty_payload(tmp_path: Path) -> None:
    """同包、分片和空载荷都只转发一次，不等待可选字节。"""
    received: list[bytes] = []
    port = _tcp_collect(received)
    proxy = _egress(tmp_path, host="127.0.0.1", methods={"*"})
    try:
        _connect(proxy, port, payload=b"same-packet", together=True)
        _connect(proxy, port, payload=b"frag-onefrag-two", together=False, split=8)
        _connect(proxy, port, payload=b"", together=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(received) < 3:
            time.sleep(0.05)
        assert received[:3] == [b"same-packet", b"frag-onefrag-two", b""]
    finally:
        proxy.close()


def test_connect_tls_method_and_authority_enforcement(tmp_path: Path) -> None:
    """没有 CA 时不透传；有证书时，错主机和被禁方法到不了上游。"""
    hits = _tcp_hits()
    opaque = _egress(tmp_path, host="127.0.0.1", methods={"GET"})
    try:
        code = _connect_status(opaque, hits["port"])
        assert code == 403
        assert b"EGRESS_METHOD_OPAQUE" in _last_body
        assert hits["count"] == 0
    finally:
        opaque.close()
    inspected = _egress(tmp_path, host="127.0.0.1", methods={"GET"}, inspection=True)
    try:
        denied = _tls_inner(inspected, hits["port"], method="POST", host="127.0.0.1")
        assert denied == "closed"
        assert hits["count"] == 0
        mismatched = _tls_inner(inspected, hits["port"], method="GET", host="evil.test")
        assert mismatched == "closed"
        assert hits["count"] == 0
        _tls_inner(inspected, hits["port"], method="GET", host="127.0.0.1")
        assert hits["count"] == 1
    finally:
        inspected.close()


def test_tunnel_cancel_and_capacity_release(tmp_path: Path) -> None:
    """超过 max_tunnels 会被拒绝；关闭后名额释放，线程不继续占着。"""
    held = _tcp_hold()
    proxy = _egress(tmp_path, host="127.0.0.1", methods={"*"}, max_tunnels=1)
    first = _open_connect(proxy, held)
    try:
        assert _connect_status(proxy, held) == 503
        assert proxy._tunnels.active == 1
    finally:
        first.close()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and proxy._tunnels.active:
        time.sleep(0.02)
    assert proxy._tunnels.active == 0
    assert _connect_status(proxy, held) == 200
    proxy.close()


_last_body = b""


def _model_world(
    tmp_path: Path, *, connect_s: float, idle_s: float, total_s: float, upstream=None, cap: float = 1.0
):
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    (ctx.root / "factory.json").write_text(
        json.dumps(
            {
                "transport": {
                    "model": {"connect_s": connect_s, "idle_s": idle_s, "total_s": total_s},
                    "egress": {"connect_s": 1, "idle_s": 1, "total_s": 2, "max_tunnels": 4},
                }
            }
        ),
        encoding="utf-8",
    )
    assert isinstance(load_transport(ctx.root)["model"].connect_s, float)
    secrets = tmp_path / "secrets-root"
    (secrets / "secrets" / "values").mkdir(parents=True)
    (secrets / "secrets" / "manifest.json").write_text("{}", encoding="utf-8")
    (secrets / "secrets" / "values" / "TOOL_TOKEN").write_text("token\n", encoding="utf-8")
    if upstream is None:
        upstream = {"url": "http://192.0.2.1:9/v1", "requests": 0}
    (ctx.root / "egress").mkdir(exist_ok=True)
    (ctx.root / "egress" / "upstreams.json").write_text(
        json.dumps({"*": {"url": upstream["url"], "secret": "TOOL_TOKEN", "header": "x-api-key"}}),
        encoding="utf-8",
    )
    (ctx.root / "egress" / "prices.json").write_text("{}", encoding="utf-8")
    station = snapshot.key("Station", "explainer@3")
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES ('j-transport', ?, ?, 'RUNNING', 1, '{}', '{}', '{}', ?, ?)",
            (station, snapshot.epoch, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
        )
    register_job_cap(ctx.db, "j-transport", station, cap)
    proxy = ModelProxy(SecretStore(secrets), SystemClock(), ctx.root)
    port = proxy.start(port=0)
    return {"ctx": ctx, "proxy": proxy, "port": port, "upstream": upstream}


def _point_upstream(world: dict, upstream: dict) -> None:
    (world["ctx"].root / "egress" / "upstreams.json").write_text(
        json.dumps({"*": {"url": upstream["url"], "secret": "TOOL_TOKEN", "header": "x-api-key"}}),
        encoding="utf-8",
    )


def _post(world: dict, *, operation: str) -> tuple[int, dict]:
    req = Request(
        f"http://127.0.0.1:{world['port']}/v1/complete",
        data=b'{"model":"x"}',
        method="POST",
        headers={
            "X-Factory-Job": "j-transport",
            "X-Factory-Operation": operation,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=5) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "read"):
            return int(exc.code), json.loads(exc.read().decode("utf-8"))
        raise


def _serve_raw(payload: bytes) -> dict:
    state = {"requests": 0, "url": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            state["requests"] += 1
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.wfile.write(payload)
            self.wfile.flush()

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
    state["server"] = server
    return state


def _stall_after(prefix: bytes) -> dict:
    state = {"requests": 0, "url": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            state["requests"] += 1
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.wfile.write(prefix)
            self.wfile.flush()
            time.sleep(2)

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
    state["server"] = server
    return state


def _drip(*, interval_s: float, seconds: float) -> dict:
    state = {"requests": 0, "chunks": 0, "url": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            state["requests"] += 1
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                self.wfile.write(b"x")
                self.wfile.flush()
                state["chunks"] += 1
                time.sleep(interval_s)

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1"
    return state


def _origin(url: str) -> str:
    return url.split("/v1")[0]


def _egress(
    tmp_path: Path, *, host: str, methods: set[str], inspection: bool = False, max_tunnels: int = 4
) -> EgressProxy:
    root = tmp_path / f"egress-{max_tunnels}-{inspection}-{host}"
    root.mkdir()
    cert = root / "cert.pem"
    key = root / "key.pem"
    cert.write_text(_CERT, encoding="utf-8")
    key.write_text(_KEY, encoding="utf-8")
    transport: dict = {
        "egress": {"connect_s": 1, "idle_s": 1, "total_s": 2, "max_tunnels": max_tunnels},
    }
    if inspection:
        transport["tls_inspection"] = {
            "ca_file": str(cert.resolve()),
            "hosts": {"127.0.0.1": {"cert_file": str(cert.resolve()), "key_file": str(key.resolve())}},
        }
    (root / "factory.json").write_text(json.dumps({"transport": transport}), encoding="utf-8")
    plan = EgressPlan("egress", (AllowRule("T1", host, frozenset(methods)),), (), (), {})
    proxy = EgressProxy(lambda _job: plan, SecretStore(root), SystemClock(), root=root)
    proxy.start()
    return proxy


def _tcp_collect(received: list[bytes]) -> int:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(2)

    def accept() -> None:
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            data = b""
            conn.settimeout(0.4)
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                pass
            received.append(data)
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    return int(server.getsockname()[1])


def _tcp_hits() -> dict:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    state = {"count": 0, "port": int(server.getsockname()[1]), "server": server}

    def accept() -> None:
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            state["count"] += 1
            conn.settimeout(1)
            try:
                conn.recv(64)
            except OSError:
                pass
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    return state


def _tcp_hold() -> int:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def accept() -> None:
        while True:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                conn.recv(8)
            except OSError:
                pass

    threading.Thread(target=accept, daemon=True).start()
    return int(server.getsockname()[1])


def _connect(proxy: EgressProxy, port: int, *, payload: bytes, together: bool, split: int = 0) -> None:
    sock = _open_connect(proxy, port, payload=payload if together else b"")
    if not together and payload:
        sock.sendall(payload[:split])
        sock.sendall(payload[split:])
    sock.shutdown(socket.SHUT_WR)
    time.sleep(0.2)
    sock.close()


def _open_connect(proxy: EgressProxy, port: int, payload: bytes = b"") -> socket.socket:
    assert proxy._server is not None
    sock = socket.create_connection(("127.0.0.1", int(proxy._server.server_address[1])))
    head = f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Factory-Job: job-1\r\n\r\n".encode()
    sock.sendall(head + payload)
    sock.settimeout(1)
    status, _body = _read_status(sock)
    assert status == 200, status
    return sock


def _connect_status(proxy: EgressProxy, port: int) -> int:
    global _last_body
    assert proxy._server is not None
    sock = socket.create_connection(("127.0.0.1", int(proxy._server.server_address[1])))
    try:
        sock.sendall(
            f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Factory-Job: job-1\r\n\r\n".encode()
        )
        sock.settimeout(1)
        status, body = _read_status(sock)
        if b"\r\n\r\n" in body:
            head, rest = body.split(b"\r\n\r\n", 1)
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    needed = int(line.split(b":", 1)[1].strip())
                    while len(rest) < needed:
                        rest += sock.recv(needed - len(rest))
            body = rest
        _last_body = body
        return status
    finally:
        sock.close()


def _read_status(sock: socket.socket) -> tuple[int, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    line = data.split(b"\r\n", 1)[0]
    return int(line.split(b" ")[1]), data


def _tls_inner(proxy: EgressProxy, port: int, *, method: str, host: str) -> str:
    sock = _open_connect(proxy, port)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        tls = context.wrap_socket(sock, server_hostname="127.0.0.1")
        tls.sendall(f"{method} / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        tls.settimeout(0.6)
        try:
            data = tls.recv(32)
        except OSError:
            return "closed"
        return "open" if data else "closed"
    except OSError:
        return "closed"
    finally:
        sock.close()
