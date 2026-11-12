"""出网计划、秘密与模型代理。"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from mao.base import utc_iso
from mao.errors import FactoryError

LIVE_EGRESS = 0
_LIVE_LOCK = threading.Lock()
_DEFAULT_METHODS = {
    "providers": frozenset({"*"}),
    "web_read": frozenset({"GET", "HEAD"}),
    "publish": frozenset({"GET", "HEAD", "PUT", "POST", "DELETE"}),
    "ingress": frozenset({"GET", "HEAD"}),
}
_ALLOWED_JOB = {"PROVISIONING", "RUNNING", "AWAITING_APPROVAL"}


@dataclass(frozen=True)
class AllowRule:
    tier: str
    host: str
    methods: frozenset[str]


@dataclass(frozen=True)
class EgressPlan:
    network: str
    allow: tuple[AllowRule, ...]
    secret_names: tuple[str, ...]
    withheld: tuple[str, ...]
    env: dict[str, str]


@dataclass(frozen=True)
class ProxyEndpoints:
    host: str
    egress_port: int
    model_port: int


class SecretStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._manifest = self._load()

    def names(self) -> list[str]:
        return sorted(self._manifest)

    def secret_class(self, name: str) -> str:
        if name not in self._manifest:
            raise FactoryError("SECRET_NOT_FOUND", self.redact(f"找不到秘密 {name}"), name=name)
        return self._manifest[name]

    def inject(self, names: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in names:
            if self.secret_class(name) == "model":
                continue
            out[name] = self._read(name)
        return out

    def redact(self, text: str) -> str:
        values = sorted(
            (self._read(name) for name in self.names() if self._read(name)), key=len, reverse=True
        )
        redacted = text
        for value in values:
            redacted = redacted.replace(value, "***")
        return redacted

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, list):
            return [self.redact_obj(item) for item in obj]
        if isinstance(obj, dict):
            return {str(key): self.redact_obj(value) for key, value in obj.items()}
        return obj

    def install_log_filter(self) -> None:
        import logging

        class _Filter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                if isinstance(record.msg, str):
                    record.msg = self_store.redact(record.msg)
                return True

        self_store = self
        logging.getLogger("mao").addFilter(_Filter())

    def _load(self) -> dict[str, str]:
        path = self.root / "secrets" / "manifest.json"
        if not path.is_file():
            return {}
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FactoryError("SECRET_MANIFEST_INVALID", "秘密清单无法解析", path=str(path)) from exc
        if not isinstance(body, dict):
            raise FactoryError("SECRET_MANIFEST_INVALID", "秘密清单必须是对象", path=str(path))
        out: dict[str, str] = {}
        for name, klass in body.items():
            if klass not in ("publish", "ingress", "model", "other"):
                raise FactoryError("SECRET_CLASS_INVALID", "秘密分类不合法", name=name, **{"class": klass})
            out[str(name)] = str(klass)
        return out

    def _read(self, name: str) -> str:
        path = self.root / "secrets" / "values" / name
        if not path.is_file():
            raise FactoryError("SECRET_NOT_FOUND", self.redact(f"找不到秘密 {name}"), name=name)
        return path.read_text(encoding="utf-8").strip("\n")


def register_job_cap(db: Any, job_id: str, station_key: str, cap_usd: float | None) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO model_caps (job_id, station, cap_usd) VALUES (?, ?, ?)",
            (job_id, station_key, cap_usd),
        )


def build_egress(snapshot: Any, station_key: str, store: SecretStore) -> EgressPlan:
    station_key = snapshot.key("Station", station_key)
    station = snapshot.spec(station_key)
    policy = snapshot.spec(snapshot.key("Policy", station["policy"]))
    egress = policy.get("egress") or {}
    if egress.get("none") is True and any(key != "none" for key in egress):
        raise FactoryError(
            "EGRESS_CONFIG_INVALID", "none 不能与其他出网等级并存", station=station_key, reason="none"
        )
    rules: list[AllowRule] = []
    if not (egress.get("none") is True or not any(key != "none" for key in egress)):
        for tier in ("providers", "web_read", "publish", "ingress"):
            if tier not in egress:
                continue
            rules.extend(_rules(snapshot, station, tier, egress[tier], station_key))
    named = _secret_names(snapshot, station)
    withheld = sorted(name for name in store.names() if store.secret_class(name) == "model")
    for name in named:
        try:
            if store.secret_class(name) == "model" and name not in withheld:
                withheld.append(name)
        except FactoryError:
            continue
    withheld_t = tuple(sorted(set(withheld)))
    secret_names = tuple(sorted(name for name in named if name not in withheld_t))
    network = "egress" if rules or _agent_uses_model_proxy(snapshot, station) else "none"
    env = {} if network == "none" else {"NO_PROXY": "model-proxy,egress"}
    return EgressPlan(
        network,
        tuple(sorted(rules, key=lambda item: (item.tier, item.host, ",".join(sorted(item.methods))))),
        secret_names,
        withheld_t,
        env,
    )


def discover_proxies(ctx: Any) -> ProxyEndpoints | None:
    from mao.durable import LeaseStore

    if LeaseStore(ctx).valid("serve") is None:
        return None
    path = Path(ctx.root) / "serve.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    proxies = body.get("proxies") if isinstance(body, dict) else None
    if not isinstance(proxies, dict):
        return None
    try:
        host = str(proxies["host"])
        egress_port = int(proxies["egress_port"])
        model_port = int(proxies["model_port"])
    except (KeyError, TypeError, ValueError):
        return None
    if not proxy_online(host, model_port):
        return None
    return ProxyEndpoints(host, egress_port, model_port)


def proxy_online(host: str, port: int, *, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


def proxy_bind_host(docker_os: str, gateway: str | None) -> str:
    if "Docker Desktop" in docker_os:
        return "127.0.0.1"
    if not gateway or gateway == "0.0.0.0":
        raise FactoryError("EGRESS_PROXY_UNAVAILABLE", "没有 mao-egress 网关", reason="no-gateway")
    return gateway


def ensure_egress_network(docker: str, *, timeout_s: float) -> str:
    inspect = [docker, "network", "inspect", "mao-egress"]
    try:
        seen = subprocess.run(inspect, capture_output=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError("EGRESS_PROXY_UNAVAILABLE", "检查 mao-egress 失败", reason=str(exc)) from exc
    if seen.returncode != 0:
        create = [docker, "network", "create", "--internal", "mao-egress"]
        try:
            made = subprocess.run(create, capture_output=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FactoryError("EGRESS_PROXY_UNAVAILABLE", "创建 mao-egress 失败", reason=str(exc)) from exc
        if made.returncode != 0:
            raise FactoryError(
                "EGRESS_PROXY_UNAVAILABLE",
                "创建 mao-egress 失败",
                reason=made.stderr.decode("utf-8", errors="replace").strip(),
            )
    show = [
        docker,
        "network",
        "inspect",
        "mao-egress",
        "--format",
        "{{(index .IPAM.Config 0).Gateway}}",
    ]
    try:
        shown = subprocess.run(show, capture_output=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError("EGRESS_PROXY_UNAVAILABLE", "读取 mao-egress 网关失败", reason=str(exc)) from exc
    if shown.returncode != 0:
        raise FactoryError(
            "EGRESS_PROXY_UNAVAILABLE",
            "读取 mao-egress 网关失败",
            reason=shown.stderr.decode("utf-8", errors="replace").strip(),
        )
    gateway = shown.stdout.decode("utf-8", errors="replace").strip()
    if not gateway or gateway == "0.0.0.0":
        raise FactoryError("EGRESS_PROXY_UNAVAILABLE", "没有 mao-egress 网关", reason="no-gateway")
    return gateway


def egress_run_args(network: str) -> list[str]:
    if network == "none":
        return ["--network", "none"]
    return [
        "--network",
        "mao-egress",
        "--add-host=egress:host-gateway",
        "--add-host=model-proxy:host-gateway",
    ]


def docker_operating_system(docker: str, *, timeout_s: float) -> str:
    try:
        shown = subprocess.run(
            [docker, "info", "--format", "{{.OperatingSystem}}"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError("EGRESS_PROXY_UNAVAILABLE", "读取 docker 系统失败", reason=str(exc)) from exc
    if shown.returncode != 0:
        raise FactoryError(
            "EGRESS_PROXY_UNAVAILABLE",
            "读取 docker 系统失败",
            reason=shown.stderr.decode("utf-8", errors="replace").strip(),
        )
    return shown.stdout.decode("utf-8", errors="replace").strip()


def proxy_env(endpoints: ProxyEndpoints, *, docker: bool) -> dict[str, str]:
    if docker:
        model = f"http://model-proxy:{endpoints.model_port}"
        egress = f"http://egress:{endpoints.egress_port}"
    else:
        model = f"http://127.0.0.1:{endpoints.model_port}"
        egress = f"http://127.0.0.1:{endpoints.egress_port}"
    return {
        "MODEL_PROXY": model,
        "EGRESS_PROXY": egress,
        "HTTP_PROXY": egress,
        "HTTPS_PROXY": egress,
        "NO_PROXY": "model-proxy,egress",
    }


_FWD_JS = (
    "const net=require('net');"
    "const host=process.env.FWD_HOST;"
    "for (const port of process.env.FWD_PORTS.split(',')) {"
    " net.createServer((c)=>{"
    "  const u=net.connect({host, port:Number(port)});"
    "  c.pipe(u); u.pipe(c);"
    "  c.on('error',()=>u.destroy()); u.on('error',()=>c.destroy());"
    " }).listen(Number(port),'0.0.0.0');"
    "}"
)


def ensure_desktop_forwarder(docker: str, *, ports: list[int], timeout_s: float) -> str:
    """Docker Desktop 的 internal 网络到不了 host-gateway。返回转发容器在 mao-egress 上的地址。"""
    shown = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--add-host=probe:host-gateway",
            "node:22-bookworm-slim",
            "node",
            "-e",
            "const dns=require('dns'); dns.lookup('probe', (e, a)=>{ if(e) process.exit(1); process.stdout.write(a); });",
        ],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    host = shown.stdout.decode("utf-8", errors="replace").strip()
    if shown.returncode != 0 or not host:
        raise FactoryError(
            "EGRESS_PROXY_UNAVAILABLE",
            "解析 host-gateway 失败",
            reason=shown.stderr.decode("utf-8", errors="replace").strip(),
        )
    name = f"mao-fwd-{os.getpid()}"
    subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=60, check=False)
    made = subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "bridge",
            "-e",
            f"FWD_HOST={host}",
            "-e",
            "FWD_PORTS=" + ",".join(str(port) for port in ports),
            "node:22-bookworm-slim",
            "node",
            "-e",
            _FWD_JS,
        ],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if made.returncode != 0:
        raise FactoryError(
            "EGRESS_PROXY_UNAVAILABLE",
            "启动出网转发失败",
            reason=made.stderr.decode("utf-8", errors="replace").strip(),
        )
    subprocess.run(
        [docker, "network", "connect", "mao-egress", name],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    ip = subprocess.run(
        [
            docker,
            "inspect",
            name,
            "--format",
            '{{index .NetworkSettings.Networks "mao-egress" "IPAddress"}}',
        ],
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    alias = ip.stdout.decode("utf-8", errors="replace").strip()
    if ip.returncode != 0 or not alias:
        raise FactoryError("EGRESS_PROXY_UNAVAILABLE", "转发出网地址不可用", reason="no-alias")
    return alias


def start_owned_proxies(ctx: Any, *, model_port: int, timeout_s: float) -> tuple[EgressProxy, ModelProxy]:
    provider = ctx.provider
    docker = getattr(provider, "docker", None)
    if getattr(provider, "name", "") == "docker" and docker:
        gateway = ensure_egress_network(docker, timeout_s=timeout_s)
        host = proxy_bind_host(docker_operating_system(docker, timeout_s=timeout_s), gateway)
        provider.proxy_host = host
    else:
        host = "127.0.0.1"
    store = SecretStore(ctx.root)
    egress = EgressProxy(lambda _job: EgressPlan("none", (), (), (), {}), store, ctx.clock)
    try:
        egress_port = egress.start(host, 0)
        model = ModelProxy(store, ctx.clock, ctx.root)
        bound = model.start(host, model_port)
    except Exception:
        egress.close()
        raise
    ctx.proxies = ProxyEndpoints(host, egress_port, bound)
    if host == "127.0.0.1" and docker:
        provider.egress_alias = ensure_desktop_forwarder(
            docker, ports=[egress_port, bound], timeout_s=timeout_s
        )
        provider.egress_forwarder = f"mao-fwd-{os.getpid()}"
    return egress, model


class EgressProxy:
    def __init__(self, plan_for: Callable[[str], EgressPlan], store: SecretStore, clock: Any) -> None:
        self.plan_for = plan_for
        self.store = store
        self.clock = clock
        self._server: ThreadingHTTPServer | None = None

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        global LIVE_EGRESS
        self._server = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        with _LIVE_LOCK:
            LIVE_EGRESS += 1
        return int(self._server.server_address[1])

    def close(self) -> None:
        global LIVE_EGRESS
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            with _LIVE_LOCK:
                LIVE_EGRESS = max(0, LIVE_EGRESS - 1)


class ModelProxy:
    def __init__(self, store: SecretStore, clock: Any, root: Path) -> None:
        self.store = store
        self.clock = clock
        self.root = Path(root)
        self._server: ThreadingHTTPServer | None = None
        self._spent: dict[str, float] = {}

    def start(self, host: str = "127.0.0.1", port: int | None = 8080) -> int:
        chosen = 0 if port is None else port
        try:
            self._server = ThreadingHTTPServer((host, chosen), self._handler())
        except OSError as exc:
            raise FactoryError("MODEL_PROXY_BIND_FAILED", "模型代理绑定失败", port=chosen) from exc
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return int(self._server.server_address[1])

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def spent(self, job_id: str) -> float:
        return round(self._spent.get(job_id, 0.0), 6)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                proxy._handle(self, raw)

            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

        return Handler

    def _handle(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:
        job_id = handler.headers.get("X-Factory-Job") or ""
        state, _station, _epoch = self._job(job_id)
        found, cap = self._registered_cap(job_id)
        if state not in _ALLOWED_JOB or not found:
            self._send(handler, 403, {"code": "EGRESS_JOB_INVALID"})
            return
        if cap is None:
            self._stop(job_id, "MODEL_BUDGET_UNSET")
            self._send(handler, 402, {"code": "MODEL_BUDGET_UNSET"})
            return
        path = handler.path or "/"
        metered = handler.command == "POST" and _meter_path(path)
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        model = str(body.get("model") or "") if isinstance(body, dict) else ""
        if metered and (self._stopped(job_id) or self.spent(job_id) > cap):
            self._send(handler, 402, {"code": "BUDGET_EXCEEDED"})
            return
        upstream = self._upstream(model)
        if upstream is None or "header" not in upstream:
            self._send(handler, 502, {"code": "MODEL_UPSTREAM_UNKNOWN"})
            return
        prices = self._prices()
        if metered and model not in prices and "*" not in prices:
            self._stop(job_id, "BUDGET_PRICE_MISSING")
            self._send(handler, 402, {"code": "BUDGET_PRICE_MISSING"})
            return
        try:
            status, payload, content_type = self._forward(upstream, raw, path)
        except (OSError, URLError, HTTPError):
            self._send(handler, 502, {"code": "MODEL_UPSTREAM_UNKNOWN"})
            return
        if not metered:
            self._send_raw(handler, status, payload, content_type)
            return
        measured = _measure(content_type, payload)
        if measured is None:
            self._stop(job_id, "MODEL_USAGE_UNPARSEABLE")
            self._send(handler, 502, {"code": "MODEL_USAGE_UNPARSEABLE"})
            return
        cost, tokens_in, tokens_out = measured
        if cost is None:
            table = prices.get(model) or prices.get("*")
            if not isinstance(table, dict):
                self._stop(job_id, "BUDGET_PRICE_MISSING")
                self._send(handler, 402, {"code": "BUDGET_PRICE_MISSING"})
                return
            cost = round(
                (
                    float(tokens_in or 0) * float(table["input_per_mtok"])
                    + float(tokens_out or 0) * float(table["output_per_mtok"])
                )
                / 1_000_000,
                6,
            )
        spent = round(self.spent(job_id) + cost, 6)
        self._spent[job_id] = spent
        self._record(job_id, model, cost, tokens_in, tokens_out)
        if spent > cap:
            self._stop(job_id, "BUDGET_EXCEEDED")
        self._send_raw(handler, status, payload, content_type)

    def _send(self, handler: BaseHTTPRequestHandler, status: int, body: Mapping[str, Any]) -> None:
        self._send_raw(handler, status, json.dumps(dict(body)).encode("utf-8"), "application/json")

    def _send_raw(
        self, handler: BaseHTTPRequestHandler, status: int, payload: bytes, content_type: str
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    def _job(self, job_id: str) -> tuple[str, str, str]:
        db = self.root / "factory.db"
        if not db.is_file() or not job_id:
            return "", "", ""
        conn = sqlite3.connect(db)
        try:
            row = conn.execute("SELECT state, station, epoch FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                try:
                    session = conn.execute(
                        "SELECT state FROM foreman_sessions WHERE id = ?", (job_id,)
                    ).fetchone()
                except sqlite3.OperationalError:
                    session = None
                if session is not None and str(session[0]) == "running":
                    return "RUNNING", "foreman", ""
                return "", "", ""
        finally:
            conn.close()
        return str(row[0]), str(row[1]), str(row[2])

    def _registered_cap(self, job_id: str) -> tuple[bool, float | None]:
        db = self.root / "factory.db"
        if not db.is_file() or not job_id:
            return False, None
        conn = sqlite3.connect(db)
        try:
            row = conn.execute("SELECT cap_usd FROM model_caps WHERE job_id = ?", (job_id,)).fetchone()
        except sqlite3.OperationalError:
            return False, None
        finally:
            conn.close()
        if row is None:
            return False, None
        return True, None if row[0] is None else float(row[0])

    def _prices(self) -> dict[str, Any]:
        path = self.root / "egress" / "prices.json"
        if not path.is_file():
            return {}
        body = json.loads(path.read_text(encoding="utf-8"))
        return body if isinstance(body, dict) else {}

    def _upstream(self, model: str) -> dict[str, Any] | None:
        path = self.root / "egress" / "upstreams.json"
        if not path.is_file():
            return None
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            return None
        found = body.get(model) or body.get("*")
        return found if isinstance(found, dict) else None

    def _forward(self, upstream: Mapping[str, Any], raw: bytes, path: str) -> tuple[int, bytes, str]:
        secret = self.store._read(str(upstream["secret"]))
        req = Request(_join_upstream(str(upstream["url"]), path), data=raw, method="POST")
        req.add_header(str(upstream["header"]), secret)
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=5) as resp:
            payload = resp.read()
            status = int(resp.status)
            content_type = resp.headers.get("Content-Type") or "application/json"
        return status, payload, content_type

    def _record(
        self, job_id: str, model: str, cost: float, tokens_in: float | None, tokens_out: float | None
    ) -> None:
        tin = None if tokens_in is None else int(tokens_in)
        tout = None if tokens_out is None else int(tokens_out)
        db = self.root / "factory.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO model_usage (job_id, at, model, cost_usd, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, utc_iso(self.clock.now()), model, cost, tin, tout),
            )
            conn.commit()
        finally:
            conn.close()

    def _stop(self, job_id: str, code: str) -> None:
        db = self.root / "factory.db"
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "INSERT INTO budget_stops (job_id, code, at) VALUES (?, ?, ?) ON CONFLICT(job_id) DO NOTHING",
                (job_id, code, utc_iso(self.clock.now())),
            )
            conn.commit()
        finally:
            conn.close()

    def _stopped(self, job_id: str) -> bool:
        db = self.root / "factory.db"
        conn = sqlite3.connect(db)
        try:
            row = conn.execute("SELECT code FROM budget_stops WHERE job_id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        return row is not None


def _rules(
    snapshot: Any, station: Mapping[str, Any], tier: str, value: Any, station_key: str
) -> list[AllowRule]:
    methods = _DEFAULT_METHODS[tier]
    hosts = _hosts(snapshot, station, tier, value, station_key)
    return [AllowRule(tier, host, methods) for host in hosts]


def _hosts(snapshot: Any, station: Mapping[str, Any], tier: str, value: Any, station_key: str) -> list[str]:
    if value == "from-machine":
        machine = snapshot.spec(snapshot.key("Machine", station["machine"]))
        raw = ((machine.get("needs") or {}).get("egress") or {}).get(tier) or []
        return [_host(item, station_key) for item in raw]
    if isinstance(value, list):
        return [_host(item, station_key) for item in value]
    if isinstance(value, dict):
        return [_host(item, station_key) for item in value.get("hosts") or []]
    raise FactoryError("EGRESS_CONFIG_INVALID", "出网配置无法规范", station=station_key, reason=tier)


def _host(value: Any, station_key: str) -> str:
    text = str(value).split("://", 1)[-1].split("/", 1)[0].rstrip(".").lower()
    if " " in text or text == "":
        raise FactoryError("EGRESS_CONFIG_INVALID", "host 不合法", station=station_key, reason=text)
    return text


_METER_SUFFIXES = ("/messages", "/responses", "/chat/completions")


def _agent_uses_model_proxy(snapshot: Any, station: Mapping[str, Any]) -> bool:
    runtime_ref = (station.get("operator") or {}).get("runtime")
    if not runtime_ref:
        return False
    runtime = snapshot.spec(snapshot.key("OperatorRuntime", runtime_ref))
    if runtime.get("kind") != "agent-cli":
        return False
    texts: list[str] = []
    for item in (runtime.get("invoke") or {}).get("argv") or []:
        texts.append(item if isinstance(item, str) else str(item))
    for value in (runtime.get("env") or {}).values():
        texts.append(value if isinstance(value, str) else str(value))
    return any("${MODEL_PROXY}" in text for text in texts)


def _meter_path(path: str) -> bool:
    pure = path.split("?", 1)[0].rstrip("/")
    return any(pure.endswith(suffix) for suffix in _METER_SUFFIXES)


def _join_upstream(url: str, path: str) -> str:
    parsed = urlsplit(url)
    request_path = path.split("?", 1)[0]
    if parsed.path and parsed.path != "/" and request_path.startswith(parsed.path):
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return url.rstrip("/") + path


def _measure(content_type: str, payload: bytes) -> tuple[float | None, float | None, float | None] | None:
    objects = _response_objects(content_type, payload)
    costs: list[float] = []
    inputs: list[float] = []
    outputs: list[float] = []
    for obj in objects:
        if _number(obj.get("cost_usd")) is not None:
            costs.append(float(obj["cost_usd"]))
        for usage in _usage_candidates(obj):
            priced = _number(usage.get("cost_usd"))
            if priced is not None:
                costs.append(priced)
            tin = _input_tokens(usage)
            tout = _output_tokens(usage)
            if tin is not None:
                inputs.append(tin)
            if tout is not None:
                outputs.append(tout)
    if costs:
        return max(costs), max(inputs) if inputs else None, max(outputs) if outputs else None
    if not inputs and not outputs:
        return None
    return None, max(inputs) if inputs else 0.0, max(outputs) if outputs else 0.0


def _response_objects(content_type: str, payload: bytes) -> list[dict[str, Any]]:
    text = payload.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type.lower():
        found: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "" or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                found.append(obj)
        return found
    try:
        obj = json.loads(text or "{}")
    except json.JSONDecodeError:
        return []
    return [obj] if isinstance(obj, dict) else []


def _usage_candidates(obj: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    usage = obj.get("usage")
    if isinstance(usage, dict):
        found.append(usage)
    message = obj.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        found.append(message["usage"])
    response = obj.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        found.append(response["usage"])
    return found


def _input_tokens(usage: Mapping[str, Any]) -> float | None:
    base = _number(usage.get("input_tokens"))
    if base is None:
        return _number(usage.get("prompt_tokens"))
    total = base
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        extra = _number(usage.get(key))
        if extra is not None:
            total += extra
    return total


def _output_tokens(usage: Mapping[str, Any]) -> float | None:
    found = _number(usage.get("output_tokens"))
    if found is not None:
        return found
    return _number(usage.get("completion_tokens"))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _secret_names(snapshot: Any, station: Mapping[str, Any]) -> list[str]:
    policy = snapshot.spec(snapshot.key("Policy", station["policy"]))
    secrets = policy.get("secrets")
    if isinstance(secrets, list):
        return [str(item) for item in secrets]
    if secrets == "from-machine":
        machine = snapshot.spec(snapshot.key("Machine", station["machine"]))
        return [str(item) for item in (machine.get("needs") or {}).get("secrets") or []]
    return []
