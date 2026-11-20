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
from mao.base import canonical_bytes, canonical_json, digest_bytes, digest_file, utc_iso
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
            "closure": _closure_or_shallow(ctx, factory_asset),
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
    checkpoint = Path(checkpoint)
    dest = Path(dest)
    report = verify_package(checkpoint)
    if not report["ok"]:
        raise FactoryError("PACKAGE_CORRUPT", "检查点不能导出", problems=report["problems"])
    inventory = json.loads((checkpoint / "inventory.json").read_text(encoding="utf-8"))
    _check_release(inventory, release)
    if _occupied(dest) or Path(str(dest) + ".part").exists():
        raise FactoryError("PACKAGE_TARGET_NOT_EMPTY", "移交包目标非空", path=str(dest))
    part = Path(str(dest) + ".part")
    try:
        part.mkdir(parents=True)
        _write_package(checkpoint, part, inventory, release)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(part, dest)
        part = None
    finally:
        if part is not None and part.exists():
            shutil.rmtree(part, ignore_errors=True)
    return json.loads((dest / "manifest.json").read_text(encoding="utf-8"))


def import_package(
    src: Path,
    root: Path,
    *,
    bindings: dict[str, Any],
    provider: Any = None,
    by: str = "human",
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    del provider, by, timeout_s
    src = Path(src)
    root = Path(root)
    report = verify_package(src)
    if not report["ok"]:
        raise FactoryError("PACKAGE_CORRUPT", "移交包校验失败", problems=report["problems"])
    package_bindings = json.loads((src / "bindings.json").read_text(encoding="utf-8"))
    _require_bindings(package_bindings, bindings, src)
    if root.exists():
        raise FactoryError("PACKAGE_TARGET_NOT_EMPTY", "导入目标非空", path=str(root))
    part = root.with_name(root.name + ".part")
    try:
        if part.exists():
            shutil.rmtree(part)
        _install_package(src, part, bindings)
        os.replace(part, root)
        part = None
    finally:
        if part is not None and part.exists():
            shutil.rmtree(part, ignore_errors=True)
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    return {
        "root": manifest.get("root") or "",
        "state": "needs_acceptance",
        "contracts": [],
        "bindings_missing": [],
        "roles_paused": [],
    }


def _check_release(inventory: dict[str, Any], release: dict[str, Any]) -> None:
    parts = {
        "closure": inventory.get("closure") or {},
        "registry": release.get("registry"),
        "contracts": release.get("contracts") or [],
        "bindings": release.get("bindings") or [],
        "omitted": release.get("omitted") or [],
    }
    review = release.get("review") or {}
    if digest_bytes(canonical_bytes(parts)) != review.get("closure_digest"):
        raise FactoryError("PACKAGE_RELEASE_INVALID", "披露审核与闭包不符", field="review")
    for resource in inventory.get("resources") or []:
        if resource.get("required") and resource.get("transfer") == "unavailable":
            raise FactoryError("PACKAGE_RELEASE_INVALID", "必需资源不能交付", field="resources")


def _open_backup(checkpoint: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(checkpoint / "backup" / "factory.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _write_package(checkpoint: Path, dest: Path, inventory: dict[str, Any], release: dict[str, Any]) -> None:
    conn = _open_backup(checkpoint)
    try:
        closure = inventory.get("closure") or {}
        asset_ids = list(closure.get("assets") or [])
        documents = []
        for asset_id in asset_ids:
            row = conn.execute("SELECT document FROM asset_records WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                raise FactoryError("PACKAGE_RELEASE_INVALID", "闭包资产不在检查点中", field="closure")
            document = json.loads(row["document"])
            for item in document.get("evidence") or []:
                if item.get("kind") in {"job", "turn", "run", "receipt", "trial"} and not (
                    document.get("kind") == "evidence"
                    and (document.get("data") or {}).get("historical_only") is True
                ):
                    raise FactoryError("PACKAGE_RELEASE_INVALID", "公开闭包含裸执行引用", field="evidence")
            documents.append({"id": asset_id, "document": document})
        documents.sort(key=lambda item: item["id"])
        (dest / "catalog.json").write_text(canonical_json(documents), encoding="utf-8")
        beans = _bean_views(conn, list(closure.get("beans") or []))
        (dest / "beans.json").write_text(canonical_json(beans), encoding="utf-8")
        for digest in closure.get("blobs") or []:
            _copy_blob(checkpoint, dest, str(digest))
        registry_id = str(release.get("registry") or "")
        registry_row = next((item for item in documents if item["id"] == registry_id), None)
        if registry_row is not None:
            bean_ids = registry_row["document"].get("beans") or []
            if bean_ids:
                _extract_tree(conn, checkpoint, dest / "registry", str(bean_ids[0]))
        (dest / "bindings.json").write_text(
            canonical_json({"resources": release.get("bindings") or []}),
            encoding="utf-8",
        )
        files = _file_list(dest)
        manifest = {
            "format": PACKAGE_FORMAT,
            "created_at": utc_iso_now(),
            "root": release.get("root"),
            "registry": release.get("registry"),
            "contracts": release.get("contracts") or [],
            "review": release.get("review") or {},
            "omitted": release.get("omitted") or [],
            "files": files,
        }
        (dest / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    finally:
        conn.close()


def utc_iso_now() -> str:
    from mao.base import SystemClock

    return utc_iso(SystemClock().now())


def _bean_views(conn: sqlite3.Connection, bean_ids: list[str]) -> list[dict[str, Any]]:
    found = []
    for bean_id in bean_ids:
        row = conn.execute(
            "SELECT id, type, meta_json, facts_json FROM beans WHERE id = ?", (bean_id,)
        ).fetchone()
        if row is None:
            continue
        payload = [
            {"path": str(item["path"]), "digest": str(item["digest"]), "size": int(item["size"])}
            for item in conn.execute(
                "SELECT path, digest, size FROM payload_files WHERE bean_id = ? ORDER BY path",
                (bean_id,),
            )
        ]
        found.append(
            {
                "id": str(row["id"]),
                "type": str(row["type"]),
                "meta": json.loads(row["meta_json"]),
                "facts": json.loads(row["facts_json"]),
                "payload": payload,
                "sources": [],
                "proofs": [],
            }
        )
    found.sort(key=lambda item: item["id"])
    return found


def _copy_blob(checkpoint: Path, dest: Path, digest: str) -> None:
    hexpart = digest.split(":", 1)[-1]
    src = checkpoint / "backup" / "cas" / "sha256" / hexpart[:2] / hexpart[2:]
    if not src.is_file():
        raise FactoryError("PACKAGE_RELEASE_INVALID", "闭包缺少载荷", field="blobs")
    target = dest / "blobs" / "sha256" / hexpart[:2] / hexpart[2:]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)


def _extract_tree(conn: sqlite3.Connection, checkpoint: Path, dest: Path, bean_id: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for item in conn.execute("SELECT path, digest FROM payload_files WHERE bean_id = ?", (bean_id,)):
        rel = str(item["path"]).replace("\\", "/")
        if rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
            raise FactoryError("PACKAGE_RELEASE_INVALID", "Registry 路径不安全", field="registry")
        hexpart = str(item["digest"]).split(":", 1)[-1]
        src = checkpoint / "backup" / "cas" / "sha256" / hexpart[:2] / hexpart[2:]
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)


def _require_bindings(package_bindings: dict[str, Any], bindings: dict[str, Any], src: Path) -> None:
    provided = bindings.get("resources") if isinstance(bindings, dict) else None
    if not isinstance(provided, dict):
        provided = {}
    for item in package_bindings.get("resources") or []:
        if not item.get("required"):
            continue
        key = str(item.get("key"))
        bound = provided.get(key)
        if not isinstance(bound, dict) or bound.get("kind") != item.get("kind"):
            raise FactoryError("PACKAGE_BINDING_REQUIRED", "缺少必需资源绑定", key=key)
        if item.get("kind") == "secret" or str(key).startswith("secret:"):
            ref = bound.get("ref")
            if not isinstance(ref, str) or not ref or _inside(Path(ref), src):
                raise FactoryError("PACKAGE_BINDING_REQUIRED", "秘密绑定必须是包外文件", key=key)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _install_package(src: Path, root: Path, bindings: dict[str, Any]) -> None:
    import subprocess

    from mao.secrets import normalize_value, store_secret

    root.mkdir(parents=True)
    registry = root / "registry"
    package_registry = src / "registry"
    if package_registry.is_dir():
        shutil.copytree(package_registry, registry)
    else:
        registry.mkdir()
    config = dict(bindings.get("factory_config") or {})
    governance = dict(config.get("governance") or {})
    governance["registry"] = str(registry)
    config["governance"] = governance
    (root / "factory.json").write_text(json.dumps(config), encoding="utf-8")
    for item in json.loads((src / "bindings.json").read_text(encoding="utf-8")).get("resources") or []:
        if item.get("kind") != "secret" and not str(item.get("key", "")).startswith("secret:"):
            continue
        key = str(item["key"])
        bound = (bindings.get("resources") or {}).get(key) or {}
        ref = bound.get("ref")
        if not isinstance(ref, str):
            continue
        name = str(item.get("name") or key.split(":", 1)[-1])
        value = normalize_value(Path(ref).read_bytes())
        store_secret(root, name, str(bound.get("secret_class") or "other"), value)
    subprocess.run(["git", "init", "-q"], cwd=registry, check=True)
    subprocess.run(["git", "add", "-A"], cwd=registry, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "buyer registry"], cwd=registry, check=True)
    from mao.context import Context
    from mao.sandbox import LocalProvider

    ctx = Context.open(root, provider=LocalProvider(root))
    try:
        catalog = json.loads((src / "catalog.json").read_text(encoding="utf-8"))
        with ctx.db.tx() as conn:
            for item in catalog:
                conn.execute(
                    "INSERT INTO asset_records (id, document, by_actor, created_at) VALUES (?, ?, ?, ?)",
                    (item["id"], canonical_json(item["document"]), "import", utc_iso_now()),
                )
            conn.execute("UPDATE agents SET state = 'paused', session = NULL")
    finally:
        ctx.close()


def _closure_or_shallow(ctx: Any, factory_asset: str) -> dict[str, Any]:
    try:
        return AssetStore(ctx).closure([factory_asset])
    except FactoryError as exc:
        if exc.code != "ASSET_REFERENCE_INVALID":
            raise
    assets = {factory_asset}
    pending = [factory_asset]
    while pending:
        asset_id = pending.pop()
        record = AssetStore(ctx).get(asset_id)
        for ref in record["document"].get("requires") or []:
            if ref not in assets:
                assets.add(ref)
                pending.append(ref)
    return {"assets": sorted(assets), "beans": [], "blobs": [], "evidence": [], "epochs": []}


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


def _mounted_workspace(ctx: Any, name: str) -> Path:
    from mao.agents import workspace_path

    return workspace_path(ctx, name)


def _classify_workspaces(ctx: Any, data: dict[str, Any]) -> list[dict[str, str]]:
    declared = data.get("workspaces") or {}
    omitted: list[dict[str, str]] = []
    agents = ctx.root / "agents"
    if not agents.is_dir():
        return omitted
    for agent_dir in sorted(path for path in agents.iterdir() if path.is_dir()):
        name = agent_dir.name
        spec = declared.get(name)
        workspace = _mounted_workspace(ctx, name)
        if not workspace.is_dir():
            continue
        if not isinstance(spec, dict):
            raise FactoryError(
                "ASSET_STATE_UNDECLARED", "岗位工作区未分类", path=workspace.relative_to(ctx.root).as_posix()
            )
        business = [str(item) for item in spec.get("business") or []]
        ephemeral = [str(item) for item in spec.get("ephemeral") or []]
        for file in sorted(path for path in workspace.rglob("*") if path.is_file()):
            rel = file.relative_to(workspace).as_posix()
            parts = rel.split("/")
            shown = file.relative_to(ctx.root).as_posix()
            if any(part in _OMITTED for part in parts):
                omitted.append({"path": shown, "reason": "private"})
                continue
            if _under(rel, business) or _under(rel, ephemeral):
                continue
            raise FactoryError("ASSET_STATE_UNDECLARED", "业务文件未分类", path=shown)
    return omitted


def _under(rel: str, prefixes: list[str]) -> bool:
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in prefixes)


def _copy_business(ctx: Any, part: Path, data: dict[str, Any]) -> None:
    declared = data.get("workspaces") or {}
    for name, spec in declared.items():
        workspace = _mounted_workspace(ctx, name)
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
    mounts = _restored_mounts(root)
    for file in business.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(business)
        agent = rel.parts[0]
        mount = mounts.get(agent, f"agents/{agent}/workspace")
        dest = root / mount / Path(*rel.parts[1:])
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, dest)


def _restored_mounts(root: Path) -> dict[str, str]:
    from mao.db import Database

    path = root / "factory.db"
    if not path.is_file():
        return {}
    db = Database(path)
    try:
        return {
            str(row["agent"]): str(row["current_path"])
            for row in db.query("SELECT agent, current_path FROM agent_workspaces")
            if row["current_path"]
        }
    finally:
        db.close()


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
