"""M4-02 验收：Plan（spec/system/dynamic.md 第 7 章）。

2026-11-13 由设计方重写（plan/review-closure.md C-06）：原测试用不是 planner 的工位、
缺 apiVersion/kind/metadata 的文档和 schema 不允许的字段，断言与 dynamic.md 5.5 相反。
夹具改用 tests/acceptance/m9/closure_world.py 的 planner@1（带 plan_bounds）与 note-plan@1。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.dynamic")
pytest.importorskip("mao.errors")

from mao.dynamic import compile_plan  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from mao.foreman import ConversionStore  # noqa: E402
from mao.jobs import JobState  # noqa: E402
from mao.runs import InlineExecutor, LineRunner, NodeState, RunState  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from tests.acceptance.m9.closure_world import (  # noqa: E402
    SHOUT,
    build_registry,
    node_map,
    open_world,
    plan_doc,
    planner_script,
    seal_note,
    seal_task,
)

DRAFT = {"draft": {"station": "write@1", "in": {"task": "$inputs.task"}}}


def _world(tmp_path: Path, script: dict | None = None):
    registry = build_registry(tmp_path)
    ctx, snapshot, runner, _clock = open_world(tmp_path / "w", registry)
    lines = LineRunner(ctx, snapshot, runner)
    task = seal_task(ctx, snapshot, script or planner_script(plan_doc(SHOUT)))
    inputs = {"task": task, "note": seal_note(ctx, snapshot, "hello\n")}
    return ctx, snapshot, runner, lines, inputs


def _check(tmp_path: Path, document: dict, *, budget_usd: float | None = None):
    ctx, snapshot, _runner, lines, inputs = _world(tmp_path)
    run = lines.start_run("note-plan@1", inputs, budget_usd=budget_usd)
    return (
        ctx,
        run,
        compile_plan(snapshot, document, planner_key=snapshot.key("Station", "planner@1"), run=run),
    )


def _codes(check) -> set[str]:
    return {item["code"] for item in check.diags}


def test_plan_feedback_on_bounds(tmp_path: Path) -> None:
    steps = {f"s{i}": {"station": "upper@1", "in": {"note": "$inputs.note"}} for i in range(5)}
    ctx, _snapshot, runner, lines, inputs = _world(tmp_path, planner_script(plan_doc(steps)))
    run = lines.start_run("note-plan@1", inputs)
    done = lines.run_to_completion(run.id, InlineExecutor(runner), max_ticks=200, wait_s=0)
    assert done.state == RunState.FAILED
    node = node_map(lines, done.id)["plan"]
    assert node.state == NodeState.FAILED
    job = runner.get(node.job_id)
    assert job.state == JobState.DEAD
    assert job.attempt == 2
    assert "PLAN_BOUNDS" in (job.feedback or "")
    assert job.outputs == {}
    assert not [name for name in node_map(lines, done.id) if name.startswith("plan/")]


def test_plan_cannot_define_specs(tmp_path: Path) -> None:
    document = plan_doc(SHOUT)
    document["kind"] = "Type"
    _ctx, _run, check = _check(tmp_path / "kind", document)
    assert check.ok is False and check.plan is None
    assert "PLAN_INVALID" in _codes(check)
    ghost = plan_doc({"g": {"station": "ghost@1", "in": {"note": "$inputs.note"}}})
    _ctx, _run, check = _check(tmp_path / "ghost", ghost)
    assert check.ok is False
    assert "PLAN_INVALID" in _codes(check)


def test_budget_number_not_template(tmp_path: Path) -> None:
    registry = build_registry(tmp_path)
    station = registry / "stations" / "planner.yaml"
    text = station.read_text(encoding="utf-8")
    assert "    budget_usd: 2.0\n" in text
    station.write_text(
        text.replace("    budget_usd: 2.0\n", '    budget_usd: "{{ run.budget.remaining_usd }}"\n'),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(FactoryError) as ei:
        Snapshot.load(registry)
    assert ei.value.code == "REGISTRY_INVALID"
    diags = ei.value.detail["diags"]
    assert any(diag["code"] == "E-SCHEMA" and "planner" in diag["subject"] for diag in diags)


def test_budget_remaining(tmp_path: Path) -> None:
    _ctx, _run, over = _check(tmp_path / "over", plan_doc(DRAFT), budget_usd=0.8)
    assert over.ok is False
    assert "PLAN_BUDGET" in _codes(over)
    _ctx, _run, equal = _check(tmp_path / "equal", plan_doc(DRAFT), budget_usd=1.0)
    assert equal.ok is True, equal.feedback


def test_missing_converter_rejects_plan(tmp_path: Path) -> None:
    document = plan_doc(
        {
            "pub": {
                "station": "publish-local@1",
                "params": {"dir": "/tmp/plan"},
                "in": {"note": "$inputs.task"},
            }
        }
    )
    ctx, run, check = _check(tmp_path, document)
    assert check.ok is False
    assert "E-NO-CONVERTER" in _codes(check)
    requests = [
        item
        for item in ConversionStore(ctx).list()
        if item.from_type.startswith("agent.task@1") and item.to_type.startswith("note.text@1")
    ]
    assert len(requests) == 1
    assert run.id not in requests[0].blocked_runs


def test_sealed_plan_same_epoch(tmp_path: Path) -> None:
    ctx, snapshot, runner, lines, inputs = _world(tmp_path)
    run = lines.start_run("note-plan@1", inputs)
    check = compile_plan(snapshot, plan_doc(SHOUT), planner_key=snapshot.key("Station", "planner@1"), run=run)
    assert check.ok is True, check.feedback
    assert check.plan is not None
    assert check.plan.epoch == snapshot.epoch
    assert [node.id for node in check.plan.nodes] == ["shout"]
    done = lines.run_to_completion(run.id, InlineExecutor(runner), max_ticks=200, wait_s=0)
    assert done.state == RunState.SUCCEEDED
    nodes = node_map(lines, done.id)
    plan_bean = runner.get(nodes["plan"].job_id).outputs["plan"][0]
    assert ctx.warehouse.get(plan_bean).sealed
    assert nodes["plan/shout"].state == NodeState.DONE
    assert runner.get(nodes["plan/shout"].job_id).epoch == done.epoch


def test_runtime_resolution_append_only(tmp_path: Path) -> None:
    ctx, snapshot, _runner, lines, inputs = _world(tmp_path)
    run = lines.start_run("note-plan@1", inputs)
    sidecar = ctx.root / "epochs" / run.epoch / "resolution-runtime.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    stale = [
        {
            "from": "note.text@1",
            "guarantees": ["non_empty"],
            "to": "note.text@1",
            "requires": ["non_empty"],
            "chain": ["note-to-text@1"],
            "origin": "runtime",
        }
    ]
    body = json.dumps(stale, sort_keys=True, separators=(",", ":"))
    sidecar.write_text(body, encoding="utf-8")
    with pytest.raises(FactoryError) as ei:
        compile_plan(snapshot, plan_doc(SHOUT), planner_key=snapshot.key("Station", "planner@1"), run=run)
    assert ei.value.code == "RUN_PLAN_INVALID"
    assert sidecar.read_text(encoding="utf-8") == body
