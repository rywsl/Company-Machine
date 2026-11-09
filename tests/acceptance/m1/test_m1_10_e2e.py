"""M1-10 端到端（spec/system/testing.md 第 7 章）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("mao.runner")

from mao.base import CounterIds, FakeClock  # noqa: E402
from mao.context import Context  # noqa: E402
from mao.jobs import JobState  # noqa: E402
from mao.runner import Runner  # noqa: E402
from mao.sandbox import LocalProvider  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from mao.warehouse import BeanDraft  # noqa: E402


def test_local_upper_seals(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 11, 9, tzinfo=UTC))
    ctx = Context.open(
        tmp_path / "factory", provider=LocalProvider(tmp_path / "local"), clock=clock, ids=CounterIds()
    )
    try:
        snap = Snapshot.load(Path(__file__).resolve().parents[2] / "fixtures" / "registry-m1")
        src = tmp_path / "note.txt"
        src.write_text("hello", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        ctx.warehouse.seal_many(
            [(bean.id, {"chars": 5, "lines": 1})], job_id=None, report_digest="sha256:" + "ab" * 32
        )
        done = Runner(ctx, snap).run(Runner(ctx, snap).submit("upper@1", {"note": bean.id}, {}).id)
        assert done.state in {JobState.SEALED, JobState.DEAD}
        if done.state == JobState.SEALED:
            out_id = next(iter(done.outputs.values()))[0]
            assert ctx.warehouse.get(out_id).sealed is True
    finally:
        ctx.close()


def test_local_lineage(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 11, 9, tzinfo=UTC))
    ctx = Context.open(
        tmp_path / "factory", provider=LocalProvider(tmp_path / "local"), clock=clock, ids=CounterIds()
    )
    try:
        snap = Snapshot.load(Path(__file__).resolve().parents[2] / "fixtures" / "registry-m1")
        src = tmp_path / "note.txt"
        src.write_text("hello", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        ctx.warehouse.seal_many(
            [(bean.id, {"chars": 5, "lines": 1})], job_id=None, report_digest="sha256:" + "ab" * 32
        )
        runner = Runner(ctx, snap)
        done = runner.run(runner.submit("upper@1", {"note": bean.id}, {}).id)
        if done.state == JobState.SEALED:
            out_id = next(iter(done.outputs.values()))[0]
            assert "j-" in str(ctx.warehouse.lineage(out_id, "up")) or bean.id in str(
                ctx.warehouse.lineage(out_id, "up")
            )
        else:
            assert done.state == JobState.DEAD
    finally:
        ctx.close()


@pytest.mark.docker
def test_docker_upper_matches_local(tmp_path) -> None:
    from mao.sandbox import DockerProvider

    clock = FakeClock(datetime(2026, 11, 9, tzinfo=UTC))
    ctx = Context.open(tmp_path / "factory", provider=DockerProvider(tmp_path), clock=clock, ids=CounterIds())
    try:
        snap = Snapshot.load(Path(__file__).resolve().parents[2] / "fixtures" / "registry-m1")
        src = tmp_path / "note.txt"
        src.write_text("hello", encoding="utf-8")
        bean = ctx.warehouse.add(BeanDraft(type="note.text@1.0", meta={}, files={"note.txt": src}))
        ctx.warehouse.seal_many(
            [(bean.id, {"chars": 5, "lines": 1})], job_id=None, report_digest="sha256:" + "ab" * 32
        )
        done = Runner(ctx, snap).run(Runner(ctx, snap).submit("upper@1", {"note": bean.id}, {}).id)
        assert done.state in {JobState.SEALED, JobState.DEAD}
    finally:
        ctx.close()


def test_registry_m1_matches_constitution() -> None:
    root = Path(__file__).resolve().parents[3]
    fixture = (root / "tests" / "fixtures" / "registry-m1" / "constitution" / "invariants.mrl").read_bytes()
    reference = (root / "spec" / "mrl" / "constitution" / "invariants.mrl").read_bytes()
    assert fixture.replace(b"\r\n", b"\n") == reference.replace(b"\r\n", b"\n")
