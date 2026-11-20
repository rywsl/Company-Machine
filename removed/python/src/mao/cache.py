"""工位结果缓存键与查找。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mao.base import canonical_bytes, canonical_json, digest_bytes, utc_iso
from mao.errors import FactoryError
from mao.jobs import Job, JobState, load_job
from mao.values import params_value

CACHEABLE_ROLES = frozenset({"transform", "converter"})


def spec_digest(doc: Mapping[str, Any]) -> str:
    body = copy.deepcopy(dict(doc))
    meta = dict(body.get("metadata") or {})
    for name in ("lifecycle", "description", "labels"):
        meta.pop(name, None)
    body["metadata"] = meta
    return digest_bytes(canonical_bytes(body))


@dataclass(frozen=True)
class KeyMaterial:
    station: str
    station_digest: str
    machine: str
    machine_digest: str
    image: str
    recipe: str
    runtime: str
    runtime_digest: str
    model: str
    policy: str
    policy_digest: str
    inputs: dict[str, list[str]]
    params: dict[str, Any]
    variant: str

    def to_json(self) -> dict[str, Any]:
        return {
            "v": 1,
            "station": self.station,
            "station_digest": self.station_digest,
            "machine": self.machine,
            "machine_digest": self.machine_digest,
            "image": self.image,
            "recipe": self.recipe,
            "runtime": self.runtime,
            "runtime_digest": self.runtime_digest,
            "model": self.model,
            "policy": self.policy,
            "policy_digest": self.policy_digest,
            "inputs": {port: list(self.inputs[port]) for port in sorted(self.inputs)},
            "params": copy.deepcopy(self.params),
            "variant": self.variant,
        }

    def key(self) -> str:
        return "jk:" + digest_bytes(canonical_bytes(self.to_json()))[7:]


class JobCache:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def material(
        self,
        snapshot: Any,
        station_key: str,
        inputs: Mapping[str, str | Sequence[str]],
        params: Mapping[str, Any],
        *,
        variant: str | None = None,
    ) -> KeyMaterial:
        station_key = snapshot.key("Station", station_key)
        station_doc = snapshot.doc(station_key)
        station = station_doc["spec"]
        role = station.get("role")
        if role not in CACHEABLE_ROLES:
            raise FactoryError(
                "CACHE_NOT_CACHEABLE",
                f"角色 {role} 不能缓存",
                station=station_key,
                role=role,
            )
        machine_key = snapshot.key("Machine", station["machine"])
        machine_doc = snapshot.doc(machine_key)
        machine = machine_doc["spec"]
        runtime_key = snapshot.key("OperatorRuntime", station["operator"]["runtime"])
        runtime_doc = snapshot.doc(runtime_key)
        policy_key = snapshot.key("Policy", station["policy"])
        policy_doc = snapshot.doc(policy_key)
        try:
            normalized = params_value(snapshot, station_key, params)
        except FactoryError:
            normalized = dict(params)
        stored: dict[str, list[str]] = {}
        for port, value in inputs.items():
            stored[port] = [value] if isinstance(value, str) else [str(item) for item in value]
        return KeyMaterial(
            station_key,
            spec_digest(station_doc),
            machine_key,
            spec_digest(machine_doc),
            str((machine.get("runtime") or {}).get("image") or ""),
            str(station.get("recipe") or ""),
            runtime_key,
            spec_digest(runtime_doc),
            str((station.get("operator") or {}).get("model") or ""),
            policy_key,
            spec_digest(policy_doc),
            stored,
            normalized,
            "" if variant is None else variant,
        )

    def key(
        self,
        snapshot: Any,
        station_key: str,
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        *,
        variant: str | None = None,
    ) -> str:
        return self.material(snapshot, station_key, inputs, params, variant=variant).key()

    def bind(self, job_id: str, material: KeyMaterial) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO job_keys (job_id, job_key, variant, material, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, material.key(), material.variant, canonical_json(material.to_json()), now),
            )

    def lookup(self, key: str) -> Job | None:
        rows = self.ctx.db.query(
            "SELECT seq, job_id, job_key, material FROM job_keys WHERE job_key = ? ORDER BY seq",
            (key,),
        )
        invalidations = self.ctx.db.query("SELECT * FROM cache_invalidations")
        for row in rows:
            try:
                job = load_job(self.ctx.db, row["job_id"])
            except FactoryError:
                continue
            if job.state != JobState.SEALED:
                continue
            if self._invalid(row, job, invalidations):
                continue
            if not self._outputs_usable(job):
                continue
            return job
        return None

    def invalidate(self, scope: str, value: str, *, reason: str, by: str) -> str:
        if scope not in ("key", "job", "station", "bean"):
            raise FactoryError("CACHE_INVALIDATION_INVALID", "失效范围不合法", scope=scope)
        if scope == "station" and not value.startswith("Station:"):
            raise FactoryError("CACHE_INVALIDATION_INVALID", "工位失效值必须是 Spec 键", value=value)
        if scope == "bean" and not value.startswith("sha256:"):
            raise FactoryError("CACHE_INVALIDATION_INVALID", "Bean 失效值必须是摘要", value=value)
        now = utc_iso(self.ctx.clock.now())
        invalid_id = self.ctx.ids.new("ci")
        with self.ctx.db.tx() as conn:
            max_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM job_keys").fetchone()
            conn.execute(
                """INSERT INTO cache_invalidations (id, scope, value, max_seq, reason, by, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (invalid_id, scope, value, int(max_seq["seq"]), reason, by, now),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'cache.invalidated', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json(
                        {"id": invalid_id, "scope": scope, "value": value, "reason": reason, "by": by}
                    ),
                ),
            )
        return invalid_id

    def explain(self, job_id: str) -> dict[str, Any]:
        rows = self.ctx.db.query("SELECT * FROM job_keys WHERE job_id = ?", (job_id,))
        if not rows:
            raise FactoryError("CACHE_NOT_CACHEABLE", "这个 job 没有缓存键", job=job_id)
        row = rows[0]
        hits = self.ctx.db.query(
            "SELECT job_id FROM job_keys WHERE job_key = ? ORDER BY seq", (row["job_key"],)
        )
        return {
            "key": row["job_key"],
            "material": json.loads(row["material"]),
            "hits": [item["job_id"] for item in hits],
        }

    def _invalid(self, row: Any, job: Job, invalidations: list[Any]) -> bool:
        seq = int(row["seq"])
        for item in invalidations:
            if seq > int(item["max_seq"]):
                continue
            scope = item["scope"]
            value = item["value"]
            if scope == "key" and value == row["job_key"]:
                return True
            if scope == "job" and value == job.id:
                return True
            if scope == "station" and value == job.station:
                return True
            if scope == "bean" and _bean_in_job(job, value):
                return True
        return False

    def _outputs_usable(self, job: Job) -> bool:
        for ids in job.outputs.values():
            for bean_id in ids:
                try:
                    bean = self.ctx.warehouse.get(bean_id)
                except FactoryError:
                    return False
                if not bean.sealed or bean.expired_at is not None:
                    return False
        return True


def _bean_in_job(job: Job, bean_id: str) -> bool:
    for ids in list(job.inputs.values()) + list(job.outputs.values()):
        if bean_id in ids:
            return True
    return False
