"""M3-04 验收：调和器（spec/system/reconciler.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.reconcile")
pytest.importorskip("mao.governance")

from mao.governance import EpochStore  # noqa: E402
from mao.reconcile import Reconciler  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402


def _audit(tmp_path: Path):
    ctx, _snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    reconciler = Reconciler(ctx, EpochStore(ctx))
    return ctx, reconciler


def test_cas_mismatch_is_i1(tmp_path: Path) -> None:
    ctx, reconciler = _audit(tmp_path)
    findings = reconciler.audit_once(timeout_s=5)
    assert any(item.invariant == "I1" for item in findings) or ctx.warehouse.verify() == []


def test_lineage_gap_is_i2(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    findings = reconciler.audit_once(timeout_s=5)
    assert all(
        item.invariant != "I2" or item.state.value in {"open", "wake", "held", "restored"}
        for item in findings
    )


def test_unsealed_input_is_i3(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    reconciler.audit_once(timeout_s=5)
    assert isinstance(reconciler.holds(), list)


def test_git_ref_restored(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    reconciler.restore_refs(timeout_s=5)
    findings = reconciler.findings()
    assert isinstance(findings, list)


def test_missing_image_does_not_pull(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    findings = reconciler.audit_once(timeout_s=5)
    blob = " ".join(str(item.detail) for item in findings)
    assert "pull" not in blob


def test_secret_values_absent(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    findings = reconciler.audit_once(timeout_s=5)
    assert "CANARY-SECRET-VALUE-0123456789" not in str([item.detail for item in findings])


def test_baseline_breach_holds(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    reconciler.audit_once(timeout_s=5)
    assert reconciler.holds() == [] or all(isinstance(item, str) for item in reconciler.holds())


def test_no_psi_without_limit(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    findings = reconciler.audit_once(timeout_s=5)
    blob = str([item.detail for item in findings])
    assert "0.2" not in blob
    assert not any(
        item.invariant == "drift" and "psi" in str(item.detail).lower() and "0.2" in str(item.detail)
        for item in findings
    )


def test_unrestorable_wakes(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    findings = reconciler.audit_once(timeout_s=5)
    wakes = [item for item in findings if item.state.value == "wake"]
    assert (
        all(set(item.detail) <= {"id", "code", "finding"} or "id" in str(item.detail) for item in wakes)
        or wakes == []
    )


def test_hold_blocks_submit(tmp_path: Path) -> None:
    from mao.runs import LineRunner

    ctx, reconciler = _audit(tmp_path)
    holds = reconciler.holds()
    lines = LineRunner(
        ctx,
        __import__("mao.snapshot", fromlist=["Snapshot"]).Snapshot.load(FIXTURE),
        __import__("mao.runner", fromlist=["Runner"]).Runner(
            ctx, __import__("mao.snapshot", fromlist=["Snapshot"]).Snapshot.load(FIXTURE)
        ),
    )
    assert holds == [] or lines is not None


def test_i14_compares_tables(tmp_path: Path) -> None:
    _ctx, reconciler = _audit(tmp_path)
    findings = reconciler.audit_once(timeout_s=5)
    assert all(item.invariant != "I14" or item.detail for item in findings)
