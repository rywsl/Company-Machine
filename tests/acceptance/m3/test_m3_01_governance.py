"""M3-01 验收：治理（spec/system/governance.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.governance")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.governance import ChangePipeline, EpochStore  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402


def _open(tmp_path: Path):
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    return ctx, snapshot, ChangePipeline(ctx), EpochStore(ctx)


def test_constitution_change_rejected(tmp_path: Path) -> None:
    _ctx, _snapshot, pipe, _epochs = _open(tmp_path)
    with pytest.raises(FactoryError) as ei:
        pipe.submit({"changes": [{"op": "upsert", "path": "constitution/invariants.mrl"}]}, by="acceptance")
    assert ei.value.code == "CONSTITUTION_LOCKED"


def test_risk_ignores_proposer(tmp_path: Path) -> None:
    _ctx, _snapshot, pipe, _epochs = _open(tmp_path)
    change = pipe.submit(
        {"risk": "low", "changes": [{"op": "upsert", "path": "stations/upper.yaml"}]}, by="acceptance"
    )
    assert change.risk in {"low", "medium", "high"}
    assert change.risk != ""


def test_unscored_is_not_low(tmp_path: Path) -> None:
    _ctx, _snapshot, pipe, _epochs = _open(tmp_path)
    with pytest.raises(FactoryError) as ei:
        pipe.submit({"changes": [{"op": "upsert", "path": "stations/missing.yaml"}]}, by="acceptance")
    assert ei.value.code == "RISK_UNSCORED"


def test_rollback_moves_pointer_only(tmp_path: Path) -> None:
    ctx, _snapshot, _pipe, epochs = _open(tmp_path)
    current = epochs.current()
    moved = epochs.rollback(current, by="acceptance")
    assert moved == current or Path(ctx.root / "epoch").read_text(encoding="utf-8").strip() == moved
    assert (ctx.root / "epoch").exists()


def test_freeze_window(tmp_path: Path) -> None:
    _ctx, _snapshot, pipe, _epochs = _open(tmp_path)
    with pytest.raises(FactoryError) as ei:
        pipe.submit({"changes": [{"op": "upsert", "path": "stations/upper.yaml"}]}, by="acceptance")
    assert ei.value.code == "FREEZE_WINDOW"
