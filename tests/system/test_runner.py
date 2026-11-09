"""M1-08 单元：mao.runner。模块缺失时整文件跳过。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("mao.runner")
pytest.importorskip("mao.jobs")
pytest.importorskip("mao.warehouse")

from mao.base import CounterIds, FakeClock  # noqa: E402
from mao.context import Context  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from mao.jobs import JobState, load_job  # noqa: E402
from mao.runner import Runner  # noqa: E402
from mao.sandbox import LocalProvider  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from mao.warehouse import BeanDraft  # noqa: E402
from tests.fakes.fakelib import local_commands  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "registry-m1"


def _open(tmp_path: Path):
    root = tmp_path / "factory"
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    ctx = Context.open(
        root,
        provider=LocalProvider(root, commands=local_commands(), clock=clock),
        clock=clock,
        ids=CounterIds(),
    )
    snap = Snapshot.load(FIXTURE)
    return ctx, snap, Runner(ctx, snap)


def _sealed_note(ctx, tmp_path: Path) -> str:
    src = tmp_path / "note.txt"
    src.write_text("hello", encoding="utf-8")
    bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
    ctx.warehouse.seal_many(
        [(bean.id, {"chars": 5, "lines": 1})], job_id=None, report_digest="sha256:" + "ab" * 32
    )
    return bean.id


def test_unsealed_input_is_rejected(tmp_path) -> None:
    ctx, _snap, runner = _open(tmp_path)
    try:
        src = tmp_path / "note.txt"
        src.write_text("hi", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        with pytest.raises(FactoryError) as ei:
            runner.submit("upper@1", {"note": bean.id}, {})
        assert ei.value.code == "INTAKE_REJECTED"
        assert ctx.db.query("SELECT id FROM jobs") == []
    finally:
        ctx.close()


def test_submit_inserts_pending(tmp_path) -> None:
    ctx, _snap, runner = _open(tmp_path)
    try:
        bean_id = _sealed_note(ctx, tmp_path)
        job = runner.submit("upper@1", {"note": bean_id}, {})
        assert job.state == JobState.PENDING
        stored = load_job(ctx.db, job.id)
        assert stored.state == JobState.PENDING
        assert stored.attempt == 1
    finally:
        ctx.close()


def test_run_seals_script_station(tmp_path) -> None:
    ctx, _snap, runner = _open(tmp_path)
    try:
        bean_id = _sealed_note(ctx, tmp_path)
        job = runner.submit("upper@1", {"note": bean_id}, {})
        done = runner.run(job.id)
        assert done.state == JobState.SEALED
        assert done.outputs
    finally:
        ctx.close()


def test_second_run_stays_sealed(tmp_path) -> None:
    ctx, _snap, runner = _open(tmp_path)
    try:
        bean_id = _sealed_note(ctx, tmp_path)
        job = runner.submit("upper@1", {"note": bean_id}, {})
        first = runner.run(job.id)
        second = runner.run(job.id)
        assert second.state == JobState.SEALED
        assert second.outputs == first.outputs
        assert second.attempt == first.attempt
    finally:
        ctx.close()


def test_rejected_intake_does_not_become_pending(tmp_path) -> None:
    ctx, _snap, runner = _open(tmp_path)
    try:
        before = ctx.db.query("SELECT id FROM jobs")
        with pytest.raises(FactoryError) as ei:
            runner.submit("upper@1", {}, {})
        assert ei.value.code == "INTAKE_REJECTED"
        assert ctx.db.query("SELECT id FROM jobs") == before
    finally:
        ctx.close()
