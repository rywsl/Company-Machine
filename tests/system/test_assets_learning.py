"""正式知识的失败路径、事务回滚和指针冲突。"""

from __future__ import annotations

from pathlib import Path

from mao.errors import FactoryError
from mao.learning import LearningStore
from tests.acceptance.m16.asset_world import World


def test_bad_evidence_and_unauthorized_promote_leave_heads_empty(tmp_path: Path) -> None:
    world = World(tmp_path)
    bean = world.note("one\ntwo\n")
    candidate = world.asset(beans=[bean])
    contract = world.contract(authorize=False)
    store = LearningStore(world.ctx, world.epochs)
    try:
        store.open_trial(candidate["id"], contract["id"], purpose="knowledge", by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_NOT_AUTHORIZED"
    else:
        raise AssertionError("未授权契约不应开试验")
    try:
        store.promote(candidate["id"], trial_id=None, expected=None, by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_PROOF_REQUIRED"
    else:
        raise AssertionError("没有证明不应晋升")
    authorized = world.contract()
    trial = store.open_trial(candidate["id"], authorized["id"], purpose="knowledge", by="ada")
    try:
        store.evaluate(trial["id"], {"normal": {"kind": "job", "id": "j-missing"}}, by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_EVIDENCE_INVALID"
    else:
        raise AssertionError("缺失执行不应通过")
    assert store.get_trial(trial["id"])["state"] == "open"
    assert store.heads("station:upper@1") == []


def test_open_trial_rolls_back_with_caller_transaction(tmp_path: Path) -> None:
    world = World(tmp_path)
    candidate = world.asset(beans=[world.note()])
    contract = world.contract()
    store = LearningStore(world.ctx, world.epochs)
    try:
        with world.ctx.db.tx():
            trial = store.open_trial(candidate["id"], contract["id"], purpose="knowledge", by="ada")
            raise RuntimeError("crash before commit")
    except RuntimeError:
        pass
    else:
        raise AssertionError("外层事务应回滚")
    try:
        store.get_trial(trial["id"])
    except FactoryError as exc:
        assert exc.code == "ASSET_NOT_FOUND"
    else:
        raise AssertionError("未提交试验不应留下记录")
    again = store.open_trial(candidate["id"], contract["id"], purpose="knowledge", by="ada")
    assert again["state"] == "open"


def test_second_promote_conflicts_without_rewriting_generation(tmp_path: Path) -> None:
    world = World(tmp_path)
    bean = world.note("one\ntwo\n")
    candidate = world.asset(beans=[bean])
    contract = world.contract()
    store = LearningStore(world.ctx, world.epochs)
    trial = store.open_trial(candidate["id"], contract["id"], purpose="knowledge", by="ada")
    job = world.job(bean)
    assert (
        store.evaluate(trial["id"], {"normal": {"kind": "job", "id": job.id}}, by="ada")["state"] == "passed"
    )
    first = store.promote(candidate["id"], trial_id=trial["id"], expected=None, by="ada")
    try:
        store.promote(candidate["id"], trial_id=trial["id"], expected=None, by="bea")
    except FactoryError as exc:
        assert exc.code == "ASSET_HEAD_CONFLICT"
        assert exc.detail["actual"] == candidate["id"]
    else:
        raise AssertionError("过时 expected 不应覆盖指针")
    assert store.heads("station:upper@1")[0]["generation"] == first["generation"]
