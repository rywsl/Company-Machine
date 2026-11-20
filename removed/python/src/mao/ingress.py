"""入站网关（spec/system/ingress.md）。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError

_ROUTE = re.compile(r"^/in/([a-z][a-z0-9-]{0,62})$")
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SECRET = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class IngressConfig:
    port: int
    bind: str = "127.0.0.1"
    max_body_kb: int = 256
    rate_per_min: int = 60


@dataclass(frozen=True)
class Route:
    name: str
    owner: str
    auth: str
    secret: str
    header: str
    deliver: str
    port: str | None
    file: str | None
    error: str | None


def load_ingress_config(root: Path) -> IngressConfig | None:
    path = Path(root) / "factory.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FactoryError("SERVE_CONFIG_INVALID", "factory.json 无法解析", field="ingress") from exc
    if not isinstance(body, dict) or "ingress" not in body or body.get("ingress") is None:
        return None
    section = body.get("ingress")
    if not isinstance(section, dict):
        raise FactoryError("SERVE_CONFIG_INVALID", "ingress 段不合法", field="ingress")
    if "port" not in section:
        raise FactoryError("SERVE_CONFIG_INVALID", "缺少 ingress.port", field="port")
    port = _port(section.get("port"))
    if port is None:
        raise FactoryError("SERVE_CONFIG_INVALID", "ingress.port 不合法", field="port")
    bind = "127.0.0.1"
    if "bind" in section:
        raw = section.get("bind")
        if not isinstance(raw, str) or not raw:
            raise FactoryError("SERVE_CONFIG_INVALID", "ingress.bind 不合法", field="bind")
        bind = raw
    max_body = 256
    if "max_body_kb" in section:
        parsed = _positive(section.get("max_body_kb"))
        if parsed is None:
            raise FactoryError("SERVE_CONFIG_INVALID", "ingress.max_body_kb 不合法", field="max_body_kb")
        max_body = parsed
    rate = 60
    if "rate_per_min" in section:
        parsed = _positive(section.get("rate_per_min"))
        if parsed is None:
            raise FactoryError("SERVE_CONFIG_INVALID", "ingress.rate_per_min 不合法", field="rate_per_min")
        rate = parsed
    return IngressConfig(port, bind, max_body, rate)


def routes(snapshot: Any) -> dict[str, Route]:
    grouped: dict[str, list[Route]] = {}
    for key in snapshot.keys("Station"):
        spec = snapshot.spec(key)
        ingress = spec.get("ingress") or {}
        if not isinstance(ingress, Mapping):
            continue
        for name, body in ingress.items():
            grouped.setdefault(str(name), []).append(_route(snapshot, key, str(name), body))
    table: dict[str, Route] = {}
    for key in snapshot.keys("Agent"):
        spec = snapshot.spec(key)
        ingress = (spec.get("subscribe") or {}).get("ingress") or {}
        if not isinstance(ingress, Mapping):
            continue
        for name, body in ingress.items():
            grouped.setdefault(str(name), []).append(_agent_route(snapshot, key, str(name), body))
    for name, items in grouped.items():
        if len(items) > 1:
            table[name] = replace(items[0], error="INGRESS_ROUTE_DUPLICATE")
        else:
            table[name] = items[0]
    return table


class IngressServer:
    def __init__(
        self,
        ctx: Any,
        epochs: Any,
        config: IngressConfig,
        *,
        instances: Any = None,
        runner_for: Callable[[Any], Any] | None = None,
    ) -> None:
        self.ctx = ctx
        self.epochs = epochs
        self.config = config
        self.instances = instances
        if runner_for is None:
            from mao.runner import open_runner

            def runner_for(snapshot: Any) -> Any:
                return open_runner(ctx, snapshot)

        self.runner_for = runner_for
        self._port = config.port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._epoch = ""
        self._snapshot: Any = None
        self._routes: dict[str, Route] = {}
        self._hits: dict[str, list[Any]] = {}

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        if self._thread is not None:
            return
        httpd = ThreadingHTTPServer((self.config.bind, self.config.port), _Handler)
        httpd.app = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._port = int(httpd.server_address[1])
        self._thread = threading.Thread(target=httpd.serve_forever, name="mao-ingress", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd = self._httpd
        thread = self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(5)

    def handle(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            return self._handle(method, path, headers, body)

    def _handle(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        if method.upper() != "POST":
            return 405, {"code": "INGRESS_METHOD"}
        route_name = _route_name(path)
        table = self._table()
        if route_name is None or route_name not in table:
            return self._reject("" if route_name is None else route_name, "INGRESS_ROUTE_NOT_FOUND", 404)
        route = table[route_name]
        if route.error:
            return self._reject(route_name, route.error, 503)
        size = _content_length(headers)
        if size is None:
            return self._reject(route_name, "INGRESS_BAD_REQUEST", 400)
        if size > self.config.max_body_kb * 1024:
            return self._reject(route_name, "INGRESS_TOO_LARGE", 413)
        now = self.ctx.clock.now()
        if not self._allow(route_name, now):
            return self._reject(route_name, "INGRESS_RATE_LIMITED", 429)
        from mao.egress import SecretStore

        try:
            secret = SecretStore(self.ctx.root).inject([route.secret])[route.secret]
        except FactoryError as exc:
            if exc.code == "SECRET_NOT_FOUND":
                return self._reject(route_name, "INGRESS_SECRET_MISSING", 503)
            raise
        if not _authorized(route, headers, body, secret):
            return self._reject(route_name, "INGRESS_AUTH_FAILED", 401)
        request_id = self.ctx.ids.new("ig")
        if route.deliver == "bean":
            target = self._deliver_bean(route, request_id, body)
            if isinstance(target, tuple):
                return target
        elif route.deliver == "instance":
            target = self._deliver_instance(route, request_id, headers, body)
            if isinstance(target, tuple):
                return target
        elif route.deliver == "agent":
            target = self._deliver_agent(route, request_id, headers, body)
            if isinstance(target, tuple):
                return target
        else:
            return self._reject(route_name, "INGRESS_ROUTE_INVALID", 503)
        self._hits.setdefault(route_name, []).append(now)
        self._event(
            "ingress.received",
            {
                "route": route_name,
                "id": request_id,
                "bytes": len(body),
                "deliver": route.deliver,
                "target": target,
            },
        )
        return 202, {"id": request_id}

    def _deliver_bean(self, route: Route, request_id: str, body: bytes) -> str | tuple[int, dict[str, Any]]:
        folder = self.ctx.root / "ingress" / "tmp" / request_id
        target = folder / str(route.file or "payload")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        try:
            from mao.instances import emit

            runner = self.runner_for(self._snapshot)
            return emit(
                runner,
                station=route.owner,
                port=str(route.port or ""),
                source=folder,
                instance_id=None,
                trigger={"on": "ingress", "route": route.name, "request": request_id},
            )
        except FactoryError as exc:
            if exc.code == "EMIT_INVALID":
                return self._reject(route.name, "INGRESS_PAYLOAD_INVALID", 422)
            raise
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _deliver_instance(
        self, route: Route, request_id: str, headers: Mapping[str, str], body: bytes
    ) -> str | tuple[int, dict[str, Any]]:
        rows = self.ctx.db.query(
            "SELECT id, state FROM instances WHERE station = ? AND mode = 'resident' AND state != 'stopped' "
            "ORDER BY id",
            (route.owner,),
        )
        if not rows or str(rows[0]["state"]) not in ("ready", "busy"):
            return self._reject(route.name, "INSTANCE_NOT_READY", 503)
        instance_id = str(rows[0]["id"])
        inbox = self.ctx.root / "instances" / instance_id / "main" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        name = f"{_next_seq(inbox):010d}-{request_id}.json"
        payload = {
            "id": request_id,
            "route": route.name,
            "received_at": utc_iso(self.ctx.clock.now()),
            "content_type": _header(headers, "content-type") or "",
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
        tmp = inbox / f"{name}.tmp"
        tmp.write_text(canonical_json(payload), encoding="utf-8")
        tmp.replace(inbox / name)
        return instance_id

    def _deliver_agent(
        self, route: Route, request_id: str, headers: Mapping[str, str], body: bytes
    ) -> str | tuple[int, dict[str, Any]]:
        from mao.agents import Mailbox

        doc = self._snapshot.doc(route.owner)
        name = str((doc.get("metadata") or {}).get("name") or route.owner)
        payload: dict[str, Any] = {
            "id": request_id,
            "route": route.name,
            "content_type": _header(headers, "content-type") or "",
        }
        try:
            payload["text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            payload["body_b64"] = base64.b64encode(body).decode("ascii")
        try:
            Mailbox(self.ctx).post(name, "ingress", f"ingress:{route.name}", payload)
        except FactoryError as exc:
            if exc.code == "AGENT_MESSAGE_TOO_LARGE":
                return self._reject(route.name, "INGRESS_TOO_LARGE", 413)
            raise
        return name

    def _table(self) -> dict[str, Route]:
        current = str(self.epochs.current())
        if current != self._epoch:
            self._snapshot = self.epochs.materialize(current)
            self._routes = routes(self._snapshot)
            self._epoch = current
        return self._routes

    def _allow(self, route: str, now: Any) -> bool:
        cutoff = now - timedelta(seconds=60)
        kept = [item for item in self._hits.get(route, []) if item > cutoff]
        self._hits[route] = kept
        return len(kept) < self.config.rate_per_min

    def _reject(self, route: str, code: str, status: int) -> tuple[int, dict[str, Any]]:
        self._event("ingress.rejected", {"route": route, "code": code, "status": status})
        return status, {"code": code}

    def _event(self, kind: str, data: Mapping[str, Any]) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
                (self.ctx.ids.new("ev"), utc_iso(self.ctx.clock.now()), kind, canonical_json(dict(data))),
            )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def do_PUT(self) -> None:  # noqa: N802
        self._serve()

    def do_DELETE(self) -> None:  # noqa: N802
        self._serve()

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _serve(self) -> None:
        app: IngressServer = self.server.app  # type: ignore[attr-defined]
        headers = {key: value for key, value in self.headers.items()}
        size = _content_length(headers)
        body = b""
        if size is not None and size <= app.config.max_body_kb * 1024:
            body = self.rfile.read(size)
        status, payload = app.handle(self.command, self.path, headers, body)
        raw = canonical_json(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if status == 405:
            self.send_header("Allow", "POST")
        self.end_headers()
        self.wfile.write(raw)


def _agent_route(snapshot: Any, agent_key: str, name: str, body: Any) -> Route:
    auth = body.get("auth") if isinstance(body, Mapping) else {}
    if not isinstance(auth, Mapping):
        auth = {}
    kind = str(auth.get("kind") or "")
    header = "Authorization" if kind == "bearer" else str(auth.get("header") or "")
    error = None
    doc = snapshot.doc(agent_key)
    lifecycle = (doc.get("metadata") or {}).get("lifecycle", "active")
    if lifecycle not in (None, "active") or kind not in ("bearer", "hmac-sha256"):
        error = "INGRESS_ROUTE_INVALID"
    else:
        from mao.agents import agent_config

        try:
            agent_config(snapshot, str(doc["metadata"]["name"]))
        except FactoryError:
            error = "INGRESS_ROUTE_INVALID"
    return Route(name, agent_key, kind, str(auth.get("secret") or ""), header, "agent", None, None, error)


def _route(snapshot: Any, station_key: str, name: str, body: Any) -> Route:
    spec = snapshot.spec(station_key)
    auth = body.get("auth") if isinstance(body, Mapping) else {}
    if not isinstance(auth, Mapping):
        auth = {}
    kind = str(auth.get("kind") or "")
    header = "Authorization" if kind == "bearer" else str(auth.get("header") or "")
    deliver = str(body.get("deliver") or "") if isinstance(body, Mapping) else ""
    port = body.get("port") if isinstance(body, Mapping) else None
    declared = body.get("file") if isinstance(body, Mapping) else None
    file = str(declared) if isinstance(declared, str) and declared else _default_file(snapshot, spec, port)
    return Route(
        name,
        station_key,
        kind,
        str(auth.get("secret") or ""),
        header,
        deliver,
        None if port is None else str(port),
        file,
        _route_error(snapshot, station_key, name, body if isinstance(body, Mapping) else {}),
    )


def _route_error(snapshot: Any, station_key: str, name: str, body: Mapping[str, Any]) -> str | None:
    if _NAME.fullmatch(name) is None:
        return "INGRESS_ROUTE_INVALID"
    doc = snapshot.doc(station_key)
    lifecycle = (doc.get("metadata") or {}).get("lifecycle", "active")
    if lifecycle not in (None, "active"):
        return "INGRESS_ROUTE_INVALID"
    spec = snapshot.spec(station_key)
    auth = body.get("auth") if isinstance(body.get("auth"), Mapping) else {}
    kind = str(auth.get("kind") or "")
    secret = str(auth.get("secret") or "")
    if kind not in ("bearer", "hmac-sha256") or _SECRET.fullmatch(secret) is None:
        return "INGRESS_ROUTE_INVALID"
    if kind == "hmac-sha256" and not auth.get("header"):
        return "INGRESS_ROUTE_INVALID"
    deliver = str(body.get("deliver") or "")
    machine = snapshot.spec(snapshot.key("Machine", spec["machine"]))
    if deliver == "bean":
        if spec.get("role") != "source":
            return "INGRESS_ROUTE_INVALID"
        port = body.get("port")
        recipe = (machine.get("recipes") or {}).get(spec.get("recipe")) or {}
        outputs = recipe.get("outputs") or {}
        if not isinstance(port, str) or port not in outputs:
            return "INGRESS_ROUTE_INVALID"
        if not body.get("file") and _default_file(snapshot, spec, port) is None:
            return "INGRESS_ROUTE_INVALID"
        return None
    if deliver == "instance":
        from mao.instances import instance_config

        try:
            mode = instance_config(machine).mode
        except FactoryError:
            return "INGRESS_ROUTE_INVALID"
        if mode != "resident":
            return "INGRESS_ROUTE_INVALID"
        return None
    return "INGRESS_ROUTE_INVALID"


def _default_file(snapshot: Any, spec: Mapping[str, Any], port: Any) -> str | None:
    if not isinstance(port, str) or not port:
        return None
    machine = snapshot.spec(snapshot.key("Machine", spec["machine"]))
    recipe = (machine.get("recipes") or {}).get(spec.get("recipe")) or {}
    decl = (recipe.get("outputs") or {}).get(port)
    if decl is None:
        decl = (spec.get("outputs") or {}).get(port)
    if decl is None:
        return None
    type_expr = decl.get("type") if isinstance(decl, Mapping) else str(decl)
    expr = str(type_expr)
    if "@" in expr:
        name, version = expr.rsplit("@", 1)
        expr = f"{name}@{version.split('.', 1)[0]}"
    try:
        payload = snapshot.spec(snapshot.type_key(expr)).get("payload") or []
    except FactoryError:
        return None
    for rule in payload:
        path = str(rule.get("path") or "")
        if rule.get("required", True) and path and "*" not in path:
            return path
    return None


def _route_name(path: str) -> str | None:
    match = _ROUTE.fullmatch(path.split("?", 1)[0])
    if match is None:
        return None
    return match.group(1)


def _content_length(headers: Mapping[str, str]) -> int | None:
    raw = _header(headers, "content-length")
    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _authorized(route: Route, headers: Mapping[str, str], body: bytes, secret: str) -> bool:
    if route.auth == "bearer":
        got = _header(headers, "authorization") or ""
        return _same(got, "Bearer " + secret)
    if route.auth != "hmac-sha256":
        return False
    raw = _header(headers, route.header.lower()) or ""
    if raw.lower().startswith("sha256="):
        raw = raw[7:]
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _same(raw.lower(), digest.lower())


def _same(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _next_seq(inbox: Path) -> int:
    best = 0
    for item in inbox.iterdir():
        stem = item.name[:-4] if item.name.endswith(".tmp") else item.stem
        head = stem.split("-", 1)[0]
        if head.isdigit():
            best = max(best, int(head))
    return best + 1


def _port(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 65535:
        return None
    return value


def _positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value
