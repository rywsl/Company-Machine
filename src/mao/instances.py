"""机器实例配置与实例表（spec/system/instances.md）。"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from mao.base import canonical_bytes, canonical_json, utc_iso
from mao.errors import FactoryError
from mao.sandbox import Mount, SandboxSpec

Mode = Literal["once", "pooled", "resident"]
_MODES = ("once", "per_job", "pooled", "resident")
_OPEN: dict[str, Any] = {}
_OCCUPYING = {"PROVISIONING", "RUNNING", "AWAITING_APPROVAL", "GATING"}


@dataclass(frozen=True)
class InstanceConfig:
    mode: Mode
    pool_size: int
    reset: tuple[str, ...]
    command: tuple[str, ...]
    health: tuple[str, ...]
    health_timeout_s: float
    restart_max: int
    emit_per_min: int


def instance_config(machine: Mapping[str, Any]) -> InstanceConfig:
    runtime = machine.get("runtime") or {}
    if not isinstance(runtime, Mapping):
        runtime = {}
    raw = runtime.get("instance", "once")
    if raw not in _MODES:
        raise _invalid(machine, "instance")
    mode: Mode = "once" if raw in ("once", "per_job") else raw
    if mode == "pooled" and not runtime.get("reset"):
        raise _invalid(machine, "reset-missing")
    if mode == "resident" and runtime.get("reset"):
        raise _invalid(machine, "reset-resident")
    if "pool_size" in runtime and mode != "pooled":
        raise _invalid(machine, "pool_size")
    pool_size = 1
    if mode == "pooled":
        pool_size = _positive_int(runtime.get("pool_size", 1))
        if pool_size is None:
            raise _invalid(machine, "pool_size")
    if mode != "resident" and ("restart" in runtime or "emit_per_min" in runtime):
        raise _invalid(machine, "resident-only")
    command = _optional_argv(runtime, "command")
    if command is None:
        raise _invalid(machine, "argv")
    health, health_timeout = _health(runtime)
    if health is None:
        raise _invalid(machine, "argv")
    reset = _reset(runtime.get("reset"))
    if reset is None:
        raise _invalid(machine, "reset-quote")
    restart_max = 5
    restart = runtime.get("restart")
    if isinstance(restart, Mapping) and restart.get("max") is not None:
        parsed = _positive_int(restart.get("max"))
        if parsed is not None:
            restart_max = parsed
    emit_per_min = 60
    if "emit_per_min" in runtime:
        parsed = _positive_int(runtime.get("emit_per_min"))
        if parsed is not None:
            emit_per_min = parsed
    return InstanceConfig(
        mode,
        pool_size,
        reset,
        command,
        health,
        health_timeout,
        restart_max,
        emit_per_min,
    )


@dataclass(frozen=True)
class InstancesConfig:
    health_every_s: float = 30.0
    idle_ttl_s: float = 600.0
    reset_timeout_s: float = 120.0
    backoff_base_s: float = 5.0
    backoff_max_s: float = 600.0


_CONFIG_FIELDS = (
    "health_every_s",
    "idle_ttl_s",
    "reset_timeout_s",
    "backoff_base_s",
    "backoff_max_s",
)


def load_instances_config(root: Path) -> InstancesConfig:
    path = Path(root) / "factory.json"
    if not path.is_file():
        return InstancesConfig()
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FactoryError("SERVE_CONFIG_INVALID", "factory.json 无法解析", field="instances") from exc
    if not isinstance(body, dict) or "instances" not in body or body.get("instances") is None:
        return InstancesConfig()
    section = body.get("instances")
    if not isinstance(section, dict):
        raise FactoryError("SERVE_CONFIG_INVALID", "instances 段不合法", field="instances")
    values: dict[str, float] = {}
    for name in _CONFIG_FIELDS:
        if name not in section:
            continue
        raw = section[name]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) <= 0:
            raise FactoryError("SERVE_CONFIG_INVALID", "instances 配置不是正数", field=name)
        values[name] = float(raw)
    return InstancesConfig(**values)


@dataclass(frozen=True)
class InstanceRecord:
    id: str
    station: str
    mode: str
    digest: str
    state: str
    sandbox: str
    job: str | None
    jobs_served: int
    failures: int
    next_restart_at: str | None
    started_at: str
    updated_at: str
    error: dict[str, Any] | None


@dataclass
class Lease:
    instance: InstanceRecord
    sandbox: Any
    job_env: Mapping[str, str]
    host_dir: Path
    token: Any = None
    guard: Any = None

    def ctl_dir(self) -> Path:
        return self.host_dir / "job" / "ctl"

    def _check(self) -> None:
        if self.guard is not None:
            self.guard()

    def stage_in(self, attempt_dir: Path) -> None:
        self._check()
        attempt = Path(attempt_dir)
        _copy_tree(attempt / "in", self.host_dir / "job" / "in", replace=True)
        _copy_tree(attempt / "ctl", self.host_dir / "job" / "ctl", replace=True)
        _clear_dir(self.host_dir / "job" / "out")
        bind = getattr(self.sandbox, "bind_attempt", None)
        if bind is not None:
            bind(attempt / "ctl")

    def stage_out(self, attempt_dir: Path) -> None:
        self._check()
        attempt = Path(attempt_dir)
        source = getattr(self.sandbox, "output_root", None) or (self.host_dir / "job" / "out")
        _copy_tree(source, attempt / "out", replace=True)
        _copy_tree(self.host_dir / "job" / "ctl", attempt / "ctl", replace=False)


class _JobSandbox:
    """exec 时并入 job_env（调用方的键优先），并把 ctl 新文件同步回 attempt。"""

    def __init__(self, inner: Any, job_env: Mapping[str, str], job_ctl: Path, guard: Any = None) -> None:
        self._inner = inner
        self._job_env = {str(key): str(value) for key, value in job_env.items()}
        self._job_ctl = job_ctl
        self._guard = guard
        self._attempt_ctl: Path | None = None
        self.spec = inner.spec
        self.name = inner.name

    def bind_attempt(self, ctl: Path) -> None:
        self._attempt_ctl = Path(ctl)

    def bind_job_output(self, job_id: str) -> None:
        bind = getattr(self._inner, "bind_job_output", None)
        if callable(bind):
            bind(job_id)

    @property
    def output_root(self) -> Path | None:
        return getattr(self._inner, "output_root", None)

    def exec(
        self,
        argv: Any,
        *,
        timeout_s: float,
        stdout: Path,
        stderr: Path,
        env: Mapping[str, str] | None = None,
        workdir: str | None = None,
    ) -> Any:
        if self._guard is not None:
            self._guard()
        merged = dict(self._job_env)
        if env:
            merged.update({str(key): str(value) for key, value in env.items()})
        result = self._inner.exec(
            argv,
            timeout_s=timeout_s,
            stdout=stdout,
            stderr=stderr,
            env=merged,
            workdir=workdir,
        )
        if self._attempt_ctl is not None:
            _copy_tree(self._job_ctl, self._attempt_ctl, replace=False)
        return result

    def alive(self) -> bool:
        return bool(self._inner.alive())

    def close(self) -> None:
        self._inner.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Instances:
    def __init__(
        self,
        ctx: Any,
        snapshot: Any,
        *,
        config: InstancesConfig | None = None,
        wakes: Any = None,
        runner_for: Callable[[], Any] | None = None,
    ) -> None:
        self.ctx = ctx
        self.snapshot = snapshot
        self.config = config if config is not None else load_instances_config(ctx.root)
        if wakes is None:
            from mao.foreman import WakeQueue

            wakes = WakeQueue(ctx)
        self.wakes = wakes
        self.runner_for = runner_for
        self._held: dict[str, Any] = {}
        self._invalid_seen: set[tuple[str, str]] = set()
        self._reconciled = False
        self._health_at: dict[str, datetime] = {}
        self._ctl: dict[str, Any] = {}

    def list(self, *, station: str | None = None, state: str | None = None) -> list[InstanceRecord]:
        sql = "SELECT * FROM instances"
        clauses: list[str] = []
        args: list[str] = []
        if station is not None:
            clauses.append("station = ?")
            args.append(station)
        if state is not None:
            clauses.append("state = ?")
            args.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [_record(row) for row in self.ctx.db.query(sql, tuple(args))]

    def ready(self, station: str) -> bool:
        rows = self.ctx.db.query(
            "SELECT 1 FROM instances WHERE station = ? AND state = 'ready' LIMIT 1",
            (station,),
        )
        return bool(rows)

    def acquire(self, job: Any, spec: SandboxSpec) -> Lease:
        station = self.snapshot.spec(job.station)
        machine = self.snapshot.spec(self.snapshot.key("Machine", station["machine"]))
        cfg = instance_config(machine)
        if cfg.mode == "once":
            raise _invalid(machine, "once")
        digest = _digest(self.snapshot, job.station)
        retried = False
        while True:
            instance_id, created = self._claim(job.station, cfg, digest)
            try:
                return self._bind(job, spec, instance_id, created, cfg)
            except FactoryError as exc:
                if exc.code == "INSTANCE_LOST" and not created and not retried:
                    self._stop_row(instance_id, "recovered")
                    retried = True
                    continue
                raise

    def release(self, lease: Lease, *, destroy: bool = False) -> None:
        if lease.guard is not None:
            lease.guard()
        try:
            self._release(lease, destroy=destroy)
            if lease.token is not None:
                from mao.durable import LeaseStore

                LeaseStore(self.ctx).release_fenced(lease.token)
        except Exception:
            _LOG.exception("释放实例失败")

    def reconcile(self, snapshot: Any | None = None) -> dict[str, list[str]]:
        if snapshot is not None:
            self.snapshot = snapshot
        report: dict[str, list[str]] = {"started": [], "stopped": [], "failed": [], "crashed": []}
        now = self.ctx.clock.now()
        first = not self._reconciled
        desired = self._desired(self.snapshot, report)
        for station in desired:
            self._reconcile_station(station, now, first, report)
        self._stop_absent(desired, report)
        self._stop_idle(now, report)
        self._close_external()
        self._reconciled = True
        for key in report:
            report[key] = sorted(set(report[key]))
        return report

    def stop(self, ref: str) -> InstanceRecord:
        record = self._resolve(ref)
        if record.state == "stopped":
            return record
        marker = record.mode == "resident"
        self._stop_row(
            record.id,
            "manual",
            code="INSTANCE_STOPPED" if marker else None,
            message="实例已停止",
            emit_failed=False,
        )
        return self._load(record.id)

    def restart(self, ref: str) -> InstanceRecord:
        record = self._resolve(ref, include_stopped=True)
        if record.mode == "pooled":
            if record.state != "stopped":
                self._stop_row(record.id, "restarted", emit_failed=False)
            return self._load(record.id)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET state = 'failed', failures = 0, next_restart_at = ?, "
                "error = NULL, updated_at = ? WHERE id = ?",
                (now, now, record.id),
            )
        return self._load(record.id)

    def recover(self) -> list[str]:
        handled: list[str] = []
        rows = self.ctx.db.query("SELECT * FROM instances WHERE state != 'stopped' ORDER BY id")
        for row in rows:
            instance_id = str(row["id"])
            self._clear_finished_job(row)
            if str(row["mode"]) == "pooled":
                self._stop_row(instance_id, "recovered", emit_failed=False)
            else:
                self._recover_resident(row)
            handled.append(instance_id)
        return sorted(handled)

    def close(self) -> None:
        for instance_id in list(self._ctl):
            self._stop_ctl(instance_id)
        for instance_id, sandbox in list(self._held.items()):
            try:
                record = self._load(instance_id)
            except FactoryError:
                continue
            if record.state != "stopped":
                continue
            try:
                sandbox.close()
            except Exception:
                _LOG.exception("关闭已停止实例的沙箱失败")
            self._held.pop(instance_id, None)

    def _desired(self, snapshot: Any, report: dict[str, list[str]]) -> list[str]:
        found: list[str] = []
        for key in snapshot.keys("Station"):
            doc = snapshot.doc(key)
            lifecycle = (doc.get("metadata") or {}).get("lifecycle", "active")
            if lifecycle not in (None, "active"):
                continue
            try:
                machine = snapshot.spec(snapshot.key("Machine", snapshot.spec(key)["machine"]))
            except FactoryError:
                continue
            runtime = machine.get("runtime") if isinstance(machine.get("runtime"), Mapping) else {}
            raw_mode = (runtime or {}).get("instance", "once")
            try:
                cfg = instance_config(machine)
            except FactoryError as exc:
                if exc.code == "INSTANCE_INVALID" and raw_mode == "resident":
                    digest = _digest(snapshot, key)
                    mark = (key, digest)
                    if mark not in self._invalid_seen:
                        self._invalid_seen.add(mark)
                        self._event(
                            "instance.failed",
                            {
                                "instance": None,
                                "station": key,
                                "code": "INSTANCE_INVALID",
                                "reason": exc.detail.get("reason"),
                            },
                        )
                continue
            if cfg.mode == "resident":
                found.append(key)
        return sorted(found)

    def _reconcile_station(
        self, station: str, now: datetime, first: bool, report: dict[str, list[str]]
    ) -> None:
        live = self._live_rows(station)
        if not live:
            if not self._stopped_marker(station):
                self._create_resident(station, 0, report)
            return
        digest = _digest(self.snapshot, station)
        stale = [row for row in live if str(row["digest"]) != digest]
        current = [row for row in live if str(row["digest"]) == digest]
        created: str | None = None
        if stale and not current:
            created = self._create_resident(station, 0, report)
        if current or created is not None:
            for row in stale:
                if row["job"] is None and str(row["state"]) != "stopped":
                    self._stop_row(str(row["id"]), "replaced", emit_failed=False)
                    report["stopped"].append(str(row["id"]))
        healthy: set[str] = set()
        for row in self._live_rows(station):
            if str(row["state"]) not in ("ready", "busy"):
                continue
            if self._inspect(row, now, first, report):
                healthy.add(str(row["id"]))
        for row in self._live_rows(station):
            if str(row["state"]) != "failed" or not row["next_restart_at"]:
                continue
            if _parse_iso(str(row["next_restart_at"])) > now:
                continue
            failures = int(row["failures"])
            self._stop_row(str(row["id"]), "restarted", emit_failed=False)
            report["stopped"].append(str(row["id"]))
            self._create_resident(station, failures, report)
        for row in self._live_rows(station):
            if str(row["state"]) not in ("ready", "busy"):
                continue
            if str(row["id"]) not in healthy:
                continue
            if _age_s(str(row["started_at"]), now) <= self.config.backoff_max_s:
                continue
            with self.ctx.db.tx() as conn:
                conn.execute(
                    "UPDATE instances SET failures = 0, updated_at = ? WHERE id = ?",
                    (utc_iso(now), row["id"]),
                )

    def _create_resident(self, station: str, failures: int, report: dict[str, list[str]]) -> str | None:
        machine = self._machine(station)
        cfg = instance_config(machine)
        digest = _digest(self.snapshot, station)
        now = utc_iso(self.ctx.clock.now())
        instance_id = self.ctx.ids.new("in")
        with self.ctx.db.tx() as conn:
            conn.execute(
                """INSERT INTO instances (
                       id, station, mode, digest, state, sandbox, job, jobs_served, failures,
                       started_at, updated_at
                   ) VALUES (?, ?, 'resident', ?, 'starting', ?, NULL, 0, ?, ?, ?)""",
                (instance_id, station, digest, f"mao-inst-{instance_id}", failures, now, now),
            )
        self._ensure_dirs(station, instance_id, "resident")
        spec = instance_spec(self.ctx, self.snapshot, station, instance_id)
        sandbox = None
        try:
            sandbox = self.ctx.provider.open(spec)
            self._held[instance_id] = sandbox
            _remember(instance_id, sandbox)
            self._health_at[instance_id] = self.ctx.clock.now()
            if not sandbox.alive():
                raise FactoryError("INSTANCE_DIED", "主进程已退出", instance=instance_id)
            health = self._run_health(sandbox, cfg, instance_id)
            if health is not None and (health.timed_out or health.exit_code != 0):
                raise FactoryError(
                    "INSTANCE_UNHEALTHY",
                    "实例健康检查失败",
                    instance=instance_id,
                    exit_code=health.exit_code,
                )
        except FactoryError as exc:
            self._close_held(instance_id, sandbox)
            self._note_failure(instance_id, station, exc.code, exc.message, cfg, report)
            return None
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET state = 'ready', updated_at = ? WHERE id = ?",
                (now, instance_id),
            )
        self._event("instance.started", {"instance": instance_id, "station": station, "mode": "resident"})
        self._start_ctl(instance_id, station)
        report["started"].append(instance_id)
        return instance_id

    def _inspect(self, row: Any, now: datetime, first: bool, report: dict[str, list[str]]) -> bool:
        instance_id = str(row["id"])
        if not first and not self._health_due(instance_id, now):
            return False
        cfg = instance_config(self._machine(str(row["station"])))
        spec = instance_spec(self.ctx, self.snapshot, str(row["station"]), instance_id)
        sandbox = self._sandbox_of(instance_id, spec)
        self._health_at[instance_id] = now
        if not sandbox.alive():
            self._close_held(instance_id)
            self._note_failure(instance_id, str(row["station"]), "INSTANCE_DIED", "主进程已退出", cfg, report)
            return False
        health = self._run_health(sandbox, cfg, instance_id)
        if health is not None and (health.timed_out or health.exit_code != 0):
            self._close_held(instance_id)
            self._note_failure(
                instance_id,
                str(row["station"]),
                "INSTANCE_UNHEALTHY",
                "实例健康检查失败",
                cfg,
                report,
            )
            return False
        return True

    def _note_failure(
        self,
        instance_id: str,
        station: str,
        code: str,
        message: str,
        cfg: InstanceConfig,
        report: dict[str, list[str]],
    ) -> None:
        try:
            current = int(self._row(instance_id)["failures"])
        except FactoryError:
            return
        failures = current + 1
        delay = min(self.config.backoff_base_s * (2 ** (failures - 1)), self.config.backoff_max_s)
        next_at = utc_iso(self.ctx.clock.now() + timedelta(seconds=delay))
        crashed = failures >= cfg.restart_max
        state = "crashed" if crashed else "failed"
        from mao.egress import SecretStore

        error = canonical_json({"code": code, "message": SecretStore(self.ctx.root).redact(message)})
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET state = ?, failures = ?, next_restart_at = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                (state, failures, next_at, error, now, instance_id),
            )
        self._event(
            "instance.failed",
            {"instance": instance_id, "station": station, "code": code, "reason": code},
        )
        report["failed"].append(instance_id)
        if crashed:
            self._event(
                "instance.crashed", {"instance": instance_id, "station": station, "failures": failures}
            )
            report["crashed"].append(instance_id)
            self.wakes.file("instance", f"instance:{station}", {"instance": instance_id, "code": code})

    def _stop_absent(self, desired: list[str], report: dict[str, list[str]]) -> None:
        wanted = set(desired)
        rows = self.ctx.db.query(
            "SELECT id, station, job FROM instances WHERE mode = 'resident' AND state != 'stopped' ORDER BY id"
        )
        for row in rows:
            if str(row["station"]) in wanted or row["job"] is not None:
                continue
            self._stop_row(str(row["id"]), "removed", emit_failed=False)
            report["stopped"].append(str(row["id"]))

    def _stop_idle(self, now: datetime, report: dict[str, list[str]]) -> None:
        rows = self.ctx.db.query(
            "SELECT id, idle_since FROM instances WHERE mode = 'pooled' AND state = 'ready' ORDER BY id"
        )
        for row in rows:
            idle = row["idle_since"]
            if not idle or _age_s(str(idle), now) < self.config.idle_ttl_s:
                continue
            self._stop_row(str(row["id"]), "idle", emit_failed=False)
            report["stopped"].append(str(row["id"]))

    def _close_external(self) -> None:
        for instance_id in list(self._held):
            try:
                record = self._load(instance_id)
            except FactoryError:
                continue
            if record.state != "stopped":
                continue
            self._close_held(instance_id)

    def _recover_resident(self, row: Any) -> None:
        instance_id = str(row["id"])
        station = str(row["station"])
        spec = instance_spec(self.ctx, self.snapshot, station, instance_id)
        sandbox = _OPEN.get(instance_id)
        try:
            if sandbox is None:
                sandbox = self.ctx.provider.attach(spec)
                _remember(instance_id, sandbox)
            self._held[instance_id] = _OPEN.get(instance_id, sandbox)
            if not self._held[instance_id].alive():
                cfg = instance_config(self._machine(station))
                self._close_held(instance_id)
                empty: dict[str, list[str]] = {"failed": [], "crashed": [], "started": [], "stopped": []}
                self._note_failure(instance_id, station, "INSTANCE_DIED", "主进程已退出", cfg, empty)
                return
        except FactoryError as exc:
            cfg = instance_config(self._machine(station))
            code = (
                exc.code
                if exc.code in ("INSTANCE_LOST", "INSTANCE_DIED", "INSTANCE_UNHEALTHY")
                else "INSTANCE_DIED"
            )
            self._note_failure(
                instance_id,
                station,
                code,
                exc.message,
                cfg,
                {"failed": [], "crashed": [], "started": [], "stopped": []},
            )
            return
        self._set_state(instance_id, "ready")
        self._start_ctl(instance_id, station)

    def _clear_finished_job(self, row: Any) -> None:
        job_id = row["job"]
        if not job_id:
            return
        jobs = self.ctx.db.query("SELECT state FROM jobs WHERE id = ?", (job_id,))
        if jobs and str(jobs[0]["state"]) in _OCCUPYING:
            return
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET job = NULL, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )

    def _live_rows(self, station: str) -> list[Any]:
        return list(
            self.ctx.db.query(
                "SELECT * FROM instances WHERE station = ? AND mode = 'resident' AND state != 'stopped' ORDER BY id",
                (station,),
            )
        )

    def _stopped_marker(self, station: str) -> bool:
        rows = self.ctx.db.query(
            "SELECT error FROM instances WHERE station = ? AND state = 'stopped' ORDER BY id",
            (station,),
        )
        for row in rows:
            if not row["error"]:
                continue
            try:
                body = json.loads(row["error"])
            except json.JSONDecodeError:
                continue
            if isinstance(body, dict) and body.get("code") == "INSTANCE_STOPPED":
                return True
        return False

    def _start_ctl(self, instance_id: str, station: str) -> None:
        if instance_id in self._ctl:
            return
        from mao.ctl.server import CtlServer

        ctl = self.ctx.root / "instances" / instance_id / "main" / "ctl"
        (ctl / "requests").mkdir(parents=True, exist_ok=True)
        (ctl / "responses").mkdir(parents=True, exist_ok=True)
        server = CtlServer(ctl, _EmitHandler(self, instance_id, station), self.ctx.clock)
        server.start(0.2)
        self._ctl[instance_id] = server

    def _stop_ctl(self, instance_id: str) -> None:
        server = self._ctl.pop(instance_id, None)
        if server is None:
            return
        try:
            server.stop()
        except Exception:
            _LOG.exception("停止实例控制通道失败")

    def _health_due(self, instance_id: str, now: datetime) -> bool:
        last = self._health_at.get(instance_id)
        if last is None:
            return True
        return (now - last).total_seconds() >= self.config.health_every_s

    def _sandbox_of(self, instance_id: str, spec: SandboxSpec) -> Any:
        found = _OPEN.get(instance_id) or self._held.get(instance_id)
        if found is not None:
            return found
        sandbox = self.ctx.provider.attach(spec)
        self._held[instance_id] = sandbox
        _remember(instance_id, sandbox)
        return sandbox

    def _claim(self, station: str, cfg: InstanceConfig, digest: str) -> tuple[str, bool]:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            if cfg.mode == "resident":
                row = conn.execute(
                    "SELECT id FROM instances WHERE station = ? AND state = 'ready' AND mode = 'resident' "
                    "ORDER BY id LIMIT 1",
                    (station,),
                ).fetchone()
                if row is None:
                    raise FactoryError("INSTANCE_NOT_READY", "常驻实例还没就绪", station=station)
                conn.execute(
                    "UPDATE instances SET state = 'busy', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                return str(row["id"]), False
            row = conn.execute(
                "SELECT id FROM instances WHERE station = ? AND state = 'ready' AND digest = ? "
                "ORDER BY id LIMIT 1",
                (station, digest),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE instances SET state = 'busy', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                return str(row["id"]), False
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM instances WHERE station = ? AND state != 'stopped'",
                (station,),
            ).fetchone()
            if int(count["n"]) >= cfg.pool_size:
                raise FactoryError("INSTANCE_BUSY", "池里没有空闲实例", station=station)
            instance_id = self.ctx.ids.new("in")
            conn.execute(
                """INSERT INTO instances (
                       id, station, mode, digest, state, sandbox, job, jobs_served, failures,
                       started_at, updated_at
                   ) VALUES (?, ?, 'pooled', ?, 'starting', ?, NULL, 0, 0, ?, ?)""",
                (instance_id, station, digest, f"mao-inst-{instance_id}", now, now),
            )
            return instance_id, True

    def _bind(
        self,
        job: Any,
        spec: SandboxSpec,
        instance_id: str,
        created: bool,
        cfg: InstanceConfig,
    ) -> Lease:
        host = self._ensure_dirs(job.station, instance_id, cfg.mode)
        inst_spec = instance_spec(self.ctx, self.snapshot, job.station, instance_id)
        sandbox = None
        try:
            if created:
                sandbox = self.ctx.provider.open(inst_spec)
                self._held[instance_id] = sandbox
                _remember(instance_id, sandbox)
                health = self._run_health(sandbox, cfg, instance_id)
                if health is not None and (health.timed_out or health.exit_code != 0):
                    self._abandon_new(instance_id, job.station, health.exit_code)
                    raise FactoryError(
                        "INSTANCE_UNHEALTHY",
                        "实例健康检查失败",
                        instance=instance_id,
                        exit_code=health.exit_code,
                    )
                self._set_state(instance_id, "busy")
                self._event(
                    "instance.started",
                    {"instance": instance_id, "station": job.station, "mode": cfg.mode},
                )
            else:
                sandbox = self.ctx.provider.attach(inst_spec)
                if instance_id not in self._held:
                    self._held[instance_id] = sandbox
                _remember(instance_id, sandbox)
        except FactoryError:
            if created and sandbox is not None:
                try:
                    if str(self._row(instance_id)["state"]) != "stopped":
                        self._abandon_new(instance_id, job.station, None)
                except FactoryError:
                    _LOG.exception("标记新建实例失败")
            raise
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET state = 'busy', job = ?, updated_at = ? WHERE id = ?",
                (job.id, now, instance_id),
            )
        self._event("instance.leased", {"instance": instance_id, "job": job.id}, job_id=job.id)
        record = self._load(instance_id)
        job_env = {str(key): str(value) for key, value in spec.env.items()}
        from mao.durable import LeaseStore

        store = LeaseStore(self.ctx)
        token = store.acquire_fenced(
            f"instance:{instance_id}",
            owner=f"job:{job.id}:{job.attempt}",
        )
        if token is None:
            raise FactoryError(
                "LEASE_LOST",
                "实例租约已被占用",
                resource=f"instance:{instance_id}",
                owner=f"job:{job.id}:{job.attempt}",
                generation=0,
            )

        def _guard() -> None:
            store.guard(token.resource, owner=token.owner, generation=token.generation)

        wrapped = _JobSandbox(sandbox, job_env, host / "job" / "ctl", guard=_guard)
        return Lease(record, wrapped, job_env, host, token=token, guard=_guard)

    def _release(self, lease: Lease, *, destroy: bool) -> None:
        instance_id = lease.instance.id
        station = lease.instance.station
        job_id = lease.instance.job
        for name in ("in", "out", "ctl"):
            _clear_dir(lease.host_dir / "job" / name)
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET job = NULL, jobs_served = jobs_served + 1, updated_at = ? WHERE id = ?",
                (now, instance_id),
            )
        ran_reset = lease.instance.mode == "pooled" and not destroy
        self._event(
            "instance.released",
            {"instance": instance_id, "job": job_id, "reset": ran_reset},
            job_id=job_id,
        )
        if destroy:
            self._stop_row(instance_id, "timeout", sandbox=lease.sandbox)
            return
        if lease.instance.mode != "pooled":
            self._set_state(instance_id, "ready")
            return
        if not self._reset(lease):
            return
        cfg = instance_config(self._machine(station))
        health = self._run_health(lease.sandbox, cfg, instance_id)
        if health is not None and (health.timed_out or health.exit_code != 0):
            self._fail_reset(instance_id, station, sandbox=lease.sandbox)
            return
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET state = 'ready', idle_since = ?, updated_at = ? WHERE id = ?",
                (now, now, instance_id),
            )

    def _reset(self, lease: Lease) -> bool:
        cfg = instance_config(self._machine(lease.instance.station))
        if not cfg.reset:
            return True
        row = self.ctx.db.query(
            "SELECT jobs_served FROM instances WHERE id = ?",
            (lease.instance.id,),
        )[0]
        logs = lease.host_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stdout = logs / f"reset-{int(row['jobs_served'])}.log"
        stderr = logs / f"reset-{int(row['jobs_served'])}.stderr"
        result = lease.sandbox.exec(
            list(cfg.reset),
            timeout_s=self.config.reset_timeout_s,
            stdout=stdout,
            stderr=stderr,
            workdir="/workspace",
        )
        if stderr.is_file():
            with stdout.open("ab") as handle:
                handle.write(stderr.read_bytes())
            stderr.unlink()
        if result.timed_out or result.exit_code != 0:
            self._fail_reset(lease.instance.id, lease.instance.station, sandbox=lease.sandbox)
            return False
        return True

    def _run_health(self, sandbox: Any, cfg: InstanceConfig, instance_id: str) -> Any:
        if not cfg.health:
            return None
        logs = self.ctx.root / "instances" / instance_id / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return sandbox.exec(
            list(cfg.health),
            timeout_s=cfg.health_timeout_s,
            stdout=logs / "health.stdout",
            stderr=logs / "health.stderr",
            workdir="/workspace",
        )

    def _fail_reset(self, instance_id: str, station: str, *, sandbox: Any) -> None:
        self._stop_row(
            instance_id,
            "reset",
            code="SERVICE_FAILED",
            message="reset 失败",
            failed_reason="reset",
            sandbox=sandbox,
        )

    def _abandon_new(self, instance_id: str, station: str, exit_code: int | None) -> None:
        self._stop_row(
            instance_id,
            "reset",
            code="INSTANCE_UNHEALTHY",
            message="实例健康检查失败",
            failed_reason="health",
            sandbox=None,
        )
        del station, exit_code

    def _stop_row(
        self,
        instance_id: str,
        reason: str,
        *,
        code: str | None = None,
        message: str = "",
        failed_reason: str | None = None,
        sandbox: Any = None,
        emit_failed: bool | None = None,
    ) -> None:
        try:
            station = str(self._row(instance_id)["station"])
        except FactoryError:
            station = ""
        now = utc_iso(self.ctx.clock.now())
        error = None
        if code:
            from mao.egress import SecretStore

            error = canonical_json({"code": code, "message": SecretStore(self.ctx.root).redact(message)})
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET state = 'stopped', job = NULL, updated_at = ?, error = ? WHERE id = ?",
                (now, error, instance_id),
            )
        self._event("instance.stopped", {"instance": instance_id, "station": station, "reason": reason})
        if emit_failed is None:
            emit_failed = bool(code) and code != "INSTANCE_STOPPED"
        if emit_failed and code:
            self._event(
                "instance.failed",
                {
                    "instance": instance_id,
                    "station": station,
                    "code": code,
                    "reason": failed_reason or reason,
                },
            )
        self._stop_ctl(instance_id)
        self._close_held(instance_id, sandbox)
        from mao.egress import close_egress

        close_egress(self.ctx.db, f"inst-{instance_id}")

    def _close_held(self, instance_id: str, sandbox: Any = None) -> None:
        held = self._held.pop(instance_id, None)
        shared = _OPEN.pop(instance_id, None)
        seen: list[Any] = []
        for item in (held, shared, sandbox):
            if item is None or item in seen:
                continue
            seen.append(item)
            target = getattr(item, "_inner", item)
            if target not in seen:
                seen.append(target)
            try:
                item.close()
            except Exception:
                _LOG.exception("关闭实例沙箱失败")

    def _set_state(self, instance_id: str, state: str) -> None:
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                "UPDATE instances SET state = ?, updated_at = ? WHERE id = ?",
                (state, now, instance_id),
            )

    def _ensure_dirs(self, station: str, instance_id: str, mode: str) -> Path:
        host = self.ctx.root / "instances" / instance_id
        for path in (
            host / "job" / "in",
            host / "job" / "out",
            host / "job" / "ctl",
            host / "main" / "ctl",
            host / "main" / "out",
            host / "main" / "inbox",
            host / "workspace",
            host / "logs",
        ):
            path.mkdir(parents=True, exist_ok=True)
        if mode == "resident":
            (self.ctx.root / "instances" / "state" / _station_name(self.snapshot, station)).mkdir(
                parents=True, exist_ok=True
            )
        return host

    def _machine(self, station: str) -> Mapping[str, Any]:
        body = self.snapshot.spec(station)
        return self.snapshot.spec(self.snapshot.key("Machine", body["machine"]))

    def _resolve(self, ref: str, *, include_stopped: bool = False) -> InstanceRecord:
        rows = self.ctx.db.query("SELECT * FROM instances WHERE id = ?", (ref,))
        if rows:
            return _record(rows[0])
        if include_stopped:
            rows = self.ctx.db.query(
                "SELECT * FROM instances WHERE station = ? ORDER BY id",
                (ref,),
            )
            live = [row for row in rows if str(row["state"]) != "stopped"]
            if live:
                return _record(live[0])
            if rows:
                return _record(rows[-1])
            raise FactoryError("INSTANCE_NOT_FOUND", "没有这个实例", instance=ref)
        rows = self.ctx.db.query(
            "SELECT * FROM instances WHERE station = ? AND state != 'stopped' ORDER BY id",
            (ref,),
        )
        if not rows:
            raise FactoryError("INSTANCE_NOT_FOUND", "没有这个实例", instance=ref)
        return _record(rows[0])

    def _load(self, instance_id: str) -> InstanceRecord:
        return _record(self._row(instance_id))

    def _row(self, instance_id: str) -> Any:
        rows = self.ctx.db.query("SELECT * FROM instances WHERE id = ?", (instance_id,))
        if not rows:
            raise FactoryError("INSTANCE_NOT_FOUND", "没有这个实例", instance=instance_id)
        return rows[0]

    def _event(self, kind: str, data: Mapping[str, Any], *, job_id: str | None = None) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
                (
                    self.ctx.ids.new("ev"),
                    utc_iso(self.ctx.clock.now()),
                    kind,
                    job_id,
                    canonical_json(dict(data)),
                ),
            )


def _invalid(machine: Mapping[str, Any], reason: str) -> FactoryError:
    meta = machine.get("metadata") if isinstance(machine.get("metadata"), Mapping) else {}
    name = str((meta or {}).get("name") or "")
    version = str((meta or {}).get("version") or "")
    ref = f"{name}@{version}" if name and version else name
    return FactoryError("INSTANCE_INVALID", "实例配置不合法", machine=ref, reason=reason)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _argv(value: Any) -> tuple[str, ...] | None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        return None
    return tuple(value)


def _optional_argv(runtime: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    if key not in runtime:
        return ()
    return _argv(runtime.get(key))


def _health(runtime: Mapping[str, Any]) -> tuple[tuple[str, ...] | None, float]:
    if "health" not in runtime:
        return (), 30.0
    body = runtime.get("health")
    if not isinstance(body, Mapping):
        return None, 30.0
    parsed = _argv(body.get("run"))
    timeout = body.get("timeout_s", 30)
    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        seconds = 30.0
    return parsed, seconds


def _record(row: Any) -> InstanceRecord:
    import json

    raw = row["error"]
    error = json.loads(raw) if raw else None
    return InstanceRecord(
        str(row["id"]),
        str(row["station"]),
        str(row["mode"]),
        str(row["digest"]),
        str(row["state"]),
        str(row["sandbox"]),
        None if row["job"] is None else str(row["job"]),
        int(row["jobs_served"]),
        int(row["failures"]),
        None if row["next_restart_at"] is None else str(row["next_restart_at"]),
        str(row["started_at"]),
        str(row["updated_at"]),
        error if isinstance(error, dict) else None,
    )


_LOG = logging.getLogger("mao.instances")


def emit(
    runner: Any,
    *,
    station: str,
    port: str,
    source: Path,
    instance_id: str | None,
    labels: Mapping[str, str] | None = None,
    trigger: Mapping[str, Any] | None = None,
) -> str:
    from datetime import timedelta

    from mao.jobs import JobState

    ctx = runner.ctx
    snapshot = runner.snapshot
    station_key = snapshot.key("Station", station)
    if instance_id is not None:
        rows = ctx.db.query("SELECT station, state FROM instances WHERE id = ?", (instance_id,))
        if not rows or str(rows[0]["state"]) == "stopped" or str(rows[0]["station"]) != station_key:
            raise FactoryError("EMIT_INVALID", "实例不能封存", reason="instance")
    try:
        station_spec = snapshot.spec(station_key)
    except FactoryError as exc:
        raise FactoryError("EMIT_INVALID", "工位不能封存", reason="role") from exc
    if station_spec.get("role") != "source":
        raise FactoryError("EMIT_INVALID", "只有 source 工位能封存", reason="role")
    outputs = station_spec.get("outputs") or {}
    decl = outputs.get(port)
    type_expr = decl.get("type") if isinstance(decl, Mapping) else decl
    if decl is None or str(type_expr).startswith("list<"):
        raise FactoryError("EMIT_INVALID", "端口不能封存", reason="port")
    folder = Path(source)
    if not folder.is_dir():
        raise FactoryError("EMIT_INVALID", "封存目录不合法", reason="dir")
    if instance_id is not None:
        root = (ctx.root / "instances" / instance_id / "main" / "out").resolve()
        try:
            folder.resolve().relative_to(root)
        except ValueError as exc:
            raise FactoryError("EMIT_INVALID", "封存目录不在实例输出目录下", reason="dir") from exc
    total = sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())
    if total > 64 * 1024 * 1024:
        raise FactoryError("EMIT_INVALID", "封存目录超过 64 MiB", reason="dir")
    if instance_id is not None:
        machine = snapshot.spec(snapshot.key("Machine", station_spec["machine"]))
        limit = instance_config(machine).emit_per_min
        cutoff = utc_iso(ctx.clock.now() - timedelta(seconds=60))
        count = ctx.db.query(
            "SELECT COUNT(*) AS n FROM emits WHERE station = ? AND at >= ?",
            (station_key, cutoff),
        )
        if int(count[0]["n"]) >= limit:
            raise FactoryError("EMIT_RATE_LIMITED", "封存过于频繁", station=station_key)
    staging = ctx.root / "tmp" / "emit" / ctx.ids.new("em")
    shutil.copytree(folder, staging / port)
    by = f"instance:{instance_id}" if instance_id else str((trigger or {}).get("on") or "emit")
    job = runner.adopt(station_key, staging, labels=labels or {}, by=by)
    if job.state != JobState.SEALED:
        feedback = ""
        if isinstance(job.error, dict):
            feedback = str((job.error.get("detail") or {}).get("feedback") or job.error.get("message") or "")
        raise FactoryError("EMIT_INVALID", "出厂检验未通过", reason="gate", feedback=feedback)
    produced = job.outputs.get(port)
    bean_id = produced[0] if isinstance(produced, list) else produced
    if not isinstance(bean_id, str) or not bean_id:
        raise FactoryError("EMIT_INVALID", "没有封存出 Bean", reason="gate")
    body = dict(trigger) if trigger is not None else {"on": "emit", "instance": instance_id}
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO emits (id, instance, station, port, bean, trigger, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job.id, instance_id, station_key, port, bean_id, canonical_json(body), now),
        )
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'instance.emit', ?, ?)",
            (
                ctx.ids.new("ev"),
                now,
                job.id,
                canonical_json(
                    {
                        "instance": instance_id,
                        "station": station_key,
                        "port": port,
                        "bean": bean_id,
                        "job": job.id,
                    }
                ),
            ),
        )
    if instance_id is not None:
        shutil.rmtree(folder, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    return bean_id


class _EmitHandler:
    def __init__(self, owner: Instances, instance_id: str, station: str) -> None:
        self.owner = owner
        self.instance_id = instance_id
        self.station = station

    def handle(self, body: Mapping[str, Any]) -> dict[str, Any]:
        rid = str(body.get("id") or "")
        if body.get("op") != "emit":
            return _ctl_fail(rid, "CTL_OP_INVALID", "不认识的控制操作", {})
        if self.owner.runner_for is None:
            return _ctl_fail(rid, "EMIT_INVALID", "没有执行器", {"reason": "runner"})
        raw = str(body.get("dir") or "")
        source = _emit_host(self.owner.ctx.root, self.instance_id, raw)
        labels = body.get("labels") if isinstance(body.get("labels"), Mapping) else {}
        try:
            bean = emit(
                self.owner.runner_for(),
                station=self.station,
                port=str(body.get("port") or ""),
                source=source,
                instance_id=self.instance_id,
                labels={str(key): str(value) for key, value in labels.items()},
            )
        except FactoryError as exc:
            from mao.egress import SecretStore

            store = SecretStore(self.owner.ctx.root)
            return _ctl_fail(rid, exc.code, store.redact(exc.message), store.redact_obj(dict(exc.detail)))
        rows = self.owner.ctx.db.query("SELECT id FROM emits WHERE bean = ? ORDER BY at", (bean,))
        job_id = str(rows[-1]["id"]) if rows else ""
        return {"id": rid, "ok": True, "result": {"bean": bean, "job": job_id}, "error": None}


def _ctl_fail(rid: str, code: str, message: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    error = {"code": code, "message": message, "detail": dict(detail)}
    return {
        "id": rid,
        "ok": False,
        "code": code,
        "message": message,
        "detail": dict(detail),
        "result": None,
        "error": error,
    }


def _emit_host(root: Path, instance_id: str, raw: str) -> Path:
    text = raw.replace("\\", "/")
    prefix = "/factory/instance/"
    if text == "/factory/instance" or text.startswith(prefix):
        rest = "" if text == "/factory/instance" else text[len(prefix) :]
        return Path(root) / "instances" / instance_id / "main" / rest
    return Path(raw)


def instance_spec(ctx: Any, snapshot: Any, station: str, instance_id: str) -> SandboxSpec:
    station_key = snapshot.key("Station", station)
    station_spec = snapshot.spec(station_key)
    machine = snapshot.spec(snapshot.key("Machine", station_spec["machine"]))
    cfg = instance_config(machine)
    runtime = machine.get("runtime") or {}
    image = str(runtime.get("image") or "")
    host = Path(ctx.root) / "instances" / instance_id
    mounts = [
        Mount(source=str((host / "job" / "in").resolve()), target="/factory/in", readonly=True),
        Mount(source=str((host / "job" / "out").resolve()), target="/factory/out", readonly=False),
        Mount(source=str((host / "job" / "ctl").resolve()), target="/factory/ctl", readonly=False),
        Mount(source=str((host / "main").resolve()), target="/factory/instance", readonly=False),
        Mount(
            source=str((Path(ctx.root) / "tools" / "factory").resolve()),
            target="/opt/factory",
            readonly=True,
        ),
    ]
    if cfg.mode == "resident":
        state = Path(ctx.root) / "instances" / "state" / _station_name(snapshot, station_key)
        mounts.append(Mount(source=str(state.resolve()), target="/factory/state", readonly=False))
    from mao.sandbox import sandbox_resources

    resources = sandbox_resources(machine)
    form = machine.get("form")
    workspace_volume = None
    if getattr(ctx.provider, "name", "") == "docker" and form == "workspace":
        workspace_volume = f"mao-inst-{instance_id}-ws"
    else:
        mounts.append(Mount(source=str((host / "workspace").resolve()), target="/workspace", readonly=False))
    env = {
        "FACTORY_INSTANCE": instance_id,
        "FACTORY_STATION": station_key,
        "FACTORY_INSTANCE_CTL": "/factory/instance/ctl",
    }
    if cfg.mode == "resident":
        env["FACTORY_STATE"] = "/factory/state"
    network = "none"
    if station_spec.get("policy"):
        from mao.egress import SecretStore, build_egress

        plan = build_egress(snapshot, station_key, SecretStore(ctx.root))
        network = plan.network if plan.network in ("none", "egress") else "none"
        env.update(_instance_egress(ctx, instance_id, plan))
    return SandboxSpec(
        name=f"mao-inst-{instance_id}",
        image=image,
        mounts=tuple(mounts),
        workdir=str(runtime.get("workdir") or "/workspace"),
        env=env,
        network=network,
        cpus=resources["cpus"],
        memory_mb=resources["memory_mb"],
        workspace_volume=workspace_volume,
        shm_mb=resources["shm_mb"],
        tmp_mb=resources["tmp_mb"],
        pids=resources["pids"],
        writable=tuple(resources["writable"]),
        command=cfg.command,
    )


def _instance_egress(ctx: Any, instance_id: str, plan: Any) -> dict[str, str]:
    if getattr(plan, "network", "none") != "egress":
        return {}
    from secrets import token_hex

    from mao.egress import (
        SecretStore,
        credentialed_env,
        proxy_env,
        register_check_egress,
        start_owned_proxies,
    )

    token = token_hex(16)
    register_check_egress(ctx.db, f"inst-{instance_id}", plan, token=token)
    store = SecretStore(ctx.root)
    env = {str(key): str(value) for key, value in plan.env.items()}
    env.update(store.inject(plan.secret_names))
    proxies = getattr(ctx, "proxies", None)
    if proxies is None:
        start_owned_proxies(ctx, model_port=0, timeout_s=60.0)
        proxies = getattr(ctx, "proxies", None)
    if proxies is not None:
        docker = getattr(ctx.provider, "name", "") == "docker"
        env.update(credentialed_env(proxy_env(proxies, docker=docker), f"inst-{instance_id}", token))
    return env


def _digest(snapshot: Any, station: str) -> str:
    station_key = snapshot.key("Station", station)
    station_spec = snapshot.spec(station_key)
    machine = snapshot.spec(snapshot.key("Machine", station_spec["machine"]))
    policy_ref = station_spec.get("policy")
    policy = snapshot.spec(snapshot.key("Policy", policy_ref)) if policy_ref else {}
    payload = {"station": station_spec, "machine": machine, "policy": policy}
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _station_name(snapshot: Any, station: str) -> str:
    key = snapshot.key("Station", station)
    meta = snapshot.doc(key).get("metadata") or {}
    name = str(meta.get("name") or "")
    if name:
        return name
    return key.rsplit(":", 1)[-1].split("@", 1)[0]


def _remember(instance_id: str, sandbox: Any) -> None:
    _OPEN.setdefault(instance_id, sandbox)


def _parse_iso(stamp: str) -> datetime:
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    return datetime.fromisoformat(text)


def _age_s(stamp: str, now: datetime) -> float:
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    then = datetime.fromisoformat(text)
    return (now - then).total_seconds()


def _clear_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in list(path.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _copy_tree(src: Path, dest: Path, *, replace: bool) -> None:
    if replace:
        _clear_dir(dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dest / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not replace and target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _reset(value: Any) -> tuple[str, ...] | None:
    if not value:
        return ()
    if isinstance(value, str):
        if "'" in value or '"' in value:
            return None
        return tuple(part for part in value.split() if part)
    return _argv(value)
