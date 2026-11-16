"""正式知识：授权契约、真实执行证据与 CAS 晋升。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mao.assets import AssetStore, assets_config, require_enabled
from mao.base import canonical_bytes, canonical_json, digest_bytes, utc_iso
from mao.errors import FactoryError

_PURPOSES = ("knowledge", "takeover", "release")
_TAKEOVER_TAGS = ("normal", "exception", "recovery", "uncertainty")
_FOREMAN_TAGS = ("planning", "budget", "change", "handoff")
_CLAIM_KINDS = ("fact", "hypothesis", "method", "directive")


def _actor(by: str) -> str:
    from mao.assets import _actor as check

    return check(by)


def _event(ctx: Any, conn: Any, kind: str, data: dict[str, Any]) -> None:
    from mao.egress import SecretStore

    stored = SecretStore(ctx.root).redact_obj(data)
    conn.execute(
        "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
        (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(stored)),
    )


def _load_asset(ctx: Any, asset_id: str) -> dict[str, Any]:
    return AssetStore(ctx).get(asset_id)


def _subject_station(station: str) -> str:
    text = station.split(":", 1)[-1] if station.startswith("Station:") else station
    if text.startswith("station:"):
        return text
    return "station:" + text


class LearningStore:
    def __init__(self, ctx: Any, epochs: Any) -> None:
        self.ctx = ctx
        self.epochs = epochs

    def authorize(self, asset_id: str, *, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        actor = _actor(by)
        record = _load_asset(self.ctx, asset_id)
        kind = _authorization_kind(record)
        standards = _standards(self.ctx, record)
        text = canonical_json(standards)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            row = conn.execute(
                "SELECT standards, kind, by_actor, at FROM asset_authorizations WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if row is not None:
                if str(row["standards"]) != text:
                    raise FactoryError("ASSET_CONTRACT_INVALID", "授权标准已变化", field="standards")
                return {
                    "asset_id": asset_id,
                    "kind": str(row["kind"]),
                    "by": str(row["by_actor"]),
                    "at": str(row["at"]),
                }
            conn.execute(
                """INSERT INTO asset_authorizations (asset_id, kind, standards, by_actor, at)
                   VALUES (?, ?, ?, ?, ?)""",
                (asset_id, kind, text, actor, now),
            )
            _event(
                self.ctx,
                conn,
                "knowledge.authorized",
                {"id": asset_id, "subject": _subject_of(record), "state": kind},
            )
        return {"asset_id": asset_id, "kind": kind, "by": actor, "at": now}

    def open_trial(self, target: str, contract: str, *, purpose: str, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        actor = _actor(by)
        if purpose not in _PURPOSES:
            raise FactoryError("ASSET_CONTRACT_INVALID", "试验用途不合法", field="purpose")
        target_rec = _load_asset(self.ctx, target)
        contract_rec = _load_asset(self.ctx, contract)
        _require_authorized(self.ctx, contract_rec)
        _match_scope(target_rec, contract_rec)
        _require_target_shape(target_rec, purpose)
        _require_coverage(contract_rec, purpose, _subject_of(target_rec))
        subject = _subject_of(target_rec)
        context = self._context(target_rec, contract_rec)
        digest = digest_bytes(canonical_bytes(context))
        trial_id = self.ctx.ids.new("atry")
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            open_row = conn.execute(
                "SELECT id FROM asset_trials WHERE subject = ? AND state = 'open'",
                (subject,),
            ).fetchone()
            if open_row is not None:
                raise FactoryError("ASSET_TRIAL_CONFLICT", "该岗位已有进行中的试验", subject=subject)
            watermark = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
            conn.execute(
                """INSERT INTO asset_trials (
                       id, target, contract, purpose, subject, context, context_digest, watermark,
                       state, result, by_actor, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?)""",
                (
                    trial_id,
                    target,
                    contract,
                    purpose,
                    subject,
                    canonical_json(context),
                    digest,
                    int(watermark),
                    actor,
                    now,
                ),
            )
            _event(
                self.ctx, conn, "asset.trial.opened", {"id": trial_id, "subject": subject, "state": "open"}
            )
        return self.get_trial(trial_id)

    def evaluate(
        self, trial_id: str, evidence: Mapping[str, Mapping[str, str]], *, by: str
    ) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        trial = self._trial_row(trial_id)
        normalized = _normalize_evidence(evidence)
        if trial["state"] != "open":
            stored = json.loads(trial["result"] or "{}")
            if stored.get("evidence") == normalized:
                return self._view(trial, stored)
            raise FactoryError("ASSET_TRIAL_CONFLICT", "终态试验不能改写证据", subject=str(trial["subject"]))
        contract = _load_asset(self.ctx, str(trial["contract"]))
        cases = list(contract["document"]["data"]["cases"])
        names = [str(case["name"]) for case in cases]
        if sorted(normalized) != sorted(names) or len(normalized) != len(names):
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "证据没有精确覆盖契约用例", case="", reason="coverage"
            )
        checks: list[dict[str, Any]] = []
        failed = False
        for case in cases:
            name = str(case["name"])
            item = normalized[name]
            try:
                out = self._execution_out(trial, case, str(item["kind"]), str(item["id"]))
            except FactoryError:
                raise
            ok, code = _assert_case(str(case["assert"]), out)
            checks.append({"name": name, "ok": ok, "code": code})
            if not ok:
                failed = True
        state = "failed" if failed else "passed"
        result = {
            "evidence": normalized,
            "checks": checks,
        }
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            current = conn.execute("SELECT state FROM asset_trials WHERE id = ?", (trial_id,)).fetchone()
            if current is None or str(current["state"]) != "open":
                raise FactoryError(
                    "ASSET_TRIAL_CONFLICT", "终态试验不能改写证据", subject=str(trial["subject"])
                )
            conn.execute(
                "UPDATE asset_trials SET state = ?, result = ?, finished_at = ? WHERE id = ?",
                (state, canonical_json(result), now, trial_id),
            )
            _event(
                self.ctx,
                conn,
                "asset.trial.finished",
                {"id": trial_id, "subject": str(trial["subject"]), "state": state},
            )
            if state == "failed" and str(trial["purpose"]) == "takeover":
                agent = str(trial["subject"]).removeprefix("agent:")
                conn.execute(
                    "UPDATE agents SET state = 'paused', updated_at = ? WHERE name = ?",
                    (now, agent),
                )
                conn.execute(
                    "UPDATE agent_workspaces SET phase = 'failed', updated_at = ? WHERE agent = ? AND trial_id = ?",
                    (now, agent, trial_id),
                )
        return self.get_trial(trial_id)

    def get_trial(self, trial_id: str) -> dict[str, Any]:
        return self._view(self._trial_row(trial_id), None)

    def cancel_trial(self, trial_id: str, *, reason: str, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        if not isinstance(reason, str) or not reason:
            raise FactoryError("ASSET_INVALID", "取消原因不能为空", field="reason")
        with self.ctx.db.tx() as conn:
            row = conn.execute("SELECT state, subject FROM asset_trials WHERE id = ?", (trial_id,)).fetchone()
            if row is None:
                raise FactoryError("ASSET_NOT_FOUND", "试验不存在", id=trial_id)
            if str(row["state"]) != "open":
                raise FactoryError("ASSET_TRIAL_CONFLICT", "终态试验不能取消", subject=str(row["subject"]))
            conn.execute(
                "UPDATE asset_trials SET state = 'cancelled', finished_at = ? WHERE id = ?",
                (utc_iso(self.ctx.clock.now()), trial_id),
            )
            _event(
                self.ctx,
                conn,
                "asset.trial.cancelled",
                {"id": trial_id, "subject": str(row["subject"]), "state": "cancelled", "reason": reason},
            )
        return self.get_trial(trial_id)

    def promote(self, target: str, *, trial_id: str | None, expected: str | None, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        record = _load_asset(self.ctx, target)
        subject = _subject_of(record)
        key = str(record["document"]["key"])
        slot = f"knowledge:{subject}:{key}"
        proof = ""
        with self.ctx.db.tx() as conn:
            if trial_id is None:
                if _claim_kind(record) != "directive":
                    raise FactoryError("ASSET_PROOF_REQUIRED", "缺少通过的试验", id=target)
                auth = conn.execute(
                    "SELECT asset_id FROM asset_authorizations WHERE asset_id = ?",
                    (target,),
                ).fetchone()
                if auth is None:
                    raise FactoryError("ASSET_NOT_AUTHORIZED", "directive 未经人授权", id=target)
                proof = target
            else:
                trial = conn.execute("SELECT * FROM asset_trials WHERE id = ?", (trial_id,)).fetchone()
                if trial is None or str(trial["state"]) != "passed" or str(trial["target"]) != target:
                    raise FactoryError("ASSET_PROOF_REQUIRED", "缺少通过的试验", id=trial_id or target)
                if self._is_stale(trial):
                    raise FactoryError("ASSET_CONTEXT_STALE", "证明的上下文已过期", id=trial_id)
                proof = trial_id
            from mao.assets import _cas_head

            head = _cas_head(self.ctx, conn, slot, target, expected, proof=proof)
            _event(
                self.ctx, conn, "knowledge.promoted", {"id": target, "subject": subject, "state": "promoted"}
            )
        head["stale"] = False
        return head

    def invalidate(self, target: str, *, reason: str, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        if not isinstance(reason, str) or not reason:
            raise FactoryError("ASSET_INVALID", "失效原因不能为空", field="reason")
        cleared: list[str] = []
        with self.ctx.db.tx() as conn:
            rows = conn.execute("SELECT slot FROM asset_heads WHERE asset_id = ?", (target,)).fetchall()
            for row in rows:
                slot = str(row["slot"])
                conn.execute("DELETE FROM asset_heads WHERE slot = ?", (slot,))
                cleared.append(slot)
            _event(
                self.ctx,
                conn,
                "knowledge.invalidated",
                {"id": target, "subject": "", "state": "invalidated", "reason": reason},
            )
        return {"target": target, "cleared": sorted(cleared)}

    def heads(self, subject: str) -> list[dict[str, Any]]:
        prefix = f"knowledge:{subject}:"
        found = []
        for row in self.ctx.db.query(
            "SELECT slot, asset_id, generation, proof FROM asset_heads WHERE slot LIKE ? ORDER BY slot",
            (prefix + "%",),
        ):
            stale = self._proof_stale(None if row["proof"] is None else str(row["proof"]))
            found.append(
                {
                    "slot": str(row["slot"]),
                    "asset_id": str(row["asset_id"]),
                    "generation": int(row["generation"]),
                    "proof": None if row["proof"] is None else str(row["proof"]),
                    "stale": stale,
                }
            )
        return found

    def prepare_takeover(self, trial_id: str, *, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        trial = self._trial_row(trial_id)
        if str(trial["state"]) != "open" or str(trial["purpose"]) != "takeover":
            raise FactoryError("ASSET_PROOF_REQUIRED", "不是进行中的接管试验", id=trial_id)
        agent = str(trial["subject"]).removeprefix("agent:")
        running = self.ctx.db.query(
            "SELECT id FROM agent_turns WHERE agent = ? AND state = 'running' AND kind = 'work'",
            (agent,),
        )
        now = utc_iso(self.ctx.clock.now())
        phase = "pending" if running else "testing"
        with self.ctx.db.tx() as conn:
            other = conn.execute(
                "SELECT trial_id, phase FROM agent_workspaces WHERE agent = ? AND phase IN ('pending', 'testing')",
                (agent,),
            ).fetchone()
            if other is not None and str(other["trial_id"] or "") != trial_id:
                raise FactoryError("ASSET_TAKEOVER_BUSY", "岗位已有接管", agent=agent)
            checkpoint = _handoff_checkpoint(self.ctx, trial)
            if phase == "pending":
                _workspace_row(conn, agent, _default_workspace(agent), None, trial_id, "pending", now)
            else:
                _install_takeover(self.ctx, conn, agent, trial_id, now, checkpoint)
            _event(
                self.ctx,
                conn,
                "agent.takeover.prepared",
                {
                    "id": trial_id,
                    "subject": str(trial["subject"]),
                    "state": phase,
                    "checkpoint": checkpoint,
                },
            )
        return {"agent": agent, "trial_id": trial_id, "phase": phase}

    def finish_takeover(self, trial_id: str, *, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        trial = self._trial_row(trial_id)
        if str(trial["state"]) != "passed" or str(trial["purpose"]) != "takeover":
            raise FactoryError("ASSET_PROOF_REQUIRED", "接管试验尚未通过", id=trial_id)
        agent = str(trial["subject"]).removeprefix("agent:")
        slot = f"capability:{trial['subject']}"
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            from mao.assets import _cas_head

            current = conn.execute("SELECT asset_id FROM asset_heads WHERE slot = ?", (slot,)).fetchone()
            expected = None if current is None else str(current["asset_id"])
            if current is not None and expected == trial_id:
                expected = trial_id
            _cas_head(
                self.ctx,
                conn,
                slot,
                str(trial["target"]),
                None if current is None else str(current["asset_id"]),
                proof=trial_id,
            )
            conn.execute(
                "UPDATE agent_workspaces SET phase = 'stable', updated_at = ? WHERE agent = ?",
                (now, agent),
            )
            _event(
                self.ctx,
                conn,
                "agent.takeover.finished",
                {"id": trial_id, "subject": str(trial["subject"]), "state": "stable"},
            )
        return {"agent": agent, "trial_id": trial_id, "phase": "stable"}

    def abort_takeover(self, trial_id: str, *, reason: str, by: str) -> dict[str, Any]:
        require_enabled(self.ctx)
        _actor(by)
        if not isinstance(reason, str) or not reason:
            raise FactoryError("ASSET_INVALID", "中止原因不能为空", field="reason")
        trial = self._trial_row(trial_id)
        agent = str(trial["subject"]).removeprefix("agent:")
        running = self.ctx.db.query(
            "SELECT id FROM agent_turns WHERE agent = ? AND state = 'running'",
            (agent,),
        )
        if running:
            raise FactoryError("ASSET_TAKEOVER_BUSY", "接管轮次仍在运行", agent=agent)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            row = conn.execute(
                "SELECT previous_path FROM agent_workspaces WHERE agent = ?", (agent,)
            ).fetchone()
            previous = None if row is None or row["previous_path"] is None else str(row["previous_path"])
            restored = previous or _default_workspace(agent)
            conn.execute(
                """UPDATE agent_workspaces
                   SET current_path = ?, phase = 'stable', trial_id = NULL, updated_at = ?
                   WHERE agent = ?""",
                (restored, now, agent),
            )
            conn.execute(
                "UPDATE agents SET session = NULL, state = 'paused', updated_at = ? WHERE name = ?",
                (now, agent),
            )
            _event(
                self.ctx,
                conn,
                "agent.takeover.aborted",
                {"id": trial_id, "subject": str(trial["subject"]), "state": "stable", "reason": reason},
            )
        return {"agent": agent, "trial_id": trial_id, "phase": "stable"}

    def _trial_row(self, trial_id: str) -> Any:
        rows = self.ctx.db.query("SELECT * FROM asset_trials WHERE id = ?", (trial_id,))
        if not rows:
            raise FactoryError("ASSET_NOT_FOUND", "试验不存在", id=trial_id)
        return rows[0]

    def _view(self, trial: Any, result: dict[str, Any] | None) -> dict[str, Any]:
        stored = result if result is not None else json.loads(trial["result"] or "{}")
        return {
            "id": str(trial["id"]),
            "target": str(trial["target"]),
            "contract": str(trial["contract"]),
            "purpose": str(trial["purpose"]),
            "subject": str(trial["subject"]),
            "state": str(trial["state"]),
            "context_digest": str(trial["context_digest"]),
            "evidence": stored.get("evidence") or {},
            "checks": stored.get("checks") or [],
            "stale": self._is_stale(trial),
        }

    def _context(self, target: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
        section = require_enabled(self.ctx)
        assets = [item["id"] for item in self.pinned_records(target)]
        return {
            "registry": {},
            "files": {},
            "environment": section.get("environment") or {},
            "assets": assets,
            "runtime": {},
            "workspace_generation": None,
            "contract": contract["id"],
        }

    def pinned_records(self, target: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        subject = _subject_of(target) if target is not None else ""
        chosen: dict[str, dict[str, Any]] = {}
        if subject:
            rows = self.ctx.db.query(
                "SELECT asset_id FROM asset_heads WHERE slot LIKE ? ORDER BY slot",
                (f"knowledge:{subject}:%",),
            )
            for row in rows:
                record = _load_asset(self.ctx, str(row["asset_id"]))
                chosen[str(record["document"]["key"])] = record
        if target is not None and (not subject or _subject_of(target) == subject):
            chosen[str(target["document"]["key"])] = target
        return [chosen[key] for key in sorted(chosen)]

    def _is_stale(self, trial: Any) -> bool:
        if str(trial["state"]) == "open":
            return False
        stored = json.loads(trial["context"] or "{}")
        section = assets_config(self.ctx) or {}
        return stored.get("environment") != (section.get("environment") or {})

    def _proof_stale(self, proof: str | None) -> bool:
        if not proof:
            return True
        rows = self.ctx.db.query("SELECT * FROM asset_trials WHERE id = ?", (proof,))
        if not rows:
            return False
        return self._is_stale(rows[0])

    def _execution_out(
        self, trial: Any, case: Mapping[str, Any], kind: str, execution_id: str
    ) -> dict[str, Any]:
        if kind != str(case["kind"]):
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "证据类型与用例不符", case=str(case["name"]), reason="kind"
            )
        watermark = int(trial["watermark"])
        target = _load_asset(self.ctx, str(trial["target"]))
        if kind == "job":
            return self._job_out(trial, case, execution_id, watermark, target)
        if kind == "turn":
            return self._turn_out(trial, case, execution_id, watermark, target)
        raise FactoryError(
            "ASSET_EVIDENCE_INVALID", "证据类型不受支持", case=str(case["name"]), reason="kind"
        )

    def _job_out(
        self, trial: Any, case: Mapping[str, Any], job_id: str, watermark: int, target: dict[str, Any]
    ) -> dict[str, Any]:
        from mao.jobs import load_job

        try:
            job = load_job(self.ctx.db, job_id)
        except FactoryError as exc:
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "job 不存在", case=str(case["name"]), reason="missing"
            ) from exc
        if job.state.value not in ("SEALED", "DEAD"):
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "job 尚未结束", case=str(case["name"]), reason="open"
            )
        if _subject_station(job.station) != str(case["subject"]):
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "job 岗位不符", case=str(case["name"]), reason="subject"
            )
        events = self.ctx.db.query(
            "SELECT seq, kind, data FROM events WHERE job_id = ? ORDER BY seq",
            (job_id,),
        )
        created = False
        running = False
        for event in events:
            if int(event["seq"]) <= watermark:
                continue
            kind = str(event["kind"])
            data = json.loads(event["data"])
            if kind == "job.created":
                created = True
            if kind == "job.state" and data.get("to") == "RUNNING":
                running = True
        if not created or not running:
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "job 不是水位后的新执行", case=str(case["name"]), reason="watermark"
            )
        inputs = {port: list(ids) for port, ids in job.inputs.items()}
        flat = [bean for ids in inputs.values() for bean in ids]
        wanted = list(target["document"].get("beans") or [])
        if case.get("uses_target"):
            if not wanted or any(bean not in flat for bean in wanted):
                raise FactoryError(
                    "ASSET_EVIDENCE_INVALID",
                    "job 没有直接使用候选材料",
                    case=str(case["name"]),
                    reason="target",
                )
        use = self.ctx.db.query(
            "SELECT context, assets FROM asset_uses WHERE kind = 'job' AND execution_id = ?",
            (job_id,),
        )
        if use:
            recorded = json.loads(use[0]["assets"])
            if case.get("uses_target") and target["id"] not in recorded:
                raise FactoryError(
                    "ASSET_EVIDENCE_INVALID", "执行记录没有绑定候选", case=str(case["name"]), reason="use"
                )
        return {
            "kind": "job",
            "id": job.id,
            "subject": _subject_station(job.station),
            "state": job.state.value,
            "cost_usd": job.cost_usd,
            "epoch": job.epoch,
            "context_digest": str(trial["context_digest"]),
            "assets": [target["id"]] if case.get("uses_target") else [],
            "fresh": True,
            "handoff": None,
            "inputs": _bean_ports(self.ctx, inputs),
            "outputs": _bean_ports(self.ctx, job.outputs),
            "attempt": job.attempt,
            "receipts": [],
            "actions": [],
        }

    def _turn_out(
        self, trial: Any, case: Mapping[str, Any], turn_id: str, watermark: int, target: dict[str, Any]
    ) -> dict[str, Any]:
        rows = self.ctx.db.query("SELECT * FROM agent_turns WHERE id = ?", (turn_id,))
        if not rows:
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "turn 不存在", case=str(case["name"]), reason="missing"
            )
        row = rows[0]
        if str(row["state"]) == "running":
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "turn 尚未结束", case=str(case["name"]), reason="open"
            )
        subject = "agent:" + str(row["agent"])
        if subject != str(case["subject"]):
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID", "turn 岗位不符", case=str(case["name"]), reason="subject"
            )
        started = self.ctx.db.query(
            "SELECT seq, data FROM events WHERE kind = 'agent.turn.started' ORDER BY seq"
        )
        fresh_start = False
        for event in started:
            data = json.loads(event["data"])
            if data.get("turn") == turn_id and int(event["seq"]) > watermark:
                fresh_start = True
        if not fresh_start:
            raise FactoryError(
                "ASSET_EVIDENCE_INVALID",
                "turn 不是水位后的新执行",
                case=str(case["name"]),
                reason="watermark",
            )
        use_rows = self.ctx.db.query(
            "SELECT assets, handoff, fresh, context, actions FROM asset_uses WHERE kind = 'turn' AND execution_id = ?",
            (turn_id,),
        )
        assets = json.loads(use_rows[0]["assets"]) if use_rows else []
        handoff = None if not use_rows or use_rows[0]["handoff"] is None else str(use_rows[0]["handoff"])
        if case.get("uses_target"):
            if str(trial["purpose"]) == "takeover":
                if handoff != target["id"]:
                    raise FactoryError(
                        "ASSET_EVIDENCE_INVALID",
                        "轮次没有使用接管交接",
                        case=str(case["name"]),
                        reason="target",
                    )
            elif target["id"] not in assets:
                raise FactoryError(
                    "ASSET_EVIDENCE_INVALID", "轮次没有挂载候选", case=str(case["name"]), reason="target"
                )
        messages = json.loads(row["messages"])
        return {
            "kind": "turn",
            "id": turn_id,
            "subject": subject,
            "state": str(row["state"]),
            "cost_usd": float(row["cost_usd"]),
            "epoch": "",
            "context_digest": str(trial["context_digest"]),
            "assets": assets,
            "fresh": bool(use_rows and use_rows[0]["fresh"]),
            "handoff": handoff,
            "agent": str(row["agent"]),
            "conversation": int(row["conversation"]),
            "messages": messages,
            "message_digests": _message_digests(self.ctx, messages),
            "workspace_generation": str(trial["id"]) if str(trial["purpose"]) == "takeover" else None,
            "actions": json.loads(use_rows[0]["actions"]) if use_rows else [],
        }


def install_pending_takeover(ctx: Any, agent: str) -> None:
    rows = ctx.db.query(
        "SELECT trial_id FROM agent_workspaces WHERE agent = ? AND phase = 'pending'",
        (agent,),
    )
    if not rows or not rows[0]["trial_id"]:
        return
    trial_id = str(rows[0]["trial_id"])
    now = utc_iso(ctx.clock.now())
    checkpoint = _prepared_checkpoint(ctx, trial_id)
    try:
        with ctx.db.tx() as conn:
            _install_takeover(ctx, conn, agent, trial_id, now, checkpoint)
    except FactoryError as exc:
        if exc.code != "ASSET_HANDOFF_INVALID":
            raise
        _refuse_stale_takeover(ctx, agent, trial_id)


def _default_workspace(agent: str) -> str:
    return f"agents/{agent}/workspace"


def _workspace_row(
    conn: Any, agent: str, current: str, previous: str | None, trial_id: str | None, phase: str, now: str
) -> None:
    conn.execute(
        """INSERT INTO agent_workspaces (agent, current_path, previous_path, trial_id, phase, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent) DO UPDATE SET
             current_path = excluded.current_path,
             previous_path = excluded.previous_path,
             trial_id = excluded.trial_id,
             phase = excluded.phase,
             updated_at = excluded.updated_at""",
        (agent, current, previous, trial_id, phase, now),
    )


def _install_takeover(
    ctx: Any, conn: Any, agent: str, trial_id: str, now: str, checkpoint: dict[str, Any] | None = None
) -> None:
    if _handoff_progressed(ctx, trial_id, checkpoint or _prepared_checkpoint(ctx, trial_id)):
        raise FactoryError("ASSET_HANDOFF_INVALID", "交接期间业务已变化", agent=agent, field="tasks")
    current = f"agents/{agent}/takeovers/{trial_id}/workspace"
    path = Path(ctx.root) / current
    trial_root = path.parent
    if path.exists():
        raise FactoryError("ASSET_TAKEOVER_BUSY", "接管工作区已存在", agent=agent)
    try:
        path.mkdir(parents=True)
        (path / "notebook").mkdir()
        (path / ".home").mkdir()
        (trial_root / "home").mkdir(parents=True, exist_ok=True)
        _materialize_declared(ctx, path, trial_id)
        if agent == "foreman":
            from mao.agents import clone_foreman_registry
            from mao.governance.epochs import EpochStore

            clone_foreman_registry(ctx, path / "registry", EpochStore(ctx).current())
    except Exception:
        import shutil

        shutil.rmtree(trial_root, ignore_errors=True)
        raise
    existing = conn.execute("SELECT current_path FROM agent_workspaces WHERE agent = ?", (agent,)).fetchone()
    previous = (
        str(existing["current_path"])
        if existing is not None and existing["current_path"]
        else _default_workspace(agent)
    )
    _workspace_row(conn, agent, current, previous, trial_id, "testing", now)
    conn.execute(
        "UPDATE agents SET session = NULL, conversation = conversation + 1, turns_in_conv = 0, "
        "sandbox = NULL, updated_at = ? WHERE name = ?",
        (now, agent),
    )


def _handoff_checkpoint(ctx: Any, trial: Any) -> dict[str, Any]:
    record = AssetStore(ctx).get(str(trial["target"]))
    data = record["document"].get("data") or {}
    tasks = []
    for task in data.get("tasks") or []:
        if isinstance(task, dict):
            tasks.append(
                {
                    "kind": str(task.get("kind") or ""),
                    "id": str(task.get("id") or ""),
                    "state": str(task.get("state") or ""),
                }
            )
    receipts = []
    for item in data.get("external") or []:
        if not isinstance(item, dict):
            continue
        receipt_id = str(item.get("receipt") or "")
        rows = ctx.db.query("SELECT status FROM receipts WHERE id = ?", (receipt_id,))
        receipts.append({"id": receipt_id, "status": None if not rows else str(rows[0]["status"])})
    return {"tasks": tasks, "receipts": receipts}


def _prepared_checkpoint(ctx: Any, trial_id: str) -> dict[str, Any]:
    rows = ctx.db.query("SELECT data FROM events WHERE kind = 'agent.takeover.prepared' ORDER BY seq DESC")
    for row in rows:
        body = json.loads(row["data"])
        if str(body.get("id") or "") == trial_id and isinstance(body.get("checkpoint"), dict):
            return body["checkpoint"]
    return {"tasks": [], "receipts": []}


def _handoff_progressed(ctx: Any, trial_id: str, checkpoint: dict[str, Any]) -> bool:
    trial = ctx.db.query("SELECT target FROM asset_trials WHERE id = ?", (trial_id,))
    if not trial:
        return True
    record = AssetStore(ctx).get(str(trial[0]["target"]))
    data = record["document"].get("data") or {}
    for task in data.get("tasks") or []:
        if not isinstance(task, dict):
            return True
        kind = str(task.get("kind") or "")
        ref = str(task.get("id") or "")
        state = str(task.get("state") or "")
        if kind == "job":
            row = ctx.db.query("SELECT state FROM jobs WHERE id = ?", (ref,))
        elif kind == "run":
            row = ctx.db.query("SELECT state FROM runs WHERE id = ?", (ref,))
        else:
            return True
        if not row or str(row[0]["state"]) != state:
            return True
    expected = {str(item.get("id")): item.get("status") for item in checkpoint.get("receipts") or []}
    for item in data.get("external") or []:
        if not isinstance(item, dict):
            return True
        receipt_id = str(item.get("receipt") or "")
        row = ctx.db.query("SELECT status FROM receipts WHERE id = ?", (receipt_id,))
        live = None if not row else str(row[0]["status"])
        if not row or (receipt_id in expected and live != expected[receipt_id]):
            return True
    return False


def _refuse_stale_takeover(ctx: Any, agent: str, trial_id: str) -> None:
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE agent_workspaces SET phase = 'stable', trial_id = NULL, updated_at = ? "
            "WHERE agent = ? AND phase = 'pending'",
            (now, agent),
        )
        _event(
            ctx,
            conn,
            "agent.takeover.stale",
            {"id": trial_id, "subject": "agent:" + agent, "state": "stable", "reason": "stale"},
        )
    from mao.agents import Mailbox
    from mao.foreman import WakeQueue

    WakeQueue(ctx).file("other", "agent:" + agent, {"id": trial_id, "reason": "stale"})
    Mailbox(ctx).post(
        "foreman",
        "message",
        "system:takeover",
        {"text": "交接已过期，请重新固化", "trial": trial_id, "agent": agent},
    )


def _materialize_declared(ctx: Any, workspace: Path, trial_id: str) -> None:
    import shutil

    from mao.assets import _unsafe_workspace_path

    trial = ctx.db.query("SELECT target FROM asset_trials WHERE id = ?", (trial_id,))
    if not trial:
        raise FactoryError("ASSET_NOT_FOUND", "试验不存在", id=trial_id)
    record = AssetStore(ctx).get(str(trial[0]["target"]))
    items = (record["document"].get("data") or {}).get("workspace") or []
    seen: list[str] = []
    root = workspace.resolve()
    for item in items:
        if not isinstance(item, dict):
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接工作区不合法", agent="", field="workspace")
        rel = str(item.get("path") or "").replace("\\", "/")
        if _unsafe_workspace_path(rel):
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接路径不安全", agent="", field="workspace")
        for other in seen:
            if rel == other or rel.startswith(other + "/") or other.startswith(rel + "/"):
                raise FactoryError("ASSET_HANDOFF_INVALID", "交接路径重叠", agent="", field="workspace")
        seen.append(rel)
        dest = workspace / rel
        if dest.is_symlink() or not _inside_workspace(dest, root):
            raise FactoryError("ASSET_HANDOFF_INVALID", "交接路径不安全", agent="", field="workspace")
        bean = ctx.warehouse.get(str(item.get("bean") or ""))
        payload_path = str(item.get("payload_path") or "")
        match = next((entry for entry in bean.payload if entry.path == payload_path), None)
        if match is None:
            raise FactoryError("ASSET_REFERENCE_INVALID", "交接载荷不存在", kind="bean", id=bean.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ctx.warehouse.blob_path(match.digest), dest)


def _inside_workspace(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _message_digests(ctx: Any, message_ids: list[str]) -> list[str]:
    found = []
    for message_id in message_ids:
        rows = ctx.db.query("SELECT body FROM agent_messages WHERE id = ?", (message_id,))
        if not rows:
            continue
        body = json.loads(rows[0]["body"])
        found.append(digest_bytes(canonical_bytes(body)))
    return found


def _authorization_kind(record: dict[str, Any]) -> str:
    document = record["document"]
    if document.get("kind") == "contract":
        _validate_contract(document)
        return "contract"
    if _claim_kind(record) == "directive":
        return "directive"
    raise FactoryError("ASSET_NOT_AUTHORIZED", "只能授权契约或 directive", id=record["id"])


def _claim_kind(record: dict[str, Any]) -> str:
    claim = (record["document"].get("data") or {}).get("claim") or {}
    kind = claim.get("kind")
    return str(kind) if isinstance(kind, str) else ""


def _subject_of(record: dict[str, Any]) -> str:
    return str(record["document"]["scope"]["subject"])


def _standards(ctx: Any, record: dict[str, Any]) -> dict[str, Any]:
    document = record["document"]
    if document.get("kind") == "contract":
        _validate_contract(document)
        data = document["data"]
        return {
            "registry": list(data.get("registry") or []),
            "files": list(data.get("files") or []),
            "cases": data["cases"],
        }
    return {"claim": document.get("data", {}).get("claim")}


def _validate_contract(document: Mapping[str, Any]) -> None:
    data = document.get("data")
    if not isinstance(data, dict):
        raise FactoryError("ASSET_CONTRACT_INVALID", "契约数据不合法", field="data")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FactoryError("ASSET_CONTRACT_INVALID", "契约用例不能为空", field="cases")
    names: set[str] = set()
    uses = False
    for case in cases:
        if not isinstance(case, dict):
            raise FactoryError("ASSET_CONTRACT_INVALID", "用例不合法", field="cases")
        name = case.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise FactoryError("ASSET_CONTRACT_INVALID", "用例名不合法", field="name")
        names.add(name)
        if case.get("kind") not in ("job", "turn"):
            raise FactoryError("ASSET_CONTRACT_INVALID", "用例类型不合法", field="kind")
        if not isinstance(case.get("subject"), str) or not case["subject"]:
            raise FactoryError("ASSET_CONTRACT_INVALID", "用例主体不合法", field="subject")
        tags = case.get("tags")
        if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag for tag in tags):
            raise FactoryError("ASSET_CONTRACT_INVALID", "用例标签不合法", field="tags")
        if not isinstance(case.get("uses_target"), bool):
            raise FactoryError("ASSET_CONTRACT_INVALID", "uses_target 必须是布尔值", field="uses_target")
        uses = uses or bool(case["uses_target"])
        _compile_assert(str(case.get("assert") or ""))
    if not uses:
        raise FactoryError("ASSET_CONTRACT_INVALID", "至少一项用例必须使用目标", field="uses_target")


def _compile_assert(expr: str) -> None:
    from mrl.expr import compile_expr
    from mrl.types import BOOL, DYN

    try:
        compile_expr(expr, {"out": DYN}, expect=BOOL)
    except Exception as exc:
        raise FactoryError("ASSET_CONTRACT_INVALID", "断言无法编译", field="assert") from exc


def _require_authorized(ctx: Any, contract: dict[str, Any]) -> None:
    rows = ctx.db.query("SELECT standards FROM asset_authorizations WHERE asset_id = ?", (contract["id"],))
    if not rows:
        raise FactoryError("ASSET_NOT_AUTHORIZED", "契约未经人授权", id=contract["id"])
    current = canonical_json(_standards(ctx, contract))
    if str(rows[0]["standards"]) != current:
        raise FactoryError("ASSET_CONTRACT_INVALID", "授权标准已变化", field="standards")


def _match_scope(target: dict[str, Any], contract: dict[str, Any]) -> None:
    if target["document"].get("scope") != contract["document"].get("scope"):
        raise FactoryError("ASSET_CONTRACT_INVALID", "目标与契约范围不一致", field="scope")


def _require_target_shape(target: dict[str, Any], purpose: str) -> None:
    kind = str(target["document"].get("kind"))
    if purpose == "knowledge" and _claim_kind(target) not in _CLAIM_KINDS:
        raise FactoryError("ASSET_CONTRACT_INVALID", "知识目标缺少 claim", field="claim")
    if purpose == "takeover" and kind != "handoff":
        raise FactoryError("ASSET_CONTRACT_INVALID", "接管目标必须是交接", field="kind")
    if purpose == "release" and kind != "factory":
        raise FactoryError("ASSET_CONTRACT_INVALID", "发布目标必须是工厂资产", field="kind")


def _require_coverage(contract: dict[str, Any], purpose: str, subject: str) -> None:
    if purpose != "takeover":
        return
    tags = {tag for case in contract["document"]["data"]["cases"] for tag in case.get("tags") or []}
    missing = [tag for tag in _TAKEOVER_TAGS if tag not in tags]
    if subject == "agent:foreman":
        missing.extend(tag for tag in _FOREMAN_TAGS if tag not in tags)
    if missing:
        raise FactoryError("ASSET_CONTRACT_INVALID", "接管覆盖不足", field="tags")


def _normalize_evidence(evidence: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    if not isinstance(evidence, Mapping):
        raise FactoryError("ASSET_EVIDENCE_INVALID", "证据格式不合法", case="", reason="shape")
    out: dict[str, dict[str, str]] = {}
    for name, item in evidence.items():
        if not isinstance(item, Mapping) or set(item) != {"kind", "id"}:
            raise FactoryError("ASSET_EVIDENCE_INVALID", "证据项不合法", case=str(name), reason="shape")
        if item.get("kind") not in ("job", "turn") or not isinstance(item.get("id"), str):
            raise FactoryError("ASSET_EVIDENCE_INVALID", "证据项不合法", case=str(name), reason="shape")
        out[str(name)] = {"kind": str(item["kind"]), "id": str(item["id"])}
    return out


def _assert_case(expr: str, out: dict[str, Any]) -> tuple[bool, str]:
    from mrl.expr import compile_expr
    from mrl.types import BOOL, DYN

    try:
        compiled = compile_expr(expr, {"out": DYN}, expect=BOOL)
        value = compiled.evaluate({"out": out})
    except FactoryError:
        raise
    except Exception:
        return False, "eval"
    if value is True:
        return True, ""
    return False, "false"


def _bean_ports(ctx: Any, ports: Mapping[str, Sequence[str]]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for port, ids in ports.items():
        views = []
        for bean_id in ids:
            bean = ctx.warehouse.get(str(bean_id))
            views.append({"id": bean.id, "type": bean.type, "meta": bean.meta, "facts": bean.facts})
        found[str(port)] = views
    return found


def record_use(
    ctx: Any,
    snapshot: Any,
    kind: str,
    execution_id: str,
    *,
    subject: str,
    fresh: bool,
    handoff: str | None,
    assets: Sequence[str],
) -> None:
    if assets_config(ctx) is None:
        return
    epoch = ""
    if snapshot is not None and hasattr(snapshot, "epoch"):
        epoch = str(snapshot.epoch)
    with ctx.db.tx() as conn:
        existing = conn.execute(
            "SELECT 1 FROM asset_uses WHERE kind = ? AND execution_id = ?",
            (kind, execution_id),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            """INSERT INTO asset_uses (
                   kind, execution_id, subject, epoch, context, assets, fresh, handoff, trial_id, actions
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]')""",
            (
                kind,
                execution_id,
                subject,
                epoch,
                "",
                canonical_json(list(assets)),
                1 if fresh else 0,
                handoff,
            ),
        )


def note_job_running(ctx: Any, conn: Any, job: Any) -> None:
    if assets_config(ctx) is None:
        return
    existing = conn.execute(
        "SELECT 1 FROM asset_uses WHERE kind = 'job' AND execution_id = ?",
        (job.id,),
    ).fetchone()
    if existing is not None:
        return
    flat = [bean for ids in job.inputs.values() for bean in ids]
    rows = conn.execute(
        "SELECT id, target, subject, context_digest FROM asset_trials WHERE state = 'open'"
    ).fetchall()
    used: list[str] = []
    trial_id = None
    digest = ""
    subject = _subject_station(job.station)
    for row in rows:
        if str(row["subject"]) != subject:
            continue
        document = json.loads(
            conn.execute("SELECT document FROM asset_records WHERE id = ?", (row["target"],)).fetchone()[
                "document"
            ]
        )
        beans = list(document.get("beans") or [])
        if beans and all(bean in flat for bean in beans):
            used.append(str(row["target"]))
            trial_id = str(row["id"])
            digest = str(row["context_digest"])
    if not used:
        return
    conn.execute(
        """INSERT INTO asset_uses (
               kind, execution_id, subject, epoch, context, assets, fresh, handoff, trial_id, actions
           ) VALUES ('job', ?, ?, ?, ?, ?, 1, NULL, ?, '[]')""",
        (job.id, subject, str(job.epoch), digest, canonical_json(used), trial_id),
    )


def mount_turn_context(ctx: Any, epochs: Any, agent: str, incoming: Any) -> tuple[str, list[str], str | None]:
    """启用后写本轮只读资产清单。返回附加提示、挂载资产 id、交接 id。"""
    if assets_config(ctx) is None:
        return "", [], None
    store = LearningStore(ctx, epochs)
    subject = f"agent:{agent}"
    rows = ctx.db.query(
        "SELECT id, target FROM asset_trials WHERE state = 'open' AND subject = ? AND purpose = 'knowledge'",
        (subject,),
    )
    target = _load_asset(ctx, str(rows[0]["target"])) if rows else None
    records = store.pinned_records(target)
    handoff_rows = ctx.db.query("SELECT asset_id FROM asset_heads WHERE slot = ?", (f"handoff:{agent}",))
    handoff = None if not handoff_rows else str(handoff_rows[0]["asset_id"])
    workspace = ctx.db.query(
        "SELECT trial_id, phase FROM agent_workspaces WHERE agent = ? AND phase = 'testing'",
        (agent,),
    )
    if workspace and workspace[0]["trial_id"]:
        trial = ctx.db.query("SELECT target FROM asset_trials WHERE id = ?", (workspace[0]["trial_id"],))
        if trial:
            handoff = str(trial[0]["target"])
            if handoff not in [item["id"] for item in records]:
                records = [_load_asset(ctx, handoff), *records]
    context = {
        "context_digest": digest_bytes(
            canonical_bytes({"assets": [item["id"] for item in records], "handoff": handoff})
        ),
        "assets": records,
        "handoff": handoff,
    }
    path = incoming / "asset-context.json"
    path.write_text(canonical_json(context), encoding="utf-8")
    lines = ["本轮资产上下文在 /factory/in/asset-context.json。"]
    for record in records:
        lines.append(f"- {record['id']} {record['document'].get('summary', '')}")
    return "\n".join(lines), [item["id"] for item in records], handoff
