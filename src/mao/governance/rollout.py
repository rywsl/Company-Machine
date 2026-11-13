"""发布阶段：指标、金丝雀路由、影子报告。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from mao.base import parse_duration, utc_iso
from mao.errors import FactoryError
from mao.governance.risk import governance_limits


@dataclass(frozen=True)
class ShadowReport:
    ok: bool
    runs: int
    spent_usd: float
    regressions: list[Any]


def run_metrics(ctx: Any, *, epoch: str, since: str) -> dict[str, Any]:
    runs = [
        row
        for row in ctx.db.query(
            "SELECT id, state, spent_usd, created_at FROM runs WHERE epoch = ? AND created_at >= ?",
            (epoch, since),
        )
        if row["state"] in {"SUCCEEDED", "FAILED"}
    ]
    jobs = []
    rejected = 0
    if runs:
        ids = [row["id"] for row in runs]
        marks = ",".join("?" for _ in ids)
        jobs = ctx.db.query(
            f"SELECT job_id FROM run_node_jobs WHERE run_id IN ({marks})",
            tuple(ids),
        )
        for job in jobs:
            events = ctx.db.query(
                "SELECT data FROM events WHERE job_id = ? AND kind = 'job.state'",
                (job["job_id"],),
            )
            for event in events:
                try:
                    body = json.loads(event["data"])
                except json.JSONDecodeError:
                    continue
                if body.get("to") == "REJECTED":
                    rejected += 1
                    break
    n = len(runs)
    m = len(jobs)
    return {
        "runs": n,
        "jobs": m,
        "success_rate": None if n == 0 else sum(1 for row in runs if row["state"] == "SUCCEEDED") / n,
        "gate_reject_rate": None if m == 0 else rejected / m,
        "cost_per_run_usd": None
        if n == 0
        else round(sum(float(row["spent_usd"] or 0) for row in runs) / n, 6),
    }


class EpochRouter:
    def __init__(self, ctx: Any, epochs: Any) -> None:
        self.ctx = ctx
        self.epochs = epochs

    def choose(self, key: str) -> str:
        rows = self.ctx.db.query(
            "SELECT \"commit\", document FROM changesets WHERE state = 'canary' ORDER BY id"
        )
        active = self.epochs.current()
        if not rows:
            return active
        body = json.loads(rows[0]["document"])
        percent = int(((body.get("spec") or {}).get("rollout") or {}).get("canary", {}).get("percent", 0))
        bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
        if bucket < percent:
            return str(rows[0]["commit"])
        return active


def slo_violation(metrics: dict[str, Any], slo: dict[str, Any]) -> dict[str, Any] | None:
    for name, expr in slo.items():
        value = metrics.get(name)
        if value is None:
            continue
        text = str(expr).strip()
        op, raw = text.split(None, 1)
        bound = float(raw)
        ok = {
            ">=": value >= bound,
            "<=": value <= bound,
            ">": value > bound,
            "<": value < bound,
        }[op]
        if not ok:
            return {"metric": name, "value": value, "bound": text}
    return None


def observe_seconds(rollout: dict[str, Any], multiplier: int) -> float:
    return parse_duration(str(rollout.get("observe") or "0s")) * multiplier


def in_freeze(ctx: Any) -> bool:
    path = ctx.root / "factory.json"
    if not path.is_file():
        return False
    body = json.loads(path.read_text(encoding="utf-8"))
    now = ctx.clock.now()
    for window in (body.get("governance") or {}).get("freeze") or []:
        start = _parse(window["start"])
        until = _parse(window["until"])
        if start <= now < until:
            return True
    return False


def safe_mode_row(ctx: Any) -> dict[str, Any]:
    rows = ctx.db.query("SELECT level, since, until, on_flag FROM governance_mode WHERE id = 1")
    if not rows:
        return {"on": False, "level": 0, "since": None, "until": None}
    row = rows[0]
    return {
        "on": bool(row["on_flag"]),
        "level": int(row["level"]),
        "since": row["since"],
        "until": row["until"],
    }


def evaluate_safe_mode(ctx: Any) -> dict[str, Any]:
    limits = governance_limits()
    now = ctx.clock.now()
    window = timedelta(seconds=limits["safe_mode_window_s"])
    rollbacks = 0
    for row in ctx.db.query("SELECT at, data FROM events WHERE kind = 'changeset.state'"):
        try:
            body = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        if body.get("code") != "SLO_ROLLBACK":
            continue
        if now - _parse(row["at"]) <= window:
            rollbacks += 1
    wakes = 0
    try:
        wakes = len(ctx.db.query("SELECT id FROM audit_findings WHERE state = 'wake'"))
    except Exception:
        wakes = 0
    triggered = rollbacks >= limits["safe_mode_rollbacks"] or wakes > 0
    current = safe_mode_row(ctx)
    level = int(current["level"])
    if triggered and not current["on"]:
        level += 1
        cooldown = limits["safe_mode_cooldown_s"] * (2 ** (level - 1))
        until = utc_iso(now + timedelta(seconds=cooldown))
        _write_mode(ctx, level, utc_iso(now), until, True)
        return {"on": True, "level": level}
    if current["on"]:
        until = _parse(current["until"]) if current["until"] else now
        if now >= until and not triggered:
            _write_mode(ctx, level, current["since"], current["until"], False)
            return {"on": False, "level": level}
        if now >= until and triggered:
            cooldown = limits["safe_mode_cooldown_s"] * (2 ** max(level - 1, 0))
            extended = utc_iso(now + timedelta(seconds=cooldown))
            _write_mode(ctx, level, current["since"], extended, True)
            return {"on": True, "level": level}
        return {"on": True, "level": level}
    return {"on": False, "level": level}


def _write_mode(ctx: Any, level: int, since: str | None, until: str | None, on: bool) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            """INSERT INTO governance_mode (id, level, since, until, on_flag) VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET level = excluded.level, since = excluded.since,
               until = excluded.until, on_flag = excluded.on_flag""",
            (level, since, until, 1 if on else 0),
        )


def _parse(text: str) -> Any:
    from datetime import UTC, datetime

    raw = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def terminal_runs(ctx: Any, epoch: str, since: str) -> list[Any]:
    return [
        row
        for row in ctx.db.query(
            "SELECT id, state, spent_usd, created_at FROM runs WHERE epoch = ? AND created_at >= ?",
            (epoch, since),
        )
        if row["state"] in {"SUCCEEDED", "FAILED"}
    ]


def sample_ready(count: int, rollout: dict[str, Any], multiplier: int) -> bool:
    canary = rollout.get("canary") or {}
    minimum = int(canary.get("min_runs", 1)) * multiplier
    return count >= minimum


def advance_open(pipeline: Any, current: Any) -> Any:
    state = current.state
    if state == "verified":
        return _from_verified(pipeline, current)
    if state == "shadow":
        return _from_shadow(pipeline, current)
    if state == "canary":
        return _from_canary(pipeline, current)
    if state == "active":
        return _from_active(pipeline, current)
    raise FactoryError("CHANGESET_STATE_INVALID", "当前状态不能推进", id=current.id, state=state)


def _from_verified(pipeline: Any, current: Any) -> Any:
    tier = current.risk_tier or "T1"
    if evaluate_safe_mode(pipeline.ctx)["on"] and tier >= "T2":
        return _stay(pipeline, current, "safe-mode")
    if len(pipeline.approvals(current.id)) < int(current.reviews_required or 0):
        return _stay(pipeline, current, "reviews")
    if _queued(pipeline):
        return _stay(pipeline, current, "queued")
    if pipeline.epochs.current() != current.base_epoch:
        pipeline.epochs.set_pointer("candidate", None, by="governance", reason="stale")
        return pipeline.reject_changeset(current, "CHANGESET_STALE", {"base_epoch": current.base_epoch})
    pipeline.epochs.set_pointer("candidate", current.commit, by="governance", reason="shadow")
    report = _shadow(pipeline, current)
    rollout = (current.document.get("spec") or {}).get("rollout") or {}
    budget = float((rollout.get("shadow") or {}).get("budget_usd") or 0)
    failed = (not report.ok) or bool(report.regressions) or report.spent_usd > budget
    if failed:
        pipeline.epochs.set_pointer("candidate", None, by="governance", reason="shadow-failed")
        return pipeline.reject_changeset(current, "SHADOW_FAILED", {"regressions": list(report.regressions)})
    return _move(pipeline, current, "shadow", {"ok": True, "runs": report.runs})


def _from_shadow(pipeline: Any, current: Any) -> Any:
    if in_freeze(pipeline.ctx):
        return _stay(pipeline, current, "freeze")
    stages = (current.report or {}).get("stages") or []
    if "canary" in stages:
        return _move(pipeline, current, "canary", {}, stamp="stage")
    return _promote(pipeline, current)


def _from_canary(pipeline: Any, current: Any) -> Any:
    rollout = (current.document.get("spec") or {}).get("rollout") or {}
    multiplier = int((current.report or {}).get("multiplier") or 1)
    since = current.stage_started_at or current.updated_at
    rows = terminal_runs(pipeline.ctx, current.commit, since)
    if not sample_ready(len(rows), rollout, multiplier):
        return _stay(pipeline, current, "samples")
    metrics = run_metrics(pipeline.ctx, epoch=current.commit, since=since)
    violation = slo_violation(metrics, rollout.get("slo") or {})
    if violation:
        return _rollback(pipeline, current, violation)
    if in_freeze(pipeline.ctx):
        return _stay(pipeline, current, "freeze")
    return _promote(pipeline, current)


def _from_active(pipeline: Any, current: Any) -> Any:
    if (current.report or {}).get("stage") == "observe" and (current.report or {}).get("ok") is True:
        return current
    rollout = (current.document.get("spec") or {}).get("rollout") or {}
    multiplier = int((current.report or {}).get("multiplier") or 1)
    since = current.activated_at or current.updated_at
    elapsed = pipeline.ctx.clock.now() - _parse(since)
    window = timedelta(seconds=observe_seconds(rollout, multiplier))
    rows = terminal_runs(pipeline.ctx, current.commit, since)
    metrics = run_metrics(pipeline.ctx, epoch=current.commit, since=since)
    violation = slo_violation(metrics, rollout.get("slo") or {})
    minimum = 1
    if rollout.get("canary"):
        minimum = int(rollout["canary"].get("min_runs", 1)) * multiplier
    if elapsed < window:
        if len(rows) >= minimum and violation:
            return _rollback(pipeline, current, violation)
        return current
    if rows and violation:
        return _rollback(pipeline, current, violation)
    return _report_only(pipeline, current, {"stage": "observe", "ok": True})


def _shadow(pipeline: Any, current: Any) -> ShadowReport:
    rollout = (current.document.get("spec") or {}).get("rollout") or {}
    golden_only = (current.risk_tier == "T1") and "runs" not in (rollout.get("shadow") or {})
    runner = pipeline.shadow
    if runner is None:
        from mao.governance.shadow import LineShadowRunner

        runner = LineShadowRunner(pipeline.ctx)
    candidate = pipeline.epochs.materialize(current.commit)
    active = pipeline.epochs.materialize(pipeline.epochs.current())
    return runner.run(current, candidate=candidate, active=active, rollout=rollout, golden_only=golden_only)


def _promote(pipeline: Any, current: Any) -> Any:
    if pipeline.epochs.current() != current.base_epoch:
        pipeline.epochs.set_pointer("candidate", None, by="governance", reason="stale")
        return pipeline.reject_changeset(
            current,
            "CHANGESET_STALE",
            {"base_epoch": current.base_epoch, "active": pipeline.epochs.current()},
        )
    previous = pipeline.epochs.current()
    now = utc_iso(pipeline.ctx.clock.now())
    pipeline.epochs.set_pointer("previous", previous, by="governance", reason="promote")
    pipeline.epochs.set_pointer("active", current.commit, by="governance", reason="promote")
    pipeline.epochs.set_pointer("candidate", None, by="governance", reason="promote")
    with pipeline.ctx.db.tx() as conn:
        conn.execute(
            """UPDATE changesets
               SET state = 'active', activated_at = ?, stage_started_at = ?, report = ?, updated_at = ?
               WHERE id = ?""",
            (
                now,
                now,
                canonical_report("shadow", True, None, {}),
                now,
                current.id,
            ),
        )
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)",
            (
                pipeline.ctx.ids.new("ev"),
                now,
                json.dumps({"id": current.id, "from": current.state, "to": "active", "code": None}),
            ),
        )
    _rebuild(pipeline, current)
    return pipeline.get(current.id)


def _rebuild(pipeline: Any, current: Any) -> None:
    from mao.foreman import ConversionStore

    snapshot = pipeline.epochs.materialize(current.commit)
    store = ConversionStore(pipeline.ctx)
    resolved = store.resolve_matching(current.id, snapshot)
    if not pipeline.rebuild:
        return
    run_ids = []
    for request in store.list():
        if request.id not in resolved:
            continue
        run_ids.extend(request.blocked_runs)
    for run_id in sorted(set(run_ids)):
        rows = pipeline.ctx.db.query("SELECT state FROM runs WHERE id = ?", (run_id,))
        if rows and rows[0]["state"] == "FAILED":
            pipeline.rebuild(run_id, snapshot)


def _rollback(pipeline: Any, current: Any, violation: dict[str, Any]) -> Any:
    now = utc_iso(pipeline.ctx.clock.now())
    report = {"stage": current.state, "ok": False, "code": "SLO_ROLLBACK", "detail": violation}
    with pipeline.ctx.db.tx() as conn:
        conn.execute(
            "UPDATE changesets SET state = 'rolled-back', report = ?, updated_at = ? WHERE id = ?",
            (json.dumps(report), now, current.id),
        )
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)",
            (
                pipeline.ctx.ids.new("ev"),
                now,
                json.dumps(
                    {"id": current.id, "from": current.state, "to": "rolled-back", "code": "SLO_ROLLBACK"}
                ),
            ),
        )
    if pipeline.epochs.current() != current.base_epoch:
        pipeline.epochs.rollback(current.base_epoch, by="governance")
    else:
        pipeline.epochs.set_pointer("candidate", None, by="governance", reason="rollback")
    from mao.foreman import WakeQueue

    WakeQueue(pipeline.ctx).file("rollback", current.id, {"id": current.id})
    return pipeline.get(current.id)


def _move(pipeline: Any, current: Any, state: str, detail: dict[str, Any], stamp: str | None = None) -> Any:
    now = utc_iso(pipeline.ctx.clock.now())
    report = {
        "stage": state,
        "ok": True,
        "code": None,
        "detail": detail,
        "stages": (current.report or {}).get("stages"),
        "multiplier": (current.report or {}).get("multiplier"),
    }
    sets = "state = ?, report = ?, updated_at = ?"
    args: list[Any] = [state, json.dumps(report), now]
    if stamp == "stage":
        sets += ", stage_started_at = ?"
        args.append(now)
    args.append(current.id)
    with pipeline.ctx.db.tx() as conn:
        conn.execute(f"UPDATE changesets SET {sets} WHERE id = ?", tuple(args))
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'changeset.state', NULL, ?)",
            (
                pipeline.ctx.ids.new("ev"),
                now,
                json.dumps({"id": current.id, "from": current.state, "to": state, "code": None}),
            ),
        )
    return pipeline.get(current.id)


def _stay(pipeline: Any, current: Any, reason: str) -> Any:
    now = utc_iso(pipeline.ctx.clock.now())
    report = dict(current.report or {})
    report["reason"] = reason
    report["stage"] = current.state
    with pipeline.ctx.db.tx() as conn:
        conn.execute(
            "UPDATE changesets SET report = ?, updated_at = ? WHERE id = ?",
            (json.dumps(report), now, current.id),
        )
    return pipeline.get(current.id)


def _report_only(pipeline: Any, current: Any, report: dict[str, Any]) -> Any:
    now = utc_iso(pipeline.ctx.clock.now())
    with pipeline.ctx.db.tx() as conn:
        conn.execute(
            "UPDATE changesets SET report = ?, updated_at = ? WHERE id = ?",
            (json.dumps(report), now, current.id),
        )
    return pipeline.get(current.id)


def _queued(pipeline: Any) -> bool:
    limit = governance_limits()["concurrent_rollouts"]
    observing = [
        row
        for row in pipeline.ctx.db.query("SELECT id, report FROM changesets WHERE state = 'active'")
        if (json.loads(row["report"] or "{}")).get("stage") != "observe"
        or (json.loads(row["report"] or "{}")).get("ok") is not True
    ]
    return len(observing) >= limit


def canonical_report(stage: str, ok: bool, code: str | None, detail: dict[str, Any]) -> str:
    return json.dumps({"stage": stage, "ok": ok, "code": code, "detail": detail})
