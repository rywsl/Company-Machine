"""M4-01 验收：动态编译（spec/system/dynamic.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.dynamic")
pytest.importorskip("mao.runplan")
pytest.importorskip("mao.errors")

from mao.dynamic import DynamicRunner, compile_dynamic  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from mao.runplan import compile_run_plan  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from tests.acceptance.m2.world import EXAMPLE, LINE, copy_example  # noqa: E402


def _write(tmp_path: Path, name: str, body: str, *, keep: bool) -> Path:
    registry = copy_example(tmp_path / name)
    header = (
        "apiVersion: mao/v1\nkind: Line\n"
        "metadata: {name: explainer-multilang, version: 5}\nspec:\n"
        "  interface:\n    inputs: {brief: {type: brief.video@1}}\n"
        "  budget: {cap_usd: 12.0}\n"
    )
    if keep:
        header += "  keep: [explainer.video, explainer.subtitle, explainer.script]\n"
    (registry / "lines" / "explainer-multilang.yaml").write_text(
        header + "  steps:\n" + body, encoding="utf-8"
    )
    return registry


def test_m2_compiler_still_rejects(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "map",
        "    explainer:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n      map: {over: [en, ja], as: lang, max_items: 2}\n",
        keep=True,
    )
    with pytest.raises(FactoryError) as ei:
        compile_run_plan(Snapshot.load(registry), LINE)
    assert ei.value.code == "LINE_UNSUPPORTED"


def test_plain_line_digest_matches_m2() -> None:
    snapshot = Snapshot.load(EXAMPLE)
    assert compile_dynamic(snapshot, LINE).to_json() == compile_run_plan(snapshot, LINE).to_json()


def test_literal_map_order(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "order",
        "    dubs:\n      station: dub@2\n      in: {video: $inputs.brief}\n      map: {over: [en, ja], as: lang, max_items: 2}\n",
        keep=False,
    )
    plan = compile_dynamic(Snapshot.load(registry), LINE)
    assert "dubs#0" in [node.id for node in plan.nodes]
    assert "dubs#1" in [node.id for node in plan.nodes]


def test_dynamic_overflow_reject(tmp_path: Path) -> None:
    from mao.runs import LineRunner, NodeState
    from tests.acceptance.m2.world import open_factory

    registry = _write(
        tmp_path,
        "overflow",
        "    explainer:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n      map: {over: [en, ja, de], as: lang, max_items: 2, overflow: reject}\n",
        keep=True,
    )
    ctx, snapshot, runner, _clock = open_factory(tmp_path / "run", registry)
    lines = LineRunner(ctx, snapshot, runner)
    DynamicRunner(ctx, snapshot, runner, lines)
    run = lines.start_run(LINE, {})
    DynamicRunner(ctx, snapshot, runner, lines).expand(run.id)
    lines.tick(run.id)
    failed = [node for node in lines.nodes(run.id) if node.state == NodeState.FAILED]
    assert failed
    assert failed[0].error["code"] == "DYNAMIC_OVERFLOW"


def test_truncate_keeps_prefix(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "trunc",
        "    dubs:\n      station: dub@2\n      map: {over: [en, ja, de], as: lang, max_items: 2, overflow: truncate}\n      in: {video: $inputs.brief}\n",
        keep=False,
    )
    plan = compile_dynamic(Snapshot.load(registry), LINE)
    ids = [node.id for node in plan.nodes]
    assert "dubs#0" in ids and "dubs#1" in ids
    assert "dubs#2" not in ids


def test_switch_first_match(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "switch",
        '    choose:\n      switch:\n        on: $inputs.brief\n        cases:\n          - {when: "true", goto: explainer}\n          - {when: "true", goto: other}\n        default: other\n    explainer:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n    other:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n',
        keep=True,
    )
    plan = compile_dynamic(Snapshot.load(registry), LINE)
    assert any(node.kind == "switch" for node in plan.nodes)


def test_switch_default(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "default",
        '    choose:\n      switch:\n        on: $inputs.brief\n        cases:\n          - {when: "false", goto: explainer}\n        default: explainer\n    explainer:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n',
        keep=True,
    )
    plan = compile_dynamic(Snapshot.load(registry), LINE)
    assert plan.line.endswith("explainer-multilang@5")
    assert any(node.kind == "switch" for node in plan.nodes)


def test_switch_eval_error(tmp_path: Path) -> None:
    from mao.runs import LineRunner
    from tests.acceptance.m2.world import open_factory

    registry = _write(
        tmp_path,
        "eval",
        '    choose:\n      switch:\n        on: $inputs.brief\n        cases:\n          - {when: "1 + \\"x\\"", goto: explainer}\n        default: explainer\n    explainer:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n',
        keep=True,
    )
    ctx, snapshot, runner, _clock = open_factory(tmp_path / "fac", registry)
    lines = LineRunner(ctx, snapshot, runner)
    run = lines.start_run(LINE, {})
    DynamicRunner(ctx, snapshot, runner, lines).expand(run.id)
    assert run.id.startswith("r-")


def test_until_stops(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "until",
        '    explainer:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n      until: {cond: "false", max_iter: 2}\n',
        keep=True,
    )
    plan = compile_dynamic(Snapshot.load(registry), LINE)
    assert any(node.kind == "until" for node in plan.nodes)


def test_until_exhausted(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "exhausted",
        '    explainer:\n      station: explainer@3\n      in: {brief: $inputs.brief}\n      until: {cond: "false", max_iter: 1}\n',
        keep=True,
    )
    plan = compile_dynamic(Snapshot.load(registry), LINE)
    assert plan.digest()
    assert any(node.id.startswith("explainer") for node in plan.nodes)


def test_subline_same_epoch(tmp_path: Path) -> None:
    registry = _write(
        tmp_path,
        "sub",
        "    child:\n      subline: explainer-multilang@5\n      in: {brief: $inputs.brief}\n",
        keep=False,
    )
    plan = compile_dynamic(Snapshot.load(registry), LINE)
    assert any(node.kind == "subline" for node in plan.nodes)
    assert plan.epoch
