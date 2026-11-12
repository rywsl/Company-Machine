"""Sink 回执与幂等键。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mao.base import canonical_json, utc_iso
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
        if current.status != ReceiptStatus.PUBLISHED:
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
    del ctx, snapshot
    return True, receipt.verify_detail or ""


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
