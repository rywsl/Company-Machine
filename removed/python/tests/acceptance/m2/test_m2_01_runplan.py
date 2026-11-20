"""M2-01 验收：运行计划编译（spec/system/lines.md 第 7.2 节）。

模块还不存在时整个文件跳过。实现之后，下面每个测试都调用 compile_run_plan。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("mao.runplan")
pytest.importorskip("mao.snapshot")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.runplan import Binding, PlanNode, RunPlan, compile_run_plan  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from tests.acceptance.m2.world import EXAMPLE, LINE, copy_example, patch_vtt  # noqa: E402

EXPECTED_ORDER = ["explainer", "dub_en", "to_github", "vertical", "to_s3", "to_shorts"]


def _load(root: Path) -> Snapshot:
    return Snapshot.load(root)


def test_example_plan_nodes_and_order() -> None:
    plan = compile_run_plan(_load(EXAMPLE), LINE)
    assert isinstance(plan, RunPlan)
    assert plan.line == "Line:explainer-multilang@5"
    assert [node.id for node in plan.nodes] == EXPECTED_ORDER
    assert all(
        isinstance(node, PlanNode) and node.kind == "station" and not node.implicit for node in plan.nodes
    )
    seen: set[str] = set()
    for node in plan.nodes:
        assert set(node.deps) <= seen
        seen.add(node.id)
    dub = plan.node("dub_en")
    assert any(
        isinstance(item, Binding) and item.port == "video" and item.node == "explainer" for item in dub.inputs
    )


def test_budget_items_equal_kernel_cost_item() -> None:
    snapshot = _load(EXAMPLE)
    plan = compile_run_plan(snapshot, LINE)
    key = snapshot.key("Line", LINE)
    kernel = sorted(
        (item, usd) for (line, item, usd) in snapshot.result.model.rows("cost_item") if line == key
    )
    assert list(plan.budget_items) == kernel
    assert plan.budget_cap_usd == 12.0


def test_implicit_converter_node(tmp_path: Path) -> None:
    snapshot = _load(patch_vtt(tmp_path))
    plan = compile_run_plan(snapshot, LINE)
    node_id = "dub_en.in.subtitle~subs-convert@1"
    node = plan.node(node_id)
    assert node.implicit is True
    assert node.kind == "converter"
    assert "explainer" in node.deps
    key = snapshot.key("Line", LINE)
    resolution = next(
        row
        for row in snapshot.result.resolutions
        if (row["line"], row["step"], row["port"]) == (key, "dub_en", "subtitle")
    )
    assert node.resolution == resolution
    dub = plan.node("dub_en")
    assert any(item.port == "subtitle" and item.node == node_id for item in dub.inputs)


def test_two_step_chain(tmp_path: Path) -> None:
    registry = patch_vtt(tmp_path)
    source = (registry / "converters" / "subs-convert" / "station.yaml").read_text(encoding="utf-8")
    bridge = registry / "converters" / "subs-bridge"
    bridge.mkdir()
    (bridge / "station.yaml").write_text(
        source.replace("name: subs-convert, version: 1", "name: subs-bridge, version: 1"),
        encoding="utf-8",
        newline="\n",
    )
    snapshot = _load(registry)
    key = snapshot.key("Line", LINE)
    for row in snapshot.result.resolutions:
        if (row["line"], row["step"], row["port"]) == (key, "dub_en", "subtitle"):
            row["status"] = "convert"
            row["chain"] = ["subs-bridge@1", "subs-convert@1"]
    plan = compile_run_plan(snapshot, LINE)
    first = plan.node("dub_en.in.subtitle~subs-bridge@1")
    second = plan.node("dub_en.in.subtitle~subs-convert@1")
    assert first.kind == "converter" and second.kind == "converter"
    assert first.id in second.deps
    assert second.id in plan.node("dub_en").deps
    assert plan.nodes.index(first) < plan.nodes.index(second)


def test_unsupported_constructs(tmp_path: Path) -> None:
    bodies = {
        "map": """\
    explainer:
      station: explainer@3
      in: {brief: $inputs.brief}
      map: {over: [en, ja], as: lang, max_items: 2}
""",
        "until": """\
    explainer:
      station: explainer@3
      in: {brief: $inputs.brief}
      until: {cond: "false", max_iter: 2}
""",
        "switch": """\
    choose:
      switch:
        on: $inputs.brief
        cases:
          - {when: "true", goto: explainer}
        default: explainer
    explainer:
      station: explainer@3
      in: {brief: $inputs.brief}
""",
        "subline": """\
    child:
      subline: explainer-multilang@5
      in: {brief: $inputs.brief}
""",
    }
    keep = "  keep: [explainer.video, explainer.subtitle, explainer.script]\n"
    for construct, body in bodies.items():
        registry = copy_example(tmp_path / construct)
        header = (
            "apiVersion: mao/v1\nkind: Line\n"
            "metadata: {name: explainer-multilang, version: 5}\nspec:\n"
            "  interface:\n    inputs: {brief: {type: brief.video@1}}\n"
            "  budget: {cap_usd: 12.0}\n"
        )
        if construct != "subline":
            header += keep
        text = header + "  steps:\n" + body
        (registry / "lines" / "explainer-multilang.yaml").write_text(text, encoding="utf-8", newline="\n")
        with pytest.raises(FactoryError) as ei:
            compile_run_plan(_load(registry), LINE)
        assert ei.value.code == "LINE_UNSUPPORTED"
        assert ei.value.detail.get("construct") == construct
        assert ei.value.detail.get("step")


def test_plan_is_deterministic(tmp_path: Path) -> None:
    first = compile_run_plan(_load(EXAMPLE), LINE)
    second = compile_run_plan(_load(EXAMPLE), LINE)
    assert first.to_json() == second.to_json()
    assert first.digest() == second.digest()
    shuffled = tmp_path / "shuffled"
    shutil.copytree(EXAMPLE, shuffled)
    # 改名再改回，迫使目录项的读取顺序与原树不同；摘要仍必须相同。
    moved = shuffled / "stations" / "explainer.yaml"
    temp = shuffled / "stations" / "explainer.yaml.tmp"
    moved.rename(temp)
    temp.rename(moved)
    again = compile_run_plan(_load(shuffled), LINE)
    assert again.digest() == first.digest()


def test_templated_params_recorded() -> None:
    plan = compile_run_plan(_load(EXAMPLE), LINE)
    node = plan.node("to_s3")
    assert "prefix" in node.templated
    assert "{{" in str(node.params["prefix"])
