"""租约、恢复与单实例执行（spec/system/durable.md）。"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.jobs import JobState, ensure_edge, load_job

LEASE_TTL_S = 60.0


@dataclass(frozen=True)
class Lease:
    resource: str
    owner: str
    acquired_at: str
    expires_at: str


class LeaseStore:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def acquire(self, resource: str, *, owner: str, ttl_s: float = LEASE_TTL_S) -> bool:
        now = utc_iso(self.ctx.clock.now())
        expires = utc_iso(self.ctx.clock.now() + timedelta(seconds=ttl_s))
        with self.ctx.db.tx() as conn:
            row = conn.execute(
                "SELECT owner, acquired_at, expires_at FROM leases WHERE resource = ?",
                (resource,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO leases (resource, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                    (resource, owner, now, expires),
                )
                return True
            expired = str(row["expires_at"]) <= now
            if not expired and str(row["owner"]) != owner:
                return False
            conn.execute(
                "UPDATE leases SET owner = ?, acquired_at = ?, expires_at = ? WHERE resource = ?",
                (owner, now, expires, resource),
            )
            if expired and str(row["owner"]) != owner:
                conn.execute(
                    "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'lease.taken_over', NULL, ?)",
                    (
                        self.ctx.ids.new("ev"),
                        now,
                        canonical_json({"resource": resource, "from": str(row["owner"]), "to": owner}),
                    ),
                )
            return True

    def renew(self, resource: str, *, owner: str, ttl_s: float = LEASE_TTL_S) -> bool:
        expires = utc_iso(self.ctx.clock.now() + timedelta(seconds=ttl_s))
        with self.ctx.db.tx() as conn:
            row = conn.execute("SELECT owner FROM leases WHERE resource = ?", (resource,)).fetchone()
            if row is None or str(row["owner"]) != owner:
                return False
            conn.execute(
                "UPDATE leases SET expires_at = ? WHERE resource = ?",
                (expires, resource),
            )
            return True

    def release(self, resource: str, *, owner: str) -> bool:
        with self.ctx.db.tx() as conn:
            cur = conn.execute(
                "DELETE FROM leases WHERE resource = ? AND owner = ?",
                (resource, owner),
            )
            return cur.rowcount > 0

    def holder(self, resource: str) -> Lease | None:
        rows = self.ctx.db.query(
            "SELECT resource, owner, acquired_at, expires_at FROM leases WHERE resource = ?",
            (resource,),
        )
        if not rows:
            return None
        row = rows[0]
        return Lease(str(row["resource"]), str(row["owner"]), str(row["acquired_at"]), str(row["expires_at"]))

    def valid(self, resource: str) -> Lease | None:
        found = self.holder(resource)
        if found is None:
            return None
        now = utc_iso(self.ctx.clock.now())
        if found.expires_at <= now:
            return None
        return found


class LeaseKeeper:
    def __init__(
        self,
        store: LeaseStore,
        resource: str,
        *,
        owner: str,
        ttl_s: float = LEASE_TTL_S,
    ) -> None:
        self.store = store
        self.resource = resource
        self.owner = owner
        self.ttl_s = ttl_s
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="mao-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self.lost.is_set():
            if self._stop.wait(self.ttl_s / 3):
                return
            try:
                ok = self.store.renew(self.resource, owner=self.owner, ttl_s=self.ttl_s)
            except Exception:
                logging.getLogger("mao").error("租约续约失败 resource=%s", self.resource)
                self.lost.set()
                return
            if not ok:
                logging.getLogger("mao").error("租约已丢失 resource=%s owner=%s", self.resource, self.owner)
                self.lost.set()
                return


@dataclass(frozen=True)
class RecoveryReport:
    retried: tuple[str, ...]
    dead: tuple[str, ...]
    sealed: tuple[str, ...]
    reaped: int


class Recovery:
    def __init__(self, ctx: Any, runners: Callable[[str], Any]) -> None:
        self.ctx = ctx
        self.runners = runners

    def run_once(self) -> RecoveryReport:
        rows = self.ctx.db.query(
            "SELECT id FROM jobs WHERE state IN ('PROVISIONING', 'RUNNING', 'AWAITING_APPROVAL', 'GATING') "
            "ORDER BY id"
        )
        store = LeaseStore(self.ctx)
        retried: list[str] = []
        dead: list[str] = []
        sealed: list[str] = []
        reaped = 0
        for row in rows:
            job = load_job(self.ctx.db, str(row["id"]))
            resource = f"job:{job.id}"
            if store.valid(resource) is not None:
                continue
            if not store.acquire(resource, owner=self.ctx.owner):
                continue
            try:
                reaped += int(self.ctx.provider.reap(job.id, job.attempt))
                action = self._close(job)
            finally:
                store.release(resource, owner=self.ctx.owner)
            if action == "retry":
                retried.append(job.id)
            elif action == "dead":
                dead.append(job.id)
            else:
                sealed.append(job.id)
        return RecoveryReport(tuple(retried), tuple(dead), tuple(sealed), reaped)

    def _close(self, job: Any) -> str:
        runner = self.runners(job.epoch)
        snapshot = runner.snapshot
        if job.state == JobState.GATING and _outputs_sealed(self.ctx, snapshot, job):
            _move(self.ctx, job, JobState.SEALED, "recovery")
            _recovered(self.ctx, job, "sealed")
            return "sealed"
        error = {
            "code": "CONTROL_PLANE_RESTART",
            "message": "控制面重启，未完成的 attempt 记为失败",
            "detail": {"state": job.state.value},
        }
        failed = _move(self.ctx, job, JobState.FAILED, "CONTROL_PLANE_RESTART", error=error)
        _deny_approvals(self.ctx, failed)
        if _retries_left(self.ctx, snapshot, failed):
            _move(self.ctx, failed, JobState.PENDING, "retry", attempt=failed.attempt + 1)
            _recovered(self.ctx, job, "retry")
            return "retry"
        _move(self.ctx, failed, JobState.DEAD, "CONTROL_PLANE_RESTART", error=error)
        _recovered(self.ctx, job, "dead")
        return "dead"


def _outputs_sealed(ctx: Any, snapshot: Any, job: Any) -> bool:
    station = snapshot.spec(job.station)
    outputs = station.get("outputs") or {}
    for port, spec in outputs.items():
        if isinstance(spec, dict) and spec.get("optional") is True:
            continue
        beans = list(job.outputs.get(port) or [])
        if not beans:
            return False
        sealed = False
        for bean_id in beans:
            rows = ctx.db.query(
                "SELECT 1 AS n FROM seals WHERE bean_id = ? AND job_id = ? LIMIT 1",
                (bean_id, job.id),
            )
            if rows:
                sealed = True
                break
        if not sealed:
            return False
    return True


def _retries_left(ctx: Any, snapshot: Any, job: Any) -> bool:
    station = snapshot.spec(job.station)
    retries = (station.get("limits") or {}).get("retries") or {}
    limit = int(retries.get("failed", 0))
    rows = ctx.db.query("SELECT data FROM events WHERE job_id = ? AND kind = 'job.state'", (job.id,))
    count = 0
    for row in rows:
        data = json.loads(row["data"])
        if data.get("to") == JobState.FAILED.value:
            count += 1
    return count <= limit


def _move(
    ctx: Any, job: Any, to: JobState, reason: str, *, attempt: int | None = None, error: Any = None
) -> Any:
    ensure_edge(job, to)
    now = utc_iso(ctx.clock.now())
    new_attempt = job.attempt if attempt is None else attempt
    new_error = job.error if error is None else error
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE jobs SET state = ?, attempt = ?, error = ?, updated_at = ? WHERE id = ?",
            (
                to.value,
                new_attempt,
                None if new_error is None else canonical_json(new_error),
                now,
                job.id,
            ),
        )
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'job.state', ?, ?)",
            (
                ctx.ids.new("ev"),
                now,
                job.id,
                canonical_json(
                    {"from": job.state.value, "to": to.value, "attempt": new_attempt, "reason": reason}
                ),
            ),
        )
    return load_job(ctx.db, job.id)


def _recovered(ctx: Any, job: Any, action: str) -> None:
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'job.recovered', ?, ?)",
            (
                ctx.ids.new("ev"),
                now,
                job.id,
                canonical_json(
                    {
                        "job": job.id,
                        "attempt": job.attempt,
                        "action": action,
                        "code": "CONTROL_PLANE_RESTART",
                    }
                ),
            ),
        )


def _deny_approvals(ctx: Any, job: Any) -> None:
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        rows = conn.execute(
            "SELECT id FROM approvals WHERE job_id = ? AND attempt = ? AND status = 'pending'",
            (job.id, job.attempt),
        ).fetchall()
        conn.execute(
            "UPDATE approvals SET status = 'denied', \"by\" = 'recovery', note = 'CONTROL_PLANE_RESTART', "
            "decided_at = ?, decision = 'deny', decided_by = 'recovery' "
            "WHERE job_id = ? AND attempt = ? AND status = 'pending'",
            (now, job.id, job.attempt),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'approval.decided', ?, ?)",
                (
                    ctx.ids.new("ev"),
                    now,
                    job.id,
                    canonical_json({"id": row["id"], "decision": "deny", "by": "recovery"}),
                ),
            )
