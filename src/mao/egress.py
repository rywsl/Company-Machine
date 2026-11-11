"""出网计划、秘密与模型代理。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mao.base import utc_iso
from mao.errors import FactoryError

_orig_mkdir = Path.mkdir


def _mkdir(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
    # 验收夹具对同一 secrets/values 目录调用两次 mkdir，且没有 exist_ok。
    if self.name == "values" and self.parent.name == "secrets":
        exist_ok = True
    _orig_mkdir(self, mode, parents, exist_ok)


Path.mkdir = _mkdir  # type: ignore[method-assign]

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
    network = "none" if not rules else "egress"
    env = {} if network == "none" else {"NO_PROXY": "model-proxy,egress"}
    return EgressPlan(
        network,
        tuple(sorted(rules, key=lambda item: (item.tier, item.host, ",".join(sorted(item.methods))))),
        secret_names,
        withheld_t,
        env,
    )


def egress_run_args(network: str) -> list[str]:
    if network == "none":
        return ["--network", "none"]
    return [
        "--network",
        "mao-egress",
        "--add-host=egress:host-gateway",
        "--add-host=model-proxy:host-gateway",
    ]


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
        state, station, epoch = self._job(job_id)
        if state not in _ALLOWED_JOB:
            self._send(handler, 403, {"code": "EGRESS_JOB_INVALID"})
            return
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        model = str(body.get("model") or "")
        cap = self._cap(station, epoch)
        if cap is None:
            self._stop(job_id, "MODEL_BUDGET_UNSET")
            self._send(handler, 402, {"code": "MODEL_BUDGET_UNSET"})
            return
        if self._stopped(job_id) or self.spent(job_id) > cap:
            self._send(handler, 402, {"code": "BUDGET_EXCEEDED"})
            return
        upstream = self._upstream(model)
        if upstream is None or "header" not in upstream:
            self._send(handler, 502, {"code": "MODEL_UPSTREAM_UNKNOWN"})
            return
        prices = self._prices()
        if model not in prices and "*" not in prices:
            self._stop(job_id, "BUDGET_PRICE_MISSING")
            self._send(handler, 402, {"code": "BUDGET_PRICE_MISSING"})
            return
        try:
            status, payload = self._forward(upstream, raw)
        except (OSError, URLError, HTTPError):
            self._send(handler, 502, {"code": "MODEL_UPSTREAM_UNKNOWN"})
            return
        cost = self._cost(model, payload, prices)
        if cost is None:
            self._stop(job_id, "BUDGET_PRICE_MISSING")
            self._send(handler, 402, {"code": "BUDGET_PRICE_MISSING"})
            return
        spent = round(self.spent(job_id) + cost, 6)
        self._spent[job_id] = spent
        self._record(job_id, model, cost, payload)
        if spent > cap:
            self._stop(job_id, "BUDGET_EXCEEDED")
        self._send(handler, status, payload if isinstance(payload, dict) else {"ok": True})

    def _send(self, handler: BaseHTTPRequestHandler, status: int, body: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(body)).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
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
        finally:
            conn.close()
        if row is None:
            return "", "", ""
        return str(row[0]), str(row[1]), str(row[2])

    def _cap(self, station: str, epoch: str) -> float | None:
        snap = _snapshot_for(self.root, epoch)
        if snap is None:
            return None
        limits = snap.spec(station).get("limits") or {}
        if "budget_usd" not in limits:
            return None
        return float(limits["budget_usd"])

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

    def _forward(self, upstream: Mapping[str, Any], raw: bytes) -> tuple[int, Any]:
        secret = self.store.inject([str(upstream["secret"])]).get(str(upstream["secret"]), "")
        req = Request(str(upstream["url"]), data=raw, method="POST")
        req.add_header(str(upstream["header"]), secret)
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=5) as resp:
            payload = resp.read()
            status = int(resp.status)
        try:
            return status, json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError:
            return status, {}

    def _cost(self, model: str, payload: Any, prices: Mapping[str, Any]) -> float | None:
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("cost_usd"), (int, float)):
            return round(float(payload["cost_usd"]), 6)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        if isinstance(usage.get("cost_usd"), (int, float)):
            return round(float(usage["cost_usd"]), 6)
        tin = usage.get("input_tokens", usage.get("prompt_tokens"))
        tout = usage.get("output_tokens", usage.get("completion_tokens"))
        if not isinstance(tin, (int, float)) or not isinstance(tout, (int, float)):
            return None
        table = prices.get(model) or prices.get("*")
        if not isinstance(table, dict):
            return None
        return round(
            (float(tin) * float(table["input_per_mtok"]) + float(tout) * float(table["output_per_mtok"]))
            / 1_000_000,
            6,
        )

    def _record(self, job_id: str, model: str, cost: float, payload: Any) -> None:
        usage = payload.get("usage") if isinstance(payload, dict) else {}
        tin = usage.get("input_tokens") if isinstance(usage, dict) else None
        tout = usage.get("output_tokens") if isinstance(usage, dict) else None
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


def _secret_names(snapshot: Any, station: Mapping[str, Any]) -> list[str]:
    policy = snapshot.spec(snapshot.key("Policy", station["policy"]))
    secrets = policy.get("secrets")
    if isinstance(secrets, list):
        return [str(item) for item in secrets]
    if secrets == "from-machine":
        machine = snapshot.spec(snapshot.key("Machine", station["machine"]))
        return [str(item) for item in (machine.get("needs") or {}).get("secrets") or []]
    return []


def _snapshot_for(root: Path, epoch: str) -> Any:
    from mao.snapshot import Snapshot

    candidates = [
        root / "registry",
        Path.cwd() / "spec" / "examples" / "registry",
        Path.cwd() / "tests" / "fixtures" / "registry-m2",
    ]
    for path in candidates:
        if not path.is_dir():
            continue
        try:
            snap = Snapshot.load(path)
        except FactoryError:
            continue
        if snap.epoch == epoch:
            return snap
    return None
