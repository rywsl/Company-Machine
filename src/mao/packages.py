"""完整冷检查点与可移交包。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from mao.assets import AssetStore
from mao.backup import create_backup, restore_backup
from mao.base import canonical_json, digest_bytes, digest_file, utc_iso
from mao.durable import LeaseStore
from mao.errors import FactoryError

CHECKPOINT_FORMAT = "mao-checkpoint/1"
PACKAGE_FORMAT = "mao-factory-package/1"
_OMITTED = {".git", ".codex", ".claude", ".home", ".runtime"}
_BUSY = ("serve", "worker", "agent")


def create_checkpoint(
    ctx: Any, dest: Path, *, factory_asset: str, by: str = "human", timeout_s: float = 120.0
) -> dict[str, Any]:
    del timeout_s
    dest = Path(dest)
    _reject_remote(ctx.root)
    if _occupied(dest) or Path(str(dest) + ".part").exists():
        raise FactoryError("PACKAGE_TARGET_NOT_EMPTY", "检查点目标非空", path=str(dest))
    record = AssetStore(ctx).get(factory_asset)
    if record["document"].get("kind") != "factory":
        raise FactoryError("ASSET_INVALID", "检查点需要 factory 资产", field="factory_asset")
    _reject_secrets(ctx.root)
    omitted = _classify_workspaces(ctx, record["document"]["data"])
    _reject_busy(ctx)
    part = Path(str(dest) + ".part")
    try:
        if part.exists():
            shutil.rmtree(part)
        part.mkdir(parents=True)
        create_backup(ctx, part / "backup", by=by)
        _copy_business(ctx, part, record["document"]["data"])
        inventory = {
            "closure": AssetStore(ctx).closure([factory_asset]),
            "resources": list(record["document"]["data"].get("resources") or []),
            "workspaces": record["document"]["data"].get("workspaces") or {},
            "omitted": omitted,
            "history_gaps": _history_gaps(ctx),
        }
        (part / "inventory.json").write_text(canonical_json(inventory), encoding="utf-8")
        files = _file_list(part)
        manifest = {
            "format": CHECKPOINT_FORMAT,
            "created_at": utc_iso(ctx.clock.now()),
            "factory_asset": factory_asset,
            "files": files,
            "inventory_digest": digest_bytes((part / "inventory.json").read_bytes()),
        }
        (part / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(part, dest)
        part = None
    finally:
        if part is not None and part.exists():
            shutil.rmtree(part, ignore_errors=True)
    return json.loads((dest / "manifest.json").read_text(encoding="utf-8"))


def restore_checkpoint(
    src: Path, root: Path, *, registry_dest: Path | None = None, timeout_s: float = 120.0
) -> dict[str, Any]:
    src = Path(src)
    root = Path(root)
    report = verify_package(src, timeout_s=timeout_s)
    if not report["ok"]:
        raise FactoryError("PACKAGE_CORRUPT", "检查点校验失败", problems=report["problems"])
    if root.exists() and any(root.iterdir()):
        raise FactoryError("PACKAGE_TARGET_NOT_EMPTY", "恢复目标非空", path=str(root))
    restored = restore_backup(src / "backup", root, registry_dest=registry_dest, timeout_s=timeout_s)
    _restore_business(src, root)
    _mark_restored(root)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    inventory = json.loads((src / "inventory.json").read_text(encoding="utf-8"))
    return {
        **restored,
        "resources_missing": [
            item["key"] for item in inventory.get("resources") or [] if item.get("transfer") == "bind"
        ],
        "roles_paused": sorted(item.name for item in (root / "agents").iterdir() if item.is_dir())
        if (root / "agents").is_dir()
        else [],
        "needs_acceptance": True,
        "factory_asset": manifest.get("factory_asset"),
    }


def verify_package(src: Path, *, timeout_s: float = 120.0) -> dict[str, Any]:
    del timeout_s
    src = Path(src)
    problems: list[dict[str, str]] = []
    manifest_path = src / "manifest.json"
    if not manifest_path.is_file():
        return {
            "ok": False,
            "problems": [{"kind": "missing", "path": "manifest.json"}],
            "acceptance": "not_run",
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "problems": [{"kind": "manifest", "path": "manifest.json"}],
            "acceptance": "not_run",
        }
    listed = {}
    for item in manifest.get("files") or []:
        if not isinstance(item, dict) or "path" not in item:
            problems.append({"kind": "manifest", "path": "manifest.json"})
            continue
        listed[str(item["path"])] = item
    for path, item in listed.items():
        file = src / path
        if not file.is_file():
            problems.append({"kind": "missing", "path": path})
            continue
        digest = str(item.get("digest") or item.get("sha256") or "")
        size = item.get("size")
        if file.stat().st_size != size or digest_file(file) != digest:
            problems.append({"kind": "mismatch", "path": path})
    for file in sorted(path for path in src.rglob("*") if path.is_file()):
        rel = file.relative_to(src).as_posix()
        if rel == "manifest.json" or rel in listed:
            continue
        problems.append({"kind": "extra", "path": rel})
    if manifest.get("format") == CHECKPOINT_FORMAT:
        inventory = src / "inventory.json"
        if inventory.is_file():
            expect = manifest.get("inventory_digest")
            if expect and digest_bytes(inventory.read_bytes()) != expect:
                problems.append({"kind": "mismatch", "path": "inventory.json"})
    problems.sort(key=lambda item: (item["kind"], item["path"]))
    return {"ok": not problems, "problems": problems, "acceptance": "not_run"}


def export_package(
    checkpoint: Path, dest: Path, *, release: dict[str, Any], by: str = "human", timeout_s: float = 120.0
) -> dict[str, Any]:
    del by, timeout_s
    raise FactoryError("PACKAGE_RELEASE_INVALID", "移交包尚未完成", field="release")


def import_package(
    src: Path,
    root: Path,
    *,
    bindings: dict[str, Any],
    provider: Any = None,
    by: str = "human",
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    del src, root, bindings, provider, by, timeout_s
    raise FactoryError("PACKAGE_BINDING_REQUIRED", "导入尚未完成", key="")


def _reject_remote(root: Path) -> None:
    path = root / "backend-active.json"
    if not path.is_file():
        return
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        body = {}
    database = str((body or {}).get("database") or "")
    blobs = str((body or {}).get("blobs") or "")
    if database in {"postgres", "postgresql"} or blobs in {"s3"}:
        raise FactoryError("BACKUP_UNSUPPORTED", "当前后端不支持完整冷检查点")


def _occupied(path: Path) -> bool:
    return path.exists() and (not path.is_dir() or any(path.iterdir()))


def _reject_busy(ctx: Any) -> None:
    store = LeaseStore(ctx)
    for row in ctx.db.query("SELECT resource FROM leases"):
        resource = str(row["resource"])
        if store.valid(resource) is None:
            continue
        if resource in _BUSY or resource.startswith("worker") or resource.startswith("agent"):
            raise FactoryError("ASSET_CHECKPOINT_BUSY", "工厂不是冷状态", resource=resource)
    running = ctx.db.query(
        "SELECT id FROM jobs WHERE state IN ('RUNNING', 'PROVISIONING', 'GATING', 'AWAITING_APPROVAL')"
    )
    if running:
        raise FactoryError("ASSET_CHECKPOINT_BUSY", "有正在执行的 job", resource="job")
    turns = ctx.db.query("SELECT id FROM agent_turns WHERE state = 'running'")
    if turns:
        raise FactoryError("ASSET_CHECKPOINT_BUSY", "有正在执行的轮次", resource="turn")


def _classify_workspaces(ctx: Any, data: dict[str, Any]) -> list[dict[str, str]]:
    declared = data.get("workspaces") or {}
    omitted: list[dict[str, str]] = []
    agents = ctx.root / "agents"
    if not agents.is_dir():
        return omitted
    for agent_dir in sorted(path for path in agents.iterdir() if path.is_dir()):
        name = agent_dir.name
        spec = declared.get(name)
        workspace = agent_dir / "workspace"
        if not workspace.is_dir():
            continue
        if not isinstance(spec, dict):
            raise FactoryError("ASSET_STATE_UNDECLARED", "岗位工作区未分类", path=f"agents/{name}/workspace")
        business = [str(item) for item in spec.get("business") or []]
        ephemeral = [str(item) for item in spec.get("ephemeral") or []]
        for file in sorted(path for path in workspace.rglob("*") if path.is_file()):
            rel = file.relative_to(workspace).as_posix()
            parts = rel.split("/")
            if any(part in _OMITTED for part in parts):
                omitted.append({"path": f"agents/{name}/workspace/{rel}", "reason": "private"})
                continue
            if _under(rel, business) or _under(rel, ephemeral):
                continue
            raise FactoryError("ASSET_STATE_UNDECLARED", "业务文件未分类", path=rel)
    return omitted


def _under(rel: str, prefixes: list[str]) -> bool:
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in prefixes)


def _copy_business(ctx: Any, part: Path, data: dict[str, Any]) -> None:
    declared = data.get("workspaces") or {}
    for name, spec in declared.items():
        workspace = ctx.root / "agents" / name / "workspace"
        if not workspace.is_dir() or not isinstance(spec, dict):
            continue
        for prefix in spec.get("business") or []:
            source = workspace / str(prefix)
            if not source.exists():
                continue
            dest = part / "business" / name / str(prefix)
            if source.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, dest)
            else:
                shutil.copytree(source, dest, dirs_exist_ok=True)


def _restore_business(src: Path, root: Path) -> None:
    business = src / "business"
    if not business.is_dir():
        return
    for file in business.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(business)
        dest = root / "agents" / rel.parts[0] / "workspace" / Path(*rel.parts[1:])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, dest)


def _mark_restored(root: Path) -> None:
    from mao.base import RandomIds
    from mao.context import Context
    from mao.sandbox import LocalProvider

    ctx = Context.open(root, provider=LocalProvider(root))
    try:
        generation = RandomIds().new("restore")
        config_path = root / "factory.json"
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        assets = config.get("assets")
        if isinstance(assets, dict):
            environment = assets.get("environment")
            if not isinstance(environment, dict):
                environment = {}
            environment["restore_generation"] = generation
            assets["environment"] = environment
            config["assets"] = assets
            config_path.write_text(json.dumps(config), encoding="utf-8")
        with ctx.db.tx() as conn:
            conn.execute("UPDATE agents SET state = 'paused', session = NULL")
            conn.execute("DELETE FROM leases")
            conn.execute("DELETE FROM asset_heads WHERE slot LIKE 'capability:%'")
    finally:
        ctx.close()


def _history_gaps(ctx: Any) -> list[str]:
    return [
        str(row["id"])
        for row in ctx.db.query("SELECT id FROM beans WHERE expired_at IS NOT NULL ORDER BY id")
    ]


def _file_list(part: Path) -> list[dict[str, Any]]:
    found = []
    for file in sorted(path for path in part.rglob("*") if path.is_file()):
        rel = file.relative_to(part).as_posix()
        if rel == "manifest.json":
            continue
        found.append({"path": rel, "digest": digest_file(file), "size": file.stat().st_size})
    return found


def _reject_secrets(root: Path) -> None:
    values = _secret_values(root)
    if not values:
        return
    cas = root / "cas"
    if not cas.is_dir():
        return
    for file in cas.rglob("*"):
        if not file.is_file():
            continue
        blob = file.read_bytes()
        if any(value.encode("utf-8") in blob for value in values):
            raise FactoryError("ASSET_SECRET", "检查点含已知秘密", path="cas")


def _secret_values(root: Path) -> list[str]:
    folder = root / "secrets" / "values"
    if not folder.is_dir():
        return []
    values = []
    for path in folder.iterdir():
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip("\n")
            if value:
                values.append(value)
    return values


def _sqlite_backup(ctx: Any, dest: Path) -> None:
    """另开只读连接复制库，避免在写事务连接上自锁。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{ctx.db.path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
