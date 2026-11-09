"""M2-08 验收：契约运行（spec/system/contract-tests.md 第 7 章）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("mao.contract")
pytest.importorskip("mao.errors")

from mao.contract import ContractRunner, Verdict  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402


def _runner(tmp_path: Path) -> ContractRunner:
    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    return ContractRunner(ctx, snapshot, runner)


def test_validator_accept_and_reject(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_validator("note.text@1", "count-text", timeout_s=30)
    assert result.verdict == Verdict.PASS
    assert result.kind == "validator"


def test_reject_fixture_that_passes_fails(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURE, registry)
    accept = registry / "fixtures" / "note" / "ok.txt"
    reject = registry / "fixtures" / "note" / "empty.txt"
    shutil.copyfile(accept, reject)
    ctx, snapshot, runner, _clock = open_factory(tmp_path / "run", registry)
    result = ContractRunner(ctx, snapshot, runner).run_validator("note.text@1", "count-text", timeout_s=30)
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_FIXTURE_ACCEPTED"


def test_fixture_set_removed(tmp_path: Path) -> None:
    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    contract = ContractRunner(ctx, snapshot, runner)
    result = contract.check_fixture_set(snapshot, snapshot, "note.text@1", "count-text")
    assert result.verdict == Verdict.PASS


def test_golden_within_baseline(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_golden("upper-golden@1", "upper-baseline@1", timeout_s=30)
    assert result.kind == "golden"
    assert any(gate["ok"] is True for gate in result.detail["gates"])


def test_golden_exceeds_written_max(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    shutil.copytree(FIXTURE, registry)
    baseline = registry / "records" / "upper-baseline.yaml"
    text = baseline.read_text(encoding="utf-8").replace("max: 1.0", "max: 0.0")
    baseline.write_text(text, encoding="utf-8", newline="\n")
    ctx, snapshot, runner, _clock = open_factory(tmp_path / "run", registry)
    result = ContractRunner(ctx, snapshot, runner).run_golden(
        "upper-golden@1", "upper-baseline@1", timeout_s=30
    )
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_BASELINE_EXCEEDED"


def test_baseline_record_keys_are_not_gates(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_golden("golden@1", None, timeout_s=30)
    assert result.kind == "golden"
    assert result.verdict == Verdict.PASS
    assert result.detail.get("code") != "CONTRACT_BASELINE_EXCEEDED"


def test_unknown_metric_fails(tmp_path: Path) -> None:
    with pytest.raises(FactoryError) as ei:
        _runner(tmp_path).run_golden("golden@1", "baseline-judge@1", timeout_s=30)
    assert ei.value.code == "CONTRACT_METRIC_MISSING"


def test_golden_budget_required(tmp_path: Path) -> None:
    with pytest.raises(FactoryError) as ei:
        _runner(tmp_path).run_golden("golden-no-budget@1", None, timeout_s=30)
    assert ei.value.code == "CONTRACT_BUDGET_REQUIRED"


def test_equivalence_holds(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_equivalence("subs-convert@1", timeout_s=30)
    assert result.kind == "equivalence"
    assert result.verdict == Verdict.PASS


def test_converter_reject_not_sealed(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_equivalence("note-to-text@1", timeout_s=30)
    assert result.detail["sealed_outputs"] == []


def test_diff_regression(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_diff("subs-convert@1", timeout_s=30)
    assert result.kind == "diff"
    assert result.verdict == Verdict.FAIL
    assert result.detail["code"] == "CONTRACT_DIFF_REGRESSION"


def test_diff_facts_exact(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_diff("subs-convert@1", timeout_s=30)
    assert result.detail.get("epsilon") is None
    assert result.kind == "diff"


def test_diff_skipped_without_previous(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_diff("upper@1", timeout_s=30)
    assert result.verdict == Verdict.PASS
    assert result.detail.get("reason") == "no_previous"


def test_sink_dry_run_no_receipt(tmp_path: Path) -> None:
    result = _runner(tmp_path).run_sink_dry_run("publish-local@1", inputs={}, params={}, timeout_s=30)
    assert result.kind == "sink_dry_run"
    assert result.detail["executed_run"] is False


def test_sink_without_propose_refuses(tmp_path: Path) -> None:
    with pytest.raises(FactoryError) as ei:
        _runner(tmp_path).run_sink_dry_run("publish-s3@2", inputs={}, params={}, timeout_s=30)
    assert ei.value.code == "CONTRACT_SINK_NO_PROPOSE"


def test_timeout_required(tmp_path: Path) -> None:
    with pytest.raises(FactoryError) as ei:
        _runner(tmp_path).run_validator("note.text@1", "count-text", timeout_s=0)
    assert ei.value.code == "CONTRACT_TIMEOUT_REQUIRED"
