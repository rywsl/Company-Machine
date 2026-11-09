"""M4-02 验收：Plan（spec/system/dynamic.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.dynamic")
pytest.importorskip("mao.errors")

from mao.dynamic import compile_plan  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import EXAMPLE, open_factory  # noqa: E402


def _plan(tmp_path: Path, document: dict):
    ctx, snapshot, runner, _clock = open_factory(tmp_path, EXAMPLE)
    from mao.runs import LineRunner

    lines = LineRunner(ctx, snapshot, runner)
    run = lines.start_run("explainer-multilang@5", {})
    return compile_plan(snapshot, document, planner_key=snapshot.key("Station", "explainer@3"), run=run)


def test_plan_feedback_on_bounds(tmp_path: Path) -> None:
    check = _plan(tmp_path, {"spec": {"steps": {f"s{i}": {"station": "explainer@3"} for i in range(50)}}})
    assert check.ok is False
    assert "PLAN_BOUNDS" in check.feedback


def test_plan_cannot_define_specs(tmp_path: Path) -> None:
    check = _plan(tmp_path, {"kind": "Type", "spec": {"steps": {}}})
    assert check.ok is False
    assert any(item["code"] == "PLAN_INVALID" for item in check.diags)


def test_budget_number_not_template(tmp_path: Path) -> None:
    check = _plan(
        tmp_path,
        {
            "spec": {
                "steps": {
                    "a": {
                        "station": "explainer@3",
                        "params": {"budget_usd": "{{ run.budget.remaining_usd }}"},
                    }
                }
            }
        },
    )
    assert check.ok is False
    assert any(item["code"] in {"E-SCHEMA", "PLAN_INVALID", "PLAN_BOUNDS"} for item in check.diags)


def test_budget_remaining(tmp_path: Path) -> None:
    check = _plan(
        tmp_path, {"spec": {"steps": {"a": {"station": "explainer@3"}}, "budget": {"cap_usd": 10**9}}}
    )
    assert check.ok is False
    assert any(item["code"] == "PLAN_BUDGET" for item in check.diags)


def test_missing_converter_rejects_plan(tmp_path: Path) -> None:
    check = _plan(
        tmp_path, {"spec": {"steps": {"a": {"station": "explainer@3", "in": {"brief": "$inputs.missing"}}}}}
    )
    assert check.ok is False
    assert any(item["code"] == "E-NO-CONVERTER" for item in check.diags)


def test_sealed_plan_same_epoch(tmp_path: Path) -> None:
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, EXAMPLE)
    from mao.runs import LineRunner

    lines = LineRunner(ctx, snapshot, _runner)
    run = lines.start_run("explainer-multilang@5", {})
    check = compile_plan(
        snapshot,
        {"spec": {"steps": {"a": {"station": "vertical@1"}}}},
        planner_key=snapshot.key("Station", "explainer@3"),
        run=run,
    )
    assert check.ok is True
    assert check.plan is not None
    assert check.plan.epoch == snapshot.epoch


def test_runtime_resolution_append_only(tmp_path: Path) -> None:
    with pytest.raises(FactoryError) as ei:
        _plan(tmp_path, {"spec": {"steps": {"a": {"station": "explainer@3"}}}})
    assert ei.value.code == "RUN_PLAN_INVALID"
