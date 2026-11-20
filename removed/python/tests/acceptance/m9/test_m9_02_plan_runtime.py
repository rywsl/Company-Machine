"""M9-02 验收：planner 工位产出的 Plan 在运行期编译、挂接、执行（spec/system/closure.md 5.2）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.dynamic")

from mao.dynamic import compile_plan  # noqa: E402
from mao.jobs import JobState  # noqa: E402
from mao.runs import InlineExecutor, LineRunner, NodeState, RunState  # noqa: E402
from tests.acceptance.m9.closure_world import (  # noqa: E402
    SHOUT,
    bean_text,
    build_registry,
    node_map,
    open_world,
    plan_doc,
    planner_script,
    seal_note,
    seal_task,
)


def _world(tmp_path: Path):
    registry = build_registry(tmp_path)
    ctx, snapshot, runner, _clock = open_world(tmp_path / "w", registry)
    return ctx, snapshot, runner, LineRunner(ctx, snapshot, runner)


def _run_plan(tmp_path: Path, *docs: dict, text: str = "hello\n"):
    ctx, snapshot, runner, lines = _world(tmp_path)
    inputs = {"task": seal_task(ctx, snapshot, planner_script(*docs)), "note": seal_note(ctx, snapshot, text)}
    run = lines.start_run("note-plan@1", inputs)
    done = lines.run_to_completion(run.id, InlineExecutor(runner), max_ticks=200, wait_s=0)
    return ctx, snapshot, runner, lines, done


def _events(ctx, kind: str) -> list[dict]:
    rows = ctx.db.query("SELECT data FROM events WHERE kind = ? ORDER BY seq", (kind,))
    return [json.loads(row["data"]) for row in rows]


def test_plan_child_steps_run(tmp_path: Path) -> None:
    ctx, snapshot, runner, lines, done = _run_plan(tmp_path, plan_doc(SHOUT, keep=["shout.note"]))
    assert done.state == RunState.SUCCEEDED
    nodes = node_map(lines, done.id)
    assert nodes["plan"].state == NodeState.DONE
    child = nodes["plan/shout"]
    assert child.state == NodeState.DONE
    job = runner.get(child.job_id)
    assert job.epoch == done.epoch == snapshot.epoch
    assert bean_text(ctx, child.outputs["note"][0]) == "HELLO\n"
    plan_bean = runner.get(nodes["plan"].job_id).outputs["plan"][0]
    bean = ctx.warehouse.get(plan_bean)
    assert bean.sealed and bean.type.startswith("plan.doc@1")
    recorded = [item for item in _events(ctx, "run.plan") if item.get("plan") == plan_bean]
    assert len(recorded) == 1


def test_rejected_plan_retried(tmp_path: Path) -> None:
    bad = plan_doc({"q": {"station": "quote@1", "in": {"note": "$inputs.note"}}})
    _ctx, _snapshot, runner, lines, done = _run_plan(tmp_path, bad, plan_doc(SHOUT))
    assert done.state == RunState.SUCCEEDED
    nodes = node_map(lines, done.id)
    planner = runner.get(nodes["plan"].job_id)
    assert planner.state == JobState.SEALED
    assert planner.attempt == 2
    assert "plan/shout" in nodes
    assert not [name for name in nodes if name.startswith("plan/q")]


def test_child_failure_fails_parent(tmp_path: Path) -> None:
    _ctx, _snapshot, _runner, lines, done = _run_plan(tmp_path, plan_doc(SHOUT), text="#fail\n")
    assert done.state == RunState.FAILED
    nodes = node_map(lines, done.id)
    assert nodes["plan"].state == NodeState.DONE
    assert nodes["plan/shout"].state == NodeState.FAILED
    assert done.error["code"] == "NODE_FAILED"


def test_planner_outside_run_dead(tmp_path: Path) -> None:
    ctx, snapshot, runner, _lines = _world(tmp_path)
    task = seal_task(ctx, snapshot, planner_script(plan_doc(SHOUT)))
    job = runner.run(runner.submit(snapshot.key("Station", "planner@1"), {"task": task}, {}).id)
    assert job.state == JobState.DEAD
    assert job.error["code"] == "RUN_PLAN_INVALID"
    assert job.error["detail"]["reason"] == "planner-outside-run"


def test_plan_inputs_and_nesting_checked(tmp_path: Path) -> None:
    ctx, snapshot, runner, lines = _world(tmp_path)
    inputs = {
        "task": seal_task(ctx, snapshot, planner_script(plan_doc(SHOUT))),
        "note": seal_note(ctx, snapshot, "x\n"),
    }
    run = lines.start_run("note-plan@1", inputs)
    planner = snapshot.key("Station", "planner@1")
    ghost = plan_doc({"shout": {"station": "upper@1", "in": {"note": "$inputs.ghost"}}})
    check = compile_plan(snapshot, ghost, planner_key=planner, run=run)
    assert check.ok is False and check.plan is None
    assert "E-PORT" in {item["code"] for item in check.diags}
    nested = plan_doc({"again": {"station": "planner@1", "in": {"task": "$inputs.task"}}})
    check = compile_plan(snapshot, nested, planner_key=planner, run=run)
    assert check.ok is False
    assert "PLAN_BOUNDS" in {item["code"] for item in check.diags}
    assert "PLAN_BOUNDS" in check.feedback
