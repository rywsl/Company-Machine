"""M16-04：完整冷检查点、一致性与恢复（assets.md 4.6、5.7）。"""

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.packages")

from mao.context import Context  # noqa: E402
from mao.durable import LeaseStore  # noqa: E402
from mao.packages import create_checkpoint, restore_checkpoint, verify_package  # noqa: E402
from mao.sandbox import LocalProvider  # noqa: E402
from tests.acceptance.m16.asset_world import World, error_code  # noqa: E402


def test_checkpoint_contains_factory_state_and_business_files(tmp_path: Path) -> None:
    """检查点含现存业务、目录、队列和 Registry，排除原生 CLI 凭据。"""
    w = World(tmp_path)
    note = w.asset(beans=[w.note()])
    folder = w.workspace() / "notebook"
    folder.mkdir(parents=True)
    (folder / "progress.md").write_text("尚待整理的资料", encoding="utf-8")
    native = w.workspace() / ".codex"
    native.mkdir()
    (native / "auth.json").write_text("private-cli-credential", encoding="utf-8")
    pending = w.post("scout", "检查点之后继续")
    root = w.factory_asset([note["id"]])
    dest = tmp_path / "snapshot"
    manifest = create_checkpoint(w.ctx, dest, factory_asset=root["id"])
    assert manifest["format"] == "mao-checkpoint/1" and verify_package(dest)["ok"]
    assert (dest / "business" / "scout" / "notebook" / "progress.md").is_file()
    assert not any(".codex" in f["path"] for f in manifest["files"])
    restored = tmp_path / "restored"
    restore_checkpoint(dest, restored)
    with Context.open(restored, provider=LocalProvider(restored)) as ctx:
        assert (
            ctx.db.query("SELECT state FROM agent_messages WHERE id = ?", (pending.id,))[0]["state"]
            == "pending"
        )
        assert ctx.db.query("SELECT id FROM asset_records WHERE id = ?", (note["id"],))
        assert not ctx.db.query("SELECT session FROM agents WHERE session IS NOT NULL")
        assert not ctx.db.query("SELECT name FROM agents WHERE state != 'paused'")
        assert not ctx.db.query("SELECT resource FROM leases")
    assert (restored / "agents" / "scout" / "workspace" / "notebook" / "progress.md").is_file()


def test_live_serve_blocks_checkpoint_without_stopping_it(tmp_path: Path) -> None:
    """活跃服务拒绝冷检查点，不擅自停止生产或发布不完整目录。"""
    w = World(tmp_path)
    root = w.factory_asset()
    assert LeaseStore(w.ctx).acquire("serve", owner="other-worker", ttl_s=3600)
    dest = tmp_path / "busy-snapshot"
    with error_code("ASSET_CHECKPOINT_BUSY"):
        create_checkpoint(w.ctx, dest, factory_asset=root["id"])
    assert not (dest / "manifest.json").exists()
    assert LeaseStore(w.ctx).valid("serve") is not None


def test_unclassified_workspace_state_blocks_complete_claim(tmp_path: Path) -> None:
    """业务文件没有分类时不能声称完整保存。"""
    w = World(tmp_path)
    w.workspace().mkdir(parents=True)
    (w.workspace() / "research.db").write_text("unclassified", encoding="utf-8")
    root = w.factory_asset()
    with error_code("ASSET_STATE_UNDECLARED"):
        create_checkpoint(w.ctx, tmp_path / "incomplete", factory_asset=root["id"])


def test_corrupt_checkpoint_fails_before_restore(tmp_path: Path) -> None:
    """摘要不符和额外文件都失败，恢复前不改目标。"""
    w = World(tmp_path)
    root = w.factory_asset()
    dest = tmp_path / "snapshot"
    create_checkpoint(w.ctx, dest, factory_asset=root["id"])
    (dest / "inventory.json").write_text("{}", encoding="utf-8")
    (dest / "unexpected.txt").write_text("extra", encoding="utf-8")
    assert not verify_package(dest)["ok"]
    target = tmp_path / "restore"
    with error_code("PACKAGE_CORRUPT"):
        restore_checkpoint(dest, target)
    assert not target.exists()


def test_backend_limit_and_existing_destination_are_explicit(tmp_path: Path) -> None:
    """不支持的后端与已有目标显式拒绝，不能覆盖或静默降级。"""
    w = World(tmp_path)
    root = w.factory_asset()
    (w.root / "backend-active.json").write_text(json.dumps({"database": "postgres"}), encoding="utf-8")
    with error_code("BACKUP_UNSUPPORTED"):
        create_checkpoint(w.ctx, tmp_path / "remote", factory_asset=root["id"])
    (w.root / "backend-active.json").unlink()
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("preserve", encoding="utf-8")
    with error_code("PACKAGE_TARGET_NOT_EMPTY"):
        create_checkpoint(w.ctx, target, factory_asset=root["id"])
    assert (target / "keep").read_text() == "preserve"


def test_restore_keeps_idempotency_and_does_not_publish(tmp_path: Path) -> None:
    """已核验回执、幂等键原样恢复，恢复动作不重新创建 job。"""
    from mao.connectors import ReceiptStore

    w = World(tmp_path)
    store = ReceiptStore(w.ctx)
    claim = store.claim(
        station_key="Station:publish-local@1",
        key="already-published",
        job_id="external-proof",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    store.publish(claim.receipt.id, {"target": "file://recorded", "locator": {}, "items": []})
    store.settle(claim.receipt.id, ok=True, detail="recorded verification")
    root = w.factory_asset()
    dest = tmp_path / "snapshot"
    create_checkpoint(w.ctx, dest, factory_asset=root["id"])
    target = tmp_path / "restored"
    restore_checkpoint(dest, target)
    with Context.open(target, provider=LocalProvider(target)) as ctx:
        row = ctx.db.query("SELECT status, idempotency_key FROM receipts WHERE id = ?", (claim.receipt.id,))[
            0
        ]
        assert row["status"] == "verified" and row["idempotency_key"] == "already-published"
        assert ctx.db.query("SELECT id FROM jobs") == []
