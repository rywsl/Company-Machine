"""M2-03 验收：LineRunner（spec/system/lines.md 第 7.2 节）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.runs")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.runs import InlineExecutor, NodeState, RunState  # noqa: E402
from tests.acceptance.m2.world import start_line  # noqa: E402


def test_happy_path_inline(tmp_path: Path) -> None:
    _ctx, _snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    run = lines.start_run("note-pipeline@1", inputs)
    done = lines.run_to_completion(run.id, InlineExecutor(runner))
    assert done.state == RunState.SUCCEEDED
    assert done.outputs
    assert done.spent_usd >= 0
    assert [node.state for node in lines.nodes(done.id)] == [NodeState.DONE] * len(lines.nodes(done.id))


def test_implicit_converter_job_runs(tmp_path: Path) -> None:
    _ctx, snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    run = lines.start_run("note-pipeline@1", inputs)
    done = lines.run_to_completion(run.id, InlineExecutor(runner))
    plan = lines.plan(done.id)
    implicit = [node for node in plan.nodes if node.implicit]
    assert implicit
    stored = {node.node_id: node for node in lines.nodes(done.id)}
    assert stored[implicit[0].id].state == NodeState.DONE
    downstream = next(node for node in plan.nodes if implicit[0].id in node.deps)
    assert stored[downstream.id].outputs


def test_fail_fast(tmp_path: Path) -> None:
    _ctx, _snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-dead@1")
    run = lines.start_run("note-dead@1", inputs)
    done = lines.run_to_completion(run.id, InlineExecutor(runner))
    assert done.state == RunState.FAILED
    assert done.error["code"] == "NODE_FAILED"
    states = [node.state for node in lines.nodes(done.id)]
    assert NodeState.FAILED in states
    assert NodeState.SKIPPED in states


def test_budget_reservation(tmp_path: Path) -> None:
    _ctx, snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    plan_cap = snapshot.spec(snapshot.key("Line", "note-pipeline@1")).get("budget", {}).get("cap_usd")
    tiny = 0.000001 if plan_cap is None else min(0.000001, float(plan_cap) / 2)
    run = lines.start_run("note-pipeline@1", inputs, budget_usd=tiny)
    done = lines.run_to_completion(run.id, InlineExecutor(runner))
    assert done.state == RunState.FAILED
    assert done.error["code"] == "RUN_BUDGET_EXHAUSTED"


def test_retry_node(tmp_path: Path) -> None:
    _ctx, _snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-dead@1")
    run = lines.start_run("note-dead@1", inputs)
    failed = lines.run_to_completion(run.id, InlineExecutor(runner))
    failed_id = next(node.node_id for node in lines.nodes(failed.id) if node.state == NodeState.FAILED)
    retried = lines.retry_node(failed.id, failed_id, by="acceptance")
    assert retried.state == RunState.RUNNING


def test_cancel(tmp_path: Path) -> None:
    _ctx, _snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    run = lines.start_run("note-pipeline@1", inputs)
    lines.tick(run.id)
    cancelled = lines.cancel(run.id, by="acceptance")
    finished = lines.run_to_completion(cancelled.id, InlineExecutor(runner))
    assert finished.state == RunState.CANCELLED
    assert any(node.state == NodeState.SKIPPED for node in lines.nodes(finished.id))


def test_tick_idempotent(tmp_path: Path) -> None:
    ctx, _snapshot, _runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    run = lines.start_run("note-pipeline@1", inputs)
    lines.tick(run.id)
    before = ctx.db.query("SELECT seq, kind, data FROM events")
    second = lines.tick(run.id)
    after = ctx.db.query("SELECT seq, kind, data FROM events")
    assert second == []
    assert before == after


def test_epoch_mismatch(tmp_path: Path) -> None:
    from mao.runs import LineRunner
    from mao.snapshot import Snapshot
    from tests.acceptance.m2.world import EXAMPLE

    ctx, _snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    run = lines.start_run("note-pipeline@1", inputs)
    other = LineRunner(ctx, Snapshot.load(EXAMPLE), runner, cache=None)
    with pytest.raises(FactoryError) as ei:
        other.tick(run.id)
    assert ei.value.code == "EPOCH_MISMATCH"
