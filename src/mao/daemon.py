"""常驻控制面 factory serve。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.durable import LeaseKeeper, LeaseStore, Recovery
from mao.errors import FactoryError
from mao.jobs import load_job
from mao.runner import Runner, open_runner
from mao.runs import RUN_TERMINAL, LineRunner, TriggerEngine
from mao.scheduler import Scheduler

_SERVE_FIELDS = (
    "tick_s",
    "max_parallel_jobs",
    "audit_every_s",
    "canary_every_s",
    "docker_timeout_s",
    "supervise",
)


@dataclass(frozen=True)
class ServeConfig:
    tick_s: float
    max_parallel_jobs: int
    audit_every_s: float | None
    canary_every_s: float | None
    docker_timeout_s: float
    supervise: bool
    model_port: int = 8080
    gc_every_s: float | None = None
    forwarder_image: str | None = None

    @classmethod
    def load(cls, root: Path) -> ServeConfig:
        path = Path(root) / "factory.json"
        if not path.is_file():
            raise FactoryError("SERVE_CONFIG_MISSING", "缺少 factory.json", path=str(path))
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FactoryError("SERVE_CONFIG_MISSING", "factory.json 无法解析", path=str(path)) from exc
        serve = body.get("serve") if isinstance(body, dict) else None
        if not isinstance(serve, dict):
            raise FactoryError("SERVE_CONFIG_MISSING", "缺少 serve 段", path=str(path))
        values: dict[str, Any] = {}
        for name in _SERVE_FIELDS:
            if name not in serve:
                raise FactoryError("SERVE_CONFIG_INVALID", f"缺少 serve.{name}", field=name)
            values[name] = _serve_value(name, serve[name])
        model_port = 8080
        if "model_port" in serve:
            model_port = _serve_value("model_port", serve["model_port"])
        gc_every_s = None
        if "gc_every_s" in serve:
            gc_every_s = _serve_value("gc_every_s", serve["gc_every_s"])
        forwarder_image = None
        if "forwarder_image" in serve:
            forwarder_image = _serve_value("forwarder_image", serve["forwarder_image"])
        return cls(
            **values,
            model_port=model_port,
            gc_every_s=gc_every_s,
            forwarder_image=forwarder_image,
        )


@dataclass(frozen=True)
class TickReport:
    tick: int
    at: str
    epochs: tuple[str, ...]
    recovered: tuple[str, ...]
    triggered: tuple[str, ...]
    advanced: tuple[str, ...]
    dispatched: tuple[str, ...]
    finished: tuple[str, ...]
    governance: tuple[str, ...]
    audited: bool
    canaries: tuple[str, ...]
    wakes: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "at": self.at,
            "epochs": list(self.epochs),
            "recovered": list(self.recovered),
            "triggered": list(self.triggered),
            "advanced": list(self.advanced),
            "dispatched": list(self.dispatched),
            "finished": list(self.finished),
            "governance": list(self.governance),
            "audited": self.audited,
            "canaries": list(self.canaries),
            "wakes": list(self.wakes),
        }


class InlineJobExecutor:
    def __init__(self, runner_for: Callable[[str], Runner]) -> None:
        self.runner_for = runner_for
        self._pending_reap: list[str] = []

    def dispatch(self, job_ids: Sequence[str]) -> None:
        for job_id in sorted(job_ids):
            try:
                self.runner_for(job_id).run(job_id)
            except FactoryError as exc:
                if exc.code == "JOB_LEASED":
                    continue
                raise
            self._pending_reap.append(job_id)

    def running(self) -> list[str]:
        return []

    def reap(self) -> list[str]:
        found = sorted(self._pending_reap)
        self._pending_reap = []
        return found

    def stop(self, timeout_s: float) -> None:
        del timeout_s


class ProcessExecutor:
    def __init__(self, root: Path, argv_prefix: Sequence[str], *, max_parallel: int, log_dir: Path) -> None:
        del root
        self.argv_prefix = list(argv_prefix)
        self.max_parallel = max_parallel
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._logs: dict[str, Any] = {}

    def dispatch(self, job_ids: Sequence[str]) -> None:
        for job_id in job_ids:
            if job_id in self._procs and self._procs[job_id].poll() is None:
                continue
            if len(self.running()) >= self.max_parallel:
                break
            log_path = self.log_dir / f"{job_id}.log"
            handle = log_path.open("ab")
            proc = subprocess.Popen(
                [*self.argv_prefix, "job", "exec", job_id],
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            self._procs[job_id] = proc
            self._logs[job_id] = handle

    def running(self) -> list[str]:
        return sorted(job_id for job_id, proc in self._procs.items() if proc.poll() is None)

    def reap(self) -> list[str]:
        done = sorted(job_id for job_id, proc in self._procs.items() if proc.poll() is not None)
        for job_id in done:
            handle = self._logs.pop(job_id, None)
            if handle is not None:
                handle.close()
            self._procs.pop(job_id, None)
        return done

    def stop(self, timeout_s: float) -> None:
        for proc in self._procs.values():
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in self._logs.values():
            handle.close()
        self._procs.clear()
        self._logs.clear()


class Daemon:
    def __init__(
        self,
        ctx: Any,
        config: ServeConfig,
        *,
        executor: Any | None = None,
        registry: Path | None = None,
        epochs: Any | None = None,
        pipeline: Any | None = None,
    ) -> None:
        from mao.commands.common import governance_registry

        self.ctx = ctx
        self.config = config
        self.registry = None if registry is None else Path(registry)
        governed = governance_registry(ctx) is not None
        if epochs is None and governed:
            from mao.governance.epochs import EpochStore

            epochs = EpochStore(ctx)
        if pipeline is None and epochs is not None:
            from mao.governance.changesets import ChangePipeline

            pipeline = ChangePipeline(ctx, epochs)
        if epochs is None and self.registry is None:
            raise FactoryError("SERVE_CONFIG_INVALID", "单快照模式需要 registry", field="registry")
        self.epochs = epochs
        self.pipeline = pipeline
        self.executor = executor if executor is not None else _default_executor(ctx, config)
        self._cache: dict[str, _Bundle] = {}
        self._started = False
        self._keeper: LeaseKeeper | None = None
        self._ticks = 0
        self._last_audit: str | None = None
        self._last_canary: str | None = None
        self._last_gc: str | None = None
        self._started_at = ""
        self._egress: Any = None
        self._model: Any = None

    def start(self) -> Any:
        store = LeaseStore(self.ctx)
        if not store.acquire("serve", owner=self.ctx.owner):
            holder = store.holder("serve")
            raise FactoryError(
                "SERVE_ALREADY_RUNNING",
                "serve 已在运行",
                owner="" if holder is None else holder.owner,
                expires_at="" if holder is None else holder.expires_at,
            )
        self._keeper = LeaseKeeper(store, "serve", owner=self.ctx.owner)
        self._keeper.start()
        self._started_at = utc_iso(self.ctx.clock.now())
        from mao.egress import start_owned_proxies

        self._egress, self._model = start_owned_proxies(
            self.ctx, model_port=self.config.model_port, timeout_s=self.config.docker_timeout_s
        )
        self._heartbeat(())
        report = Recovery(self.ctx, self._runner_for_epoch).run_once()
        _event(self.ctx, "serve.started", {"owner": self.ctx.owner})
        self._started = True
        return report

    def tick(self) -> TickReport:
        if not self._started:
            raise FactoryError("SERVE_NOT_STARTED", "serve 尚未 start")
        if self._keeper is not None and self._keeper.lost.is_set():
            raise FactoryError("LEASE_LOST", "serve 租约丢失", resource="serve", owner=self.ctx.owner)
        self._ticks += 1
        at = utc_iso(self.ctx.clock.now())
        recovered_report = Recovery(self.ctx, self._runner_for_epoch).run_once()
        recovered = tuple(recovered_report.retried + recovered_report.dead + recovered_report.sealed)
        commits = self._active_epochs()
        triggered: list[str] = []
        if commits:
            active = self._bundle(commits[0])
            active.scheduler.tick(pump=False, route=self.route)
            triggered = list(active.triggers.process(route=self.route))
        advanced: list[str] = []
        for commit in commits:
            lines = self._bundle(commit).lines
            for run in lines.list():
                if run.state in RUN_TERMINAL or run.epoch != commit:
                    continue
                lines.tick(run.id)
                advanced.append(run.id)
        dispatched: list[str] = []
        capacity = self.config.max_parallel_jobs - len(self.executor.running())
        for commit in commits:
            if capacity <= 0:
                break
            admitted = self._bundle(commit).scheduler.admit(running=self.executor.running())
            take = admitted[:capacity]
            if take:
                self.executor.dispatch(take)
                dispatched.extend(take)
                capacity -= len(take)
        finished = list(self.executor.reap())
        governance: list[str] = []
        if self.pipeline is not None:
            governance = [item.id for item in self.pipeline.tick()]
        audited = False
        if (
            self.config.audit_every_s is not None
            and self.epochs is not None
            and _due(self.ctx, self._last_audit, self.config.audit_every_s, first=True)
        ):
            from mao.reconcile import Reconciler

            Reconciler(self.ctx, self.epochs).audit_once(timeout_s=self.config.docker_timeout_s)
            self._last_audit = at
            audited = True
        canaries = self._canaries(commits, at)
        wakes = self._pump()
        self._maybe_gc(at)
        self._heartbeat(commits)
        report = TickReport(
            self._ticks,
            at,
            tuple(commits),
            recovered,
            tuple(triggered),
            tuple(advanced),
            tuple(dispatched),
            tuple(finished),
            tuple(governance),
            audited,
            tuple(canaries),
            tuple(wakes),
        )
        _event(
            self.ctx,
            "serve.tick",
            {
                key: (len(value) if isinstance(value, list) else value)
                for key, value in report.to_json().items()
            },
        )
        return report

    def serve(self, stop: threading.Event) -> None:
        self.start()
        try:
            while not stop.is_set():
                try:
                    self.tick()
                except FactoryError as exc:
                    if exc.code == "LEASE_LOST":
                        break
                    _event(self.ctx, "serve.tick_failed", {"tick": self._ticks, "code": exc.code})
                    logging.getLogger("mao").error("serve tick 失败 code=%s", exc.code)
                except OSError:
                    _event(self.ctx, "serve.tick_failed", {"tick": self._ticks, "code": "OS_ERROR"})
                    logging.getLogger("mao").exception("serve tick 失败")
                stop.wait(self.config.tick_s)
        finally:
            self.shutdown(timeout_s=60)

    def shutdown(self, timeout_s: float) -> None:
        self.executor.stop(timeout_s)
        for proxy in (self._model, self._egress):
            if proxy is not None:
                proxy.close()
        self._model = None
        self._egress = None
        from mao.egress import stop_owned_forwarder

        stop_owned_forwarder(self.ctx)
        if self._keeper is not None:
            self._keeper.stop()
            self._keeper = None
        LeaseStore(self.ctx).release("serve", owner=self.ctx.owner)
        if self._started:
            _event(self.ctx, "serve.stopped", {"owner": self.ctx.owner, "reason": "shutdown"})
        self._started = False

    def route(self, key: str) -> LineRunner:
        if self.epochs is None:
            return self._bundle(self._single_epoch()).lines
        from mao.governance.rollout import EpochRouter

        commit = EpochRouter(self.ctx, self.epochs).choose(key)
        return self._bundle(commit).lines

    def _runner_for_epoch(self, epoch: str) -> Runner:
        return self._bundle(epoch).runner

    def _single_epoch(self) -> str:
        return self._bundle("__single__").snapshot.epoch

    def _active_epochs(self) -> list[str]:
        if self.epochs is None:
            return [self._single_epoch()]
        active = self.epochs.current()
        candidate = self.epochs.pointers().get("candidate")
        found = {active}
        if candidate:
            found.add(candidate)
        for run_row in self.ctx.db.query("SELECT epoch, state FROM runs"):
            if run_row["state"] not in {item.value for item in RUN_TERMINAL}:
                found.add(str(run_row["epoch"]))
        for job_row in self.ctx.db.query("SELECT epoch, state FROM jobs"):
            if job_row["state"] not in {"SEALED", "DEAD"}:
                found.add(str(job_row["epoch"]))
        ordered = [active]
        if candidate and candidate != active:
            ordered.append(candidate)
        ordered.extend(sorted(found - set(ordered)))
        return ordered

    def _bundle(self, commit: str) -> _Bundle:
        cached = self._cache.get(commit)
        if cached is not None:
            return cached
        if self.epochs is None:
            from mao.snapshot import Snapshot

            snapshot = Snapshot.load(self.registry)
            key = snapshot.epoch
        else:
            snapshot = self.epochs.materialize(commit)
            key = commit
        if key in self._cache and self.epochs is None:
            return self._cache[key]
        runner = open_runner(self.ctx, snapshot)
        lines = LineRunner(self.ctx, snapshot, runner)
        scheduler = Scheduler(self.ctx, snapshot, runner)
        triggers = TriggerEngine(self.ctx, snapshot, lines)
        bundle = _Bundle(snapshot, runner, lines, scheduler, triggers)
        self._cache[key] = bundle
        if commit != key:
            self._cache[commit] = bundle
        return bundle

    def _canaries(self, commits: Sequence[str], at: str) -> list[str]:
        if self.config.canary_every_s is None or not commits:
            return []
        if not _due(self.ctx, self._last_canary, self.config.canary_every_s, first=False):
            return []
        from mao.contract import ContractRunner
        from mao.foreman import WakeQueue

        bundle = self._bundle(commits[0])
        stations = _canary_stations(bundle.snapshot)
        runner = ContractRunner(self.ctx, bundle.snapshot, bundle.runner)
        found: list[str] = []
        for station in stations:
            result = runner.canary(station)
            found.append(result.id)
            verdict = result.verdict.value if hasattr(result.verdict, "value") else result.verdict
            if verdict != "pass":
                _event(self.ctx, "canary.failed", {"station": station, "result": result.id})
                WakeQueue(self.ctx).file("drift", f"canary:{station}", {"result": result.id})
        self._last_canary = at
        return found

    def _reopen_failed_conversions(self) -> None:
        if self.pipeline is None:
            return
        from mao.foreman import ConversionStore

        store = ConversionStore(self.ctx)
        for request in store.list(status="in_progress"):
            if not request.changeset_id:
                continue
            try:
                changeset = self.pipeline.get(request.changeset_id)
            except FactoryError:
                continue
            if changeset.state in {"rejected", "withdrawn", "rolled-back"}:
                store.reopen(request.id)

    def _pump(self) -> list[str]:
        from mao.foreman import ConversionStore, WakeQueue

        self._reopen_failed_conversions()
        queue = WakeQueue(self.ctx)
        before = {wake.id for wake in queue.list(status="open")}
        now = utc_iso(self.ctx.clock.now())
        for request in ConversionStore(self.ctx).list(status="open"):
            since = getattr(request, "updated_at", None) or request.at
            queue.file("conversion", request.id, {"id": request.id}, since=since)
        if self._cache:
            scheduler = next(iter(self._cache.values())).scheduler
            for letter in scheduler.list_dead():
                job = load_job(self.ctx.db, letter.job_id)
                queue.file(
                    "dead-letter",
                    letter.job_id,
                    {"id": letter.job_id, "code": letter.error_code},
                    since=job.updated_at,
                )
        if self.pipeline is not None:
            for changeset in self.pipeline.list(state="verified"):
                have = len(self.pipeline.approvals(changeset.id))
                if have >= changeset.reviews_required:
                    continue
                queue.file(
                    "review",
                    changeset.id,
                    {"id": changeset.id, "need": changeset.reviews_required - have},
                    since=changeset.stage_started_at,
                )
            mode = self.pipeline.safe_mode()
            if mode.get("on"):
                queue.file("other", "safe-mode", {"level": mode.get("level")}, since=mode.get("since"))
        fresh = [wake.id for wake in queue.list(status="open") if wake.id not in before and wake.at == now]
        return fresh

    def _maybe_gc(self, at: str) -> None:
        if self.config.gc_every_s is None:
            return
        from mao.retention import RetentionConfig, collect

        try:
            config = RetentionConfig.load(self.ctx.root)
        except FactoryError as exc:
            if exc.code == "GC_NOT_CONFIGURED":
                return
            raise
        if not _due(self.ctx, self._last_gc, self.config.gc_every_s, first=True):
            return
        try:
            collect(self.ctx, config, by="serve")
        except FactoryError as exc:
            if exc.code != "GC_BUSY":
                raise
            import logging

            logging.getLogger("mao").error("回收忙，跳过本轮 owner=%s", exc.detail.get("owner"))
            return
        self._last_gc = at

    def _heartbeat(self, commits: Sequence[str]) -> None:
        body = {
            "owner": self.ctx.owner,
            "started_at": self._started_at,
            "tick": self._ticks,
            "last_tick_at": utc_iso(self.ctx.clock.now()),
            "epochs": list(commits),
            "running": list(self.executor.running()),
        }
        proxies = getattr(self.ctx, "proxies", None)
        if proxies is not None:
            recorded = {
                "host": proxies.host,
                "egress_port": proxies.egress_port,
                "model_port": proxies.model_port,
            }
            if getattr(proxies, "alias", None):
                recorded["alias"] = proxies.alias
            body["proxies"] = recorded
        target = Path(self.ctx.root) / "serve.json"
        part = Path(self.ctx.root) / "tmp" / "serve.json.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_text(canonical_json(body), encoding="utf-8")
        os.replace(part, target)


@dataclass
class _Bundle:
    snapshot: Any
    runner: Runner
    lines: LineRunner
    scheduler: Scheduler
    triggers: TriggerEngine


def _serve_value(name: str, value: Any) -> Any:
    if name == "model_port":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FactoryError("SERVE_CONFIG_INVALID", f"serve.{name} 类型不对", field=name)
        return value
    if name == "supervise":
        if not isinstance(value, bool):
            raise FactoryError("SERVE_CONFIG_INVALID", f"serve.{name} 类型不对", field=name)
        return value
    if name == "forwarder_image":
        if not isinstance(value, str) or "@sha256:" not in value:
            raise FactoryError("SERVE_CONFIG_INVALID", "forwarder_image 必须带 digest", field=name)
        return value
    if name == "gc_every_s":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise FactoryError("SERVE_CONFIG_INVALID", f"serve.{name} 类型不对", field=name)
        return float(value)
    if name in {"audit_every_s", "canary_every_s"}:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FactoryError("SERVE_CONFIG_INVALID", f"serve.{name} 类型不对", field=name)
        return float(value)
    if name == "max_parallel_jobs":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise FactoryError("SERVE_CONFIG_INVALID", f"serve.{name} 类型不对", field=name)
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FactoryError("SERVE_CONFIG_INVALID", f"serve.{name} 类型不对", field=name)
    return float(value)


def _due(ctx: Any, last: str | None, every_s: float, *, first: bool) -> bool:
    if last is None:
        return first
    from datetime import datetime

    previous = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return (ctx.clock.now() - previous).total_seconds() >= every_s


def _canary_stations(snapshot: Any) -> list[str]:
    stations: set[str] = set()
    for key in snapshot.keys("GoldenSet"):
        spec = snapshot.spec(key)
        station = spec.get("station")
        if isinstance(station, str):
            stations.add(snapshot.key("Station", station))
    return sorted(stations)


def _event(ctx: Any, kind: str, data: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
            (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(data)),
        )


def run_serve(
    ctx: Any,
    config: ServeConfig,
    *,
    executor: Any,
    stop: threading.Event,
    registry: Path | None = None,
    epochs: Any | None = None,
    pipeline: Any | None = None,
) -> None:
    daemon = Daemon(ctx, config, executor=executor, registry=registry, epochs=epochs, pipeline=pipeline)
    thread: threading.Thread | None = None
    if config.supervise:
        from mao.supervisor import ForemanSupervisor, SupervisorConfig

        sup = ForemanSupervisor(
            ctx,
            daemon.epochs,
            SupervisorConfig.load(ctx.root),
            pipeline=daemon.pipeline,
        )
        thread = threading.Thread(target=sup.serve, args=(stop,), name="mao-supervisor", daemon=True)
        thread.start()
    try:
        daemon.serve(stop)
    finally:
        stop.set()
        if thread is not None:
            from mao.supervisor import SupervisorConfig

            timeout = float(SupervisorConfig.load(ctx.root).session_timeout_s) + 60
            thread.join(timeout)


def _default_executor(ctx: Any, config: ServeConfig) -> ProcessExecutor:
    import sys

    argv = [sys.executable, "-m", "mao", "--root", str(ctx.root), "--provider", ctx.provider.name]
    return ProcessExecutor(
        ctx.root,
        argv,
        max_parallel=config.max_parallel_jobs,
        log_dir=Path(ctx.root) / "logs" / "jobs",
    )
