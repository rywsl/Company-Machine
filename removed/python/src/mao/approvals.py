"""两阶段审批。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from mao.base import canonical_json, parse_duration, utc_iso
from mao.errors import FactoryError
from mao.values import approval_env
from mrl.errors import EvalError

_LOG = logging.getLogger("mao.approvals")


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalPhase(StrEnum):
    PROPOSE = "propose"
    IN_RUN = "in_run"


@dataclass(frozen=True)
class Approval:
    id: str
    job_id: str
    attempt: int
    phase: ApprovalPhase
    status: ApprovalStatus
    route: str
    payload: dict[str, Any]
    deadline: str | None
    decision: str | None
    decided_by: str | None
    note: str
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class Vote:
    approval_id: str
    runtime: str
    by: str
    decision: str
    note: str
    at: str


class Approvals:
    def __init__(self, ctx: Any, snapshot: Any) -> None:
        self.ctx = ctx
        self.snapshot = snapshot

    def rule(self, station_key: str, phase: ApprovalPhase) -> dict[str, Any]:
        station_key = self.snapshot.key("Station", station_key)
        raw = (self.snapshot.spec(station_key).get("approval") or {}).get(phase.value)
        if raw is None:
            return {"mode": "missing"}
        if isinstance(raw, str):
            return {"mode": raw}
        return {"mode": "conditional", "else": raw.get("else")}

    def open_proposal(
        self,
        *,
        job_id: str,
        attempt: int,
        station_key: str,
        payload: Mapping[str, Any],
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> Approval:
        if not payload:
            raise FactoryError("PROPOSAL_MISSING", "没有方案", job=job_id)
        return self._open(ApprovalPhase.PROPOSE, job_id, attempt, station_key, payload, inputs, params)

    def open_request(
        self,
        *,
        job_id: str,
        attempt: int,
        station_key: str,
        payload: Mapping[str, Any],
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        request_id: str | None = None,
    ) -> Approval:
        return self._open(
            ApprovalPhase.IN_RUN,
            job_id,
            attempt,
            station_key,
            payload,
            inputs,
            params,
            request_id=request_id,
        )

    def open_directed(
        self,
        *,
        job_id: str,
        attempt: int,
        station_key: str,
        payload: Mapping[str, Any],
        detail: str = "",
        timeout: str | None,
        request_id: str | None = None,
    ) -> Approval:
        self._check_directed(payload, detail)
        station_key = self.snapshot.key("Station", station_key)
        self._ensure_job(job_id, station_key)
        stored = _redact(self.ctx, dict(payload))
        stored["detail"] = _redact(self.ctx, detail)
        return self._insert(
            ApprovalPhase.IN_RUN,
            job_id,
            attempt,
            station_key,
            stored,
            route="foreman",
            status=ApprovalStatus.PENDING,
            decision=None,
            decided_by=None,
            note="",
            deadline=self._deadline_after(timeout),
            request_id=request_id,
            delegated_from=None,
            auto=False,
        )

    def escalate(self, approval_id: str, *, by: str, note: str = "") -> Approval:
        current = self.get(approval_id)
        if current.status != ApprovalStatus.PENDING or current.route != "foreman":
            raise FactoryError(
                "APPROVAL_STATE_INVALID",
                "审批不能改道",
                approval=approval_id,
                status=current.status.value,
                op="escalate",
            )
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE approvals SET route = 'human' WHERE id = ?", (approval_id,))
            self._event(
                conn,
                current.job_id,
                "approval.escalated",
                {"id": approval_id, "by": by},
                now,
            )
        updated = self.get(approval_id)
        self._human_inbox(updated, note=note)
        return updated

    def apply_decision(self, approval_id: str, decision: str, *, by: str, note: str = "") -> Approval:
        current = self.get(approval_id)
        if current.status != ApprovalStatus.PENDING or decision not in ("approve", "deny"):
            raise FactoryError(
                "APPROVAL_STATE_INVALID",
                "审批不在可决定状态",
                approval=approval_id,
                status=current.status.value,
                op="decide",
            )
        if current.route == "review":
            return self._vote(current, decision, by, note)
        return self._settle(current, decision, by, note)

    def expire_due(self) -> list[str]:
        now = self.ctx.clock.now()
        expired: list[str] = []
        for row in self.ctx.db.query(
            "SELECT * FROM approvals WHERE status = 'pending' AND deadline IS NOT NULL"
        ):
            deadline = datetime.fromisoformat(str(row["deadline"]).replace("Z", "+00:00"))
            if now < deadline:
                continue
            approval = self._from_row(row)
            self._settle(approval, "deny", "timeout", "APPROVAL_TIMEOUT")
            kind = str(approval.payload.get("kind") or "")
            if not (kind.startswith("ask:") or kind.startswith("monitor:") or kind == "egress"):
                self._fail_job(approval.job_id, "APPROVAL_TIMEOUT", approval.id)
            expired.append(approval.id)
        return sorted(expired)

    def preset(
        self, *, station_key: str, phase: ApprovalPhase, decision: str, by: str, note: str = ""
    ) -> None:
        station_key = self.snapshot.key("Station", station_key)
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO approval_presets (station_key, phase, decision, by, note) VALUES (?, ?, ?, ?, ?)",
                (station_key, phase.value, decision, by, note),
            )
        return None

    def get(self, approval_id: str) -> Approval:
        rows = self.ctx.db.query("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not rows:
            raise FactoryError("APPROVAL_NOT_FOUND", f"找不到审批 {approval_id}", approval=approval_id)
        return self._from_row(rows[0])

    def list(self, *, job_id: str | None = None, status: str | None = None) -> list[Approval]:
        rows = [self._from_row(row) for row in self.ctx.db.query("SELECT * FROM approvals")]
        if job_id is not None:
            rows = [row for row in rows if row.job_id == job_id]
        if status is not None:
            rows = [row for row in rows if row.status.value == status]
        return rows

    def votes(self, approval_id: str) -> list[Vote]:
        rows = self.ctx.db.query(
            "SELECT * FROM approval_votes WHERE approval_id = ? ORDER BY runtime", (approval_id,)
        )
        return [
            Vote(row["approval_id"], row["runtime"], row["by"], row["decision"], row["note"], row["at"])
            for row in rows
        ]

    def for_provenance(self, job_id: str) -> list[dict[str, Any]]:
        items = []
        for row in self.list(job_id=job_id):
            cost = row.payload.get("cost_estimate_usd", row.payload.get("cost_usd"))
            items.append(
                {
                    "id": row.id,
                    "phase": row.phase.value,
                    "decision": row.decision,
                    "decided_by": row.decided_by,
                    "route": row.route,
                    "cost_usd": cost,
                }
            )
        return items

    def _open(
        self,
        phase: ApprovalPhase,
        job_id: str,
        attempt: int,
        station_key: str,
        payload: Mapping[str, Any],
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
        request_id: str | None = None,
    ) -> Approval:
        station_key = self.snapshot.key("Station", station_key)
        spec = self.snapshot.spec(station_key)
        rule = (spec.get("approval") or {}).get(phase.value)
        if rule is None:
            if spec.get("role") == "sink" and phase == ApprovalPhase.PROPOSE:
                rule = "auto"
            else:
                raise FactoryError(
                    "APPROVAL_RULE_MISSING",
                    "工位没有这条审批规则",
                    station=station_key,
                    phase=phase.value,
                )
        self._ensure_job(job_id, station_key)
        route, note, auto = self._route(station_key, phase, rule, payload, inputs, params)
        preset = self._take_preset(station_key, phase)
        delegated_from = None
        if preset is None:
            rewritten = _autonomy(self.ctx).route(route)
            if rewritten != route:
                delegated_from = route
                route = rewritten
        if route == "review" and preset is None and not auto and self._quorum() is None:
            self._fail_job(job_id, "REVIEW_QUORUM_UNSET", "")
            raise FactoryError("REVIEW_QUORUM_UNSET", "复核法定人数没有设置", station=station_key)
        status = ApprovalStatus.PENDING
        decision = None
        decided_by = None
        if preset is not None:
            decision = preset["decision"]
            decided_by = preset["by"]
            status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.DENIED
        elif auto:
            route = "auto"
            status = ApprovalStatus.APPROVED
            decision = "approve"
            decided_by = "auto"
        return self._insert(
            phase,
            job_id,
            attempt,
            station_key,
            _redact(self.ctx, dict(payload)),
            route=route,
            status=status,
            decision=decision,
            decided_by=decided_by,
            note=note,
            deadline=self._deadline(spec),
            request_id=request_id,
            delegated_from=delegated_from,
            auto=auto,
        )

    def _route(
        self,
        station_key: str,
        phase: ApprovalPhase,
        rule: Any,
        payload: Mapping[str, Any],
        inputs: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> tuple[str, str, bool]:
        if rule == "auto":
            return "auto", "", True
        if isinstance(rule, str):
            return rule, "", False
        else_route = str(rule.get("else") or "foreman")
        stripped = {
            key: value
            for key, value in payload.items()
            if key in ("summary", "cost_estimate_usd", "time_estimate_s", "kind", "cost_usd")
        }
        try:
            ok = (
                self.snapshot.compiled(f"{station_key}/approval.{phase.value}").evaluate(
                    approval_env(phase.value, stripped, inputs, params)
                )
                is True
            )
        except EvalError:
            return else_route, "APPROVAL_EVAL_ERROR", False
        if ok:
            return "auto", "", True
        return else_route, "", False

    def _vote(self, current: Approval, decision: str, by: str, note: str) -> Approval:
        runtime = by.split(":", 1)[-1].split("@", 1)[0] if by.startswith("runtime:") else by
        existing = self.votes(current.id)
        if any(item.runtime == runtime for item in existing):
            raise FactoryError(
                "APPROVAL_STATE_INVALID",
                "同一 runtime 不能重复投票",
                approval=current.id,
                status=current.status.value,
                op="vote",
            )
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO approval_votes (approval_id, runtime, by, decision, note, at) VALUES (?, ?, ?, ?, ?, ?)",
                (current.id, runtime, by, decision, note, now),
            )
        votes = self.votes(current.id)
        if any(item.decision == "deny" for item in votes):
            return self._settle(current, "deny", by, note)
        quorum = self._quorum() or 2
        if sum(1 for item in votes if item.decision == "approve") >= quorum:
            return self._settle(current, "approve", ",".join(item.by for item in votes), note)
        return self.get(current.id)

    def _settle(self, current: Approval, decision: str, by: str, note: str) -> Approval:
        status = ApprovalStatus.APPROVED if decision == "approve" else ApprovalStatus.DENIED
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, decision = ?, decided_by = ?, by = ?, note = ?, decided_at = ? WHERE id = ?",
                (status.value, decision, by, by, note, now, current.id),
            )
        if decision == "deny" and current.phase == ApprovalPhase.PROPOSE and by != "timeout":
            code = "APPROVAL_REVIEW_SPLIT" if current.route == "review" else "APPROVAL_DENIED"
            self._fail_job(current.job_id, code, current.id)
        settled = self.get(current.id)
        self._close_inbox(settled, by)
        return settled

    def _insert(
        self,
        phase: ApprovalPhase,
        job_id: str,
        attempt: int,
        station_key: str,
        payload: Mapping[str, Any],
        *,
        route: str,
        status: ApprovalStatus,
        decision: str | None,
        decided_by: str | None,
        note: str,
        deadline: str | None,
        request_id: str | None,
        delegated_from: str | None,
        auto: bool,
    ) -> Approval:
        del station_key, auto
        approval_id = self.ctx.ids.new("a")
        now = utc_iso(self.ctx.clock.now())
        kind = phase.value
        cost = float(payload.get("cost_estimate_usd", payload.get("cost_usd", 0)) or 0)
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO approvals
                   (id, job_id, attempt, request_id, phase, kind, summary, cost_usd, status, by, note,
                    created_at, decided_at, route, payload, deadline, decision, decided_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval_id,
                    job_id,
                    attempt,
                    request_id or approval_id,
                    phase.value,
                    kind,
                    str(payload.get("summary") or ""),
                    cost,
                    status.value,
                    decided_by,
                    note,
                    now,
                    now if decision else None,
                    route,
                    canonical_json(dict(payload)),
                    deadline,
                    decision,
                    decided_by,
                ),
            )
            if delegated_from is not None:
                self._event(
                    conn,
                    job_id,
                    "approval.delegated",
                    {"id": approval_id, "from": delegated_from, "to": route},
                    now,
                )
            self._event(
                conn,
                job_id,
                "approval.requested",
                {
                    "id": approval_id,
                    "job": job_id,
                    "phase": phase.value,
                    "route": route,
                    "deadline": deadline,
                },
                now,
            )
        opened = self.get(approval_id)
        if status == ApprovalStatus.PENDING and route in ("foreman", "human", "review"):
            self._park(job_id)
        if status == ApprovalStatus.PENDING and route == "foreman":
            self._wake(opened)
        if status == ApprovalStatus.PENDING and route == "human":
            self._human_inbox(opened)
        return opened

    def _event(self, conn: Any, job_id: str, kind: str, data: Mapping[str, Any], now: str) -> None:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
            (self.ctx.ids.new("ev"), now, kind, job_id, canonical_json(dict(data))),
        )

    def _display_kind(self, approval: Approval) -> str:
        if approval.phase == ApprovalPhase.PROPOSE:
            return "proposal"
        return str(approval.payload.get("kind") or "")

    def _wake(self, approval: Approval) -> None:
        try:
            from mao.foreman import WakeQueue

            WakeQueue(self.ctx).file(
                "approval",
                approval.id,
                {"id": approval.id, "job": approval.job_id, "kind": self._display_kind(approval)},
            )
        except Exception:
            _LOG.exception("审批唤醒票写入失败")

    def _human_inbox(self, approval: Approval, note: str | None = None) -> None:
        from mao.inbox import Inbox
        from mao.jobs import load_job

        job = load_job(self.ctx.db, approval.job_id)
        cost = approval.payload.get("cost_usd", approval.payload.get("cost_estimate_usd", 0))
        payload: dict[str, Any] = {
            "approval": approval.id,
            "job": approval.job_id,
            "station": job.station,
            "phase": approval.phase.value,
            "kind": self._display_kind(approval),
            "cost_usd": float(cost or 0),
            "deadline": approval.deadline,
        }
        if note is not None:
            payload["note"] = note
        summary = str(approval.payload.get("summary") or approval.id)
        Inbox(self.ctx).open(
            "approval",
            subject=approval.id,
            origin="job:" + approval.job_id,
            summary=summary,
            payload=payload,
            deadline=approval.deadline,
        )

    def _close_inbox(self, approval: Approval, by: str) -> None:
        from mao.inbox import Inbox

        inbox = Inbox(self.ctx)
        item = inbox.find("approval", approval.id)
        if item is None:
            return
        if approval.decided_by == "timeout":
            inbox.close(item.id, status="expired", answer=None, by="timeout")
            return
        decision = "approve" if approval.status == ApprovalStatus.APPROVED else "deny"
        inbox.close(
            item.id,
            status="answered",
            answer={"decision": decision, "note": approval.note},
            by=by,
        )

    def _deadline_after(self, timeout: str | None) -> str | None:
        if not timeout:
            return None
        moment = self.ctx.clock.now() + timedelta(seconds=parse_duration(str(timeout)))
        return utc_iso(moment)

    def _check_directed(self, payload: Mapping[str, Any], detail: str) -> None:
        if not isinstance(payload.get("kind"), str) or not isinstance(payload.get("summary"), str):
            raise FactoryError("APPROVAL_PAYLOAD_INVALID", "定向审批载荷不合法", reason="payload")
        cost = payload.get("cost_usd", 0)
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise FactoryError("APPROVAL_PAYLOAD_INVALID", "定向审批载荷不合法", reason="cost_usd")
        if not isinstance(detail, str) or len(detail) > 8000:
            raise FactoryError("APPROVAL_PAYLOAD_INVALID", "定向审批详情不合法", reason="detail")

    def view(self, approval_id: str) -> dict[str, Any]:
        approval = self.get(approval_id)
        from mao.jobs import load_job

        job = load_job(self.ctx.db, approval.job_id)
        body = {
            "id": approval.id,
            "job_id": approval.job_id,
            "attempt": approval.attempt,
            "phase": approval.phase.value,
            "status": approval.status.value,
            "route": approval.route,
            "payload": dict(approval.payload),
            "deadline": approval.deadline,
            "decision": approval.decision,
            "decided_by": approval.decided_by,
            "note": approval.note,
            "created_at": approval.created_at,
            "decided_at": approval.decided_at,
            "job": {"id": job.id, "station": job.station, "state": job.state.value},
            "detail": str(approval.payload.get("detail") or ""),
            "kind": self._display_kind(approval),
        }
        return _redact(self.ctx, body)

    def ctl_result(self, approval: Approval) -> dict[str, str]:
        if approval.decided_by == "timeout":
            decision, code = "timeout", "APPROVAL_TIMEOUT"
        elif approval.status == ApprovalStatus.APPROVED:
            decision, code = "approve", ""
        elif approval.route == "review" and approval.phase == ApprovalPhase.PROPOSE:
            decision, code = "deny", "APPROVAL_REVIEW_SPLIT"
        else:
            decision, code = "deny", "APPROVAL_DENIED"
        return {"decision": decision, "note": _redact(self.ctx, approval.note), "code": code}

    def by_request(self, request_id: str) -> Approval | None:
        rows = self.ctx.db.query("SELECT * FROM approvals WHERE request_id = ?", (request_id,))
        return self._from_row(rows[0]) if rows else None

    def _deadline(self, spec: Mapping[str, Any]) -> str | None:
        timeout = (spec.get("approval") or {}).get("timeout")
        if not timeout:
            return None
        moment = self.ctx.clock.now() + timedelta(seconds=parse_duration(str(timeout)))
        return utc_iso(moment)

    def _quorum(self) -> int | None:
        model = getattr(self.snapshot.result, "model", None)
        if model is None:
            return None
        values = [row[1] for row in model.rows("limit") if row[0] == "approval_review_quorum"]
        if len(values) != 1:
            return None
        value = values[0]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or int(value) != value
            or int(value) < 1
        ):
            return None
        return int(value)

    def _take_preset(self, station_key: str, phase: ApprovalPhase) -> dict[str, Any] | None:
        rows = self.ctx.db.query(
            "SELECT * FROM approval_presets WHERE station_key = ? AND phase = ?",
            (station_key, phase.value),
        )
        if not rows:
            return None
        with self.ctx.db.tx() as conn:
            conn.execute(
                "DELETE FROM approval_presets WHERE station_key = ? AND phase = ?", (station_key, phase.value)
            )
        return dict(rows[0])

    def _ensure_job(self, job_id: str, station_key: str) -> None:
        if self.ctx.db.query("SELECT id FROM jobs WHERE id = ?", (job_id,)):
            return
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at)
                   VALUES (?, ?, ?, 'RUNNING', 1, '{}', '{}', '{}', ?, ?)""",
                (job_id, station_key, self.snapshot.epoch, now, now),
            )

    def _park(self, job_id: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'AWAITING_APPROVAL', updated_at = ? WHERE id = ?", (now, job_id)
            )

    def _fail_job(self, job_id: str, code: str, approval_id: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        body = canonical_json({"code": code, "message": code, "detail": {"approval": approval_id}})
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'FAILED', error = ?, updated_at = ? WHERE id = ?",
                (body, now, job_id),
            )

    def _from_row(self, row: Any) -> Approval:
        payload = json.loads(row["payload"] or "{}")
        return Approval(
            row["id"],
            row["job_id"],
            int(row["attempt"]),
            ApprovalPhase(row["phase"]),
            ApprovalStatus(row["status"]),
            row["route"] or "foreman",
            payload,
            row["deadline"],
            row["decision"],
            row["decided_by"] or row["by"],
            row["note"] or "",
            row["created_at"],
            row["decided_at"],
        )


def _autonomy(ctx: Any) -> Any:
    from mao.delegation import AutonomyConfig

    return AutonomyConfig.load(ctx.root)


def _redact(ctx: Any, value: Any) -> Any:
    from mao.egress import SecretStore

    return SecretStore(ctx.root).redact_obj(value)
