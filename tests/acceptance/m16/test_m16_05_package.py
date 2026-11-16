"""M16-05：审核闭包、移交隔离与买家准入（assets.md 4.7、5.8）。"""

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.packages")

from mao.context import Context  # noqa: E402
from mao.packages import create_checkpoint, export_package, import_package, verify_package  # noqa: E402
from mao.sandbox import LocalProvider  # noqa: E402
from tests.acceptance.m13.agent_world import SCOUT_TOKEN, git  # noqa: E402
from tests.acceptance.m16.asset_world import World, error_code  # noqa: E402


def _prepared(tmp_path: Path):
    w = World(tmp_path)
    material = w.asset(beans=[w.note("PUBLIC EXAMPLE\nSECOND LINE\n")])
    registry = w.registry_asset()
    contract = w.contract()
    root = w.factory_asset([material["id"], registry["id"], contract["id"]])
    checkpoint = tmp_path / "checkpoint"
    create_checkpoint(w.ctx, checkpoint, factory_asset=root["id"])
    release = w.release(root, registry, [contract["id"]])
    return w, root, registry, checkpoint, release


def test_export_rebuilds_closed_package_without_database_or_git_history(tmp_path: Path) -> None:
    """只导出审核闭包：不带未选资产、Git 旧史或运行数据库。"""
    w = World(tmp_path)
    marker = "SELLER-PRIVATE-HISTORY-947205"
    hidden = w.repo / "seller-private.txt"
    hidden.write_text(marker, encoding="utf-8")
    git(w.repo, "add", "-A")
    git(w.repo, "commit", "-q", "-m", "private history")
    hidden.unlink()
    git(w.repo, "add", "-A")
    git(w.repo, "commit", "-q", "-m", "remove private data")
    w.asset(key="private-only", beans=[w.note(marker + "\n")])
    public = w.asset(beans=[w.note("PUBLIC\nMATERIAL\n")])
    registry = w.registry_asset()
    root = w.factory_asset([public["id"], registry["id"]])
    checkpoint = tmp_path / "checkpoint"
    create_checkpoint(w.ctx, checkpoint, factory_asset=root["id"])
    dest = tmp_path / "public-package"
    manifest = export_package(checkpoint, dest, release=w.release(root, registry))
    assert manifest["format"] == "mao-factory-package/1"
    paths = {item["path"] for item in manifest["files"]}
    assert not any(
        p.endswith("factory.db") or "registry.bundle" in p or ".git" in p.split("/") for p in paths
    )
    assert all(marker.encode() not in p.read_bytes() for p in dest.rglob("*") if p.is_file())
    assert verify_package(dest) == {"ok": True, "problems": [], "acceptance": "not_run"}


def test_changed_release_cannot_reuse_disclosure_review(tmp_path: Path) -> None:
    """审核绑定确切闭包，改内容/披露范围后必须重新审核。"""
    _w, _root, _registry, checkpoint, release = _prepared(tmp_path)
    release["omitted"].append({"item": "new omission", "reason": "private", "replacement": None})
    with error_code("PACKAGE_RELEASE_INVALID"):
        export_package(checkpoint, tmp_path / "bad-package", release=release)


def test_required_unavailable_resource_cannot_be_sold_as_runnable(tmp_path: Path) -> None:
    """不能交付的必需资源不能伪装为完整可运行能力包。"""
    w = World(tmp_path)
    registry = w.registry_asset()
    resources = w.resources() + [
        {
            "key": "private-volume",
            "kind": "volume",
            "required": True,
            "fingerprint": "private",
            "state": None,
            "transfer": "unavailable",
            "license": "seller-only",
        }
    ]
    root = w.factory_asset([registry["id"]], resources=resources)
    checkpoint = tmp_path / "checkpoint"
    # 检查点记录不可移交资源；公开导出拒绝完整可运行主张。
    create_checkpoint(w.ctx, checkpoint, factory_asset=root["id"])
    with error_code("PACKAGE_RELEASE_INVALID"):
        export_package(checkpoint, tmp_path / "package", release=w.release(root, registry))


def test_known_secret_in_binary_blocks_checkpoint(tmp_path: Path) -> None:
    """已知秘密不能因藏在二进制而进入完整包，也不静默改写。"""
    w = World(tmp_path)
    raw = w.root / "incoming" / "binary.bin"
    raw.parent.mkdir(exist_ok=True)
    raw.write_bytes(b"\x00\xff" + SCOUT_TOKEN.encode() + b"\x00")
    w.ctx.warehouse.put_file(raw)
    root = w.factory_asset()
    with error_code("ASSET_SECRET"):
        create_checkpoint(w.ctx, tmp_path / "bad", factory_asset=root["id"])
    assert raw.read_bytes().startswith(b"\x00\xff")


def test_verify_rejects_extra_bytes_and_unsafe_paths(tmp_path: Path) -> None:
    """额外字节和穿越路径不能通过静态校验或影响包外文件。"""
    _w, _root, _registry, checkpoint, release = _prepared(tmp_path)
    dest = tmp_path / "package"
    export_package(checkpoint, dest, release=release)
    (dest / "unlisted-credentials.txt").write_text("unlisted", encoding="utf-8")
    manifest = json.loads((dest / "manifest.json").read_text())
    manifest["files"].append({"path": "../outside", "digest": "sha256:" + "0" * 64, "size": 0})
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = verify_package(dest)
    assert not report["ok"] and report["problems"]
    assert report["acceptance"] == "not_run"
    assert not (tmp_path / "outside").exists()


def test_missing_buyer_binding_fails_before_install(tmp_path: Path) -> None:
    """缺少必需绑定先失败，不建立半成品买家工厂。"""
    _w, _root, _registry, checkpoint, release = _prepared(tmp_path)
    src = tmp_path / "package"
    export_package(checkpoint, src, release=release)
    target = tmp_path / "buyer"
    with error_code("PACKAGE_BINDING_REQUIRED"):
        import_package(src, target, bindings={"resources": {}, "factory_config": {}})
    assert not target.exists()


def test_import_has_fresh_execution_state_and_untrusted_knowledge(tmp_path: Path) -> None:
    """导入真实准入数据，但不继承授权、正式指针、旧订单或会话。"""
    w, root, _registry, checkpoint, release = _prepared(tmp_path)
    src = tmp_path / "package"
    export_package(checkpoint, src, release=release)
    target = tmp_path / "buyer"
    from tests.fakes.fakelib import local_commands

    report = import_package(
        src,
        target,
        bindings=w.buyer_bindings(root),
        provider=LocalProvider(target, commands=local_commands(), clock=w.clock),
    )
    assert report["state"] == "needs_acceptance" and report["bindings_missing"] == []
    with Context.open(target, provider=LocalProvider(target)) as ctx:
        for table in [
            "jobs",
            "runs",
            "receipts",
            "agent_messages",
            "asset_authorizations",
            "asset_trials",
            "asset_heads",
        ]:
            assert not ctx.db.query("SELECT * FROM " + table)
        assert not ctx.db.query("SELECT session FROM agents WHERE session IS NOT NULL OR state != 'paused'")
        assert ctx.db.query("SELECT id FROM asset_records WHERE id = ?", (root["id"],))
        from mao.egress import SecretStore

        value = SecretStore(target).inject(["SCOUT_TOKEN"])["SCOUT_TOKEN"]
        assert value == "buyer-SCOUT_TOKEN-value-v1" and value != SCOUT_TOKEN
    assert git(target / "registry", "rev-list", "--count", "HEAD") == "1"


def test_source_execution_reference_requires_sanitized_evidence_asset(tmp_path: Path) -> None:
    """裸执行引用不能带入公开图；需要重建为明确的历史资料。"""
    w = World(tmp_path)
    job = w.job(w.note("one\ntwo\n"))
    evidence = w.asset(key="execution-proof", kind="evidence", evidence=[{"kind": "job", "id": job.id}])
    registry = w.registry_asset()
    root = w.factory_asset([evidence["id"], registry["id"]])
    checkpoint = tmp_path / "checkpoint"
    create_checkpoint(w.ctx, checkpoint, factory_asset=root["id"])
    with error_code("PACKAGE_RELEASE_INVALID"):
        export_package(checkpoint, tmp_path / "unsafe", release=w.release(root, registry))
