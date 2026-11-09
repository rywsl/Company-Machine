"""M1-02 单元：mao.snapshot。模块缺失时整文件跳过。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("mao.snapshot")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "registry-m1"


def test_load_fixture_epoch_stable() -> None:
    first = Snapshot.load(FIXTURE)
    second = Snapshot.load(FIXTURE)
    assert first.epoch
    assert first.epoch == second.epoch


def test_epoch_ignores_copy_path(tmp_path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    shutil.copytree(FIXTURE, a)
    shutil.copytree(FIXTURE, b)
    assert Snapshot.load(a).epoch == Snapshot.load(b).epoch


def test_key_and_spec() -> None:
    snap = Snapshot.load(FIXTURE)
    key = snap.key("Station", "upper@1")
    assert key.startswith("Station:")
    assert "upper" in key
    spec = snap.spec(key)
    assert spec["machine"]


def test_spec_is_a_copy() -> None:
    snap = Snapshot.load(FIXTURE)
    key = snap.key("Station", "upper@1")
    spec = snap.spec(key)
    spec["machine"] = "mutated"
    assert snap.spec(key)["machine"] != "mutated"


def test_invalid_registry_rejected(tmp_path) -> None:
    root = tmp_path / "bad"
    (root / "types").mkdir(parents=True)
    (root / "types" / "nope.yaml").write_text("kind: Nope\n", encoding="utf-8")
    with pytest.raises(FactoryError):
        Snapshot.load(root)
