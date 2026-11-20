"""M3-01 验收：epoch 存储、指针与回滚（spec/system/governance.md 7.1）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.governance")

from mao.errors import FactoryError  # noqa: E402
from mao.governance import EpochStore  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    broken_station,
    commit_change,
    constitution_edit,
    git,
    insert_run,
    open_world,
    upper_v2_limits,
)


def test_init_sets_active_pointer_and_ref(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    assert len(w.base) == 40
    assert w.epochs.pointers() == {"active": w.base, "candidate": None, "previous": None}
    assert w.epochs.current() == w.base
    assert git(w.repo, "rev-parse", "refs/factory/active") == w.base
    assert w.snapshot.epoch == w.base
    rows = w.ctx.db.query("SELECT \"commit\" FROM epoch_pointers WHERE name = 'active'")
    assert [r["commit"] for r in rows] == [w.base]


def test_init_twice_rejected(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    with pytest.raises(FactoryError) as ei:
        w.epochs.init(w.base, by="human:acceptance")
    assert ei.value.code == "EPOCH_STATE_INVALID"


def test_build_writes_frozen_resolutions_once(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    frozen = w.ctx.root / "epochs" / w.base / "resolutions.json"
    stored = json.loads(frozen.read_text(encoding="utf-8"))
    expected = [{**dict(item), "origin": "precomputed"} for item in w.snapshot.result.resolutions]
    assert stored
    assert stored == json.loads(json.dumps(expected))
    assert (w.ctx.root / "epochs" / w.base / "registry" / "stations" / "upper.yaml").is_file()
    frozen.write_text("[]", encoding="utf-8")
    again = w.epochs.build(w.base)
    assert again.commit == w.base
    assert frozen.read_text(encoding="utf-8") == "[]"


def test_build_rejects_invalid_registry(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    bad = commit_change(w.repo, broken_station())
    with pytest.raises(FactoryError) as ei:
        w.epochs.build(bad)
    assert ei.value.code == "REGISTRY_INVALID"
    assert w.ctx.db.query('SELECT "commit" FROM epochs WHERE "commit" = ?', (bad,)) == []
    assert not (w.ctx.root / "epochs" / bad).exists()
    assert w.epochs.current() == w.base


def test_rollback_moves_pointers_only(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    second = commit_change(w.repo, upper_v2_limits())
    w.epochs.build(second)
    w.epochs.set_pointer("previous", w.base, by="human:acceptance", reason="test")
    w.epochs.set_pointer("active", second, by="human:acceptance", reason="test")
    assert w.epochs.current() == second

    moved = w.epochs.rollback(by="human:acceptance")
    assert moved == w.base
    assert w.epochs.pointers() == {"active": w.base, "candidate": None, "previous": second}
    assert git(w.repo, "rev-parse", "refs/factory/active") == w.base
    assert git(w.repo, "rev-parse", "refs/heads/main") == w.base
    assert git(w.repo, "cat-file", "-t", second) == "commit"
    kinds = [r["kind"] for r in w.ctx.db.query("SELECT kind FROM events WHERE kind = 'epoch.pointer'")]
    assert len(kinds) >= 3


def test_adopt_requires_constitution_only_signed_commit(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    mixed = commit_change(w.repo, {**constitution_edit(), **upper_v2_limits()})
    with pytest.raises(FactoryError) as ei:
        w.epochs.adopt(mixed, by="human:acceptance")
    assert ei.value.code == "EPOCH_ADOPT_INVALID"
    assert ei.value.detail["reason"] == "paths"
    assert "stations/upper.yaml" in ei.value.detail["paths"]

    unsigned = commit_change(w.repo, constitution_edit())
    with pytest.raises(FactoryError) as ei:
        w.epochs.adopt(unsigned, by="human:acceptance")
    assert ei.value.code == "EPOCH_ADOPT_INVALID"
    assert ei.value.detail["reason"] == "unsigned"
    assert w.epochs.current() == w.base


def test_rollback_target_must_be_built(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    with pytest.raises(FactoryError) as ei:
        w.epochs.rollback(by="human:acceptance")
    assert ei.value.code == "EPOCH_UNSET"
    unbuilt = commit_change(w.repo, upper_v2_limits())
    with pytest.raises(FactoryError) as ei:
        w.epochs.rollback(unbuilt, by="human:acceptance")
    assert ei.value.code == "EPOCH_NOT_FOUND"
    with pytest.raises(FactoryError) as ei:
        w.epochs.rollback(w.base, by="human:acceptance")
    assert ei.value.code == "EPOCH_STATE_INVALID"


def test_retain_rules(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    second = commit_change(w.repo, upper_v2_limits())
    third = commit_change(w.repo, {"README.md": "third\n"}, parent=second)
    w.epochs.build(second)
    w.epochs.build(third)
    insert_run(w.ctx, "r-0001", epoch=third, state="RUNNING", created_at="2026-11-10T00:00:00.000Z")
    assert w.epochs.retain(w.base) is True
    assert w.epochs.retain(third) is True
    assert w.epochs.retain(second) is False
    listed = {e.commit: e.retain for e in w.epochs.list()}
    assert listed == {w.base: True, second: False, third: True}
    assert (w.ctx.root / "epochs" / second / "registry").is_dir()


def test_registry_not_configured(tmp_path: Path) -> None:
    w = open_world(tmp_path, init=False)
    (w.ctx.root / "factory.json").unlink()
    with pytest.raises(FactoryError) as ei:
        EpochStore(w.ctx).current()
    assert ei.value.code == "REGISTRY_NOT_CONFIGURED"
