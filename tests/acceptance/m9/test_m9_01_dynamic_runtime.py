"""M9-01 验收：动态结构在运行期执行（spec/system/closure.md 5.1）。

全部经 LineRunner.start_run / tick / run_to_completion 驱动，不直接调用 DynamicRunner。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.runs")

from mao.runs import InlineExecutor, LineRunner, NodeState, RunState  # noqa: E402
from tests.acceptance.m9.closure_world import (  # noqa: E402
    bean_text,
    build_registry,
    node_map,
    open_world,
    seal_note,
)

TEXT = "alpha\nbeta\ngamma\n"


def _world(tmp_path: Path):
    registry = build_registry(tmp_path)
    ctx, snapshot, runner, _clock = open_world(tmp_path / "w", registry)
    return ctx, snapshot, runner, LineRunner(ctx, snapshot, runner)


def _run(tmp_path: Path, line: str, text: str = TEXT):
    ctx, snapshot, runner, lines = _world(tmp_path)
    run = lines.start_run(line, {"note": seal_note(ctx, snapshot, text)})
    done = lines.run_to_completion(run.id, InlineExecutor(runner), max_ticks=200, wait_s=0)
    return ctx, runner, lines, done


def test_map_expands_runtime_list(tmp_path: Path) -> None:
    ctx, _runner, lines, done = _run(tmp_path, "note-map@1")
    assert done.state == RunState.SUCCEEDED
    nodes = node_map(lines, done.id)
    instances = sorted(name for name in nodes if name.startswith("shout#"))
    assert instances == ["shout#0", "shout#1", "shout#2"]
    assert all(nodes[name].state == NodeState.DONE and nodes[name].job_id for name in instances)
    assert nodes["shout"].state == NodeState.DONE
    assert nodes["shout"].job_id is None
    ordered = [nodes[f"shout#{i}"].outputs["note"][0] for i in range(3)]
    assert nodes["shout"].outputs == {"note": ordered}
    assert done.outputs["notes"] == ordered
    assert [bean_text(ctx, bean) for bean in ordered] == ["ALPHA\n", "BETA\n", "GAMMA\n"]


def test_map_overflow_rejects(tmp_path: Path) -> None:
    _ctx, _runner, lines, done = _run(tmp_path, "note-map-reject@1")
    assert done.state == RunState.FAILED
    nodes = node_map(lines, done.id)
    assert nodes["split"].state == NodeState.DONE
    assert nodes["shout"].state == NodeState.FAILED
    assert nodes["shout"].error["code"] == "DYNAMIC_OVERFLOW"
    assert [name for name in nodes if name.startswith("shout#")] == []


def test_map_truncate_keeps_upstream_order(tmp_path: Path) -> None:
    ctx, _runner, lines, done = _run(tmp_path, "note-map-trunc@1")
    assert done.state == RunState.SUCCEEDED
    nodes = node_map(lines, done.id)
    assert sorted(name for name in nodes if name.startswith("shout#")) == ["shout#0", "shout#1"]
    assert [bean_text(ctx, bean) for bean in done.outputs["notes"]] == ["ALPHA\n", "BETA\n"]


def test_map_max_parallel_limits_submissions(tmp_path: Path) -> None:
    ctx, snapshot, runner, lines = _world(tmp_path)
    run = lines.start_run("note-map-serial@1", {"note": seal_note(ctx, snapshot, TEXT)})
    executor = InlineExecutor(runner)
    states: dict[str, NodeState] = {}
    for _ in range(20):
        new = lines.tick(run.id)
        states = {node.node_id: node.state for node in lines.nodes(run.id)}
        if any(name.startswith("shout#") and state == NodeState.SUBMITTED for name, state in states.items()):
            break
        executor.dispatch(new)
    assert states.get("shout#0") == NodeState.SUBMITTED
    assert states.get("shout#1") == NodeState.WAITING
    assert states.get("shout#2") == NodeState.WAITING
    done = lines.run_to_completion(run.id, executor, max_ticks=200, wait_s=0)
    assert done.state == RunState.SUCCEEDED
    assert len(done.outputs["notes"]) == 3


def test_switch_selects_one_branch(tmp_path: Path) -> None:
    ctx, snapshot, runner, lines = _world(tmp_path)
    executor = InlineExecutor(runner)
    loud = lines.start_run("note-switch@1", {"note": seal_note(ctx, snapshot, "hello\n", title="loud")})
    calm = lines.start_run("note-switch@1", {"note": seal_note(ctx, snapshot, "hush\n", title="calm")})
    for run, chosen, other in ((loud, "loud", "calm"), (calm, "calm", "loud")):
        done = lines.run_to_completion(run.id, executor, max_ticks=200, wait_s=0)
        assert done.state == RunState.SUCCEEDED
        nodes = node_map(lines, run.id)
        assert nodes["choose"].state == NodeState.DONE
        assert nodes["choose"].job_id is None
        assert nodes[chosen].state == NodeState.DONE
        assert nodes[other].state == NodeState.SKIPPED
        assert nodes[other].job_id is None


def test_until_stops_when_condition_holds(tmp_path: Path) -> None:
    _ctx, _runner, lines, done = _run(tmp_path, "note-until@1")
    assert done.state == RunState.SUCCEEDED
    nodes = node_map(lines, done.id)
    assert nodes["shout@1"].state == NodeState.DONE
    assert nodes["shout@2"].state == NodeState.DONE
    assert nodes["shout@1"].job_id and nodes["shout@2"].job_id
    assert nodes["shout@1"].job_id != nodes["shout@2"].job_id
    assert "shout@3" not in nodes
    assert nodes["shout"].state == NodeState.DONE
    assert nodes["shout"].outputs == nodes["shout@2"].outputs
    assert done.outputs["note"] == nodes["shout@2"].outputs["note"]


def test_until_exhausted_fails_run(tmp_path: Path) -> None:
    _ctx, _runner, lines, done = _run(tmp_path, "note-until-never@1")
    assert done.state == RunState.FAILED
    nodes = node_map(lines, done.id)
    assert nodes["shout@1"].state == NodeState.DONE
    assert nodes["shout@2"].state == NodeState.DONE
    assert "shout@3" not in nodes
    assert nodes["shout"].state == NodeState.FAILED
    assert nodes["shout"].error["code"] == "UNTIL_EXHAUSTED"
    assert done.outputs == {}


def test_subline_runs_child_in_parent_epoch(tmp_path: Path) -> None:
    ctx, _runner, lines, done = _run(tmp_path, "note-parent@1")
    assert done.state == RunState.SUCCEEDED
    nodes = node_map(lines, done.id)
    assert nodes["child"].state == NodeState.DONE
    assert nodes["child"].job_id is None
    children = [run for run in lines.list() if run.trigger.get("on") == "subline"]
    assert len(children) == 1
    child = children[0]
    assert child.trigger == {"on": "subline", "parent": done.id, "step": "child"}
    assert child.line.endswith("note-child@1")
    assert child.epoch == done.epoch
    assert child.state == RunState.SUCCEEDED
    assert nodes["child"].outputs == child.outputs
    assert done.outputs["note"] == child.outputs["note"]
    assert bean_text(ctx, child.outputs["note"][0]) == "ALPHA\nBETA\nGAMMA\n"
