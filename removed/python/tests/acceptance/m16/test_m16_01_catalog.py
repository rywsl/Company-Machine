"""M16-01：资产身份、依赖保留和显式释放（assets.md 4.1、5.1）。"""

from pathlib import Path

import pytest

pytest.importorskip("mao.assets")

from mao.base import canonical_bytes, digest_bytes  # noqa: E402
from mao.retention import RetentionConfig, collect  # noqa: E402
from mao.warehouse import BeanDraft  # noqa: E402
from tests.acceptance.m16.asset_world import World, error_code  # noqa: E402


def test_content_address_and_idempotency(tmp_path: Path) -> None:
    """相同内容幂等，不覆盖登记人；修改摘要产生新版本。"""
    w = World(tmp_path)
    first = w.asset(beans=[w.note()])
    again = w.assets.put(first["document"], by="bea")
    assert first == again and first["by"] == "ada"
    assert first["id"] == "asset:" + digest_bytes(canonical_bytes(first["document"]))
    other = w.assets.put({**first["document"], "summary": "新增边界说明"}, by="bea")
    assert other["id"] != first["id"] and w.assets.get(first["id"]) == first


def test_rejects_unsealed_missing_and_secret_references(tmp_path: Path) -> None:
    """未封存/不存在引用和含已知秘密的记录都不能登记。"""
    w = World(tmp_path)
    raw = w.ctx.warehouse.add(BeanDraft(type="note.text@1.0", files={}))
    for bean in [raw.id, "sha256:" + "f" * 64]:
        with error_code("ASSET_REFERENCE_INVALID"):
            w.asset(beans=[bean])
    with error_code("ASSET_SECRET"):
        w.asset(summary="不得保存 " + agent_secret())
    assert w.assets.list() == []


def agent_secret() -> str:
    from tests.acceptance.m13.agent_world import SCOUT_TOKEN

    return SCOUT_TOKEN


def test_dependency_closure_shares_payload_and_keeps_proofs(tmp_path: Path) -> None:
    """多资产共享一个 Bean，闭包包含真实准入报告且不复制载荷。"""
    w = World(tmp_path)
    bean = w.note("原始资料\n")
    source = w.asset(key="source.notes", kind="dataset", beans=[bean])
    method = w.asset(beans=[bean], requires=[source["id"]])
    closure = w.assets.closure([method["id"]])
    assert closure["assets"] == sorted([source["id"], method["id"]])
    assert closure["beans"] == [bean]
    digest = w.ctx.warehouse.get(bean).payload[0].digest
    assert closure["blobs"].count(digest) == 1
    reports = w.rows("SELECT report_digest FROM seals WHERE bean_id = ?", (bean,))
    assert all(r["report_digest"] in closure["blobs"] for r in reports)


def test_gc_and_manual_expire_preserve_registered_evidence(tmp_path: Path) -> None:
    """GC 与人工 expire 不能删活跃依赖；解除根之后才允许回收。"""
    w = World(tmp_path)
    bean = w.note()
    record = w.asset(beans=[bean])
    w.clock.advance(2 * 86400)
    cfg = RetentionConfig(beans={"note.text@1": 0}, workspaces_days=None, logs_days=None, sessions_days=None)
    assert bean not in collect(w.ctx, cfg).expired
    with error_code("ASSET_IN_USE"):
        w.ctx.warehouse.expire(bean, by="ada", reason="manual")
    w.assets.release(record["id"], reason="明确退役", by="ada")
    assert bean in collect(w.ctx, cfg).expired


def test_release_cannot_break_another_asset(tmp_path: Path) -> None:
    """仍被其他未释放资产引用时拒绝 release，原件继续存在。"""
    w = World(tmp_path)
    source = w.asset(key="source", kind="dataset", beans=[w.note()])
    w.asset(requires=[source["id"]])
    with error_code("ASSET_IN_USE"):
        w.assets.release(source["id"], reason="还不能删", by="ada")
    assert not w.assets.get(source["id"])["released"]


def test_broken_payload_is_reported_without_truncated_closure(tmp_path: Path) -> None:
    """活动证据字节丢失必须报错，不能返回看似完整的闭包。"""
    w = World(tmp_path)
    bean = w.note()
    record = w.asset(beans=[bean])
    w.ctx.warehouse.blob_path(w.ctx.warehouse.get(bean).payload[0].digest).unlink()
    with error_code("ASSET_REFERENCE_INVALID"):
        w.assets.closure([record["id"]])


def test_unknown_business_kind_is_data_not_execution(tmp_path: Path) -> None:
    """允许业务扩展 kind，登记工具/流程不自动改 Registry 或执行。"""
    w = World(tmp_path)
    before = w.epochs.current()
    record = w.asset(kind="quant/research", data={"hypothesis": "仅为研究资料"})
    assert w.assets.list(kind="quant/research")[0]["id"] == record["id"]
    assert w.epochs.current() == before and w.rows("SELECT id FROM jobs") == []


def test_disabled_factory_has_no_implicit_asset_work(tmp_path: Path) -> None:
    """旧工厂运行不产生 use，显式资产写入提示尚未启用。"""
    w = World(tmp_path, enabled=False)
    with error_code("ASSET_NOT_ENABLED"):
        w.asset()
    assert w.job(w.note("one\ntwo\n")).state.value == "SEALED"
    assert w.rows("SELECT execution_id FROM asset_uses") == []


def test_registration_and_expire_serialize_across_connections(tmp_path: Path) -> None:
    """未提交登记与另一连接的过期操作串行，提交后证据仍完整。"""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from mao.context import Context
    from mao.errors import FactoryError
    from mao.sandbox import LocalProvider

    w = World(tmp_path)
    bean = w.note()
    started = Event()
    with Context.open(w.root, provider=LocalProvider(w.root), clock=w.clock) as other:

        def expire() -> str:
            started.set()
            try:
                other.warehouse.expire(bean, by="other", reason="concurrent cleanup")
            except FactoryError as exc:
                return exc.code
            return "expired"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with w.ctx.db.tx():
                record = w.asset(beans=[bean])
                pending = pool.submit(expire)
                assert started.wait(5)
            assert pending.result(timeout=10) == "ASSET_IN_USE"
    assert not w.ctx.warehouse.get(bean).expired_at
    assert bean in w.assets.closure([record["id"]])["beans"]
