"""调度队列、熔断与 cron。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import canonical_json, parse_duration, utc_iso
from mao.errors import FactoryError
from mao.jobs import TERMINAL, JobState, load_job

_INSTALLED: Scheduler | None = None
_MEM = re.compile(r"^([0-9]+)(Mi|Gi)$")


@dataclass(frozen=True)
class Demand:
    cpu: float
    memory_mb: int
    gpu: int
    pool: str


@dataclass(frozen=True)
class DeadLetter:
    job_id: str
    station: str
    error_code: str
    error: dict[str, Any]
    at: str


class Scheduler:
    def __init__(self, ctx: Any, snapshot: Any, runner: Any) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.runner = runner
        self.config: dict[str, Any] | None = None
        self._used: dict[str, dict[str, float]] = {}

    def load_config(self, path: Path | None = None) -> None:
        path = path or (self.ctx.root / "scheduler.json")
        if not path.is_file():
            raise FactoryError("SCHEDULER_CONFIG_MISSING", "缺少 scheduler.json", path=str(path))
        self.config = json.loads(path.read_text(encoding="utf-8"))
        self._used = {}

    def demand(self, station_key: str) -> Demand:
        self._require()
        station_key = self.snapshot.key("Station", station_key)
        station = self.snapshot.spec(station_key)
        machine = self.snapshot.spec(self.snapshot.key("Machine", station["machine"]))
        resources = dict((machine.get("runtime") or {}).get("resources") or {})
        policy = self.snapshot.spec(self.snapshot.key("Policy", station["policy"]))
        for key, value in (policy.get("resources") or {}).items():
            if key in resources or key in ("cpus", "memory", "gpu", "pool"):
                resources[key] = value
        cpu = float(resources.get("cpus") or 0)
        memory = _memory(resources.get("memory", 0))
        pools = (self.config or {}).get("pools") or {}
        if isinstance(pools.get("memory"), int):
            memory = int(pools["memory"])
        gpu = resources.get("gpu") or 0
        gpu_n = 0 if gpu in (0, "none", None) else int(gpu) if isinstance(gpu, int) else 1
        compartments = (self.config or {}).get("compartments") or {}
        pool = station_key if compartments else "*"
        if cpu > 0 and not pools:
            raise FactoryError("SCHEDULER_CAPACITY_UNSET", "cpu 容量没有配置", station=station_key)
        return Demand(cpu, memory, gpu_n, pool)

    def enqueue(self, job_ids: Sequence[str]) -> None:
        self._require()
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            for job_id in job_ids:
                job = load_job(self.ctx.db, job_id)
                if job.state == JobState.DEAD:
                    self._dead_row(conn, job)
                    continue
                conn.execute(
                    "INSERT INTO schedule_queue (job_id, enqueued_at) VALUES (?, ?) ON CONFLICT(job_id) DO NOTHING",
                    (job_id, now),
                )

    def pump(self) -> list[str]:
        self._require()
        rows = self.ctx.db.query("SELECT job_id FROM schedule_queue ORDER BY job_id")
        chosen: list[str] = []
        pools = (self.config or {}).get("pools") or {}
        cpu_left = pools.get("cpu")
        for row in rows:
            job = load_job(self.ctx.db, row["job_id"])
            if job.state in TERMINAL or job.state == JobState.DEAD:
                continue
            if self._wip_blocked(job):
                continue
            if self._breaker_blocks(job):
                continue
            try:
                need = self.demand(job.station)
            except FactoryError:
                need = Demand(0, 0, 0, "*")
            if isinstance(cpu_left, int) and need.cpu > cpu_left:
                continue
            if isinstance(cpu_left, int):
                cpu_left -= int(need.cpu or 0)
            chosen.append(job.id)
        pumped = []
        for job_id in chosen:
            self.runner.run(job_id)
            pumped.append(job_id)
            with self.ctx.db.tx() as conn:
                conn.execute("DELETE FROM schedule_queue WHERE job_id = ?", (job_id,))
        return pumped

    def tick(self) -> dict[str, list[str]]:
        self._require()
        self._open_breakers()
        cron = self._cron()
        pumped = self.pump()
        return {"expired": [], "cron": cron, "triggered": [], "pumped": pumped, "dead": []}

    def wait_change(self, timeout_s: float) -> None:
        before = self._states()
        pumped = self.pump()
        after = self._states()
        if pumped or after != before:
            return
        self.ctx.clock.sleep(timeout_s)

    def is_quarantined(self, bean_id: str) -> bool:
        rows = self.ctx.db.query("SELECT bean_id FROM quarantines WHERE bean_id = ?", (bean_id,))
        return bool(rows)

    def quarantine(self, bean_id: str, *, by: str, reason: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO quarantines (bean_id, reason, by, at, source) VALUES (?, ?, ?, ?, 'manual') "
                "ON CONFLICT(bean_id) DO UPDATE SET reason = excluded.reason, by = excluded.by, at = excluded.at",
                (bean_id, reason, by, now),
            )

    def release(self, bean_id: str, *, by: str) -> None:
        del by
        with self.ctx.db.tx() as conn:
            conn.execute("DELETE FROM quarantines WHERE bean_id = ?", (bean_id,))

    def dead(self, job_id: str) -> DeadLetter:
        rows = self.ctx.db.query("SELECT * FROM dead_letters WHERE job_id = ?", (job_id,))
        if not rows:
            raise FactoryError("JOB_NOT_FOUND", "没有这条死信", job_id=job_id)
        return _letter(rows[0])

    def list_dead(self) -> list[DeadLetter]:
        self._scan_dead()
        rows = self.ctx.db.query("SELECT * FROM dead_letters ORDER BY job_id")
        return [_letter(row) for row in rows]

    def _require(self) -> None:
        if self.config is None:
            self.load_config()

    def _states(self) -> list[tuple[str, str]]:
        return [
            (row["id"], row["state"]) for row in self.ctx.db.query("SELECT id, state FROM jobs ORDER BY id")
        ]

    def _wip_blocked(self, job: Any) -> bool:
        for item in (self.config or {}).get("wip") or []:
            if item.get("station") in (job.station, job.station.split(":", 1)[-1].split("@", 1)[0]):
                return int(item.get("max", 0)) <= 0
        return False

    def _breaker_blocks(self, job: Any) -> bool:
        subject = "station:" + job.station
        rows = self.ctx.db.query("SELECT state, probes_out FROM breaker_state WHERE subject = ?", (subject,))
        if not rows:
            return False
        state = rows[0]["state"]
        if state == "open":
            return True
        if state == "half_open":
            limit = 1
            for rule in self._breaker_rules():
                if rule.get("subject") == subject:
                    limit = int(rule.get("probe_limit", 1))
            if int(rows[0]["probes_out"]) >= limit:
                return True
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE breaker_state SET probes_out = probes_out + 1 WHERE subject = ?",
                    (subject,),
                )
        return False

    def _breaker_rules(self) -> list[dict[str, Any]]:
        raw = (self.config or {}).get("breakers") or []
        if isinstance(raw, dict):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def _open_breakers(self) -> None:
        for rule in self._breaker_rules():
            subject = str(rule["subject"])
            if not subject.startswith("station:"):
                continue
            station = subject.split(":", 1)[1]
            window = int(rule["min_samples"])
            rows = self.ctx.db.query(
                "SELECT state FROM jobs WHERE station = ? AND state IN ('SEALED', 'DEAD', 'REJECTED') ORDER BY updated_at DESC",
                (station,),
            )
            sample = rows[:window]
            if len(sample) < window:
                continue
            dead = sum(1 for row in sample if row["state"] == "DEAD")
            rate = dead / window
            current = self.ctx.db.query(
                "SELECT state, changed_at FROM breaker_state WHERE subject = ?", (subject,)
            )
            state = current[0]["state"] if current else "closed"
            now = self.ctx.clock.now()
            if state == "closed" and rate > float(rule["failure_rate_gt"]):
                self._set_breaker(subject, "open")
            elif state == "open":
                changed = current[0]["changed_at"]
                opened = _parse_time(changed)
                if (now - opened).total_seconds() >= parse_duration(str(rule["open_for"])):
                    self._set_breaker(subject, "half_open")

    def _set_breaker(self, subject: str, state: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO breaker_state (subject, state, changed_at, probes_out) VALUES (?, ?, ?, 0)
                   ON CONFLICT(subject) DO UPDATE SET state = excluded.state, changed_at = excluded.changed_at, probes_out = 0""",
                (subject, state, now),
            )

    def _cron(self) -> list[str]:
        started: list[str] = []
        cap = (self.config or {}).get("run_cap")
        if cap == 0:
            return []
        enabled = (self.config or {}).get("cron") is True
        for key, doc in self.snapshot._docs.items():
            if not key.startswith("Line:"):
                continue
            spec = doc.get("spec") or {}
            triggers = spec.get("trigger") or []
            if not any(item.get("on") == "cron" for item in triggers):
                continue
            inputs = (spec.get("interface") or {}).get("inputs") or {}
            if inputs:
                self._event("scheduler.cron", {"line": key, "code": "CRON_INPUT_REQUIRED"})
                continue
            if not enabled:
                continue
            if self._cron_recent(key):
                continue
            started.append(key)
            now = utc_iso(self.ctx.clock.now())
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "INSERT INTO cron_fires (line, last_at, run_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(line) DO UPDATE SET last_at = excluded.last_at, run_id = excluded.run_id",
                    (key, now, key),
                )
        return started

    def _cron_recent(self, line: str) -> bool:
        rows = self.ctx.db.query("SELECT last_at FROM cron_fires WHERE line = ?", (line,))
        if not rows:
            return False
        last = _parse_time(rows[0]["last_at"])
        return (self.ctx.clock.now() - last).total_seconds() < 60

    def _scan_dead(self) -> None:
        rows = self.ctx.db.query("SELECT id FROM jobs WHERE state = 'DEAD'")
        with self.ctx.db.tx() as conn:
            for row in rows:
                self._dead_row(conn, load_job(self.ctx.db, row["id"]))

    def _dead_row(self, conn: Any, job: Any) -> None:
        now = utc_iso(self.ctx.clock.now())
        error = job.error or {"code": "DEAD", "message": ""}
        conn.execute(
            "INSERT INTO dead_letters (job_id, station, error_code, error, at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO NOTHING",
            (job.id, job.station, str(error.get("code", "")), canonical_json(error), now),
        )

    def _event(self, kind: str, data: Mapping[str, Any]) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
                (self.ctx.ids.new("ev"), utc_iso(self.ctx.clock.now()), kind, canonical_json(dict(data))),
            )


class ScheduledExecutor:
    def __init__(self, ctx: Any, runner: Any, scheduler: Scheduler) -> None:
        self.ctx = ctx
        self.runner = runner
        self.scheduler = scheduler

    def dispatch(self, job_ids: Sequence[str]) -> None:
        self.scheduler.enqueue(job_ids)

    def wait(self, timeout_s: float) -> None:
        self.scheduler.wait_change(timeout_s)


def install(scheduler: Scheduler) -> None:
    global _INSTALLED
    _INSTALLED = scheduler


def is_quarantined(bean_id: str) -> bool:
    if _INSTALLED is None:
        return False
    return _INSTALLED.is_quarantined(bean_id)


def _memory(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return 0
    match = _MEM.fullmatch(value)
    if match is None:
        raise FactoryError("SCHEDULER_RESOURCE_INVALID", "内存单位不合法", memory=value)
    amount = int(match.group(1))
    return amount * 1024 if match.group(2) == "Gi" else amount


def _letter(row: Any) -> DeadLetter:
    return DeadLetter(row["job_id"], row["station"], row["error_code"], json.loads(row["error"]), row["at"])


def _parse_time(text: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
