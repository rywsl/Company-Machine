"""资产目录的失败路径、事务回滚和跨连接过期。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from mao.assets import AssetStore
from mao.context import Context
from mao.errors import FactoryError
from mao.sandbox import LocalProvider
from tests.acceptance.m16.asset_world import World


def _document(bean: str | None = None, **overrides: object) -> dict:
    document = {
        "key": "method.notes",
        "kind": "knowledge",
        "summary": "保留原始材料的整理方法",
        "scope": {"subject": "station:upper@1"},
        "beans": [bean] if bean else [],
        "requires": [],
        "evidence": [],
        "data": {
            "claim": {
                "kind": "method",
                "statement": "按材料完成整理",
                "limits": "仅适用于约定输入",
                "revalidate": ["工具版本变化"],
            }
        },
    }
    document.update(overrides)
    return document


def test_invalid_document_release_and_missing_root(tmp_path: Path) -> None:
    world = World(tmp_path)
    store = AssetStore(world.ctx)
    bean = world.note()
    for document, field in (
        ({**_document(bean), "extra": 1}, "extra"),
        (_document(bean, summary=""), "summary"),
        (_document(bean, key=""), "key"),
    ):
        try:
            store.put(document, by="ada")
        except FactoryError as exc:
            assert exc.code == "ASSET_INVALID"
            assert exc.detail["field"] == field
        else:
            raise AssertionError("非法文档不应登记")
    assert store.list() == []
    try:
        store.release("asset:sha256:" + "a" * 64, reason="不存在", by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_NOT_FOUND"
    else:
        raise AssertionError("缺失资产不应释放")
    try:
        store.release("asset:sha256:" + "b" * 64, reason="", by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_INVALID"
    else:
        raise AssertionError("空原因不应释放")
    try:
        store.closure(["asset:sha256:" + "c" * 64])
    except FactoryError as exc:
        assert exc.code == "ASSET_NOT_FOUND"
    else:
        raise AssertionError("缺失根不应给出闭包")
    try:
        store.put(_document("sha256:" + "d" * 64), by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_REFERENCE_INVALID"
        assert exc.detail["kind"] == "bean"
    else:
        raise AssertionError("缺失 Bean 不应登记")
    assert world.rows("SELECT id FROM asset_records") == []


def test_disabled_write_and_rollback_leave_no_row(tmp_path: Path) -> None:
    disabled = World(tmp_path / "off", enabled=False)
    try:
        AssetStore(disabled.ctx).put(_document(), by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_NOT_ENABLED"
    else:
        raise AssertionError("未启用工厂不应登记")

    world = World(tmp_path / "on")
    store = AssetStore(world.ctx)
    bean = world.note()
    try:
        with world.ctx.db.tx():
            created = store.put(_document(bean), by="ada")
            raise RuntimeError("crash before commit")
    except RuntimeError:
        pass
    else:
        raise AssertionError("事务应因崩溃回滚")
    try:
        store.get(created["id"])
    except FactoryError as exc:
        assert exc.code == "ASSET_NOT_FOUND"
    else:
        raise AssertionError("未提交登记不应留下资产")
    assert world.rows("SELECT asset_id FROM asset_edges") == []


def test_expire_from_other_connection_sees_committed_asset(tmp_path: Path) -> None:
    world = World(tmp_path)
    bean = world.note()
    started = Event()
    with Context.open(world.root, provider=LocalProvider(world.root), clock=world.clock) as other:

        def expire() -> str:
            started.set()
            try:
                other.warehouse.expire(bean, by="other", reason="concurrent cleanup")
            except FactoryError as exc:
                return exc.code
            return "expired"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with world.ctx.db.tx():
                record = AssetStore(world.ctx).put(_document(bean), by="ada")
                pending = pool.submit(expire)
                assert started.wait(5)
            assert pending.result(timeout=10) == "ASSET_IN_USE"
    assert world.ctx.warehouse.get(bean).expired_at is None
    assert bean in AssetStore(world.ctx).closure([record["id"]])["beans"]
