"""M5-03 验收：租约、恢复与 job exec（spec/system/durable.md 第 7 章）。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("mao.durable")

from mao.errors import FactoryError  # noqa: E402
from mao.jobs import load_job  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, ROOT, open_factory, seal_input  # noqa: E402

AT = "2026-01-01T00:00:00.000Z"


def _commands_file(tmp_path: Path) -> Path:
    from tests.fakes.fakelib import local_commands

    path = tmp_path / "local-commands.json"
    path.write_text(json.dumps(local_commands()), encoding="utf-8")
    return path


def _insert_job(ctx, snapshot, job_id: str, station_ref: str, state: str = "RUNNING") -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, '{}', '{}', '{}', ?, ?)",
            (job_id, snapshot.key("Station", station_ref), snapshot.epoch, state, AT, AT),
        )


def _seal_task(ctx, snapshot, script: dict) -> str:
    from mao.warehouse import BeanDraft

    path = ctx.root / "incoming-task" / "task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(script), encoding="utf-8")
    type_id = snapshot.type_key("agent.task@1").split(":", 1)[1]
    bean = ctx.warehouse.add(BeanDraft(type=type_id, meta={}, files={"task.json": path}))
    ctx.warehouse.seal_many([(bean.id, {"steps": 1})], job_id=None, report_digest="sha256:" + "0" * 64)
    return bean.id


def _recovery(ctx, snapshot):
    from mao.durable import Recovery
    from mao.runner import Runner

    return Recovery(ctx, lambda epoch: Runner(ctx, snapshot))


def test_lease_exclusive_until_expiry(tmp_path: Path) -> None:
    from mao.durable import LEASE_TTL_S, LeaseStore

    ctx, _snapshot, _runner, clock = open_factory(tmp_path, FIXTURE)
    store = LeaseStore(ctx)
    assert store.acquire("serve", owner="A") is True
    assert store.acquire("serve", owner="B") is False
    assert store.acquire("serve", owner="A") is True
    clock.advance(LEASE_TTL_S + 1)
    assert store.valid("serve") is None
    assert store.acquire("serve", owner="B") is True
    assert store.renew("serve", owner="A") is False
    assert store.holder("serve").owner == "B"
    kinds = [row["kind"] for row in ctx.db.query("SELECT kind FROM events")]
    assert "lease.taken_over" in kinds


def test_release_only_by_owner(tmp_path: Path) -> None:
    from mao.durable import LeaseStore

    ctx, _snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    store = LeaseStore(ctx)
    assert store.acquire("job:j-0042", owner="A")
    assert store.release("job:j-0042", owner="B") is False
    assert store.holder("job:j-0042").owner == "A"
    assert store.release("job:j-0042", owner="A") is True
    assert store.holder("job:j-0042") is None


def test_runner_refuses_leased_job(tmp_path: Path) -> None:
    from mao.durable import LeaseStore

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    note = seal_input(ctx, snapshot, "note.text@1")
    job = runner.submit(snapshot.key("Station", "upper@1"), {"note": note}, {})
    assert LeaseStore(ctx).acquire(f"job:{job.id}", owner="someone-else")
    with pytest.raises(FactoryError) as ei:
        runner.run(job.id)
    assert ei.value.code == "JOB_LEASED"
    assert load_job(ctx.db, job.id).state.value == "PENDING"


def test_runner_releases_lease(tmp_path: Path) -> None:
    from mao.durable import LeaseStore

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    note = seal_input(ctx, snapshot, "note.text@1")
    job = runner.submit(snapshot.key("Station", "upper@1"), {"note": note}, {})
    done = runner.run(job.id)
    assert done.state.value == "SEALED"
    assert LeaseStore(ctx).holder(f"job:{job.id}") is None


def test_recovery_retries_or_dead(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    _insert_job(ctx, snapshot, "j-0101", "writer@1")  # retries.failed = 1
    _insert_job(ctx, snapshot, "j-0102", "write@1")  # retries.failed = 0
    report = _recovery(ctx, snapshot).run_once()
    assert report.retried == ("j-0101",)
    assert report.dead == ("j-0102",)
    retried = load_job(ctx.db, "j-0101")
    assert retried.state.value == "PENDING"
    assert retried.attempt == 2
    dead = load_job(ctx.db, "j-0102")
    assert dead.state.value == "DEAD"
    assert dead.error["code"] == "CONTROL_PLANE_RESTART"
    recovered = [
        json.loads(row["data"])
        for row in ctx.db.query("SELECT data FROM events WHERE kind = 'job.recovered'")
    ]
    assert sorted(item["job"] for item in recovered) == ["j-0101", "j-0102"]


def test_recovery_skips_live_lease(tmp_path: Path) -> None:
    from mao.durable import LeaseStore

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    _insert_job(ctx, snapshot, "j-0103", "writer@1")
    assert LeaseStore(ctx).acquire("job:j-0103", owner="worker-elsewhere")
    report = _recovery(ctx, snapshot).run_once()
    assert report.retried == () and report.dead == () and report.sealed == ()
    assert load_job(ctx.db, "j-0103").state.value == "RUNNING"


def test_recovery_reaps_sandbox(tmp_path: Path) -> None:
    from mao.base import CounterIds, FakeClock
    from mao.context import Context
    from mao.sandbox import LocalProvider
    from mao.snapshot import Snapshot
    from tests.fakes.fakelib import local_commands

    calls: list[tuple[str, int]] = []

    class RecordingProvider(LocalProvider):
        def reap(self, job_id: str, attempt: int) -> int:
            calls.append((job_id, attempt))
            return super().reap(job_id, attempt)

    root = tmp_path / "factory"
    root.mkdir()
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    ctx = Context.open(
        root,
        provider=RecordingProvider(root, commands=local_commands(), clock=clock),
        clock=clock,
        ids=CounterIds(),
    )
    snapshot = Snapshot.load(FIXTURE)
    _insert_job(ctx, snapshot, "j-0104", "write@1", state="PROVISIONING")
    _recovery(ctx, snapshot).run_once()
    assert calls == [("j-0104", 1)]


def test_job_exec_command(tmp_path: Path, capsys) -> None:
    from mao.cli import main

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    note = seal_input(ctx, snapshot, "note.text@1")
    job = runner.submit(snapshot.key("Station", "upper@1"), {"note": note}, {})
    argv = [
        "--root",
        str(ctx.root),
        "--registry",
        str(FIXTURE),
        "--provider",
        "local",
        "--local-commands",
        str(_commands_file(tmp_path)),
        "--json",
        "job",
        "exec",
        job.id,
    ]
    capsys.readouterr()
    assert main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["state"] == "SEALED"
    assert main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["state"] == "SEALED"
    assert second["attempt"] == first["attempt"]


def test_killed_process_is_recovered(tmp_path: Path) -> None:
    from mao.base import FakeClock, RandomIds
    from mao.context import Context
    from mao.sandbox import LocalProvider
    from tests.fakes.fakelib import local_commands

    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    task = _seal_task(ctx, snapshot, {"default": [{"do": "sleep", "seconds": 20}]})
    job = runner.submit(snapshot.key("Station", "writer@1"), {"task": task}, {})
    argv = [
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
        str(_commands_file(tmp_path)),
        "job",
        "exec",
        job.id,
    ]
    proc = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        state = "PENDING"
        while time.monotonic() < deadline:
            state = ctx.db.query("SELECT state FROM jobs WHERE id = ?", (job.id,))[0]["state"]
            if state == "RUNNING":
                break
            time.sleep(0.2)
        assert state == "RUNNING"
    finally:
        proc.kill()
        proc.wait(timeout=30)

    later = FakeClock(datetime.now(UTC) + timedelta(hours=1))
    ctx2 = Context.open(
        ctx.root,
        provider=LocalProvider(ctx.root, commands=local_commands(), clock=later),
        clock=later,
        ids=RandomIds(),
    )
    report = _recovery(ctx2, snapshot).run_once()
    assert job.id in report.retried + report.dead
    row = ctx2.db.query("SELECT state FROM jobs WHERE id = ?", (job.id,))[0]
    assert row["state"] in {"PENDING", "DEAD"}
    recovered = [
        json.loads(r["data"]) for r in ctx2.db.query("SELECT data FROM events WHERE kind = 'job.recovered'")
    ]
    assert any(item["job"] == job.id and item["code"] == "CONTROL_PLANE_RESTART" for item in recovered)
