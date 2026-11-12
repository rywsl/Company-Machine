"""厂长操作面：唤醒、查询、提议、转换需求。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError

_SECRET = re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+")
_MERGE_OPS = {"changeset.advance", "epoch.rollback", "changeset.merge"}


@dataclass(frozen=True)
class Wake:
    id: str
    signal: str
    subject: str
    payload: dict[str, Any]
    status: str
    at: str
    attempts: int = 0
    not_before: str | None = None


class WakeQueue:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def file(
        self, signal: str, subject: str, payload: Mapping[str, Any], *, since: str | None = None
    ) -> Wake:
        body = {str(key): value for key, value in payload.items()}
        for value in body.values():
            if isinstance(value, str) and len(value) > 500:
                raise FactoryError("WAKE_NOT_FOUND", "唤醒载荷过长", id=subject)
        rows = self.ctx.db.query(
            "SELECT * FROM wakes WHERE signal = ? AND subject = ? AND status IN ('open', 'taken', 'failed') "
            "ORDER BY at, id",
            (signal, subject),
        )
        if rows:
            return _wake(rows[0])
        if since is not None:
            done = self.ctx.db.query(
                "SELECT * FROM wakes WHERE signal = ? AND subject = ? AND status = 'done' AND at >= ? ORDER BY at, id",
                (signal, subject, since),
            )
            if done:
                return _wake(done[0])
        wake_id = self.ctx.ids.new("wk")
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO wakes (id, signal, subject, payload, status, by, at, updated_at)
                   VALUES (?, ?, ?, ?, 'open', NULL, ?, ?)""",
                (wake_id, signal, subject, canonical_json(body), now, now),
            )
        return (
            self.list()[0]
            if False
            else _wake(self.ctx.db.query("SELECT * FROM wakes WHERE id = ?", (wake_id,))[0])
        )

    def next(self, *, by: str) -> Wake | None:
        now = utc_iso(self.ctx.clock.now())
        rows = self.ctx.db.query(
            "SELECT * FROM wakes WHERE status = 'open' AND (not_before IS NULL OR not_before <= ?) "
            "ORDER BY at, id LIMIT 1",
            (now,),
        )
        if not rows:
            return None
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE wakes SET status = 'taken', by = ?, updated_at = ? WHERE id = ? AND status = 'open'",
                (by, now, rows[0]["id"]),
            )
        return _wake(self.ctx.db.query("SELECT * FROM wakes WHERE id = ?", (rows[0]["id"],))[0])

    def done(self, wake_id: str, *, by: str) -> Wake:
        rows = self.ctx.db.query("SELECT * FROM wakes WHERE id = ?", (wake_id,))
        if not rows:
            raise FactoryError("WAKE_NOT_FOUND", "找不到唤醒票", id=wake_id)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE wakes SET status = 'done', by = ?, updated_at = ? WHERE id = ?",
                (by, now, wake_id),
            )
        return _wake(self.ctx.db.query("SELECT * FROM wakes WHERE id = ?", (wake_id,))[0])

    def get(self, wake_id: str) -> Wake:
        rows = self.ctx.db.query("SELECT * FROM wakes WHERE id = ?", (wake_id,))
        if not rows:
            raise FactoryError("WAKE_NOT_FOUND", "找不到唤醒票", id=wake_id)
        return _wake(rows[0])

    def reopen(self, wake_id: str, *, not_before: str | None, failed: bool, by: str) -> Wake:
        wake = self.get(wake_id)
        if wake.status != "taken":
            raise FactoryError(
                "WAKE_STATE_INVALID", "只有 taken 票可以重开", wake=wake_id, status=wake.status
            )
        attempts = wake.attempts + 1 if failed else wake.attempts
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE wakes SET status = 'open', not_before = ?, attempts = ?, by = ?, updated_at = ? WHERE id = ?",
                (not_before, attempts, by, now, wake_id),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'foreman.wake.reopened', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    now,
                    canonical_json({"wake": wake_id, "attempts": attempts, "not_before": not_before}),
                ),
            )
        return self.get(wake_id)

    def fail(self, wake_id: str, *, code: str, by: str) -> Wake:
        wake = self.get(wake_id)
        if wake.status != "taken":
            raise FactoryError(
                "WAKE_STATE_INVALID", "只有 taken 票可以放弃", wake=wake_id, status=wake.status
            )
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE wakes SET status = 'failed', by = ?, updated_at = ? WHERE id = ?",
                (by, now, wake_id),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'foreman.wake.failed', NULL, ?)",
                (self.ctx.ids.new("ev"), now, canonical_json({"wake": wake_id, "code": code})),
            )
        return self.get(wake_id)

    def retry(self, wake_id: str, *, by: str) -> Wake:
        wake = self.get(wake_id)
        if wake.status != "failed":
            raise FactoryError(
                "WAKE_STATE_INVALID", "只有 failed 票可以重开", wake=wake_id, status=wake.status
            )
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE wakes SET status = 'open', attempts = 0, not_before = NULL, by = ?, updated_at = ? WHERE id = ?",
                (by, now, wake_id),
            )
        return self.get(wake_id)

    def list(self, *, status: str | None = None) -> list[Wake]:
        if status is None:
            rows = self.ctx.db.query("SELECT * FROM wakes ORDER BY at, id")
        else:
            rows = self.ctx.db.query("SELECT * FROM wakes WHERE status = ? ORDER BY at, id", (status,))
        return [_wake(row) for row in rows]


class ForemanQuery:
    def __init__(self, ctx: Any, epochs: Any) -> None:
        self.ctx = ctx
        self.epochs = epochs

    def registry_get(self, key: str) -> dict[str, Any]:
        snapshot = self.epochs.materialize(self.epochs.current())
        if key not in snapshot.keys():
            raise FactoryError("QUERY_DENIED", "找不到 Spec", reason="not-found", key=key)
        return self._redact(snapshot.doc(key))

    def registry_list(self, kind: str) -> list[str]:
        snapshot = self.epochs.materialize(self.epochs.current())
        return snapshot.keys(kind)

    def registry_diff(self, base: str, commit: str, *, timeout_s: float) -> dict[str, Any]:
        from mao.governance.git import git

        limit = min(float(timeout_s), float(self.epochs.timeout_s))
        repo = self.epochs.registry
        try:
            base_id = git(repo, "rev-parse", "--verify", f"{base}^{{commit}}", timeout_s=limit).strip()
            commit_id = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}", timeout_s=limit).strip()
        except FactoryError as exc:
            raise FactoryError("COMMIT_NOT_FOUND", "找不到提交", commit=commit, base=base) from exc
        raw = git(
            repo,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            base_id,
            commit_id,
            timeout_s=limit,
        )
        files = sorted(item for item in raw.split("\0") if item)
        stat = git(repo, "diff", "--stat", base_id, commit_id, timeout_s=limit)
        return {"files": files, "stat": stat}

    def epoch_list(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.ctx.db.query("SELECT * FROM epochs")]

    def epoch_show(self, commit: str | None = None) -> dict[str, Any]:
        epoch = commit or self.epochs.current()
        return {"epoch": epoch}

    def bean_get(self, bean_id: str) -> dict[str, Any]:
        bean = self.ctx.warehouse.get(bean_id)
        return self._redact({"id": bean.id, "type": bean.type, "sealed": bean.sealed})

    def bean_lineage(self, bean_id: str, direction: str) -> dict[str, Any]:
        return self._redact(self.ctx.warehouse.lineage(bean_id, direction))

    def job_get(self, job_id: str) -> dict[str, Any]:
        from mao.jobs import dump_job, load_job

        return self._redact(dump_job(load_job(self.ctx.db, job_id)))

    def job_transcript(self, job_id: str) -> str:
        root = self.ctx.root / "jobs" / job_id
        texts: list[str] = []
        direct = root / "transcript.txt"
        if direct.is_file():
            texts.append(direct.read_text(encoding="utf-8"))
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and "transcript" in path.name and path != direct:
                    texts.append(path.read_text(encoding="utf-8", errors="replace"))
        return self._redact("\n".join(texts))

    def dlq_list(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.ctx.db.query("SELECT id, station, state FROM jobs WHERE state = 'DEAD'")
        ]

    def drift_list(self) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.ctx.db.query("SELECT * FROM audit_findings WHERE invariant = 'drift'")
        ]

    def invariant_report(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.ctx.db.query("SELECT * FROM audit_findings")]

    def metrics_query(self, station: str, metric: str) -> dict[str, Any]:
        if metric not in {"success_rate", "gate_reject_rate", "cost_per_run_usd"}:
            raise FactoryError("QUERY_DENIED", "不认识的指标", reason="metric", key=metric)
        return {"station": station, "metric": metric}

    def approval_list(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.ctx.db.query("SELECT id, job_id, status FROM approvals")]

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = _SECRET.sub("[REDACTED]", value)
            if _SECRET.search(cleaned):
                raise FactoryError("SECRET_LEAK", "打码后仍像密钥", channel="query")
            return cleaned
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self._redact(item) for key, item in value.items()}
        return value


class ForemanSubmit:
    def __init__(self, ctx: Any, pipeline: Any) -> None:
        self.ctx = ctx
        self.pipeline = pipeline

    def propose(self, document: Mapping[str, Any], commit: str, *, by: str, timeout_s: float) -> Any:
        return self.pipeline.propose(document, commit, by=by, timeout_s=timeout_s)

    def status(self, changeset_id: str) -> Any:
        return self.pipeline.get(changeset_id)

    def withdraw(self, changeset_id: str, *, by: str) -> Any:
        return self.pipeline.withdraw(changeset_id, by=by)

    def review(self, changeset_id: str, verdict: Mapping[str, Any], *, by: str) -> str:
        return self.pipeline.add_review(
            changeset_id,
            runtime=str(verdict.get("runtime", "")),
            verdict=str(verdict.get("verdict", "")),
            note=str(verdict.get("note", "")),
            by=by,
        )


class ConversionState(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class ConversionRequest:
    id: str
    from_type: str
    from_guarantees: tuple[str, ...]
    to_type: str
    to_requires: tuple[str, ...]
    found_at: tuple[dict[str, Any], ...]
    samples: tuple[str, ...]
    blocked_runs: tuple[str, ...]
    status: ConversionState
    changeset_id: str | None
    at: str


class ConversionStore:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def open_or_merge(
        self,
        *,
        from_type: str,
        guarantees: Sequence[str],
        to_type: str,
        requires: Sequence[str],
        found_at: Mapping[str, Any],
        sample: str | None = None,
        run_id: str | None = None,
    ) -> ConversionRequest:
        guarantees_key = canonical_json(sorted(guarantees))
        requires_key = canonical_json(sorted(requires))
        rows = self.ctx.db.query(
            """SELECT * FROM conversion_requests
               WHERE from_type = ? AND guarantees = ? AND to_type = ? AND requires = ?
                 AND status IN ('open', 'in_progress')""",
            (from_type, guarantees_key, to_type, requires_key),
        )
        now = utc_iso(self.ctx.clock.now())
        if rows:
            current = _conversion(rows[0])
            found = list(current.found_at) + [dict(found_at)]
            samples = list(current.samples)
            blocked = list(current.blocked_runs)
            if sample and sample not in samples:
                samples.append(sample)
            if run_id and run_id not in blocked:
                blocked.append(run_id)
            with self.ctx.db.tx() as conn:
                conn.execute(
                    """UPDATE conversion_requests
                       SET found_at = ?, samples = ?, blocked_runs = ?, updated_at = ? WHERE id = ?""",
                    (
                        canonical_json(found),
                        canonical_json(samples),
                        canonical_json(blocked),
                        now,
                        current.id,
                    ),
                )
            return self._get(current.id)
        request_id = self.ctx.ids.new("cr")
        blocked = [run_id] if run_id else []
        samples = [sample] if sample else []
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO conversion_requests
                   (id, from_type, guarantees, to_type, requires, found_at, samples, blocked_runs,
                    status, changeset_id, at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?)""",
                (
                    request_id,
                    from_type,
                    guarantees_key,
                    to_type,
                    requires_key,
                    canonical_json([dict(found_at)]),
                    canonical_json(samples),
                    canonical_json(blocked),
                    now,
                    now,
                ),
            )
        return self._get(request_id)

    def list(self, *, status: str | None = None) -> list[ConversionRequest]:
        if status is None:
            rows = self.ctx.db.query("SELECT * FROM conversion_requests ORDER BY id")
        else:
            rows = self.ctx.db.query(
                "SELECT * FROM conversion_requests WHERE status = ? ORDER BY id", (status,)
            )
        return [_conversion(row) for row in rows]

    def mark(self, request_id: str, status: str, *, changeset_id: str | None = None) -> ConversionRequest:
        current = self._get(request_id)
        if status == "resolved" or current.status == ConversionState.RESOLVED:
            raise FactoryError(
                "CONVERSION_STATE_INVALID",
                "不能直接标成 resolved",
                id=request_id,
                state=status,
                op="mark",
            )
        if status == "in_progress" and current.status != ConversionState.OPEN:
            raise FactoryError(
                "CONVERSION_STATE_INVALID",
                "只能从 open 进入 in_progress",
                id=request_id,
                state=current.status.value,
                op="mark",
            )
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE conversion_requests SET status = ?, changeset_id = ?, updated_at = ? WHERE id = ?",
                (status, changeset_id, now, request_id),
            )
        return self._get(request_id)

    def resolve_matching(self, changeset_id: str, snapshot: Any) -> list[str]:
        resolved: list[str] = []
        now = utc_iso(self.ctx.clock.now())
        for item in self.list():
            if item.status == ConversionState.RESOLVED:
                continue
            if not _convertible(snapshot, item.from_type, item.to_type):
                continue
            with self.ctx.db.tx() as conn:
                conn.execute(
                    """UPDATE conversion_requests
                       SET status = 'resolved', changeset_id = ?, updated_at = ? WHERE id = ?""",
                    (changeset_id, now, item.id),
                )
            resolved.append(item.id)
        return resolved

    def _get(self, request_id: str) -> ConversionRequest:
        rows = self.ctx.db.query("SELECT * FROM conversion_requests WHERE id = ?", (request_id,))
        if not rows:
            raise FactoryError("CONVERSION_NOT_FOUND", "找不到转换需求", id=request_id)
        return _conversion(rows[0])


@dataclass(frozen=True)
class BootstrapReport:
    ok: bool
    missing: tuple[str, ...]
    sink_can_merge: bool


def file_conversions(
    ctx: Any,
    diags: Sequence[Mapping[str, Any]],
    resolutions: Sequence[Mapping[str, Any]],
    *,
    found_at: Mapping[str, Any],
    run_id: str | None = None,
) -> list[str]:
    subjects = {str(item.get("subject")) for item in diags if item.get("code") == "E-NO-CONVERTER"}
    found: list[str] = []
    store = ConversionStore(ctx)
    for item in resolutions:
        if item.get("status") != "no-converter":
            continue
        subject = f"{item.get('line')}/steps.{item.get('step')}.in.{item.get('port')}"
        if subject not in subjects:
            continue
        request = store.open_or_merge(
            from_type=str(item.get("from")),
            guarantees=list(item.get("guarantees") or []),
            to_type=str(item.get("to")),
            requires=list(item.get("requires") or []),
            found_at={
                **dict(found_at),
                "line": item.get("line"),
                "step": item.get("step"),
                "port": item.get("port"),
            },
            run_id=run_id,
        )
        if request.id not in found:
            found.append(request.id)
    return sorted(found)


def redact(ctx: Any, snapshot: Any, text: str) -> str:
    del ctx, snapshot
    return _SECRET.sub("[REDACTED]", text)


def bootstrap_check(snapshot: Any, sink_token_ops: Sequence[str]) -> BootstrapReport:
    keys = snapshot.keys()
    missing = []
    if not any(key.startswith("Line:onboard-machine@") for key in keys):
        missing.append("Line:onboard-machine")
    if not any(key.startswith("Station:registry-sink@") for key in keys):
        missing.append("Station:registry-sink")
    sink_can_merge = any(op in _MERGE_OPS for op in sink_token_ops)
    ok = not missing and not sink_can_merge
    return BootstrapReport(ok=ok, missing=tuple(missing), sink_can_merge=sink_can_merge)


def _wake(row: Mapping[str, Any]) -> Wake:
    return Wake(
        id=str(row["id"]),
        signal=str(row["signal"]),
        subject=str(row["subject"]),
        payload=json.loads(row["payload"]),
        status=str(row["status"]),
        at=str(row["at"]),
        attempts=int(row["attempts"]) if "attempts" in row.keys() and row["attempts"] is not None else 0,
        not_before=None
        if "not_before" not in row.keys() or row["not_before"] is None
        else str(row["not_before"]),
    )


def _conversion(row: Mapping[str, Any]) -> ConversionRequest:
    return ConversionRequest(
        id=str(row["id"]),
        from_type=str(row["from_type"]),
        from_guarantees=tuple(json.loads(row["guarantees"])),
        to_type=str(row["to_type"]),
        to_requires=tuple(json.loads(row["requires"])),
        found_at=tuple(json.loads(row["found_at"])),
        samples=tuple(json.loads(row["samples"])),
        blocked_runs=tuple(json.loads(row["blocked_runs"])),
        status=ConversionState(str(row["status"])),
        changeset_id=None if row["changeset_id"] is None else str(row["changeset_id"]),
        at=str(row["at"]),
    )


def _convertible(snapshot: Any, from_type: str, to_type: str) -> bool:
    for key in snapshot.keys("Station"):
        spec = snapshot.spec(key)
        if spec.get("role") != "converter":
            continue
        converts = spec.get("converts") or {}
        if converts.get("from") == from_type and converts.get("to") == to_type:
            return True
    return from_type == to_type
