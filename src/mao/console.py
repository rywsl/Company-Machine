"""本机只读 Web 控制台。"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from mao.base import canonical_bytes
from mao.commands.common import to_jsonable
from mao.errors import FactoryError
from mao.foreman import ForemanQuery, WakeQueue
from mao.status import factory_status, render_metrics, tail_events

TOKEN_FILE = "console.token"
COOKIE = "mao_console"
DEFAULT_PORT = 8090

_LOG = logging.getLogger("mao.console")
_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
_NAV = (
    "<nav>"
    '<a href="/">状态</a> '
    '<a href="/runs">runs</a> '
    '<a href="/changesets">changesets</a> '
    '<a href="/wakes">wakes</a> '
    '<a href="/sessions">sessions</a> '
    '<a href="/approvals">approvals</a> '
    '<a href="/events">events</a> '
    '<a href="/metrics">metrics</a>'
    "</nav>"
)
_JSON = "application/json; charset=utf-8"
_HTML = "text/html; charset=utf-8"
_TEXT = "text/plain; charset=utf-8"
_METRICS = "text/plain; version=0.0.4; charset=utf-8"


class _BadRequest(Exception):
    def __init__(self, field: str) -> None:
        self.field = field


def load_token(root: Path) -> str:
    path = Path(root) / TOKEN_FILE
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise FactoryError("CONSOLE_TOKEN_INVALID", "令牌过短", path=str(path))
        return token
    token = secrets.token_urlsafe(32)
    part = Path(root) / "tmp" / f"console.token.{os.getpid()}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_text(token + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(part, 0o600)
    os.replace(part, path)
    if os.name == "posix":
        os.chmod(path, 0o600)
    return token


def listed_wakes(ctx: Any, status: str | None = None) -> list[dict[str, Any]]:
    wakes = WakeQueue(ctx).list(status=status)
    updated = {str(row["id"]): row["updated_at"] for row in ctx.db.query("SELECT id, updated_at FROM wakes")}
    return [
        {
            "id": wake.id,
            "signal": wake.signal,
            "subject": wake.subject,
            "status": wake.status,
            "attempts": wake.attempts,
            "not_before": wake.not_before,
            "at": wake.at,
            "updated_at": updated.get(wake.id),
        }
        for wake in wakes
    ]


class _BoundServer(HTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        super().server_bind()

    def get_request(self) -> tuple[Any, Any]:
        conn, addr = super().get_request()
        conn.settimeout(10)
        return conn, addr


class ConsoleServer:
    def __init__(
        self,
        ctx: Any,
        *,
        token: str,
        epochs: Any | None = None,
        pipeline: Any | None = None,
        snapshot: Any | None = None,
    ) -> None:
        self.ctx = ctx
        self.token = token
        self.epochs = epochs
        self.pipeline = pipeline
        self.snapshot = snapshot
        self._httpd: _BoundServer | None = None
        self._port = 0

    def start(self, port: int = DEFAULT_PORT) -> int:
        try:
            httpd = _BoundServer(("127.0.0.1", port), _Handler)
        except OSError as exc:
            raise FactoryError("CONSOLE_BIND_FAILED", "端口绑定失败", port=port) from exc
        httpd.app = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._port = int(httpd.server_address[1])
        return self._port

    def serve_forever(self) -> None:
        if self._httpd is None:
            raise FactoryError("CONSOLE_BIND_FAILED", "控制台尚未绑定", port=self._port)
        self._httpd.serve_forever()

    def close(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        self._httpd = None
        httpd.shutdown()
        httpd.server_close()

    def dispatch(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            self._dispatch(handler)
        except _BadRequest as exc:
            self._json(handler, 400, {"error": "bad_request", "field": exc.field})
        except FactoryError as exc:
            if exc.code.endswith("_NOT_FOUND"):
                self._missing(handler)
                return
            if exc.code == "EVENTS_QUERY_INVALID":
                self._json(handler, 400, {"error": "bad_request", "field": exc.detail.get("field", "")})
                return
            _LOG.exception("控制台请求失败 code=%s", exc.code)
            self._json(handler, 500, {"error": "internal"})
        except Exception:
            _LOG.exception("控制台请求失败")
            self._json(handler, 500, {"error": "internal"})

    def _dispatch(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if not self._host_ok(handler):
            self._send(handler, 421, b"misdirected", _TEXT)
            return
        public = path in {"/login", "/healthz"}
        if not public and not self._authorized(handler):
            if path.startswith("/api/") or path == "/metrics":
                self._send(
                    handler,
                    401,
                    canonical_bytes({"error": "unauthorized"}),
                    _JSON,
                    {"WWW-Authenticate": "Bearer"},
                )
            else:
                self._send(handler, 303, b"", _TEXT, {"Location": "/login"})
            return
        allowed = ("GET", "HEAD", "POST") if path == "/login" else ("GET", "HEAD")
        if handler.command not in allowed:
            allow = "GET, POST" if path == "/login" else "GET"
            self._send(handler, 405, b"", _TEXT, {"Allow": allow})
            return
        if path == "/healthz":
            self._send(handler, 200, b"ok", "text/plain")
            return
        if path == "/login":
            self._login(handler)
            return
        if path == "/metrics":
            self._send(handler, 200, self._metrics().encode("utf-8"), _METRICS)
            return
        if path.startswith("/api/"):
            self._api(handler, path[len("/api") :], query)
            return
        self._page(handler, path, query)

    def _login(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.command == "POST":
            form = _form(handler)
            given = form.get("token", "")
            if given and hmac.compare_digest(given, self.token):
                cookie = f"{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/"
                self._send(handler, 303, b"", _HTML, {"Location": "/", "Set-Cookie": cookie})
                return
            self._send(
                handler,
                401,
                _html(
                    "登录", '<form method="post" action="/login"><input type="password" name="token"></form>'
                ),
                _HTML,
            )
            return
        self._send(
            handler,
            200,
            _html("登录", '<form method="post" action="/login"><input type="password" name="token"></form>'),
            _HTML,
        )

    def _api(self, handler: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> None:
        parts = [item for item in path.split("/") if item]
        if parts == ["status"]:
            self._json(handler, 200, self._status())
            return
        if parts == ["runs"]:
            self._json(handler, 200, self._runs(_one(query, "state")))
            return
        if len(parts) == 2 and parts[0] == "runs":
            self._json(handler, 200, self._run(parts[1]))
            return
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "nodes":
            self._json(handler, 200, self._nodes(parts[1]))
            return
        if len(parts) == 2 and parts[0] == "jobs":
            self._json(handler, 200, self._job(parts[1]))
            return
        if len(parts) == 2 and parts[0] == "beans":
            self._json(handler, 200, self._query().bean_get(parts[1]))
            return
        if len(parts) == 3 and parts[0] == "beans" and parts[2] == "lineage":
            direction = _one(query, "direction")
            if direction not in {"up", "down"}:
                raise _BadRequest("direction")
            self._json(handler, 200, self._query().bean_lineage(parts[1], direction))
            return
        if parts == ["changesets"]:
            self._json(handler, 200, self._changesets(_one(query, "state")))
            return
        if len(parts) == 2 and parts[0] == "changesets":
            self._json(handler, 200, self._changeset(parts[1]))
            return
        if parts == ["wakes"]:
            self._json(handler, 200, listed_wakes(self.ctx, status=_one(query, "status")))
            return
        if parts == ["sessions"]:
            self._json(handler, 200, self._sessions(_one(query, "state")))
            return
        if len(parts) == 2 and parts[0] == "sessions":
            self._json(handler, 200, self._session(parts[1]))
            return
        if parts == ["approvals"]:
            self._json(handler, 200, self._approvals(_one(query, "status")))
            return
        if parts == ["events"]:
            self._json(handler, 200, self._events(query))
            return
        self._missing(handler)

    def _page(self, handler: BaseHTTPRequestHandler, path: str, query: dict[str, list[str]]) -> None:
        parts = [item for item in path.split("/") if item]
        if not parts:
            status = self._status()
            body = _html("状态", _dl(status))
        elif parts == ["runs"]:
            limit = _bounded(query, "limit", 100, 1000)
            runs = self._runs(_one(query, "state"))[-limit:]
            body = _html("runs", "".join(_link("/runs/", item["id"]) + "<br>" for item in runs))
        elif len(parts) == 2 and parts[0] == "runs":
            run = self._run(parts[1])
            nodes = self._nodes(parts[1])
            links = "".join(
                _link("/jobs/", str(node["job_id"])) + "<br>" for node in nodes if node.get("job_id")
            )
            body = _html(parts[1], _dl(run) + links)
        elif len(parts) == 2 and parts[0] == "jobs":
            job = self._job(parts[1])
            transcript = _tail(_query_transcript(self._query(), parts[1]))
            body = _html(parts[1], _dl(job) + "<pre>" + _esc(transcript) + "</pre>")
        elif len(parts) == 2 and parts[0] == "beans":
            body = _html(parts[1], _dl(self._bean_page(parts[1])))
        elif parts == ["changesets"]:
            items = self._changesets(None)
            body = _html("changesets", "".join(_link("/changesets/", item["id"]) + "<br>" for item in items))
        elif len(parts) == 2 and parts[0] == "changesets":
            body = _html(parts[1], _dl(self._changeset(parts[1])))
        elif parts == ["wakes"]:
            rows = listed_wakes(self.ctx, status=_one(query, "status"))
            body = _html("wakes", "".join(_esc(row["subject"]) + "<br>" for row in rows))
        elif parts == ["sessions"]:
            rows = self._sessions(_one(query, "state"))
            body = _html("sessions", "".join(_link("/sessions/", item["id"]) + "<br>" for item in rows))
        elif len(parts) == 2 and parts[0] == "sessions":
            found = self._session(parts[1])
            body = _html(parts[1], _dl(found) + "<pre>" + _esc(found["transcript"]) + "</pre>")
        elif parts == ["approvals"]:
            status = _one(query, "status") or "pending"
            body = _html("approvals", _dl(self._approvals(status)))
        elif parts == ["events"]:
            body = _html("events", _dl(self._events(query)))
        else:
            self._missing(handler)
            return
        self._send(handler, 200, body, _HTML)

    def _status(self) -> dict[str, Any]:
        epochs, pipeline = self._bound()
        # factory status 用 SystemClock 打开新的 Context。规范要求两边 JSON 除 at 外相等。
        return self._cli_clock(lambda: factory_status(self.ctx, epochs=epochs, pipeline=pipeline))

    def _metrics(self) -> str:
        epochs, pipeline = self._bound()
        return self._cli_clock(lambda: render_metrics(self.ctx, epochs=epochs, pipeline=pipeline))

    def _cli_clock(self, fn: Any) -> Any:
        from mao.base import SystemClock

        previous = self.ctx.clock
        self.ctx.clock = SystemClock()
        try:
            return fn()
        finally:
            self.ctx.clock = previous

    def _bound(self) -> tuple[Any, Any]:
        epochs = self.epochs
        pipeline = self.pipeline
        if epochs is None:
            return None, None
        if pipeline is None:
            from mao.governance.changesets import ChangePipeline

            pipeline = ChangePipeline(self.ctx, epochs)
        return epochs, pipeline

    def _snapshot(self, epoch: str | None = None) -> Any:
        if self.epochs is not None:
            return self.epochs.materialize(epoch or self.epochs.current())
        if self.snapshot is not None:
            return self.snapshot
        from mao.snapshot import Snapshot

        return Snapshot.load(self.ctx.root / "registry")

    def _lines(self, epoch: str | None = None) -> Any:
        from mao.runs import open_line_runner

        snapshot = self._snapshot(epoch)
        return open_line_runner(self.ctx, snapshot)

    def _runs(self, state: str | None) -> list[dict[str, Any]]:
        return to_jsonable(self._lines().list(state=state))

    def _run(self, run_id: str) -> dict[str, Any]:
        run = self._lines().get(run_id)
        if run.epoch:
            run = self._lines(run.epoch).get(run_id)
        return to_jsonable(run)

    def _nodes(self, run_id: str) -> list[dict[str, Any]]:
        run = self._lines().get(run_id)
        return to_jsonable(self._lines(run.epoch or None).nodes(run_id))

    def _job(self, job_id: str) -> dict[str, Any]:
        return to_jsonable(self._query().job_get(job_id))

    def _query(self) -> ForemanQuery:
        epochs = self.epochs
        if epochs is None:
            from mao.governance.epochs import EpochStore

            epochs = EpochStore(self.ctx)
        return ForemanQuery(self.ctx, epochs)

    def _bean_page(self, bean_id: str) -> dict[str, Any]:
        bean = self.ctx.warehouse.get(bean_id)
        return {
            "id": bean.id,
            "type": bean.type,
            "sealed": bean.sealed,
            "facts": bean.facts,
            "meta": bean.meta,
            "labels": self.ctx.warehouse.labels(bean_id),
            "upstream": _one_hop(self.ctx, bean_id, "up"),
            "downstream": _one_hop(self.ctx, bean_id, "down"),
        }

    def _changesets(self, state: str | None) -> list[dict[str, Any]]:
        pipeline = self._bound()[1]
        if pipeline is None:
            return []
        return to_jsonable(pipeline.list(state=state))

    def _changeset(self, changeset_id: str) -> dict[str, Any]:
        pipeline = self._bound()[1]
        if pipeline is None:
            raise FactoryError("CHANGESET_NOT_FOUND", "找不到 ChangeSet", id=changeset_id)
        return to_jsonable(pipeline.get(changeset_id))

    def _sessions(self, state: str | None) -> list[dict[str, Any]]:
        from mao.supervisor import list_sessions

        return [item.to_json() for item in list_sessions(self.ctx, state=state)]

    def _session(self, session_id: str) -> dict[str, Any]:
        found = [item for item in self._sessions(None) if item["id"] == session_id]
        if not found:
            raise FactoryError("JOB_NOT_FOUND", "找不到会话", id=session_id)
        record = dict(found[0])
        record["transcript"] = _session_transcript(self.ctx.root, session_id)
        return record

    def _approvals(self, status: str | None) -> list[dict[str, Any]]:
        from mao.approvals import Approvals

        return to_jsonable(Approvals(self.ctx, self._snapshot()).list(status=status))

    def _events(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        limit = _bounded(query, "limit", 100, 10000)
        return tail_events(self.ctx, since=_one(query, "since"), kind=_one(query, "kind"), limit=limit)

    def _host_ok(self, handler: BaseHTTPRequestHandler) -> bool:
        host = handler.headers.get("Host")
        return host in {f"127.0.0.1:{self._port}", f"localhost:{self._port}"}

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        presented = _bearer(handler.headers.get("Authorization")) or _cookie(handler.headers.get("Cookie"))
        if presented is None:
            return False
        return hmac.compare_digest(presented, self.token)

    def _missing(self, handler: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        if path.startswith("/api/"):
            self._json(handler, 404, {"error": "not_found"})
            return
        self._send(handler, 404, _html("找不到", "<p>not_found</p>"), _HTML)

    def _json(self, handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        self._send(handler, status, canonical_bytes(to_jsonable(payload)), _JSON)

    def _send(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in _HEADERS.items():
            handler.send_header(name, value)
        for name, value in (extra or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536 or not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            self.server.app.dispatch(self)  # type: ignore[attr-defined]
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True
        except Exception:
            _LOG.exception("控制台请求失败")
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        _LOG.info("%s", fmt % args)


def _form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise _BadRequest("token") from exc
    if length < 0 or length > 4096:
        raise _BadRequest("token")
    parsed = urllib.parse.parse_qs(
        handler.rfile.read(length).decode("utf-8", errors="replace"), keep_blank_values=True
    )
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _one(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    return values[0]


def _bounded(query: dict[str, list[str]], name: str, default: int, maximum: int) -> int:
    raw = _one(query, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise _BadRequest(name) from exc
    if value < 1 or value > maximum:
        raise _BadRequest(name)
    return value


def _bearer(header: str | None) -> str | None:
    if header is None or not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :]
    return token or None


def _cookie(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip() == COOKIE:
            return value.strip() or None
    return None


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _link(prefix: str, ident: str) -> str:
    href = _esc(urllib.parse.quote(ident, safe=""))
    return f'<a href="{prefix}{href}">{_esc(ident)}</a>'


def _html(title: str, body: str) -> bytes:
    page = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>'
        + _esc(title)
        + "</title></head><body>"
        + _NAV
        + body
        + "</body></html>"
    )
    return page.encode("utf-8")


def _dl(value: Any) -> str:
    return "<pre>" + _esc(canonical_bytes(to_jsonable(value)).decode("utf-8")) + "</pre>"


def _tail(text: str) -> str:
    lines = text.splitlines()[-200:]
    clipped = [line[:4000] + "…" if len(line) > 4000 else line for line in lines]
    return "\n".join(clipped)


def _query_transcript(query: ForemanQuery, job_id: str) -> str:
    return query.job_transcript(job_id)


def _session_transcript(root: Path, session_id: str) -> str:
    path = Path(root) / "foreman" / "sessions" / session_id / "logs" / "transcript.jsonl"
    if not path.is_file():
        return "无 transcript"
    return _tail(path.read_text(encoding="utf-8", errors="replace"))


def _one_hop(ctx: Any, bean_id: str, direction: str) -> list[str]:
    column = "out" if direction == "up" else "in"
    other = "in" if direction == "up" else "out"
    rows = ctx.db.query(
        "SELECT job_id FROM lineage WHERE bean_id = ? AND direction = ?",
        (bean_id, column),
    )
    found: list[str] = []
    for row in rows:
        nxt = ctx.db.query(
            "SELECT bean_id FROM lineage WHERE job_id = ? AND direction = ?",
            (row["job_id"], other),
        )
        found.extend(str(item["bean_id"]) for item in nxt if str(item["bean_id"]) != bean_id)
    return sorted(set(found))
