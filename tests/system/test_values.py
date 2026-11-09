"""M1-02 单元：mao.values（spec/system/snapshot.md 5.10）。模块缺失时整文件跳过。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.values")
pytest.importorskip("mao.snapshot")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402
from mao.values import params_value, recipe_env  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "registry-m1"


def _writer(snap: Snapshot) -> str:
    return snap.key("Station", "writer@1")


def test_recipe_env_keeps_inputs_and_params() -> None:
    env = recipe_env({"note": "hello"}, {"times": 2})
    assert env["inputs"]["note"] == "hello"
    assert env["params"]["times"] == 2


def test_recipe_env_does_not_alias_caller_dict() -> None:
    params = {"times": 2}
    env = recipe_env({}, params)
    params["times"] = 9
    assert env["params"]["times"] == 2


def test_params_value_applies_writer_default() -> None:
    snap = Snapshot.load(FIXTURE)
    got = params_value(snap, _writer(snap), {})
    assert got["topic"] == "工厂"


def test_params_value_keeps_given_topic() -> None:
    snap = Snapshot.load(FIXTURE)
    assert params_value(snap, _writer(snap), {"topic": "验收"})["topic"] == "验收"


def test_params_value_rejects_unknown_and_wrong_type() -> None:
    snap = Snapshot.load(FIXTURE)
    station = _writer(snap)
    with pytest.raises(FactoryError) as unknown:
        params_value(snap, station, {"nope": 1})
    assert unknown.value.code == "PARAM_INVALID"
    with pytest.raises(FactoryError) as wrong:
        params_value(snap, station, {"topic": 1})
    assert wrong.value.code == "PARAM_INVALID"
