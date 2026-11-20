"""M12-05：入站网关（spec/system/ingress.md）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import threading
import time
from pathlib import Path

from tests.acceptance.m12.instance_world import SCHEDULER, SECRET_VALUES, World, serve_config

NOTE = {"Authorization": "Bearer " + SECRET_VALUES["NOTE_TOKEN"]}


def _server(w: World, **config):
    from mao.ingress import IngressConfig, IngressServer

    return IngressServer(
        w.ctx, w.epochs, IngressConfig(port=0, **config), instances=w.instances, runner_for=lambda s: w.runner
    )


def _post(server, route: str, body: bytes, headers: dict[str, str] | None = None):
    sent = {"Content-Length": str(len(body))}
    sent.update(headers or {})
    return server.handle("POST", f"/in/{route}", sent, body)


def test_bearer_route_seals_bean(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        status, body = _post(_server(w), "note-push", b"hello from outside\n", NOTE)
        assert status == 202, body
        assert body["id"].startswith("ig-")
        [row] = w.rows("SELECT instance, station, port, bean, trigger FROM emits")
        assert row["instance"] is None and row["station"] == w.key("hook-bean") and row["port"] == "note"
        assert json.loads(row["trigger"]) == {"on": "ingress", "route": "note-push", "request": body["id"]}
        assert w.bean_text(row["bean"]) == "hello from outside\n"
        assert w.events("ingress.received") == [
            {"route": "note-push", "id": body["id"], "bytes": 19, "deliver": "bean", "target": row["bean"]}
        ]
        assert not (w.root / "ingress" / "tmp" / body["id"]).exists()
    finally:
        w.close()


def test_hmac_route(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        server = _server(w)
        payload = b"signed body\n"
        sig = hmac.new(SECRET_VALUES["SIGN_TOKEN"].encode(), payload, hashlib.sha256).hexdigest()
        assert _post(server, "note-sign", payload, {"X-Signature": sig})[0] == 202
        assert _post(server, "note-sign", payload, {"x-signature": "sha256=" + sig.upper()})[0] == 202
        assert _post(server, "note-sign", payload, {"X-Signature": "0" * 64}) == (
            401,
            {"code": "INGRESS_AUTH_FAILED"},
        )
        assert _post(server, "note-sign", payload, NOTE) == (401, {"code": "INGRESS_AUTH_FAILED"})
        assert len(w.rows("SELECT id FROM emits")) == 2
    finally:
        w.close()


def test_ingress_rejections(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        server = _server(w, rate_per_min=2, max_body_kb=1)
        assert server.handle("GET", "/in/note-push", {}, b"") == (405, {"code": "INGRESS_METHOD"})
        assert _post(server, "nope", b"x\n", NOTE) == (404, {"code": "INGRESS_ROUTE_NOT_FOUND"})
        assert server.handle("POST", "/elsewhere", {"Content-Length": "2"}, b"x\n") == (
            404,
            {"code": "INGRESS_ROUTE_NOT_FOUND"},
        )
        assert server.handle("POST", "/in/note-push", dict(NOTE), b"x\n") == (
            400,
            {"code": "INGRESS_BAD_REQUEST"},
        )
        assert _post(server, "note-push", b"x" * 1025, NOTE) == (413, {"code": "INGRESS_TOO_LARGE"})
        assert _post(server, "note-nokey", b"x\n", {"Authorization": "Bearer anything"}) == (
            503,
            {"code": "INGRESS_SECRET_MISSING"},
        )
        assert _post(server, "note-push", b"x\n", {"Authorization": "Bearer wrong"}) == (
            401,
            {"code": "INGRESS_AUTH_FAILED"},
        )
        assert _post(server, "note-wrongfile", b"x\n", NOTE) == (422, {"code": "INGRESS_PAYLOAD_INVALID"})
        assert _post(server, "note-push", b"one\n", NOTE)[0] == 202
        assert _post(server, "note-push", b"two\n", NOTE)[0] == 202
        assert _post(server, "note-push", b"three\n", NOTE) == (429, {"code": "INGRESS_RATE_LIMITED"})
        w.clock.advance(61)
        assert _post(server, "note-push", b"four\n", NOTE)[0] == 202
        rejected = [(e["route"], e["code"], e["status"]) for e in w.events("ingress.rejected")]
        assert rejected == [
            ("nope", "INGRESS_ROUTE_NOT_FOUND", 404),
            ("", "INGRESS_ROUTE_NOT_FOUND", 404),
            ("note-push", "INGRESS_BAD_REQUEST", 400),
            ("note-push", "INGRESS_TOO_LARGE", 413),
            ("note-nokey", "INGRESS_SECRET_MISSING", 503),
            ("note-push", "INGRESS_AUTH_FAILED", 401),
            ("note-wrongfile", "INGRESS_PAYLOAD_INVALID", 422),
            ("note-push", "INGRESS_RATE_LIMITED", 429),
        ]
    finally:
        w.close()


def test_route_duplicate_and_invalid(tmp_path: Path) -> None:
    from mao.ingress import routes

    w = World(tmp_path)
    try:
        server = _server(w)
        assert _post(server, "dup", b"x\n", NOTE) == (503, {"code": "INGRESS_ROUTE_DUPLICATE"})
        assert _post(server, "bad-role", b"x\n", NOTE) == (503, {"code": "INGRESS_ROUTE_INVALID"})
        table = routes(w.snapshot)
        assert table["dup"].error == "INGRESS_ROUTE_DUPLICATE"
        assert table["bad-role"].error == "INGRESS_ROUTE_INVALID"
        push = table["note-push"]
        assert (push.owner, push.auth, push.header, push.deliver, push.port, push.file, push.error) == (
            w.key("hook-bean"),
            "bearer",
            "Authorization",
            "bean",
            "note",
            "note.txt",
            None,
        )
        sign = table["note-sign"]
        assert (sign.auth, sign.secret, sign.header) == ("hmac-sha256", "SIGN_TOKEN", "X-Signature")
        assert table["res-drop"].deliver == "instance" and table["res-drop"].error is None
    finally:
        w.close()


def test_deliver_to_instance(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        server = _server(w)
        headers = {
            "Authorization": "Bearer " + SECRET_VALUES["RES_TOKEN"],
            "Content-Type": "text/plain",
            "X-Extra": "drop-me",
        }
        assert _post(server, "res-drop", b"early\n", headers) == (503, {"code": "INSTANCE_NOT_READY"})
        w.instances.reconcile()
        inst = w.live("res-listen")
        status, body = _post(server, "res-drop", b"external event\n", headers)
        assert status == 202, body
        result = w.result(inst.id, f"{1:010d}-{body['id']}")
        raw = result["raw"]
        assert sorted(raw) == ["body_b64", "content_type", "id", "received_at", "route"]
        assert (raw["id"], raw["route"], raw["content_type"]) == (body["id"], "res-drop", "text/plain")
        assert base64.b64decode(raw["body_b64"]) == b"external event\n"
        assert result["code"] == 0, result
        assert w.events("ingress.received")[-1] == {
            "route": "res-drop",
            "id": body["id"],
            "bytes": 15,
            "deliver": "instance",
            "target": inst.id,
        }
    finally:
        w.close()


def test_no_secret_or_body_in_events(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        server = _server(w)
        private = b"super private body 4c1e\n"
        responses = [
            _post(server, "note-push", private, NOTE),
            _post(server, "note-push", private, {"Authorization": "Bearer " + SECRET_VALUES["SIGN_TOKEN"]}),
            _post(server, "note-sign", private, {"X-Signature": "bad"}),
        ]
        assert [status for status, _ in responses] == [202, 401, 401]
        dump = json.dumps([dict(r) for r in w.rows("SELECT kind, data FROM events")]) + json.dumps(responses)
        logs = w.root / "logs"
        if logs.is_dir():
            dump += "".join(
                p.read_text(encoding="utf-8", errors="replace") for p in logs.rglob("*") if p.is_file()
            )
        for value in SECRET_VALUES.values():
            assert value not in dump
        assert "super private body" not in dump
        assert "Bearer" not in dump
    finally:
        w.close()


def test_serve_starts_ingress(tmp_path: Path) -> None:
    from mao.daemon import Daemon, InlineJobExecutor

    w = World(tmp_path, ingress={"port": 0})
    (w.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    daemon = Daemon(
        w.ctx, serve_config(tick_s=0.05), executor=InlineJobExecutor(lambda job_id: w.runner), epochs=w.epochs
    )
    stop = threading.Event()
    thread = threading.Thread(target=daemon.serve, args=(stop,), daemon=True)
    thread.start()
    try:
        port = None
        deadline = time.monotonic() + 60
        while port is None and time.monotonic() < deadline:
            beat = w.root / "serve.json"
            if beat.is_file():
                try:
                    port = json.loads(beat.read_text(encoding="utf-8")).get("ingress_port")
                except ValueError:
                    port = None
            time.sleep(0.05)
        assert isinstance(port, int) and port > 0
        assert daemon.ingress is not None and daemon.ingress.port == port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.request("POST", "/in/note-push", body=b"via http\n", headers=NOTE)
            resp = conn.getresponse()
            assert resp.status == 202
            assert json.loads(resp.read())["id"].startswith("ig-")
        finally:
            conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.request("GET", "/in/note-push")
            resp = conn.getresponse()
            assert resp.status == 405 and resp.getheader("Allow") == "POST"
            resp.read()
        finally:
            conn.close()
    finally:
        stop.set()
        thread.join(60)
        for record in daemon.instances.list():
            if record.state != "stopped":
                daemon.instances.stop(record.id)
        daemon.instances.close()
        w.close()
