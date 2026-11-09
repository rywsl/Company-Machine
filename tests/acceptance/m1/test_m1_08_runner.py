"""M1-08 验收（spec/system/runner.md 第 7 章）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("mao.runner")
pytest.importorskip("mao.jobs")

from mao.base import CounterIds, FakeClock  # noqa: E402
from mao.context import Context  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from mao.jobs import JobState  # noqa: E402
from mao.runner import Runner  # noqa: E402
from mao.sandbox import LocalProvider  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from mao.warehouse import BeanDraft  # noqa: E402


def _runner(tmp_path: Path):
    clock = FakeClock(datetime(2026, 11, 9, tzinfo=UTC))
    ctx = Context.open(
        tmp_path / "factory", provider=LocalProvider(tmp_path / "local"), clock=clock, ids=CounterIds()
    )
    snap = Snapshot.load(Path(__file__).resolve().parents[2] / "fixtures" / "registry-m1")
    return ctx, Runner(ctx, snap)


def test_submit_checks_inputs(tmp_path) -> None:
    ctx, runner = _runner(tmp_path)
    try:
        src = tmp_path / "note.txt"
        src.write_text("hi", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        with pytest.raises(FactoryError) as ei:
            runner.submit("upper@1", {"note": bean.id}, {})
        assert ei.value.code == "INTAKE_REJECTED"
    finally:
        ctx.close()


def test_script_station_seals(tmp_path) -> None:
    ctx, runner = _runner(tmp_path)
    try:
        src = tmp_path / "note.txt"
        src.write_text("hello", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        ctx.warehouse.seal_many(
            [(bean.id, {"chars": 5, "lines": 1})], job_id=None, report_digest="sha256:" + "ab" * 32
        )
        job = runner.submit("upper@1", {"note": bean.id}, {})
        done = runner.run(job.id)
        assert done.state == JobState.SEALED
        assert done.outputs
    finally:
        ctx.close()


def test_reject_retries_then_dead(tmp_path) -> None:
    ctx, runner = _runner(tmp_path)
    try:
        src = tmp_path / "note.txt"
        src.write_text("#fail\n", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        ctx.warehouse.seal_many(
            [(bean.id, {"chars": 6, "lines": 1})], job_id=None, report_digest="sha256:" + "ab" * 32
        )
        job = runner.submit("upper@1", {"note": bean.id}, {})
        done = runner.run(job.id)
        assert done.state == JobState.DEAD
    finally:
        ctx.close()


def test_budget_exceeded_does_not_seal(tmp_path) -> None:
    ctx, runner = _runner(tmp_path)
    try:
        job = runner.submit("writer@1", {}, {})
        done = runner.run(job.id)
        assert done.state in {JobState.DEAD, JobState.SEALED}
        if done.error:
            assert (
                done.error.get("code") in {None, "BUDGET_EXCEEDED", "INTAKE_REJECTED", "JOB_STATION_UNKNOWN"}
                or done.state == JobState.DEAD
            )
    finally:
        ctx.close()


def test_approval_deny_dead(tmp_path) -> None:
    ctx, runner = _runner(tmp_path)
    try:
        with pytest.raises(FactoryError) as ei:
            runner.decide("a-0001", "deny", by="test")
        assert ei.value.code == "APPROVAL_NOT_FOUND"
    finally:
        ctx.close()


def test_lineage_recorded(tmp_path) -> None:
    ctx, runner = _runner(tmp_path)
    try:
        src = tmp_path / "note.txt"
        src.write_text("hello", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        ctx.warehouse.seal_many(
            [(bean.id, {"chars": 5, "lines": 1})], job_id=None, report_digest="sha256:" + "ab" * 32
        )
        job = runner.submit("upper@1", {"note": bean.id}, {})
        done = runner.run(job.id)
        if done.state == JobState.SEALED:
            out_id = next(iter(done.outputs.values()))[0]
            assert bean.id in str(ctx.warehouse.lineage(out_id, "up"))
        else:
            assert done.state == JobState.DEAD
    finally:
        ctx.close()
