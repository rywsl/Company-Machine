"""M2-02 验收：run 存储（spec/system/lines.md 第 7.2 节）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.runs")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.runs import NodeState, RunState  # noqa: E402
from tests.acceptance.m2.world import start_line  # noqa: E402

from mrl.expr import compile_expr  # noqa: E402
from mrl.factory.sites import RUN  # noqa: E402
from mrl.stdlib import make_table  # noqa: E402

LINE = "note-pipeline@1"


def test_start_run_creates_pending(tmp_path: Path) -> None:
    ctx, _snapshot, _runner, _clock, lines, inputs = start_line(tmp_path, LINE)
    run = lines.start_run(LINE, inputs)
    assert run.state == RunState.PENDING
    assert run.id.startswith("r-")
    nodes = lines.nodes(run.id)
    assert nodes
    assert all(node.state == NodeState.WAITING for node in nodes)
    kinds = [row["kind"] for row in ctx.db.query("SELECT kind FROM events WHERE run_id = ?", (run.id,))]
    assert "run.created" in kinds


def test_input_validation(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock, lines, inputs = start_line(tmp_path, LINE)
    from mao.warehouse import BeanDraft

    missing = dict(inputs)
    missing.pop(next(iter(missing)))
    with pytest.raises(FactoryError) as ei:
        lines.start_run(LINE, missing)
    assert ei.value.code == "RUN_INPUT_INVALID"
    assert ei.value.detail.get("problems")
    extra = dict(inputs)
    extra["not-a-port"] = next(iter(inputs.values()))
    with pytest.raises(FactoryError) as ei:
        lines.start_run(LINE, extra)
    assert ei.value.code == "RUN_INPUT_INVALID"
    iface = snapshot.spec(snapshot.key("Line", LINE))["interface"]["inputs"]
    port, decl = next(iter(iface.items()))
    type_ref = decl["type"] if isinstance(decl, dict) else decl
    draft = ctx.warehouse.add(BeanDraft(type=snapshot.type_key(type_ref).split(":", 1)[1], meta={}, files={}))
    assert draft.sealed is False
    bad = dict(inputs)
    bad[port] = draft.id
    with pytest.raises(FactoryError) as ei:
        lines.start_run(LINE, bad)
    assert ei.value.code == "RUN_INPUT_INVALID"
    assert ei.value.detail.get("problems")


def test_budget_rules(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, _clock, lines, inputs = start_line(tmp_path, LINE)
    spec = snapshot.spec(snapshot.key("Line", LINE))
    cap = (spec.get("budget") or {}).get("cap_usd")
    if cap is None:
        with pytest.raises(FactoryError) as ei:
            lines.start_run(LINE, inputs)
        assert ei.value.code == "RUN_BUDGET_REQUIRED"
    else:
        with pytest.raises(FactoryError) as ei:
            lines.start_run(LINE, inputs, budget_usd=float(cap) + 1)
        assert ei.value.code == "RUN_BUDGET_INVALID"
        smaller = lines.start_run(LINE, inputs, budget_usd=float(cap) / 2)
        assert smaller.budget_cap_usd == float(cap) / 2


def test_run_value_shape(tmp_path: Path) -> None:
    _ctx, _snapshot, _runner, _clock, lines, inputs = start_line(tmp_path, LINE)
    run = lines.start_run(LINE, inputs, budget_usd=1.0)
    value = run.value()
    compiled = compile_expr(
        'run.id != "" && run.epoch != "" && run.line != "" && run.budget.cap_usd >= run.budget.spent_usd && run.budget.remaining_usd == run.budget.cap_usd - run.budget.spent_usd',
        {"run": RUN},
        make_table(),
    )
    assert compiled.evaluate({"run": value}) is True
