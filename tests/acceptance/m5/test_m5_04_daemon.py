"""M5-04 验收：常驻控制面（spec/system/daemon.md 第 7 章）。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mao.daemon")

from mao.errors import FactoryError  # noqa: E402
from mao.jobs import load_job  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory, seal_input  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    ROLLOUT,
    changeset_doc,
    commit_change,
    open_world,
    toolbox_v2_image,
    upsert,
)

SCHEDULER = {
    "pools": {"cpu": None, "memory_mb": None, "gpu": None},
    "compartments": [],
    "breakers": [],
    "wip": [],
    "rates": [],
    "run_cap": None,
    "toxic_failures": None,
    "tick_s": None,
}


def _config(**overrides):
    from mao.daemon import ServeConfig

    values = {
        "tick_s": 0.0,
        "max_parallel_jobs": 4,
        "audit_every_s": None,
        "canary_every_s": None,
        "docker_timeout_s": 60.0,
        "supervise": False,
    }
    values.update(overrides)
    return ServeConfig(**values)


def _scheduler_json(root: Path) -> None:
    (root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")


def _single(tmp_path: Path):
    from mao.daemon import Daemon, InlineJobExecutor

    ctx, snapshot, runner, clock = open_factory(tmp_path, FIXTURE)
    _scheduler_json(ctx.root)
    daemon = Daemon(ctx, _config(), executor=InlineJobExecutor(lambda job_id: runner), registry=FIXTURE)
    return ctx, snapshot, runner, clock, daemon


def _labelled_note(ctx, snapshot) -> str:
    bean = seal_input(ctx, snapshot, "note.text@1")
    ctx.warehouse.label(bean, "line", "note-trigger", by="acceptance")
    return bean


class _PassResult:
    def __init__(self, n: int) -> None:
        self.id = f"ct-{n:04d}"
        self.kind = "fake"
        self.subject = "fake"
        self.verdict = "pass"
        self.detail: dict = {}
        self.at = "2026-11-10T00:00:00.000Z"


class _PassContracts:
    def __init__(self) -> None:
        self.n = 0

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            self.n += 1
            return _PassResult(self.n)

        return call


class _PassShadow:
    def run(self, cs, *, candidate, active, rollout, golden_only):
        from mao.governance import ShadowReport

        return ShadowReport(ok=True, runs=1, spent_usd=0.0, regressions=[])


def _gov_daemon(w, pipeline):
    from mao.daemon import Daemon, InlineJobExecutor
    from mao.runner import Runner

    _scheduler_json(w.ctx.root)

    def runner_for(job_id: str):
        return Runner(w.ctx, w.epochs.materialize(load_job(w.ctx.db, job_id).epoch))

    return Daemon(
        w.ctx, _config(), executor=InlineJobExecutor(runner_for), epochs=w.epochs, pipeline=pipeline
    )


def _gov_pipeline(w):
    from mao.governance import ChangePipeline

    fake = _PassContracts()
    return ChangePipeline(
        w.ctx, w.epochs, contracts=lambda snapshot: fake, shadow=_PassShadow(), rebuild=lambda r, s: r
    )


def test_config_required(tmp_path: Path) -> None:
    from mao.daemon import ServeConfig

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(FactoryError) as ei:
        ServeConfig.load(root)
    assert ei.value.code == "SERVE_CONFIG_MISSING"
    (root / "factory.json").write_text(json.dumps({"serve": {"tick_s": 1.0}}), encoding="utf-8")
    with pytest.raises(FactoryError) as ei:
        ServeConfig.load(root)
    assert ei.value.code == "SERVE_CONFIG_INVALID"


def test_single_instance(tmp_path: Path) -> None:
    from mao.base import RandomIds
    from mao.context import Context
    from mao.daemon import Daemon, InlineJobExecutor
    from mao.sandbox import LocalProvider
    from tests.fakes.fakelib import local_commands

    ctx, _snapshot, runner, clock, first = _single(tmp_path)
    first.start()
    other = Context.open(
        ctx.root,
        provider=LocalProvider(ctx.root, commands=local_commands(), clock=clock),
        clock=clock,
        ids=RandomIds(),
    )
    second = Daemon(other, _config(), executor=InlineJobExecutor(lambda job_id: runner), registry=FIXTURE)
    with pytest.raises(FactoryError) as ei:
        second.start()
    assert ei.value.code == "SERVE_ALREADY_RUNNING"
    first.shutdown(timeout_s=5)
    second.start()
    second.shutdown(timeout_s=5)


def test_trigger_to_sealed(tmp_path: Path) -> None:
    from mao.runs import LineRunner

    ctx, snapshot, runner, _clock, daemon = _single(tmp_path)
    _labelled_note(ctx, snapshot)
    daemon.start()
    reports = [daemon.tick() for _ in range(6)]
    triggered = [run_id for report in reports for run_id in report.triggered]
    assert len(triggered) == 1
    dispatched = [job_id for report in reports for job_id in report.dispatched]
    assert dispatched
    assert all(load_job(ctx.db, job_id).state.value == "SEALED" for job_id in dispatched)
    run = LineRunner(ctx, snapshot, runner).get(triggered[0])
    assert run.state.value == "SUCCEEDED"
    daemon.shutdown(timeout_s=5)


def test_tick_is_deterministic(tmp_path: Path) -> None:
    def scenario(base: Path) -> list[dict]:
        ctx, snapshot, _runner, _clock, daemon = _single(base)
        _labelled_note(ctx, snapshot)
        daemon.start()
        out = [daemon.tick().to_json() for _ in range(6)]
        daemon.shutdown(timeout_s=5)
        return out

    assert scenario(tmp_path / "a") == scenario(tmp_path / "b")


def test_canary_routing(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipeline = _gov_pipeline(w)
    rollout = dict(ROLLOUT, canary={"percent": 100, "min_runs": 50})
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")], rollout=rollout),
        source,
        by="fake-agent@1",
        timeout_s=60,
    )
    assert pipeline.advance(cs.id).state == "verified"
    pipeline.add_review(cs.id, runtime="script@1", verdict="approve", note="", by="script")
    assert pipeline.advance(cs.id).state == "shadow"
    assert pipeline.advance(cs.id).state == "canary"
    daemon = _gov_daemon(w, pipeline)
    daemon.start()
    _labelled_note(w.ctx, w.snapshot)
    report = daemon.tick()
    assert len(report.triggered) == 1
    epoch = w.ctx.db.query("SELECT epoch FROM runs WHERE id = ?", (report.triggered[0],))[0]["epoch"]
    assert epoch == cs.commit
    daemon.shutdown(timeout_s=5)


def test_signal_pump(tmp_path: Path) -> None:
    from mao.foreman import ConversionStore, WakeQueue

    ctx, snapshot, runner, _clock, daemon = _single(tmp_path)
    request = ConversionStore(ctx).open_or_merge(
        from_type="note.raw@1",
        guarantees=[],
        to_type="note.text@1",
        requires=[],
        found_at={"line": "acceptance"},
    )
    note = seal_input(ctx, snapshot, "note.text@1")
    failing = runner.submit(snapshot.key("Station", "fail@1"), {"note": note}, {})
    daemon.start()
    for _ in range(4):
        daemon.tick()
    assert load_job(ctx.db, failing.id).state.value == "DEAD"
    open_wakes = WakeQueue(ctx).list(status="open")
    pairs = {(wake.signal, wake.subject) for wake in open_wakes}
    assert ("conversion", request.id) in pairs
    assert ("dead-letter", failing.id) in pairs
    again = daemon.tick()
    assert again.wakes == ()
    assert len(WakeQueue(ctx).list(status="open")) == len(open_wakes)
    dead = next(wake for wake in open_wakes if wake.signal == "dead-letter")
    WakeQueue(ctx).done(dead.id, by="acceptance")
    assert daemon.tick().wakes == ()
    assert [wake.subject for wake in WakeQueue(ctx).list(status="open") if wake.signal == "dead-letter"] == []
    daemon.shutdown(timeout_s=5)


def test_review_wake(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    w = open_world(tmp_path)
    pipeline = _gov_pipeline(w)
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    cs = pipeline.advance(cs.id)
    assert cs.state == "verified" and cs.reviews_required > 0
    daemon = _gov_daemon(w, pipeline)
    daemon.start()
    report = daemon.tick()
    wakes = [wake for wake in WakeQueue(w.ctx).list(status="open") if wake.signal == "review"]
    assert [wake.subject for wake in wakes] == [cs.id]
    assert wakes[0].payload["need"] == cs.reviews_required
    assert wakes[0].id in report.wakes
    daemon.shutdown(timeout_s=5)


def test_start_recovers(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock, daemon = _single(tmp_path)
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES ('j-0900', ?, ?, 'RUNNING', 1, '{}', '{}', '{}', ?, ?)",
            (
                snapshot.key("Station", "writer@1"),
                snapshot.epoch,
                "2026-01-01T00:00:00.000Z",
                "2026-01-01T00:00:00.000Z",
            ),
        )
    report = daemon.start()
    assert report.retried == ("j-0900",)
    daemon.shutdown(timeout_s=5)


def test_process_executor(tmp_path: Path) -> None:
    from mao.daemon import ProcessExecutor
    from tests.fakes.fakelib import local_commands

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    commands = tmp_path / "local-commands.json"
    commands.write_text(json.dumps(local_commands()), encoding="utf-8")
    note = seal_input(ctx, snapshot, "note.text@1")
    job = runner.submit(snapshot.key("Station", "upper@1"), {"note": note}, {})
    executor = ProcessExecutor(
        ctx.root,
        [
            sys.executable,
            "-m",
            "mao",
            "--root",
            str(ctx.root),
            "--registry",
            str(FIXTURE),
            "--provider",
            "local",
            "--local-commands",
            str(commands),
        ],
        max_parallel=2,
        log_dir=ctx.root / "logs" / "jobs",
    )
    executor.dispatch([job.id])
    assert executor.running() == [job.id]
    finished: list[str] = []
    deadline = time.monotonic() + 120
    while job.id not in finished and time.monotonic() < deadline:
        finished += executor.reap()
        time.sleep(0.2)
    executor.stop(timeout_s=10)
    assert job.id in finished
    assert load_job(ctx.db, job.id).state.value == "SEALED"
    assert (ctx.root / "logs" / "jobs" / f"{job.id}.log").is_file()
