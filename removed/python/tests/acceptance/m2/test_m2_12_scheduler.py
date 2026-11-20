"""M2-12 验收：调度（spec/system/scheduler.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.scheduler")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.scheduler import ScheduledExecutor, Scheduler, install, is_quarantined  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402


def _sched(tmp_path: Path, config: dict | None = None):
    ctx, snapshot, runner, clock = open_factory(tmp_path, FIXTURE)
    if config is not None:
        import json

        (ctx.root / "scheduler.json").write_text(json.dumps(config), encoding="utf-8")
    sched = Scheduler(ctx, snapshot, runner)
    return ctx, snapshot, runner, clock, sched


def test_executor_methods_match_lines(tmp_path: Path) -> None:
    ctx, _snapshot, runner, _clock, sched = _sched(tmp_path, {"pools": {}})
    sched.load_config()
    executor = ScheduledExecutor(ctx, runner, sched)
    from mao.runs import LineRunner

    lines = LineRunner(ctx, _snapshot, runner)
    note = __import__("tests.acceptance.m2.world", fromlist=["seal_input"]).seal_input(
        ctx, _snapshot, "note.text@1"
    )
    run = lines.start_run("note-trigger@1", {"note": note})
    finished = lines.run_to_completion(run.id, executor)
    assert finished.id == run.id
    assert finished.state.value in {"SUCCEEDED", "FAILED", "CANCELLED"}


def test_dispatch_does_not_run_inline(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock, sched = _sched(tmp_path, {"pools": {"cpu": 1}})
    sched.load_config()
    executor = ScheduledExecutor(ctx, runner, sched)
    job = runner.submit(snapshot.key("Station", "upper@1"), {}, {})
    executor.dispatch([job.id])
    assert runner.get(job.id).state.value not in {"SEALED", "DEAD"}


def test_config_missing(tmp_path: Path) -> None:
    _ctx, _snapshot, _runner, _clock, sched = _sched(tmp_path, None)
    with pytest.raises(FactoryError) as ei:
        sched.load_config()
    assert ei.value.code == "SCHEDULER_CONFIG_MISSING"


def test_capacity_unset_does_not_default(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, _clock, sched = _sched(tmp_path, {"pools": {}})
    sched.load_config()
    with pytest.raises(FactoryError) as ei:
        sched.demand(snapshot.key("Station", "upper@1"))
    assert ei.value.code == "SCHEDULER_CAPACITY_UNSET"


def test_pool_limits_parallel_admission(tmp_path: Path) -> None:
    _ctx, snapshot, runner, _clock, sched = _sched(tmp_path, {"pools": {"cpu": 1}})
    sched.load_config()
    first = runner.submit(snapshot.key("Station", "upper@1"), {}, {})
    second = runner.submit(snapshot.key("Station", "upper@1"), {}, {})
    sched.enqueue([first.id, second.id])
    pumped = sched.pump()
    assert pumped == [min(first.id, second.id)]


def test_compartment_does_not_borrow(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, _clock, sched = _sched(
        tmp_path, {"pools": {"cpu": 1}, "compartments": {"a": {"cpu": 1}, "b": {"cpu": 1}}}
    )
    sched.load_config()
    upper = sched.demand(snapshot.key("Station", "upper@1"))
    idle = sched.demand(snapshot.key("Station", "idle@1"))
    assert upper.cpu == 1
    assert idle.cpu == 0
    assert upper.pool != idle.pool


def test_wip_backpressure(tmp_path: Path) -> None:
    _ctx, snapshot, runner, _clock, sched = _sched(
        tmp_path, {"pools": {"cpu": 4}, "wip": [{"station": "upper", "max": 0}]}
    )
    sched.load_config()
    job = runner.submit(snapshot.key("Station", "upper@1"), {}, {})
    sched.enqueue([job.id])
    assert sched.pump() == []


def test_breaker_unarmed(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock, sched = _sched(tmp_path, {"pools": {"cpu": 4}, "breakers": {}})
    sched.load_config()
    job = runner.submit(snapshot.key("Station", "upper@1"), {}, {})
    sched.enqueue([job.id])
    assert sched.pump() == [job.id]


def _terminal_job(ctx, station: str, epoch: str, job_id: str, state: str, at: str) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, '{}', '{}', '{}', ?, ?)",
            (job_id, station, epoch, state, at, at),
        )


def test_breaker_uses_configured_rate(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock, sched = _sched(tmp_path, None)
    station = snapshot.key("Station", "upper@1")
    subject = "station:" + station
    (ctx.root / "scheduler.json").write_text(
        json.dumps(
            {
                "pools": {"cpu": 4},
                "breakers": [
                    {
                        "subject": subject,
                        "min_samples": 2,
                        "failure_rate_gt": 0.5,
                        "open_for": "1h",
                        "probe_limit": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sched.load_config()
    _terminal_job(ctx, station, snapshot.epoch, "j-rate-dead", "DEAD", "2026-01-01T00:00:00.000Z")
    _terminal_job(ctx, station, snapshot.epoch, "j-rate-sealed", "SEALED", "2026-01-01T00:00:01.000Z")
    sched.tick()
    held = runner.submit(station, {}, {})
    sched.enqueue([held.id])
    assert held.id in sched.pump()
    _terminal_job(ctx, station, snapshot.epoch, "j-rate-dead-2", "DEAD", "2026-01-01T00:00:02.000Z")
    _terminal_job(ctx, station, snapshot.epoch, "j-rate-dead-3", "DEAD", "2026-01-01T00:00:03.000Z")
    sched.tick()
    opened = ctx.db.query("SELECT state FROM breaker_state WHERE subject = ?", (subject,))
    assert opened[0]["state"] == "open"
    blocked = runner.submit(station, {}, {})
    sched.enqueue([blocked.id])
    assert blocked.id not in sched.pump()


def test_half_open_probe_limit(tmp_path: Path) -> None:
    ctx, snapshot, runner, clock, sched = _sched(tmp_path, None)
    station = snapshot.key("Station", "upper@1")
    subject = "station:" + station
    (ctx.root / "scheduler.json").write_text(
        json.dumps(
            {
                "pools": {"cpu": 4},
                "breakers": [
                    {
                        "subject": subject,
                        "min_samples": 1,
                        "failure_rate_gt": 0.0,
                        "open_for": "1s",
                        "probe_limit": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sched.load_config()
    _terminal_job(ctx, station, snapshot.epoch, "j-open-sample", "DEAD", "2026-01-01T00:00:00.000Z")
    sched.tick()
    clock.advance(1)
    first = runner.submit(station, {}, {})
    second = runner.submit(station, {}, {})
    sched.enqueue([first.id, second.id])
    pumped = sched.tick()["pumped"]
    assert len(pumped) == 1
    assert set(pumped) <= {first.id, second.id}
    state = ctx.db.query("SELECT state FROM breaker_state WHERE subject = ?", (subject,))
    assert state[0]["state"] == "half_open"


def test_run_cap(tmp_path: Path) -> None:
    _ctx, _snapshot, _runner, _clock, sched = _sched(tmp_path, {"pools": {}, "run_cap": 0})
    sched.load_config()
    triggered = sched.tick()["triggered"]
    assert triggered == []


def test_cron_every(tmp_path: Path) -> None:
    _ctx, _snapshot, _runner, clock, sched = _sched(tmp_path, {"pools": {}, "cron": True})
    sched.load_config()
    first = sched.tick()["cron"]
    assert first
    second = sched.tick()["cron"]
    assert second == []
    clock.advance(60)
    third = sched.tick()["cron"]
    assert third
    assert set(third).isdisjoint(second)


def test_cron_requires_empty_interface(tmp_path: Path) -> None:
    ctx, _snapshot, _runner, _clock, sched = _sched(tmp_path, {"pools": {}})
    sched.load_config()
    assert sched.tick()["cron"] == []
    rows = ctx.db.query("SELECT kind, data FROM events")
    blob = "\n".join(str(row["kind"]) + str(row["data"]) for row in rows)
    assert "CRON_INPUT_REQUIRED" in blob


def test_dlq_not_redispatched(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock, sched = _sched(tmp_path, {"pools": {"cpu": 1}})
    sched.load_config()
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES ('j-dead', ?, ?, 'DEAD', 1, '{}', '{}', '{}', ?, ?)",
            (
                snapshot.key("Station", "upper@1"),
                snapshot.epoch,
                "2026-01-01T00:00:00.000Z",
                "2026-01-01T00:00:00.000Z",
            ),
        )
    dead = sched.list_dead()
    assert [item.job_id for item in dead] == ["j-dead"]
    before = runner.get("j-dead").attempt
    sched.enqueue(["j-dead"])
    assert sched.pump() == []
    assert runner.get("j-dead").attempt == before


def test_quarantine_visible_to_cache(tmp_path: Path) -> None:
    _ctx, _snapshot, _runner, _clock, sched = _sched(tmp_path, {"pools": {}})
    assert is_quarantined("sha256:" + "b" * 64) is False
    install(sched)
    sched.quarantine("sha256:" + "b" * 64, by="acceptance", reason="toxic")
    assert is_quarantined("sha256:" + "b" * 64) is True
    assert sched.is_quarantined("sha256:" + "b" * 64) is True


def test_toxic_unarmed(tmp_path: Path) -> None:
    _ctx, _snapshot, _runner, _clock, sched = _sched(tmp_path, {"pools": {}, "toxic_failures": None})
    sched.load_config()
    install(sched)
    assert is_quarantined("sha256:" + "c" * 64) is False


def test_memory_unit(tmp_path: Path) -> None:
    from mao.scheduler import Demand

    demand = Demand(cpu=0, memory_mb=8192, gpu=0, pool="*")
    assert demand.memory_mb == 8192
    _ctx, snapshot, _runner, _clock, sched = _sched(tmp_path, {"pools": {"memory": 8192}})
    sched.load_config()
    parsed = sched.demand(snapshot.key("Station", "upper@1"))
    assert parsed.memory_mb == 8192
