"""Local, single-user Web App server for M18."""

from __future__ import annotations

import getpass
import json
import mimetypes
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mao.errors import FactoryError
from mao.talk import post_human
from mao.webapp.read_model import WebReadModel

DEFAULT_PORT = 8091
_STATIC = Path(__file__).with_name("static")
_JSON = "application/json; charset=utf-8"


class _WebError(Exception):
    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class _WebHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class WebServer:
    def __init__(self, ctx: Any, *, snapshot: Any | None = None) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.model = WebReadModel(ctx, snapshot)
        self._httpd: _WebHTTPServer | None = None
        self._port = 0

    @property
    def port(self) -> int:
        return self._port

    def start(self, port: int = DEFAULT_PORT) -> int:
        try:
            httpd = _WebHTTPServer(("127.0.0.1", int(port)), _Handler)
        except OSError as exc:
            raise FactoryError("CONSOLE_BIND_FAILED", "Web App 端口绑定失败", port=port) from exc
        httpd.app = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._port = int(httpd.server_address[1])
        return self._port

    def serve_forever(self) -> None:
        if self._httpd is None:
            raise FactoryError("CONSOLE_BIND_FAILED", "Web App 尚未绑定", port=self._port)
        self._httpd.serve_forever()

    def close(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        self._httpd = None
        httpd.shutdown()
        httpd.server_close()

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(handler.path)
        try:
            if handler.command == "OPTIONS":
                self._send(handler, 204, b"", "text/plain; charset=utf-8")
                return
            if handler.command == "GET":
                if parsed.path.startswith("/api/web/") or parsed.path == "/healthz":
                    self._api_get(handler, parsed.path, parse_qs(parsed.query))
                else:
                    self._static(handler, parsed.path)
                return
            if handler.command == "POST" and parsed.path == "/api/web/talk":
                self._talk(handler)
                return
            self._error(handler, 405, "WEB_METHOD_NOT_ALLOWED", "请求方法不支持", {"method": handler.command})
        except _WebError as exc:
            status = (
                404 if exc.code == "WEB_NOT_FOUND" else 405 if exc.code == "WEB_METHOD_NOT_ALLOWED" else 400
            )
            self._error(handler, status, exc.code, exc.message, exc.detail)
        except FactoryError as exc:
            status = (
                409
                if exc.code in {"TALK_SERVE_DOWN", "TALK_FOREMAN_ABSENT", "REGISTRY_NOT_CONFIGURED"}
                else 400
            )
            self._error(handler, status, exc.code, exc.message, exc.detail)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._error(handler, 400, "WEB_BAD_REQUEST", "请求无效", {"reason": type(exc).__name__})
        except Exception as exc:
            self._error(
                handler, 500, "WEB_INTERNAL_ERROR", "Web App 读取失败", {"reason": type(exc).__name__}
            )

    def _api_get(self, handler: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> None:
        if path == "/healthz":
            self._json(handler, 200, {"ok": True, "service": "mao-web", "port": self._port})
            return
        route = path.removeprefix("/api/web/")
        if route == "bootstrap":
            self._json(handler, 200, self.model.bootstrap())
            return
        if route == "graph":
            scope = _one(query, "scope") or "factory"
            if scope == "factory":
                self._json(handler, 200, self.model.factory_graph())
                return
            if scope == "workarea":
                self._json(handler, 200, self.model.workarea_graph(_one(query, "workarea")))
                return
            raise _WebError("WEB_BAD_REQUEST", "scope 不合法", field="scope")
        if route == "assets":
            self._json(handler, 200, self.model.assets(limit=_int(query, "limit", 50)))
            return
        if route == "logs":
            self._json(
                handler,
                200,
                self.model.logs(limit=_int(query, "limit", 100), since=_one(query, "since")),
            )
            return
        if route == "talk":
            self._json(handler, 200, self.model.talk(limit=_int(query, "limit", 100)))
            return
        if route == "harnesses":
            self._json(handler, 200, self.model.harnesses())
            return
        if route == "secrets":
            self._json(handler, 200, self.model.secret_resources(limit=_int(query, "limit", 100)))
            return
        if route == "constitution":
            self._json(handler, 200, self.model.constitution())
            return
        raise _WebError("WEB_NOT_FOUND", "找不到 Web App API", path=path)

    def _talk(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        if length < 1 or length > 64 * 1024:
            raise _WebError("WEB_BAD_REQUEST", "请求体长度不合法", field="Content-Length")
        try:
            body = json.loads(handler.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _WebError("WEB_BAD_REQUEST", "请求体不是合法 JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("text"), str) or not body["text"].strip():
            raise _WebError("WEB_BAD_REQUEST", "text 必须是非空字符串", field="text")
        message = post_human(self.ctx, body["text"].strip(), by=getpass.getuser())
        response = message.to_json()
        response["body"] = WebReadModel._safe_text(response.get("body"), 1200)
        response.pop("mailbox_id", None)
        self._json(handler, 202, {"state": "queued", "message": response})

    def _static(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.removeprefix("/")
        candidate = (_STATIC / relative).resolve()
        root = _STATIC.resolve()
        if candidate != root and root not in candidate.parents:
            raise _WebError("WEB_NOT_FOUND", "找不到静态资源", path=path)
        if not candidate.is_file():
            raise _WebError("WEB_NOT_FOUND", "找不到静态资源", path=path)
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send(handler, 200, body, content_type)

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        WebServer._send(handler, status, body, _JSON)

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        )
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)

    @staticmethod
    def _error(handler: BaseHTTPRequestHandler, status: int, code: str, message: str, detail: Any) -> None:
        WebServer._json(handler, status, {"code": code, "message": message, "detail": detail or {}})


class _Handler(BaseHTTPRequestHandler):
    server_version = "mao-web/1"

    def do_GET(self) -> None:
        self.server.app.handle(self)  # type: ignore[attr-defined]

    def do_POST(self) -> None:
        self.server.app.handle(self)  # type: ignore[attr-defined]

    def do_OPTIONS(self) -> None:
        self.server.app.handle(self)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def _one(query: dict[str, list[str]], name: str) -> str | None:
    found = query.get(name) or []
    return found[0] if found else None


def _int(query: dict[str, list[str]], name: str, default: int) -> int:
    value = _one(query, name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise _WebError("WEB_BAD_REQUEST", f"{name} 必须是整数", field=name) from exc
    if parsed < 1 or parsed > 500:
        raise _WebError("WEB_BAD_REQUEST", f"{name} 超出范围", field=name)
    return parsed


def serve_until_signal(server: WebServer) -> None:
    def stop(*_args: object) -> None:
        threading.Thread(target=server.close, daemon=True).start()

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        signal.signal(signal.SIGINT, previous)
        server.close()
