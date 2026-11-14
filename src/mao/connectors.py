"""Sink 回执与幂等键。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from mao.base import canonical_json, parse_duration, utc_iso
from mao.errors import FactoryError
from mrl.errors import EvalError


class ReceiptStatus(StrEnum):
    CLAIMED = "claimed"
    PUBLISHED = "published"
    VERIFIED = "verified"
    FAILED = "failed"
    VOID = "void"


@dataclass(frozen=True)
class Receipt:
    id: str
    station_name: str
    station: str
    idempotency_key: str
    job_id: str
    run_id: str | None
    inputs: dict[str, list[str]]
    status: ReceiptStatus
    target: str | None
    locator: dict[str, str]
    items: list[dict[str, str]]
    verify_method: str
    verify_detail: str | None
    verified_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Claim:
    kind: str
    receipt: Receipt


def station_name_of(station_key: str) -> str:
    ref = station_key.split(":", 1)[-1]
    return ref.split("@", 1)[0]


def idempotency_key(
    snapshot: Any,
    station_key: str,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
    run: Mapping[str, Any],
) -> str:
    station_key = snapshot.key("Station", station_key)
    try:
        rendered = snapshot.compiled(f"{station_key}/idempotency_key").render(
            {
                "inputs": _inputs_env(snapshot, station_key, inputs),
                "params": _params_env(snapshot, station_key, params),
                "run": dict(run),
            }
        )
    except EvalError as exc:
        raise FactoryError("IDEMPOTENCY_KEY_EMPTY", "幂等键无法渲染", station=station_key) from exc
    if rendered == "":
        raise FactoryError("IDEMPOTENCY_KEY_EMPTY", "幂等键渲染结果为空", station=station_key)
    return rendered


class ReceiptStore:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def claim(
        self,
        *,
        station_key: str,
        key: str,
        job_id: str,
        run_id: str | None,
        inputs: Mapping[str, Sequence[str]],
        verify_method: str,
    ) -> Claim:
        if key == "":
            raise FactoryError("IDEMPOTENCY_KEY_EMPTY", "幂等键为空", station=station_key)
        name = station_name_of(station_key)
        stored = {port: [str(item) for item in value] for port, value in inputs.items()}
        existing = self._active(name, key)
        if existing is not None:
            if existing.status == ReceiptStatus.VERIFIED or existing.station != station_key:
                self._event(
                    "sink.duplicate",
                    job_id,
                    {"receipt": existing.id, "station": station_key, "key": key, "job": job_id},
                )
                return Claim("duplicate", existing)
            if existing.status in (ReceiptStatus.CLAIMED, ReceiptStatus.PUBLISHED):
                raise FactoryError(
                    "IDEMPOTENCY_CONFLICT",
                    "幂等键已被占用",
                    station=station_key,
                    key=key,
                    receipt=existing.id,
                )
            raise FactoryError(
                "RECEIPT_UNVERIFIED_EXISTS",
                "同键已有未通过核验的回执",
                station=station_key,
                key=key,
                receipt=existing.id,
            )
        now = utc_iso(self.ctx.clock.now())
        receipt_id = self.ctx.ids.new("rc")
        try:
            with self.ctx.db.tx() as conn:
                conn.execute(
                    """INSERT INTO receipts
                       (id, station_name, station, idempotency_key, job_id, run_id, inputs, status,
                        locator, items, verify_method, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', '{}', '[]', ?, ?, ?)""",
                    (
                        receipt_id,
                        name,
                        station_key,
                        key,
                        job_id,
                        run_id,
                        canonical_json(stored),
                        verify_method,
                        now,
                        now,
                    ),
                )
                self._event_conn(
                    conn,
                    "sink.claimed",
                    job_id,
                    {"receipt": receipt_id, "station": station_key, "key": key, "job": job_id},
                )
        except Exception as exc:
            again = self._active(name, key)
            if again is not None:
                raise FactoryError(
                    "IDEMPOTENCY_CONFLICT",
                    "幂等键已被占用",
                    station=station_key,
                    key=key,
                    receipt=again.id,
                ) from exc
            raise
        return Claim("new", self.get(receipt_id))

    def publish(self, receipt_id: str, payload: Mapping[str, Any]) -> Receipt:
        current = self.get(receipt_id)
        if current.status != ReceiptStatus.CLAIMED:
            raise FactoryError(
                "RECEIPT_STATE_INVALID",
                "回执不在可发布状态",
                receipt=receipt_id,
                status=current.status.value,
                op="publish",
            )
        problems = _payload_problems(payload)
        if problems:
            raise FactoryError("RECEIPT_INVALID", "回执载荷不合法", job=current.job_id, problems=problems)
        target = str(payload["target"])
        locator = {str(k): str(v) for k, v in dict(payload.get("locator") or {}).items()}
        items = [dict(item) for item in payload.get("items") or []]
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """UPDATE receipts
                   SET status = 'published', target = ?, locator = ?, items = ?, note = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    target,
                    canonical_json(locator),
                    canonical_json(items),
                    str(payload.get("note") or ""),
                    now,
                    receipt_id,
                ),
            )
            self._event_conn(
                conn,
                "sink.published",
                current.job_id,
                {"receipt": receipt_id, "target": target, "locator": locator},
            )
        return self.get(receipt_id)

    def settle(self, receipt_id: str, *, ok: bool, detail: str) -> Receipt:
        current = self.get(receipt_id)
        if current.status not in (
            ReceiptStatus.PUBLISHED,
            ReceiptStatus.VERIFIED,
            ReceiptStatus.FAILED,
        ):
            raise FactoryError(
                "RECEIPT_STATE_INVALID",
                "回执不在可核验状态",
                receipt=receipt_id,
                status=current.status.value,
                op="settle",
            )
        status = ReceiptStatus.VERIFIED if ok else ReceiptStatus.FAILED
        now = utc_iso(self.ctx.clock.now())
        kind = "sink.verified" if ok else "sink.verify_failed"
        with self.ctx.db.tx() as conn:
            conn.execute(
                """UPDATE receipts
                   SET status = ?, verify_detail = ?, verified_at = ?, updated_at = ?
                   WHERE id = ?""",
                (status.value, detail, now, now, receipt_id),
            )
            self._event_conn(
                conn,
                kind,
                current.job_id,
                {"receipt": receipt_id, "method": current.verify_method, "detail": detail},
            )
        return self.get(receipt_id)

    def note_format_error(self, receipt_id: str, problems: Sequence[str]) -> None:
        detail = "receipt.json 格式错误：" + ", ".join(str(item) for item in problems)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE receipts SET verify_detail = ?, updated_at = ? WHERE id = ?",
                (detail, now, receipt_id),
            )

    def void(self, receipt_id: str, *, by: str, reason: str) -> Receipt:
        current = self.get(receipt_id)
        if current.status == ReceiptStatus.VOID:
            raise FactoryError(
                "RECEIPT_STATE_INVALID",
                "回执已经作废",
                receipt=receipt_id,
                status=current.status.value,
                op="void",
            )
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """UPDATE receipts
                   SET status = 'void', voided_by = ?, void_reason = ?, updated_at = ?
                   WHERE id = ?""",
                (by, reason, now, receipt_id),
            )
            self._event_conn(
                conn, "receipt.voided", current.job_id, {"receipt": receipt_id, "by": by, "reason": reason}
            )
        return self.get(receipt_id)

    def get(self, receipt_id: str) -> Receipt:
        rows = self.ctx.db.query("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
        if not rows:
            raise FactoryError("RECEIPT_NOT_FOUND", f"找不到回执 {receipt_id}", receipt=receipt_id)
        return _receipt(rows[0])

    def list(self, *, station: str | None = None, status: str | None = None) -> list[Receipt]:
        rows = self.ctx.db.query("SELECT * FROM receipts")
        found = [_receipt(row) for row in rows]
        if station is not None:
            found = [item for item in found if item.station == station or item.station_name == station]
        if status is not None:
            found = [item for item in found if item.status.value == status]
        return found

    def for_bean(self, bean_id: str) -> list[Receipt]:
        found = []
        for item in self.list():
            if any(bean_id in ids for ids in item.inputs.values()):
                found.append(item)
        return found

    def _active(self, station_name: str, key: str) -> Receipt | None:
        rows = self.ctx.db.query(
            "SELECT * FROM receipts WHERE station_name = ? AND idempotency_key = ? AND status != 'void'",
            (station_name, key),
        )
        return _receipt(rows[0]) if rows else None

    def _event(self, kind: str, job_id: str | None, data: Mapping[str, Any]) -> None:
        with self.ctx.db.tx() as conn:
            self._event_conn(conn, kind, job_id, data)

    def _event_conn(self, conn: Any, kind: str, job_id: str | None, data: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
            (self.ctx.ids.new("ev"), utc_iso(self.ctx.clock.now()), kind, job_id, canonical_json(dict(data))),
        )


def verify(ctx: Any, snapshot: Any, receipt: Receipt) -> tuple[bool, str]:
    if receipt.status not in (ReceiptStatus.PUBLISHED, ReceiptStatus.VERIFIED, ReceiptStatus.FAILED):
        raise FactoryError(
            "RECEIPT_STATE_INVALID",
            "回执不在可核验状态",
            receipt=receipt.id,
            status=receipt.status.value,
            op="verify",
        )
    try:
        return _verify_in_sandbox(ctx, snapshot, receipt)
    except FactoryError as exc:
        if exc.code in {"RECEIPT_VERIFY_ERROR", "RECEIPT_STATE_INVALID"}:
            raise
        raise FactoryError(
            "RECEIPT_VERIFY_ERROR",
            "回执核验设施故障",
            receipt=receipt.id,
            cause={"reason": exc.code},
        ) from exc
    except Exception as exc:
        raise FactoryError(
            "RECEIPT_VERIFY_ERROR",
            "回执核验设施故障",
            receipt=receipt.id,
            cause={"reason": type(exc).__name__},
        ) from exc


_MEMORY = re.compile(r"^([0-9]+)(Mi|M|MB|Gi|G|GB)$")
_VERIFY_LIMIT_S = 300.0


def _verify_in_sandbox(ctx: Any, snapshot: Any, receipt: Receipt) -> tuple[bool, str]:
    from mao.jobs import load_job
    from mao.sandbox import Mount, SandboxSpec
    from mao.values import recipe_env

    job = load_job(ctx.db, receipt.job_id)
    station = snapshot.spec(job.station)
    machine_key = snapshot.key("Machine", station["machine"])
    machine = snapshot.spec(machine_key)
    recipe_name = str(station.get("verify_receipt") or "")
    recipe = (machine.get("recipes") or {}).get(recipe_name)
    if not isinstance(recipe, dict) or not recipe.get("run"):
        raise FactoryError(
            "RECEIPT_VERIFY_ERROR",
            "核验配方没有 run",
            receipt=receipt.id,
            cause={"reason": "recipe"},
        )
    number = _next_verify_n(ctx.root, job.id)
    principal = f"{job.id}-verify-{number}"
    root = ctx.root / "jobs" / job.id / f"verify-{number}"
    for name in ("in", "out", "ctl", "workspace"):
        (root / name).mkdir(parents=True, exist_ok=True)
    beans = _materialize_inputs(ctx, station, job.inputs, root / "in")
    _write_verify_ctl(ctx, receipt, job, root / "ctl")
    image = str((machine.get("runtime") or {}).get("image") or "")
    from mao.sandbox import sandbox_resources

    resources = sandbox_resources(machine)
    cpus, memory_mb = resources["cpus"], resources["memory_mb"]
    base_env = {"FACTORY_JOB_ID": job.id, "FACTORY_ATTEMPT": str(job.attempt)}
    network, env = _verify_network(ctx, snapshot, job.station, base_env, principal)
    spec = SandboxSpec(
        name=f"mao-{job.id}-verify-{number}",
        image=image,
        mounts=(
            Mount(source=str((root / "in").resolve()), target="/factory/in", readonly=True),
            Mount(source=str((root / "out").resolve()), target="/factory/out", readonly=False),
            Mount(source=str((root / "ctl").resolve()), target="/factory/ctl", readonly=True),
            Mount(
                source=str((ctx.root / "tools" / "factory").resolve()),
                target="/opt/factory",
                readonly=True,
            ),
            Mount(source=str((root / "workspace").resolve()), target="/workspace", readonly=False),
        ),
        workdir="/workspace",
        env=env,
        network=network,
        cpus=cpus,
        memory_mb=memory_mb,
        shm_mb=resources["shm_mb"],
        tmp_mb=resources["tmp_mb"],
        pids=resources["pids"],
        writable=tuple(resources["writable"]),
    )
    argv = _render_run(
        snapshot, machine_key, recipe_name, recipe.get("run") or [], recipe_env(beans, job.params)
    )
    timeout_s = _verify_timeout(station)
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stdout = logs / "verify.stdout"
    stderr = logs / "verify.stderr"
    sandbox = None
    try:
        sandbox = ctx.provider.open(spec)
        result = sandbox.exec(
            argv,
            timeout_s=timeout_s,
            stdout=stdout,
            stderr=stderr,
            workdir="/workspace",
        )
    finally:
        if network == "egress":
            from mao.egress import close_egress

            close_egress(ctx.db, principal)
        if sandbox is not None:
            sandbox.close()
    detail = _detail_text(stdout)
    if result.timed_out or result.exit_code is None:
        raise FactoryError(
            "RECEIPT_VERIFY_ERROR",
            "回执核验超时",
            receipt=receipt.id,
            cause={"timeout_s": timeout_s},
        )
    if result.exit_code == 0:
        return True, detail
    if result.exit_code == 1:
        return False, detail
    raise FactoryError(
        "RECEIPT_VERIFY_ERROR",
        "回执核验退出码异常",
        receipt=receipt.id,
        cause={"exit_code": result.exit_code},
    )


def _next_verify_n(root: Path, job_id: str) -> int:
    base = Path(root) / "jobs" / job_id
    highest = 0
    if base.is_dir():
        for child in base.iterdir():
            if not child.is_dir() or not child.name.startswith("verify-"):
                continue
            suffix = child.name[len("verify-") :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return highest + 1


def _materialize_inputs(
    ctx: Any, station: Mapping[str, Any], inputs: Mapping[str, Sequence[str]], in_dir: Path
) -> dict[str, Any]:
    declared = station.get("inputs") or {}
    beans: dict[str, Any] = {}
    for port, ids in inputs.items():
        loaded = []
        expr = declared.get(port)
        type_expr = expr.get("type") if isinstance(expr, dict) else str(expr or "")
        listed = str(type_expr).startswith("list<")
        for index, bean_id in enumerate(ids):
            dest = in_dir / port / str(index) if listed else in_dir / port
            ctx.warehouse.materialize(bean_id, dest, layout="port")
            loaded.append(ctx.warehouse.get(bean_id))
        if loaded:
            beans[port] = loaded if listed else loaded[0]
    return beans


def _write_verify_ctl(ctx: Any, receipt: Receipt, job: Any, ctl: Path) -> None:
    rows = ctx.db.query("SELECT note FROM receipts WHERE id = ?", (receipt.id,))
    note = ""
    if rows and rows[0]["note"] is not None:
        note = str(rows[0]["note"])
    body = {
        "target": receipt.target or "",
        "locator": dict(receipt.locator),
        "items": [dict(item) for item in receipt.items],
        "note": note,
    }
    (ctl / "receipt.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    source = ctx.root / "jobs" / job.id / str(job.attempt) / "ctl" / "job.json"
    if not source.is_file():
        raise FactoryError(
            "RECEIPT_VERIFY_ERROR",
            "找不到 job.json",
            receipt=receipt.id,
            cause={"reason": "job.json"},
        )
    (ctl / "job.json").write_bytes(source.read_bytes())


def _verify_network(
    ctx: Any, snapshot: Any, station_key: str, base_env: Mapping[str, str], principal: str
) -> tuple[str, dict[str, str]]:
    from secrets import token_hex

    from mao.egress import (
        SecretStore,
        build_egress,
        credentialed_env,
        proxy_env,
        register_check_egress,
    )

    station = snapshot.spec(station_key)
    env = {str(key): str(value) for key, value in base_env.items()}
    if not station.get("policy"):
        return "none", env
    store = SecretStore(ctx.root)
    plan = build_egress(snapshot, station_key, store)
    if plan.network != "egress":
        return "none", env
    if getattr(ctx, "proxies", None) is None:
        from mao.egress import start_owned_proxies

        start_owned_proxies(ctx, model_port=0, timeout_s=60.0)
    env.update(plan.env)
    env.update(store.inject(plan.secret_names))
    token = token_hex(16)
    register_check_egress(ctx.db, principal, plan, token=token)
    proxies = getattr(ctx, "proxies", None)
    if proxies is not None:
        docker = getattr(ctx.provider, "name", "") == "docker"
        env.update(credentialed_env(proxy_env(proxies, docker=docker), principal, token))
    return "egress", env


def _render_run(
    snapshot: Any, machine_key: str, recipe_name: str, run: Sequence[Any], env: Mapping[str, Any]
) -> list[str]:
    argv: list[str] = []
    for index, item in enumerate(run):
        text = item if isinstance(item, str) else str(item)
        if "{{" not in text:
            argv.append(text)
            continue
        subject = f"{machine_key}/recipes.{recipe_name}.run[{index}]"
        rendered = snapshot.compiled(subject).render(dict(env))
        if not isinstance(rendered, str):
            raise FactoryError(
                "RECEIPT_VERIFY_ERROR",
                "核验配方渲染结果不是字符串",
                cause={"reason": "render"},
            )
        argv.append(rendered)
    return argv


def _verify_timeout(station: Mapping[str, Any]) -> float:
    limits = station.get("limits") or {}
    if "timeout" not in limits:
        return _VERIFY_LIMIT_S
    return min(float(parse_duration(limits["timeout"])), _VERIFY_LIMIT_S)


def _detail_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        if isinstance(detail, str):
            return detail
        return json.dumps(detail, ensure_ascii=False)
    return text


def _machine_resources(resources: Any) -> tuple[float | None, int | None]:
    if not isinstance(resources, dict) or not resources:
        return None, None
    cpus = resources.get("cpus")
    memory = resources.get("memory")
    memory_mb = None
    if memory is not None:
        match = _MEMORY.fullmatch(str(memory))
        if match is None:
            raise FactoryError("SANDBOX_SPEC_INVALID", "memory 无法解析", memory=str(memory))
        amount = int(match.group(1))
        memory_mb = amount if match.group(2) in ("Mi", "M", "MB") else amount * 1024
    return (None if cpus is None else float(cpus), memory_mb)


def _payload_problems(payload: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if not isinstance(payload.get("target"), str) or payload.get("target") == "":
        problems.append("target")
    locator = payload.get("locator")
    if not isinstance(locator, dict) or any(not isinstance(value, str) for value in locator.values()):
        problems.append("locator")
    items = payload.get("items", [])
    if not isinstance(items, list):
        problems.append("items")
    return problems


def _inputs_env(snapshot: Any, station_key: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
    spec = snapshot.spec(station_key)
    out: dict[str, Any] = {}
    for name in spec.get("inputs") or {}:
        value = inputs.get(name)
        if value is None or value == {} or value == []:
            out[name] = {"id": ""}
            continue
        declared = (spec.get("inputs") or {}).get(name)
        type_expr = declared.get("type") if isinstance(declared, dict) else str(declared or "")
        listed = type_expr.startswith("list<")
        if isinstance(value, str):
            out[name] = {"id": value}
        elif isinstance(value, (list, tuple)) and not listed and len(value) == 1:
            out[name] = {"id": str(value[0])}
        elif isinstance(value, (list, tuple)):
            out[name] = (
                [{"id": str(item)} for item in value] if value and isinstance(value[0], str) else value
            )
        else:
            out[name] = value
    return out


def _params_env(snapshot: Any, station_key: str, params: Mapping[str, Any]) -> dict[str, Any]:
    spec = snapshot.spec(station_key)
    out = dict(params)
    for name, decl in (spec.get("params") or {}).items():
        if name not in out:
            kind = decl.get("type") if isinstance(decl, dict) else "string"
            out[name] = {"string": "", "int": 0, "float": 0.0, "bool": False, "num": 0.0}.get(str(kind), "")
    return out


def _receipt(row: Any) -> Receipt:
    return Receipt(
        id=row["id"],
        station_name=row["station_name"],
        station=row["station"],
        idempotency_key=row["idempotency_key"],
        job_id=row["job_id"],
        run_id=row["run_id"],
        inputs={
            str(port): [str(item) for item in value] for port, value in json.loads(row["inputs"]).items()
        },
        status=ReceiptStatus(row["status"]),
        target=row["target"],
        locator={str(k): str(v) for k, v in json.loads(row["locator"] or "{}").items()},
        items=list(json.loads(row["items"] or "[]")),
        verify_method=row["verify_method"],
        verify_detail=row["verify_detail"],
        verified_at=row["verified_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
