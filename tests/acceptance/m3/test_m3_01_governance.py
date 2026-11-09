"""M3-01 验收：治理（spec/system/governance.md 第 7 章）。"""

from __future__ import annotations

import json
import subprocess
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
    document = {"changes": [{"op": "upsert", "path": "stations/upper.yaml"}]}
    claimed_low = pipe.submit({**document, "risk": "low"}, by="acceptance")
    claimed_high = pipe.submit({**document, "risk": "high"}, by="acceptance")
    assert claimed_low.risk == claimed_high.risk
    assert claimed_low.risk in {"low", "medium", "high"}


def test_unscored_is_not_low(tmp_path: Path) -> None:
    _ctx, _snapshot, pipe, _epochs = _open(tmp_path)
    with pytest.raises(FactoryError) as ei:
        pipe.submit({"changes": [{"op": "upsert", "path": "stations/missing.yaml"}]}, by="acceptance")
    assert ei.value.code == "RISK_UNSCORED"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return proc.stdout.strip()


def test_rollback_moves_pointer_only(tmp_path: Path) -> None:
    ctx, _snapshot, _pipe, epochs = _open(tmp_path)
    repo = ctx.root
    _git(repo, "init")
    _git(repo, "config", "user.email", "acceptance@example.com")
    _git(repo, "config", "user.name", "acceptance")
    readme = repo / "README"
    readme.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "base")
    first = _git(repo, "rev-parse", "HEAD")
    readme.write_text("later\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "later")
    second = _git(repo, "rev-parse", "HEAD")
    (repo / "epoch").write_text(second + "\n", encoding="utf-8")
    moved = epochs.rollback(first, by="acceptance")
    pointer = (repo / "epoch").read_text(encoding="utf-8").strip()
    assert moved == first
    assert pointer == first
    assert pointer != second
    assert _git(repo, "rev-parse", second) == second
    history = _git(repo, "log", "--format=%H")
    assert first in history
    assert second in history


def test_freeze_window(tmp_path: Path) -> None:
    ctx, _snapshot, _pipe, _epochs = _open(tmp_path)
    (ctx.root / "factory.json").write_text(
        json.dumps({"freeze": {"start": "2025-01-01T00:00:00Z", "until": "2027-01-01T00:00:00Z"}}),
        encoding="utf-8",
    )
    pipe = ChangePipeline(ctx)
    with pytest.raises(FactoryError) as ei:
        pipe.submit({"changes": [{"op": "upsert", "path": "stations/upper.yaml"}]}, by="acceptance")
    assert ei.value.code == "FREEZE_WINDOW"
    assert ei.value.detail.get("until")
