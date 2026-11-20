"""资产目录：内容地址、依赖闭包与证据保留。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from mao.base import canonical_bytes, canonical_json, digest_bytes, utc_iso
from mao.errors import FactoryError

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SUBJECT = re.compile(
    r"^(?:factory|agent:[A-Za-z0-9][A-Za-z0-9._-]*|station:[A-Za-z0-9][A-Za-z0-9._-]*@[0-9]+)$"
)
_HUMAN = re.compile(r"^[A-Za-z0-9._@-]+$")
_FIELDS = ("key", "kind", "summary", "scope", "beans", "requires", "evidence", "data")
_EVIDENCE_KINDS = ("job", "turn", "run", "receipt", "trial")
_JOB_DONE = ("SEALED", "DEAD")
_RUN_DONE = ("SUCCEEDED", "FAILED", "CANCELLED")
_TURN_DONE = ("ok", "failed", "timeout", "interrupted")
_RECEIPT_DONE = ("published", "verified", "failed", "void")
_MAX_DOCUMENT = 256 * 1024


def assets_config(ctx: Any) -> dict[str, Any] | None:
    """已启用的 assets 段。没有非空 environment 时返回 None。"""
    path = ctx.root / "factory.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    section = body.get("assets")
    if not isinstance(section, dict):
        return None
    environment = section.get("environment")
    if not isinstance(environment, dict) or not environment:
        return None
    return section


def require_enabled(ctx: Any) -> dict[str, Any]:
    section = assets_config(ctx)
    if section is None:
        raise FactoryError("ASSET_NOT_ENABLED", "资产目录尚未启用", field="assets")
    return section


def _contains_secret(ctx: Any, text: str) -> bool:
    from mao.egress import SecretStore

    return SecretStore(ctx.root).redact(text) != text


def _invalid(field: str, message: str = "资产文档不合法") -> FactoryError:
    return FactoryError("ASSET_INVALID", message, field=field)


def _reference(kind: str, ref: str, message: str = "资产引用无效") -> FactoryError:
    return FactoryError("ASSET_REFERENCE_INVALID", message, kind=kind, id=ref)


def _actor(by: str) -> str:
    if not isinstance(by, str) or _HUMAN.fullmatch(by) is None:
        raise _invalid("by", "登记人不合法")
    return by


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise _invalid(field)
    return sorted(set(value))


def _evidence(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _invalid("evidence")
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "id"}:
            raise _invalid("evidence")
        kind = item.get("kind")
        ref = item.get("id")
        if kind not in _EVIDENCE_KINDS or not isinstance(ref, str) or not ref:
            raise _invalid("evidence")
        key = (str(kind), str(ref))
        if key in seen:
            continue
        seen.add(key)
        found.append({"kind": key[0], "id": key[1]})
    found.sort(key=lambda item: (item["kind"], item["id"]))
    return found


def _scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid("scope")
    unknown = sorted(set(value) - {"subject", "conditions"})
    if unknown:
        raise _invalid("scope")
    subject = value.get("subject")
    if not isinstance(subject, str) or _SUBJECT.fullmatch(subject) is None:
        raise _invalid("scope")
    out: dict[str, Any] = {"subject": subject}
    if "conditions" in value:
        conditions = value.get("conditions")
        if not isinstance(conditions, str):
            raise _invalid("scope")
        out["conditions"] = conditions
    return out


def normalize_document(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise _invalid("document")
    unknown = sorted(set(document) - set(_FIELDS))
    if unknown:
        raise _invalid(unknown[0])
    for field in ("key", "kind", "summary", "scope"):
        if field not in document:
            raise _invalid(field)
    key = document.get("key")
    kind = document.get("kind")
    summary = document.get("summary")
    if not isinstance(key, str) or _NAME.fullmatch(key) is None:
        raise _invalid("key")
    if not isinstance(kind, str) or _NAME.fullmatch(kind) is None:
        raise _invalid("kind")
    if not isinstance(summary, str) or not summary:
        raise _invalid("summary")
    data = document.get("data", {})
    if not isinstance(data, dict):
        raise _invalid("data")
    normalized = {
        "key": key,
        "kind": kind,
        "summary": summary,
        "scope": _scope(document.get("scope")),
        "beans": _strings(document.get("beans", []), "beans"),
        "requires": _strings(document.get("requires", []), "requires"),
        "evidence": _evidence(document.get("evidence", [])),
        "data": data,
    }
    raw = canonical_bytes(normalized)
    if len(raw) > _MAX_DOCUMENT:
        raise _invalid("document", "资产文档超过 256 KiB")
    return json.loads(raw.decode("utf-8"))


def asset_id_for(document: Mapping[str, Any]) -> str:
    return "asset:" + digest_bytes(canonical_bytes(document))


def _record(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "document": json.loads(row["document"]),
        "by": str(row["by_actor"]),
        "created_at": str(row["created_at"]),
        "released": row["released_at"] is not None,
    }


def _event(ctx: Any, conn: Any, kind: str, data: dict[str, Any]) -> None:
    from mao.egress import SecretStore

    stored = SecretStore(ctx.root).redact_obj(data)
    conn.execute(
        "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
        (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(stored)),
    )


def _finished(conn: Any, kind: str, ref: str) -> None:
    if kind == "job":
        row = conn.execute("SELECT state FROM jobs WHERE id = ?", (ref,)).fetchone()
        if row is None or str(row["state"]) not in _JOB_DONE:
            raise _reference("job", ref)
        return
    if kind == "turn":
        row = conn.execute("SELECT state FROM agent_turns WHERE id = ?", (ref,)).fetchone()
        if row is None or str(row["state"]) not in _TURN_DONE:
            raise _reference("turn", ref)
        return
    if kind == "run":
        row = conn.execute("SELECT state FROM runs WHERE id = ?", (ref,)).fetchone()
        if row is None or str(row["state"]) not in _RUN_DONE:
            raise _reference("run", ref)
        return
    if kind == "receipt":
        row = conn.execute("SELECT status FROM receipts WHERE id = ?", (ref,)).fetchone()
        if row is None or str(row["status"]) not in _RECEIPT_DONE:
            raise _reference("receipt", ref)
        return
    if kind == "trial":
        row = conn.execute("SELECT state FROM asset_trials WHERE id = ?", (ref,)).fetchone()
        if row is None or str(row["state"]) == "open":
            raise _reference("trial", ref)
        return
    raise _reference(kind, ref)


def _bean_ok(ctx: Any, conn: Any, bean_id: str) -> None:
    row = conn.execute("SELECT sealed, expired_at FROM beans WHERE id = ?", (bean_id,)).fetchone()
    if row is None or not row["sealed"] or row["expired_at"] is not None:
        raise _reference("bean", bean_id)
    payloads = conn.execute("SELECT digest FROM payload_files WHERE bean_id = ?", (bean_id,)).fetchall()
    reports = conn.execute("SELECT report_digest FROM seals WHERE bean_id = ?", (bean_id,)).fetchall()
    for item in list(payloads) + list(reports):
        digest = str(item[0])
        path = ctx.warehouse.blob_path(digest)
        if not path.is_file():
            raise _reference("blob", digest)


def _validate_refs(ctx: Any, conn: Any, document: Mapping[str, Any]) -> None:
    for bean_id in document["beans"]:
        _bean_ok(ctx, conn, str(bean_id))
    for ref in document["requires"]:
        row = conn.execute(
            "SELECT released_at FROM asset_records WHERE id = ?",
            (ref,),
        ).fetchone()
        if row is None or row["released_at"] is not None:
            raise _reference("asset", str(ref))
    for item in document["evidence"]:
        _finished(conn, str(item["kind"]), str(item["id"]))


def _insert_edges(conn: Any, asset_id: str, document: Mapping[str, Any]) -> None:
    for bean_id in document["beans"]:
        conn.execute(
            "INSERT INTO asset_edges (asset_id, kind, ref_id) VALUES (?, 'bean', ?)",
            (asset_id, bean_id),
        )
    for ref in document["requires"]:
        conn.execute(
            "INSERT INTO asset_edges (asset_id, kind, ref_id) VALUES (?, 'requires', ?)",
            (asset_id, ref),
        )
    for item in document["evidence"]:
        conn.execute(
            "INSERT INTO asset_edges (asset_id, kind, ref_id) VALUES (?, ?, ?)",
            (asset_id, item["kind"], item["id"]),
        )


class AssetStore:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def put(self, document: Mapping[str, Any], *, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        actor = _actor(by)
        normalized = normalize_document(document)
        text = canonical_json(normalized)
        if _contains_secret(self.ctx, text):
            raise FactoryError("ASSET_SECRET", "资产文档含已知秘密", path="document")
        asset_id = "asset:" + digest_bytes(text.encode("utf-8"))
        with self.ctx.db.tx() as conn:
            existing = conn.execute("SELECT * FROM asset_records WHERE id = ?", (asset_id,)).fetchone()
            if existing is not None:
                return _record(existing)
            _validate_refs(self.ctx, conn, normalized)
            now = utc_iso(self.ctx.clock.now())
            conn.execute(
                "INSERT INTO asset_records (id, document, by_actor, created_at) VALUES (?, ?, ?, ?)",
                (asset_id, text, actor, now),
            )
            _insert_edges(conn, asset_id, normalized)
            _event(
                self.ctx,
                conn,
                "asset.created",
                {"id": asset_id, "subject": normalized["scope"]["subject"], "state": "created"},
            )
            row = conn.execute("SELECT * FROM asset_records WHERE id = ?", (asset_id,)).fetchone()
        return _record(row)

    def get(self, asset_id: str) -> dict[str, Any]:
        rows = self.ctx.db.query("SELECT * FROM asset_records WHERE id = ?", (asset_id,))
        if not rows:
            raise FactoryError("ASSET_NOT_FOUND", "资产不存在", id=asset_id)
        return _record(rows[0])

    def list(
        self, *, kind: str | None = None, subject: str | None = None, released: bool = False
    ) -> list[dict[str, Any]]:
        if released:
            sql = "SELECT * FROM asset_records WHERE released_at IS NOT NULL ORDER BY id"
        else:
            sql = "SELECT * FROM asset_records WHERE released_at IS NULL ORDER BY id"
        found = []
        for row in self.ctx.db.query(sql):
            record = _record(row)
            document = record["document"]
            if kind is not None and document.get("kind") != kind:
                continue
            if subject is not None and document.get("scope", {}).get("subject") != subject:
                continue
            found.append(record)
        return found

    def closure(self, roots: Sequence[str]) -> dict[str, Any]:
        with self.ctx.db.tx() as conn:
            return _closure(self.ctx, conn, list(roots))

    def release(self, asset_id: str, *, reason: str, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        if not isinstance(reason, str) or not reason:
            raise _invalid("reason")
        if _contains_secret(self.ctx, reason):
            raise FactoryError("ASSET_SECRET", "释放原因含已知秘密", path="reason")
        with self.ctx.db.tx() as conn:
            row = conn.execute("SELECT * FROM asset_records WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                raise FactoryError("ASSET_NOT_FOUND", "资产不存在", id=asset_id)
            if row["released_at"] is not None:
                return _record(row)
            holders = _release_blockers(conn, asset_id)
            if holders:
                raise FactoryError("ASSET_IN_USE", "释放会破坏活跃闭包", id=asset_id, roots=holders)
            now = utc_iso(self.ctx.clock.now())
            conn.execute(
                "UPDATE asset_records SET released_at = ?, release_reason = ? WHERE id = ?",
                (now, reason, asset_id),
            )
            document = json.loads(row["document"])
            _event(
                self.ctx,
                conn,
                "asset.released",
                {
                    "id": asset_id,
                    "subject": document.get("scope", {}).get("subject", ""),
                    "state": "released",
                    "reason": reason,
                },
            )
            updated = conn.execute("SELECT * FROM asset_records WHERE id = ?", (asset_id,)).fetchone()
        return _record(updated)


def _release_blockers(conn: Any, asset_id: str) -> list[str]:
    holders: set[str] = set()
    for row in conn.execute(
        """SELECT e.asset_id FROM asset_edges e
           JOIN asset_records r ON r.id = e.asset_id
           WHERE e.kind = 'requires' AND e.ref_id = ? AND r.released_at IS NULL""",
        (asset_id,),
    ):
        holders.add(str(row["asset_id"]))
    for row in conn.execute("SELECT slot FROM asset_heads WHERE asset_id = ?", (asset_id,)):
        holders.add(str(row["slot"]))
    for row in conn.execute(
        "SELECT id FROM asset_trials WHERE state = 'open' AND (target = ? OR contract = ?)",
        (asset_id, asset_id),
    ):
        holders.add(str(row["id"]))
    for row in conn.execute(
        """SELECT id FROM objectives
           WHERE asset_id = ? AND state IN ('active', 'paused')""",
        (asset_id,),
    ):
        holders.add(str(row["id"]))
    for row in conn.execute(
        """SELECT id FROM objective_cycles
           WHERE plan_asset = ? AND state NOT IN ('idle', 'blocked', 'completed', 'failed')""",
        (asset_id,),
    ):
        holders.add(str(row["id"]))
    return sorted(holders)


def _closure(ctx: Any, conn: Any, roots: list[str]) -> dict[str, Any]:
    assets: set[str] = set()
    pending = list(roots)
    for root in roots:
        row = conn.execute("SELECT id FROM asset_records WHERE id = ?", (root,)).fetchone()
        if row is None:
            raise FactoryError("ASSET_NOT_FOUND", "资产不存在", id=root)
    while pending:
        asset_id = pending.pop()
        if asset_id in assets:
            continue
        assets.add(asset_id)
        for row in conn.execute(
            "SELECT ref_id FROM asset_edges WHERE asset_id = ? AND kind = 'requires'",
            (asset_id,),
        ):
            ref = str(row["ref_id"])
            exists = conn.execute("SELECT 1 FROM asset_records WHERE id = ?", (ref,)).fetchone()
            if exists is None:
                raise _reference("asset", ref)
            pending.append(ref)
    beans: set[str] = set()
    evidence: set[tuple[str, str]] = set()
    for asset_id in assets:
        for row in conn.execute(
            "SELECT kind, ref_id FROM asset_edges WHERE asset_id = ?",
            (asset_id,),
        ):
            kind = str(row["kind"])
            ref = str(row["ref_id"])
            if kind == "bean":
                beans.add(ref)
            elif kind in _EVIDENCE_KINDS:
                evidence.add((kind, ref))
    _expand_evidence(conn, evidence, beans, assets)
    beans = _upstream_beans(conn, beans)
    blobs: set[str] = set()
    epochs: set[str] = set()
    for bean_id in beans:
        row = conn.execute("SELECT id FROM beans WHERE id = ?", (bean_id,)).fetchone()
        if row is None:
            raise _reference("bean", bean_id)
        for item in conn.execute("SELECT digest FROM payload_files WHERE bean_id = ?", (bean_id,)):
            blobs.add(str(item["digest"]))
        for item in conn.execute("SELECT report_digest, job_id FROM seals WHERE bean_id = ?", (bean_id,)):
            digest = str(item["report_digest"])
            if ctx.warehouse.blob_path(digest).is_file():
                blobs.add(digest)
            if item["job_id"]:
                _add_epoch(conn, epochs, _job_epoch(conn, str(item["job_id"])))
    for digest in sorted(blobs):
        if not ctx.warehouse.blob_path(digest).is_file():
            raise _reference("blob", digest)
    for commit in sorted(epochs):
        row = conn.execute('SELECT 1 FROM epochs WHERE "commit" = ?', (commit,)).fetchone()
        if row is None:
            raise _reference("epoch", commit)
    return {
        "assets": sorted(assets),
        "beans": sorted(beans),
        "blobs": sorted(blobs),
        "evidence": [{"kind": kind, "id": ref} for kind, ref in sorted(evidence)],
        "epochs": sorted(epochs),
    }


def _job_epoch(conn: Any, job_id: str) -> str:
    row = conn.execute("SELECT epoch FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise _reference("job", job_id)
    return str(row["epoch"] or "")


def _add_epoch(conn: Any, epochs: set[str], commit: str) -> None:
    if commit:
        epochs.add(commit)


def _json_ids(text: Any) -> list[str]:
    try:
        body = json.loads(text or "{}")
    except json.JSONDecodeError:
        return []
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and value.startswith("sha256:"):
            found.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(body)
    return found


def _expand_evidence(conn: Any, evidence: set[tuple[str, str]], beans: set[str], assets: set[str]) -> None:
    pending = list(evidence)
    seen = set(evidence)
    while pending:
        kind, ref = pending.pop()
        if kind == "job":
            row = conn.execute("SELECT inputs, outputs, epoch FROM jobs WHERE id = ?", (ref,)).fetchone()
            if row is None:
                raise _reference("job", ref)
            beans.update(_json_ids(row["inputs"]))
            beans.update(_json_ids(row["outputs"]))
            continue
        if kind == "run":
            row = conn.execute("SELECT id FROM runs WHERE id = ?", (ref,)).fetchone()
            if row is None:
                raise _reference("run", ref)
            for node in conn.execute(
                "SELECT job_id FROM run_nodes WHERE run_id = ? AND job_id IS NOT NULL", (ref,)
            ):
                item = ("job", str(node["job_id"]))
                if item not in seen:
                    seen.add(item)
                    evidence.add(item)
                    pending.append(item)
            continue
        if kind == "turn":
            row = conn.execute("SELECT id FROM agent_turns WHERE id = ?", (ref,)).fetchone()
            if row is None:
                raise _reference("turn", ref)
            continue
        if kind == "receipt":
            row = conn.execute("SELECT inputs FROM receipts WHERE id = ?", (ref,)).fetchone()
            if row is None:
                raise _reference("receipt", ref)
            beans.update(_json_ids(row["inputs"]))
            continue
        if kind == "trial":
            row = conn.execute(
                "SELECT target, contract, result FROM asset_trials WHERE id = ?", (ref,)
            ).fetchone()
            if row is None:
                raise _reference("trial", ref)
            for asset_id in (str(row["target"]), str(row["contract"])):
                assets.add(asset_id)
            if row["result"]:
                try:
                    result = json.loads(row["result"])
                except json.JSONDecodeError as exc:
                    raise _reference("trial", ref) from exc
                for item in result.get("evidence") or []:
                    if isinstance(item, dict) and item.get("kind") in _EVIDENCE_KINDS and item.get("id"):
                        pair = (str(item["kind"]), str(item["id"]))
                        if pair not in seen:
                            seen.add(pair)
                            evidence.add(pair)
                            pending.append(pair)


def _upstream_beans(conn: Any, seeds: set[str]) -> set[str]:
    found = set(seeds)
    queue = list(seeds)
    while queue:
        bean_id = queue.pop()
        rows = conn.execute(
            """SELECT src.bean_id FROM lineage out
               JOIN lineage src ON src.job_id = out.job_id AND src.direction = 'in'
               WHERE out.bean_id = ? AND out.direction = 'out'""",
            (bean_id,),
        ).fetchall()
        for row in rows:
            ref = str(row["bean_id"])
            if ref not in found:
                found.add(ref)
                queue.append(ref)
    return found


def roots_holding_bean(conn: Any, bean_id: str) -> list[str]:
    """同一写事务里重检：哪些未释放根的闭包包含这个 Bean。"""
    roots = [
        str(row["id"])
        for row in conn.execute("SELECT id FROM asset_records WHERE released_at IS NULL ORDER BY id")
    ]
    if not roots:
        return []
    holding: list[str] = []
    for root in roots:
        if bean_id in _closure_beans_only(conn, root):
            holding.append(root)
    return holding


def _closure_beans_only(conn: Any, root: str) -> set[str]:
    assets: set[str] = set()
    pending = [root]
    while pending:
        asset_id = pending.pop()
        if asset_id in assets:
            continue
        if conn.execute("SELECT 1 FROM asset_records WHERE id = ?", (asset_id,)).fetchone() is None:
            continue
        assets.add(asset_id)
        for row in conn.execute(
            "SELECT ref_id FROM asset_edges WHERE asset_id = ? AND kind = 'requires'",
            (asset_id,),
        ):
            pending.append(str(row["ref_id"]))
    beans: set[str] = set()
    evidence: set[tuple[str, str]] = set()
    for asset_id in assets:
        for row in conn.execute("SELECT kind, ref_id FROM asset_edges WHERE asset_id = ?", (asset_id,)):
            kind = str(row["kind"])
            ref = str(row["ref_id"])
            if kind == "bean":
                beans.add(ref)
            elif kind in _EVIDENCE_KINDS:
                evidence.add((kind, ref))
    if evidence:
        _expand_evidence(conn, evidence, beans, assets)
    return _upstream_beans(conn, beans)


def capture_handoff(
    ctx: Any, agent: str, document: Mapping[str, Any], *, expected: str | None, by: str
) -> dict[str, Any]:
    """验证并保存 handoff 资产，CAS 更新 handoff:<agent> 指针。"""
    require_enabled(ctx)
    _actor(by)
    if not isinstance(agent, str) or not agent:
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接岗位不合法", agent=agent, field="agent")
    normalized = normalize_document(document)
    data = normalized.get("data") or {}
    if normalized.get("kind") != "handoff" or data.get("agent") != agent:
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接文档与岗位不符", agent=agent, field="kind")
    if normalized.get("scope", {}).get("subject") != f"agent:{agent}":
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接范围不符", agent=agent, field="scope")
    if normalized.get("key") != f"handoff.{agent}":
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接键不符", agent=agent, field="key")
    _check_handoff_data(ctx, agent, data)
    record = AssetStore(ctx).put(normalized, by=by)
    slot = f"handoff:{agent}"
    with ctx.db.tx() as conn:
        _cas_head(ctx, conn, slot, record["id"], expected, proof=record["id"])
        _event(
            ctx,
            conn,
            "knowledge.handoff",
            {"id": record["id"], "subject": f"agent:{agent}", "state": "captured"},
        )
    return record


def _check_handoff_data(ctx: Any, agent: str, data: Mapping[str, Any]) -> None:
    required = ("agent", "tasks", "decisions", "uncertainties", "knowledge", "workspace", "external")
    if not isinstance(data, Mapping) or any(field not in data for field in required):
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接数据不完整", agent=agent, field="data")
    if data.get("agent") != agent:
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接岗位不符", agent=agent, field="agent")
    for field in ("decisions", "uncertainties", "knowledge"):
        values = data.get(field)
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接列表不合法", agent=agent, field=field)
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接任务不合法", agent=agent, field="tasks")
    for task in tasks:
        if not isinstance(task, dict) or set(task) < {"kind", "id", "state", "next"}:
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接任务不合法", agent=agent, field="tasks")
        if (
            task.get("kind") not in ("job", "run")
            or not isinstance(task.get("next"), str)
            or not task["next"]
        ):
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接任务不合法", agent=agent, field="tasks")
        _task_state(ctx, agent, str(task["kind"]), str(task["id"]), str(task["state"]))
    workspace = data.get("workspace")
    if not isinstance(workspace, list):
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接工作区不合法", agent=agent, field="workspace")
    for item in workspace:
        if not isinstance(item, dict):
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接工作区不合法", agent=agent, field="workspace")
        path = str(item.get("path") or "")
        if _unsafe_workspace_path(path):
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接路径不安全", agent=agent, field="workspace")
    external = data.get("external")
    if not isinstance(external, list):
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接外部事项不合法", agent=agent, field="external")


def _task_state(ctx: Any, agent: str, kind: str, ref: str, state: str) -> None:
    if kind == "job":
        row = ctx.db.query("SELECT state FROM jobs WHERE id = ?", (ref,))
    else:
        row = ctx.db.query("SELECT state FROM runs WHERE id = ?", (ref,))
    if not row or str(row[0]["state"]) != state:
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接任务状态与库中不符", agent=agent, field="tasks")


def _unsafe_workspace_path(path: str) -> bool:
    text = path.replace("\\", "/")
    if not text or text.startswith("/") or ".." in text.split("/") or ":" in text:
        return True
    blocked = (".git", ".codex", ".claude", ".home", ".runtime")
    parts = [part for part in text.split("/") if part]
    return any(part in blocked or part.startswith(".") and part in blocked for part in parts)


def _cas_head(
    ctx: Any, conn: Any, slot: str, asset_id: str, expected: str | None, *, proof: str
) -> dict[str, Any]:
    row = conn.execute("SELECT asset_id, generation FROM asset_heads WHERE slot = ?", (slot,)).fetchone()
    actual = None if row is None else str(row["asset_id"])
    if actual != expected:
        raise FactoryError(
            "ASSET_HEAD_CONFLICT", "正式指针与预期不符", slot=slot, expected=expected, actual=actual
        )
    generation = 1 if row is None else int(row["generation"]) + 1
    now = utc_iso(ctx.clock.now())
    if row is None:
        conn.execute(
            "INSERT INTO asset_heads (slot, asset_id, generation, proof, updated_at) VALUES (?, ?, ?, ?, ?)",
            (slot, asset_id, generation, proof, now),
        )
    else:
        conn.execute(
            "UPDATE asset_heads SET asset_id = ?, generation = ?, proof = ?, updated_at = ? WHERE slot = ?",
            (asset_id, generation, proof, now, slot),
        )
    return {"slot": slot, "asset_id": asset_id, "generation": generation, "proof": proof, "stale": False}


def protected_bean_ids(ctx: Any) -> set[str]:
    with ctx.db.tx() as conn:
        found: set[str] = set()
        for row in conn.execute("SELECT id FROM asset_records WHERE released_at IS NULL"):
            found.update(_closure_beans_only(conn, str(row["id"])))
        return found
