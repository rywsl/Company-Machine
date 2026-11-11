"""M2-08 验收：契约运行（spec/system/contract-tests.md 第 7 章）。

夹具里没有的变体（删样例、改门槛、第二个 converter 版本）都在测试自己的副本上打补丁，
不改受保护的夹具原件。实现不得为这些名字准备任何内置文档。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("mao.contract")
pytest.importorskip("mao.errors")

from mao.contract import ContractRunner, Verdict  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory, seal_input  # noqa: E402


def _runner(tmp_path: Path, registry: Path = FIXTURE) -> ContractRunner:
    ctx, snapshot, runner, _clock = open_factory(tmp_path / "run", registry)
    return ContractRunner(ctx, snapshot, runner)


def _copy(tmp_path: Path) -> Path:
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURE, registry)
    return registry


def _patch(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{path.name} 里没有 {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def _converter_v2(registry: Path, recipe: str, run: str) -> None:
    """给 toolbox 加一个配方，并新增 note-to-text@2 使用它。其余字段与 @1 相同。"""
    machine = registry / "machines" / "toolbox" / "machine.yaml"
    _patch(
        machine,
        "    publish-local:\n",
        f"    {recipe}:\n"
        "      inputs:  {in: {type: note.raw@1}}\n"
        "      outputs: {out: {type: note.text@1, harvest: /factory/out/out/note.txt}}\n"
        f"      run: {run}\n"
        "    publish-local:\n",
    )
    source = (registry / "stations" / "note-to-text.yaml").read_text(encoding="utf-8")
    v2 = source.replace("version: 1}", "version: 2}").replace("recipe: note-to-text\n", f"recipe: {recipe}\n")
    assert v2 != source
    (registry / "stations" / "note-to-text-2.yaml").write_text(v2, encoding="utf-8", newline="\n")


def test_validator_accept_and_reject(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_validator("note.text@1", "count-text", timeout_s=30)
    assert result.verdict == Verdict.PASS
    assert result.kind == "validator"
    accept = result.detail["accept"]
    reject = result.detail["reject"]
    assert accept
    assert reject
    assert all(item["ok"] is True for item in accept)
    assert all(item["ok"] is True for item in reject)


def test_reject_fixture_that_passes_fails(tmp_path: Path) -> None:
    registry = _copy(tmp_path)
    accept = registry / "fixtures" / "note" / "ok.txt"
    reject = registry / "fixtures" / "note" / "empty.txt"
    assert reject.read_bytes() == b""
    assert accept.read_bytes()
    shutil.copyfile(accept, reject)
    assert reject.read_bytes() == accept.read_bytes()
    result = _runner(tmp_path, registry).run_validator("note.text@1", "count-text", timeout_s=30)
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_FIXTURE_ACCEPTED"


def test_fixture_set_removed(tmp_path: Path) -> None:
    from mao.snapshot import Snapshot

    old = Snapshot.load(FIXTURE)
    removed = _copy(tmp_path / "removed")
    _patch(
        removed / "types" / "note.yaml",
        "        reject: [fixtures/note/empty.txt]\n",
        "        reject: [fixtures/note/fail.txt]\n",  # I8 要求至少一个反样例：换掉而不是清空
    )
    added = _copy(tmp_path / "added")
    _patch(
        added / "types" / "note.yaml",
        "        accept: [fixtures/note/ok.txt]\n",
        "        accept: [fixtures/note/ok.txt, fixtures/note/fail.txt]\n",
    )
    contract = _runner(tmp_path)
    shrunk = contract.check_fixture_set(old, Snapshot.load(removed), "note.text@1", "count-text")
    assert shrunk.verdict == Verdict.FAIL
    assert shrunk.detail["code"] == "CONTRACT_FIXTURE_REMOVED"
    grown = contract.check_fixture_set(old, Snapshot.load(added), "note.text@1", "count-text")
    assert grown.verdict == Verdict.PASS


def test_golden_within_baseline(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_golden("upper-golden@1", "upper-baseline@1", timeout_s=30)
    assert result.kind == "golden"
    assert result.verdict == Verdict.PASS
    assert result.detail["gates"]
    assert all(gate["ok"] is True for gate in result.detail["gates"])


def test_golden_exceeds_written_max(tmp_path: Path) -> None:
    registry = _copy(tmp_path)
    # 测试自己写的门槛：观测值 0 也越过 -1。规范不规定这个数
    _patch(registry / "baselines" / "upper-baseline.yaml", "max: 1.0", "max: -1.0")
    result = _runner(tmp_path, registry).run_golden("upper-golden@1", "upper-baseline@1", timeout_s=30)
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_BASELINE_EXCEEDED"


def test_baseline_record_keys_are_not_gates(tmp_path: Path) -> None:
    registry = _copy(tmp_path)
    _patch(registry / "baselines" / "upper-baseline.yaml", "cost_usd: {max: 1.0}", "cost_usd: {p50: -1.0}")
    result = _runner(tmp_path, registry).run_golden("upper-golden@1", "upper-baseline@1", timeout_s=30)
    assert result.kind == "golden"
    assert result.verdict == Verdict.PASS
    assert result.detail.get("code") != "CONTRACT_BASELINE_EXCEEDED"


def test_unknown_metric_fails(tmp_path: Path) -> None:
    registry = _copy(tmp_path)
    _patch(registry / "baselines" / "upper-baseline.yaml", "cost_usd: {max: 1.0}", "judge_score: {max: 1.0}")
    result = _runner(tmp_path, registry).run_golden("upper-golden@1", "upper-baseline@1", timeout_s=30)
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_METRIC_MISSING"


def test_golden_budget_required(tmp_path: Path) -> None:
    registry = _copy(tmp_path)
    _patch(registry / "goldens" / "upper-golden.yaml", "  budget_usd: 1.0\n", "")
    with pytest.raises(FactoryError) as ei:
        _runner(tmp_path, registry).run_golden("upper-golden@1", None, timeout_s=30)
    assert ei.value.code == "CONTRACT_BUDGET_REQUIRED"


def test_equivalence_holds(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_equivalence("note-to-text@1", timeout_s=30)
    assert result.kind == "equivalence"
    assert result.verdict == Verdict.PASS
    assert result.detail["accept"]
    assert all(item["sealed"] is True and item["failed_expr"] == [] for item in result.detail["accept"])
    registry = _copy(tmp_path / "false")
    _patch(registry / "stations" / "note-to-text.yaml", '"dst.facts.chars == src.facts.chars"', '"false"')
    broken = _runner(tmp_path / "false", registry).run_equivalence("note-to-text@1", timeout_s=30)
    assert broken.verdict == Verdict.FAIL
    assert broken.detail["code"] == "CONTRACT_EQUIVALENCE_FAILED"


def test_converter_reject_not_sealed(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_equivalence("note-to-text@1", timeout_s=30)
    assert result.detail["reject"]
    assert all(item["sealed"] is False for item in result.detail["reject"])


def test_diff_regression(tmp_path: Path) -> None:
    registry = _copy(tmp_path)
    _converter_v2(
        registry, "note-to-text-broken", "[exit-three, /factory/in/in/raw.txt, /factory/out/out/note.txt]"
    )
    result = _runner(tmp_path, registry).run_diff("note-to-text@2", timeout_s=30)
    assert result.kind == "diff"
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_DIFF_REGRESSION"


def test_diff_facts_exact(tmp_path: Path) -> None:
    registry = _copy(tmp_path)
    # 新版本原样复制文本、不转大写：chars 与 lines 相同，has_lower 不同
    _converter_v2(registry, "note-to-text-keep", "[pull-notes, /factory/in/in, /factory/out/out/note.txt]")
    result = _runner(tmp_path, registry).run_diff("note-to-text@2", timeout_s=30)
    assert result.kind == "diff"
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_DIFF_FACTS"
    assert result.detail.get("epsilon") is None


def test_diff_skipped_without_previous(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_diff("note-to-text@1", timeout_s=30)
    assert result.verdict == Verdict.PASS
    assert result.detail.get("reason") == "no_previous"


def test_sink_dry_run_no_receipt(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock = open_factory(tmp_path / "run", FIXTURE)
    note = seal_input(ctx, snapshot, "note.text@1")
    contract = ContractRunner(ctx, snapshot, runner)
    target = tmp_path / "published"
    result = contract.run_sink_dry_run(
        "publish-staged@1", inputs={"note": note}, params={"dir": str(target)}, timeout_s=30
    )
    assert result.kind == "sink_dry_run"
    assert result.verdict == Verdict.PASS
    assert result.detail["executed_run"] is False
    assert ctx.db.query("SELECT id FROM receipts") == []
    assert not target.exists() or not any(target.iterdir())


def test_sink_without_propose_refuses(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock = open_factory(tmp_path / "run", FIXTURE)
    note = seal_input(ctx, snapshot, "note.text@1")
    with pytest.raises(FactoryError) as ei:
        ContractRunner(ctx, snapshot, runner).run_sink_dry_run(
            "publish-local@1", inputs={"note": note}, params={"dir": str(tmp_path / "out")}, timeout_s=30
        )
    assert ei.value.code == "CONTRACT_SINK_NO_PROPOSE"
    assert ctx.db.query("SELECT id FROM receipts") == []


def test_timeout_required(tmp_path: Path) -> None:
    # note-to-text@1 没有 limits.timeout；调用方也不传
    with pytest.raises(FactoryError) as ei:
        _runner(tmp_path).run_equivalence("note-to-text@1")
    assert ei.value.code == "CONTRACT_TIMEOUT_REQUIRED"
