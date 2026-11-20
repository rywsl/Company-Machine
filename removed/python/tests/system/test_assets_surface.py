"""操作面失败、回滚与并发授权只留下一条记录。"""

from __future__ import annotations

import threading
from pathlib import Path

from mao.context import Context
from mao.errors import FactoryError
from mao.governance.epochs import EpochStore
from mao.learning import LearningStore
from mao.sandbox import LocalProvider
from tests.acceptance.m13.agent_world import tool_result
from tests.acceptance.m16.asset_world import World
from tests.fakes.fakelib import local_commands


def test_failed_authorize_and_spoofed_put_leave_no_rows(tmp_path: Path) -> None:
    world = World(tmp_path)
    candidate = world.asset()
    code, body, err = world.cli("knowledge", "authorize", candidate["id"], "--by", "ada")
    assert code == 1, err
    assert body["error"]["code"] == "ASSET_NOT_AUTHORIZED"
    assert world.rows("SELECT asset_id FROM asset_authorizations") == []

    missing = tmp_path / "missing-package"
    code, report, err = world.cli("package", "verify", str(missing))
    assert code == 1 and report["ok"] is False and report["acceptance"] == "not_run", err
    assert not missing.exists()

    document = {
        "key": "scout.other",
        "kind": "knowledge",
        "summary": "不该登记到别的岗位",
        "scope": {"subject": "agent:helper"},
        "beans": [],
        "requires": [],
        "evidence": [],
        "data": {},
    }
    _result, error = tool_result(world.mcp("asset_put", {"document": document}))
    assert error is not None and error["code"] == "ASSET_INVALID"
    assert world.assets.list(subject="agent:helper") == []


def test_authorize_rolls_back_and_concurrent_callers_share_one_row(tmp_path: Path) -> None:
    world = World(tmp_path)
    contract = world.contract(authorize=False)
    store = world.learning
    try:
        with world.ctx.db.tx():
            store.authorize(contract["id"], by="ada")
            raise RuntimeError("crash before commit")
    except RuntimeError:
        pass
    else:
        raise AssertionError("外层事务应回滚")
    assert world.rows("SELECT asset_id FROM asset_authorizations") == []

    other = Context.open(
        world.root, provider=LocalProvider(world.root, commands=local_commands()), clock=world.clock
    )
    errors: list[BaseException] = []
    results: list[dict] = []

    def run(ctx: object) -> None:
        try:
            results.append(LearningStore(ctx, EpochStore(ctx)).authorize(contract["id"], by="ada"))
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(world.ctx,)),
        threading.Thread(target=run, args=(other,)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        other.close()
    assert errors == [], errors
    assert len(results) == 2
    assert {item["asset_id"] for item in results} == {contract["id"]}
    rows = world.rows("SELECT asset_id, by_actor FROM asset_authorizations")
    assert len(rows) == 1 and rows[0]["asset_id"] == contract["id"] and rows[0]["by_actor"] == "ada"
    again = store.authorize(contract["id"], by="bea")
    assert again["by"] == "ada"
    assert len(world.rows("SELECT asset_id FROM asset_authorizations")) == 1


def test_cli_authorize_uses_the_same_service_row(tmp_path: Path) -> None:
    world = World(tmp_path)
    contract = world.contract(authorize=False)
    code, body, err = world.cli("knowledge", "authorize", contract["id"], "--by", "ada")
    assert code == 0, err
    stored = LearningStore(world.ctx, world.epochs).authorize(contract["id"], by="bea")
    assert stored["asset_id"] == body["asset_id"] == contract["id"]
    assert stored["by"] == body["by"] == "ada"
    try:
        world.learning.authorize("asset:missing", by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_NOT_FOUND"
    else:
        raise AssertionError("缺失资产不应授权")
