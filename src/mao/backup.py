"""备份、校验与恢复。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

from mao.base import canonical_json, digest_file, utc_iso
from mao.durable import LEASE_TTL_S, LeaseKeeper, LeaseStore
from mao.errors import FactoryError

FORMAT = "mao-backup/1"
_CONFIG_FILES = (
    ("factory.json", "config/factory.json"),
    ("egress/upstreams.json", "config/egress/upstreams.json"),
    ("egress/prices.json", "config/egress/prices.json"),
    ("secrets/manifest.json", "config/secrets/manifest.json"),
)


def create_backup(ctx: Any, dest: Path, *, with_logs: bool = False, by: str = "human") -> dict[str, Any]:
    dest = Path(dest)
    if (ctx.root / "backend-active.json").is_file():
        raise FactoryError("BACKUP_UNSUPPORTED", "当前后端不支持本地备份")
    if _nonempty(dest):
        raise FactoryError("BACKUP_TARGET_NOT_EMPTY", "备份目标非空", path=str(dest))
    store = LeaseStore(ctx)
    if not store.acquire("gc", owner=ctx.owner, ttl_s=LEASE_TTL_S):
        holder = store.holder("gc")
        raise FactoryError("BACKUP_BUSY", "回收正在进行", owner="" if holder is None else holder.owner)
    keeper = LeaseKeeper(store, "gc", owner=ctx.owner)
    keeper.start()
    part = Path(str(dest) + ".part")
    try:
        if part.exists():
            shutil.rmtree(part) if part.is_dir() else part.unlink()
        part.mkdir(parents=True)
        ctx.db.backup_to(part / "factory.db")
        _copy_payload(ctx, part)
        _copy_config(ctx.root, part)
        _copy_epochs(ctx.root, part)
        registry = _registry_path(ctx.root)
        registry_doc = None
        if registry is not None:
            _git(registry, "bundle", "create", str(part / "registry.bundle"), "--all")
            registry_doc = _registry_doc(registry)
        if with_logs:
            _copy_logs(ctx.root, part)
        created_at = utc_iso(ctx.clock.now())
        manifest = _manifest(
            part,
            created_at=created_at,
            by=by,
            source_root=str(ctx.root),
            registry=registry_doc,
            secrets=_secret_names(ctx.root),
            with_logs=with_logs,
        )
        _drop_sidecars(part / "factory.db")
        _atomic_write(part / "manifest.json", canonical_json(manifest).encode("utf-8"))
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        os.replace(part, dest)
        part = None
        _event(
            ctx,
            "backup.created",
            {
                "dest": str(dest),
                "files": len(manifest["files"]),
                "bytes": sum(item["size"] for item in manifest["files"]),
            },
        )
        return manifest
    finally:
        keeper.stop()
        store.release("gc", owner=ctx.owner)
        if part is not None and part.exists():
            shutil.rmtree(part, ignore_errors=True)


def verify_backup(dest: Path, *, timeout_s: float = 120.0) -> dict[str, Any]:
    dest = Path(dest)
    manifest, problem = _read_manifest(dest)
    if problem is not None:
        return {"ok": False, "problems": [problem]}
    problems: list[dict[str, str]] = []
    listed = {item["path"]: item for item in manifest["files"]}
    for path, item in listed.items():
        file = dest / path
        if not file.is_file():
            problems.append({"kind": "missing", "path": path})
            continue
        if file.stat().st_size != item["size"] or digest_file(file) != item["sha256"]:
            problems.append({"kind": "mismatch", "path": path})
    for file in sorted(p for p in dest.rglob("*") if p.is_file()):
        rel = file.relative_to(dest).as_posix()
        if rel == "manifest.json" or rel in listed:
            continue
        problems.append({"kind": "extra", "path": rel})
    db_path = dest / "factory.db"
    if db_path.is_file():
        problems.extend(_database_problems(db_path))
        problems.extend(_blob_problems(dest, db_path))
    if manifest.get("registry") is not None:
        bundle = dest / "registry.bundle"
        if bundle.is_file() and not _bundle_ok(bundle, timeout_s):
            problems.append({"kind": "bundle", "path": "registry.bundle"})
    problems.extend(_secret_problems(dest, listed))
    problems.sort(key=lambda item: (item["kind"], item["path"]))
    return {"ok": not problems, "problems": problems}


def restore_backup(
    dest: Path, root: Path, *, registry_dest: Path | None = None, timeout_s: float = 120.0
) -> dict[str, Any]:
    dest = Path(dest)
    root = Path(root)
    report = verify_backup(dest, timeout_s=timeout_s)
    if not report["ok"]:
        raise FactoryError("BACKUP_CORRUPT", "备份校验失败", problems=report["problems"])
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    registry_dest = Path(registry_dest) if registry_dest is not None else root / "registry"
    if _nonempty(root):
        raise FactoryError("BACKUP_TARGET_NOT_EMPTY", "恢复目标非空", path=str(root))
    if manifest.get("registry") is not None and _nonempty(registry_dest):
        raise FactoryError("BACKUP_TARGET_NOT_EMPTY", "仓库目标非空", path=str(registry_dest))
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dest / "factory.db", root / "factory.db")
    if (dest / "cas").is_dir():
        shutil.copytree(dest / "cas", root / "cas", dirs_exist_ok=True)
    _restore_epochs(dest, root)
    _restore_config(dest, root)
    _restore_logs(dest, root)
    if manifest.get("registry") is not None:
        _restore_registry(dest, registry_dest, manifest["registry"], timeout_s)
        _point_registry(root, registry_dest)
    from mao.context import Context
    from mao.sandbox import LocalProvider

    ctx = Context.open(root, provider=LocalProvider(root))
    try:
        with ctx.db.tx() as conn:
            conn.execute("DELETE FROM leases")
        epochs = _materialize_retained(ctx, registry_dest if manifest.get("registry") else None)
        problems = ctx.warehouse.verify()
        if problems:
            raise FactoryError("BACKUP_CORRUPT", "恢复后的仓库校验失败", problems=problems)
        _event(ctx, "backup.restored", {"from": str(dest), "created_at": manifest["created_at"]})
    finally:
        ctx.close()
    return {
        "root": str(root),
        "registry": None if manifest.get("registry") is None else str(registry_dest),
        "epochs": epochs,
        "secrets_missing": list(manifest.get("secrets") or []),
    }


def _nonempty(path: Path) -> bool:
    if not path.exists():
        return False
    if not path.is_dir():
        return True
    return any(path.iterdir())


def _copy_payload(ctx: Any, part: Path) -> None:
    needed = _live_digests(part / "factory.db")
    cas = ctx.root / "cas"
    copied: set[str] = set()
    if cas.is_dir():
        for src in cas.rglob("*"):
            if not src.is_file() or src.is_symlink():
                continue
            rel = src.relative_to(ctx.root).as_posix()
            dest = part / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            copied.add(rel)
            if rel.startswith("cas/sha256/") and src.parent.name and len(src.name) == 62:
                expect = "sha256:" + src.parent.name + src.name
                if digest_file(dest) != expect:
                    raise FactoryError("BACKUP_SOURCE_CORRUPT", "CAS 摘要不符", path=rel)
    for digest in sorted(needed):
        hexpart = digest.split(":", 1)[-1]
        rel = f"cas/sha256/{hexpart[:2]}/{hexpart[2:]}"
        if rel not in copied:
            raise FactoryError("BACKUP_SOURCE_CORRUPT", "CAS 缺少被引用的文件", path=rel)


def _copy_config(root: Path, part: Path) -> None:
    for src_rel, dest_rel in _CONFIG_FILES:
        src = root / src_rel
        if src.is_file() and not src.is_symlink():
            dest = part / dest_rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)


def _copy_epochs(root: Path, part: Path) -> None:
    epochs = root / "epochs"
    if not epochs.is_dir():
        return
    for commit_dir in epochs.iterdir():
        if not commit_dir.is_dir():
            continue
        for name in ("resolutions.json", "resolution-runtime.json"):
            src = commit_dir / name
            if src.is_file() and not src.is_symlink():
                dest = part / "epochs" / commit_dir.name / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)


def _copy_logs(root: Path, part: Path) -> None:
    jobs = root / "jobs"
    if jobs.is_dir():
        for job in jobs.iterdir():
            if not job.is_dir():
                continue
            for attempt in job.iterdir():
                logs = attempt / "logs"
                if logs.is_dir():
                    _copy_tree(logs, part / "logs" / "jobs" / job.name / attempt.name)
    sessions = root / "foreman" / "sessions"
    if sessions.is_dir():
        for session in sessions.iterdir():
            logs = session / "logs"
            if logs.is_dir():
                _copy_tree(logs, part / "logs" / "foreman" / "sessions" / session.name)


def _copy_tree(src: Path, dest: Path) -> None:
    for file in src.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        target = dest / file.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)


def _manifest(
    part: Path,
    *,
    created_at: str,
    by: str,
    source_root: str,
    registry: dict[str, Any] | None,
    secrets: list[str],
    with_logs: bool,
) -> dict[str, Any]:
    files = []
    for file in sorted(p for p in part.rglob("*") if p.is_file()):
        rel = file.relative_to(part).as_posix()
        files.append({"path": rel, "sha256": digest_file(file), "size": file.stat().st_size})
    files.sort(key=lambda item: item["path"])
    return {
        "format": FORMAT,
        "created_at": created_at,
        "by": by,
        "source_root": source_root,
        "schema_version": _user_version(part / "factory.db"),
        "files": files,
        "registry": registry,
        "secrets": secrets,
        "with_logs": with_logs,
    }


def _read_manifest(dest: Path) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    path = dest / "manifest.json"
    if not path.is_file():
        return None, {"kind": "manifest", "path": "manifest.json"}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {"kind": "manifest", "path": "manifest.json"}
    if not isinstance(body, dict) or body.get("format") != FORMAT:
        return None, {"kind": "manifest", "path": "manifest.json"}
    return body, None


def _sqlite_ro(path: Path) -> sqlite3.Connection:
    uri = "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _drop_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.exists():
            side.unlink()


def _database_problems(path: Path) -> list[dict[str, str]]:
    conn = _sqlite_ro(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
        _drop_sidecars(path)
    if row is None or str(row[0]) != "ok":
        return [{"kind": "database", "path": "factory.db"}]
    return []


def _blob_problems(dest: Path, db_path: Path) -> list[dict[str, str]]:
    problems = []
    for digest in sorted(_live_digests(db_path)):
        hexpart = digest.split(":", 1)[-1]
        rel = f"cas/sha256/{hexpart[:2]}/{hexpart[2:]}"
        if not (dest / rel).is_file():
            problems.append({"kind": "blob", "path": digest})
    return problems


def _secret_problems(dest: Path, listed: dict[str, Any]) -> list[dict[str, str]]:
    problems = []
    seen: set[str] = set()
    for rel in list(listed) + [p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()]:
        if rel in seen:
            continue
        seen.add(rel)
        if "secrets/values" in rel or Path(rel).name == "console.token":
            problems.append({"kind": "secret", "path": rel})
    return problems


def _live_digests(db_path: Path) -> set[str]:
    conn = _sqlite_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT pf.digest FROM payload_files pf JOIN beans b ON b.id = pf.bean_id "
            "WHERE b.expired_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
        _drop_sidecars(db_path)
    return {str(row["digest"]) for row in rows}


def _user_version(db_path: Path) -> int:
    conn = _sqlite_ro(db_path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
        _drop_sidecars(db_path)


def _registry_path(root: Path) -> Path | None:
    path = root / "factory.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    governance = body.get("governance") if isinstance(body, dict) else None
    if not isinstance(governance, dict) or not governance.get("registry"):
        return None
    registry = Path(str(governance["registry"]))
    if not registry.is_dir():
        return None
    return registry


def _registry_doc(registry: Path) -> dict[str, Any]:
    refs: dict[str, str] = {}
    for line in _git(registry, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        if " " not in line:
            continue
        name, oid = line.split(" ", 1)
        refs[name] = oid
    head = None
    try:
        head = _git(registry, "symbolic-ref", "HEAD").strip() or None
    except FactoryError:
        head = None
    return {"bundle": "registry.bundle", "head": head, "refs": refs}


def _secret_names(root: Path) -> list[str]:
    path = root / "secrets" / "manifest.json"
    if not path.is_file():
        return []
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(body, dict):
        return []
    return sorted(str(key) for key in body)


def _restore_epochs(dest: Path, root: Path) -> None:
    epochs = dest / "epochs"
    if not epochs.is_dir():
        return
    for file in epochs.rglob("*"):
        if file.is_file() and file.name in {"resolutions.json", "resolution-runtime.json"}:
            target = root / file.relative_to(dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)


def _restore_config(dest: Path, root: Path) -> None:
    for src_rel, dest_rel in _CONFIG_FILES:
        src = dest / dest_rel
        if src.is_file():
            target = root / src_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)


def _restore_logs(dest: Path, root: Path) -> None:
    jobs = dest / "logs" / "jobs"
    if jobs.is_dir():
        for job in jobs.iterdir():
            for attempt in job.iterdir() if job.is_dir() else []:
                _copy_tree(attempt, root / "jobs" / job.name / attempt.name / "logs")
    sessions = dest / "logs" / "foreman" / "sessions"
    if sessions.is_dir():
        for session in sessions.iterdir():
            if session.is_dir():
                _copy_tree(session, root / "foreman" / "sessions" / session.name / "logs")


def _restore_registry(dest: Path, registry_dest: Path, registry: dict[str, Any], timeout_s: float) -> None:
    registry_dest.mkdir(parents=True, exist_ok=True)
    _git(registry_dest, "init", timeout_s=timeout_s)
    bundle = dest / "registry.bundle"
    _git(registry_dest, "fetch", str(bundle), "+refs/*:refs/*", timeout_s=timeout_s)
    head = registry.get("head")
    if head:
        _git(registry_dest, "symbolic-ref", "HEAD", str(head), timeout_s=timeout_s)
        _git(registry_dest, "reset", "--hard", timeout_s=timeout_s)
    problems = []
    for name, oid in sorted((registry.get("refs") or {}).items()):
        try:
            got = _git(registry_dest, "rev-parse", name, timeout_s=timeout_s).strip()
        except FactoryError:
            got = ""
        if got != oid:
            problems.append({"kind": "bundle", "path": name})
    if problems:
        raise FactoryError("BACKUP_CORRUPT", "恢复后的引用不一致", problems=problems)


def _point_registry(root: Path, registry_dest: Path) -> None:
    path = root / "factory.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    governance = body.setdefault("governance", {})
    governance["registry"] = str(registry_dest.resolve())
    _atomic_write(path, canonical_json(body).encode("utf-8"))


def _materialize_retained(ctx: Any, registry_dest: Path | None) -> list[str]:
    from mao.governance import EpochStore

    store = EpochStore(ctx)
    retained = [item.commit for item in store.list() if item.retain]
    for commit in retained:
        checkout = ctx.root / "epochs" / commit / "registry"
        if not checkout.is_dir() and registry_dest is not None:
            _extract_commit(registry_dest, commit, checkout)
        store.materialize(commit)
    return sorted(retained)


def _extract_commit(registry: Path, commit: str, dest: Path) -> None:
    data = _git_bytes(registry, "archive", "--format=tar", commit)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=BytesIO(data), mode="r:") as archive:
        for member in archive.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise FactoryError(
                    "BACKUP_CORRUPT", "归档路径不合法", problems=[{"kind": "bundle", "path": member.name}]
                )
        archive.extractall(dest, filter="data")


def _bundle_ok(bundle: Path, timeout_s: float) -> bool:
    try:
        proc = subprocess.run(
            ["git", "bundle", "verify", str(bundle)],
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _git(repo: Path, *args: str, timeout_s: float = 120.0) -> str:
    command = ["git", "-C", str(repo), *args]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FactoryError("GIT_FAILED", "git 超时", args=list(command), stderr="") from exc
    if proc.returncode != 0:
        raise FactoryError("GIT_FAILED", "git 失败", args=list(command), stderr=(proc.stderr or "")[:4000])
    return proc.stdout


def _git_bytes(repo: Path, *args: str, timeout_s: float = 120.0) -> bytes:
    command = ["git", "-C", str(repo), *args]
    proc = subprocess.run(command, capture_output=True, timeout=timeout_s, check=False)
    if proc.returncode != 0:
        raise FactoryError(
            "GIT_FAILED",
            "git 失败",
            args=list(command),
            stderr=proc.stderr.decode("utf-8", errors="replace")[:4000],
        )
    return proc.stdout


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    part.write_bytes(payload)
    os.replace(part, path)


def _event(ctx: Any, kind: str, data: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
            (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(data)),
        )
