from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from mao.context import Context
from mao.sandbox import LocalProvider
from mao.snapshot import Snapshot
from mao.webapp.server import WebServer

FIXTURE = Path("spec/examples/registry")


def _open_server(tmp_path: Path) -> tuple[Context, WebServer, str, threading.Thread]:
    root = tmp_path / "factory"
    ctx = Context.open(root, provider=LocalProvider(root))
    server = WebServer(ctx, snapshot=Snapshot.load(FIXTURE))
    port = server.start(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return ctx, server, f"http://127.0.0.1:{port}", thread


def _get(base: str, path: str) -> tuple[int, dict[str, str], object]:
    with urllib.request.urlopen(base + path, timeout=5) as response:
        raw = response.read().decode("utf-8")
        if "application/json" in response.headers.get("Content-Type", ""):
            raw = json.loads(raw)
        return response.status, dict(response.headers), raw


def test_web_server_serves_static_health_and_bootstrap(tmp_path: Path) -> None:
    ctx, server, base, _thread = _open_server(tmp_path)
    try:
        status, headers, body = _get(base, "/")
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert "登录" not in body

        status, _headers, health = _get(base, "/healthz")
        assert status == 200
        assert health["ok"] is True

        status, _headers, bootstrap = _get(base, "/api/web/bootstrap")
        assert status == 200
        assert bootstrap["version"] == "m18-web-1"
        assert bootstrap["connection"]["state"] == "exception"
        assert bootstrap["graph"]["factory"]["scope"] == "factory"
        assert bootstrap["graph"]["factory"]["nodes"][0]["type"] == "workarea"
        assert all(
            node["type"] in {"operator", "machine"} for node in bootstrap["graph"]["workarea"]["nodes"]
        )
    finally:
        server.close()
        ctx.close()


def test_web_api_has_no_login_and_returns_explicit_errors(tmp_path: Path) -> None:
    ctx, server, base, _thread = _open_server(tmp_path)
    try:
        try:
            urllib.request.urlopen(base + "/login", timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 404
            body = json.loads(error.read().decode("utf-8"))
            assert body["code"] == "WEB_NOT_FOUND"
        else:
            raise AssertionError("Web App must not expose login")

        request = urllib.request.Request(
            base + "/api/web/talk",
            data=json.dumps({"text": "请建立一个厂区"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code in {400, 409}
            payload = json.loads(error.read().decode("utf-8"))
            assert payload["code"] in {"SUPERVISOR_CONFIG_MISSING", "TALK_FOREMAN_ABSENT", "TALK_SERVE_DOWN"}
        else:
            raise AssertionError("talk should expose the real unavailable state")
    finally:
        server.close()
        ctx.close()
