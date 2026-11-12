"""保留期、墓碑与回收。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.durable import LEASE_TTL_S, LeaseStore
from mao.errors import FactoryError
from mao.jobs import TERMINAL

_SECTION_KEYS = {"beans", "workspaces_days", "logs_days", "sessions_days"}
_TYPE_ID = re.compile(r"^(\*|[A-Za-z0-9][A-Za-z0-9._-]*@[0-9]+)$")
_JOB_TERMINAL = tuple(item.value for item in TERMINAL)
_RUN_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
_PIN_OFF = {"", "false", "0"}


@dataclass(frozen=True)
class RetentionConfig:
    beans: dict[str, int | None]
    workspaces_days: int | None
    logs_days: int | None
    sessions_days: int | None

    @classmethod
    def load(cls, root: Path) -> RetentionConfig:
        section = _section(root)
        if section is None:
            raise FactoryError("GC_NOT_CONFIGURED", "没有 retention 段")
        unknown = sorted(set(section) - _SECTION_KEYS)
        if unknown:
            raise FactoryError("RETENTION_CONFIG_INVALID", "retention 有未知键", field=unknown[0])
        beans = section.get("beans", {})
        if not isinstance(beans, dict):
            raise FactoryError("RETENTION_CONFIG_INVALID", "beans 必须是对象", field="beans")
        parsed: dict[str, int | None] = {}
        for key, value in beans.items():
            if not isinstance(key, str) or _TYPE_ID.fullmatch(key) is None:
                raise FactoryError("RETENTION_CONFIG_INVALID", "类型 id 不合法", field="beans")
            parsed[key] = _days(value, f"beans.{key}")
        return cls(
            beans=parsed,
            workspaces_days=_days(section.get("workspaces_days"), "workspaces_days"),
            logs_days=_days(section.get("logs_days"), "logs_days"),
            sessions_days=_days(section.get("sessions_days"), "sessions_days"),
        )


@dataclass
class GcReport:
    dry_run: bool
    expired: list[str]
    kept: dict[str, int]
    blobs_deleted: int
    bytes_freed: int
    workspaces: list[str]
    logs: list[str]
    sessions: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "expired": list(self.expired),
            "kept": dict(self.kept),
            "blobs_deleted": self.blobs_deleted,
            "bytes_freed": self.bytes_freed,
            "workspaces": list(self.workspaces),
            "logs": list(self.logs),
            "sessions": list(self.sessions),
        }


def collect(ctx: Any, config: RetentionConfig, *, dry_run: bool = False, by: str = "gc") -> GcReport:
    store = LeaseStore(ctx)
    if not store.acquire("gc", owner=ctx.owner, ttl_s=LEASE_TTL_S):
        holder = store.holder("gc")
        raise FactoryError("GC_BUSY", "回收正在进行", owner="" if holder is None else holder.owner)
    try:
        return _collect(ctx, config, dry_run=dry_run, by=by)
    finally:
        store.release("gc", owner=ctx.owner)


def _collect(ctx: Any, config: RetentionConfig, *, dry_run: bool, by: str) -> GcReport:
    now = ctx.clock.now()
    chosen, kept = _select(ctx, config, now)
    if not dry_run:
        for bean_id in chosen:
            ctx.warehouse.expire(bean_id, by=by, reason="retention")
    extra = set(chosen) if dry_run else set()
    blobs_deleted, bytes_freed = _sweep_blobs(ctx, extra, dry_run=dry_run)
    workspaces, logs, sessions = _sweep_dirs(ctx, config, now, dry_run=dry_run)
    report = GcReport(
        dry_run=dry_run,
        expired=sorted(chosen),
        kept=kept,
        blobs_deleted=blobs_deleted,
        bytes_freed=bytes_freed,
        workspaces=workspaces,
        logs=logs,
        sessions=sessions,
    )
    if not dry_run:
        data = report.to_json()
        for key in ("expired", "workspaces", "logs", "sessions"):
            data[key] = len(data[key])
        with ctx.db.tx() as conn:
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'gc.done', NULL, ?)",
                (ctx.ids.new("ev"), utc_iso(now), canonical_json(data)),
            )
    return report


def _select(ctx: Any, config: RetentionConfig, now: Any) -> tuple[list[str], dict[str, int]]:
    kept = {"pinned": 0, "in_use": 0, "quarantined": 0}
    used = _referenced(ctx)
    quarantined = {str(row["bean_id"]) for row in ctx.db.query("SELECT bean_id FROM quarantines")}
    expired: list[str] = []
    for bean in ctx.warehouse.list(sealed_only=True, include_expired=False):
        days = _bean_days(config, bean.type)
        if days is None:
            continue
        if bean.created_at > utc_iso(now - timedelta(days=days)):
            continue
        pin = ctx.warehouse.labels(bean.id).get("pin")
        if pin is not None and pin not in _PIN_OFF:
            kept["pinned"] += 1
            continue
        if bean.id in quarantined:
            kept["quarantined"] += 1
            continue
        if bean.id in used:
            kept["in_use"] += 1
            continue
        expired.append(bean.id)
    return expired, kept


def _bean_days(config: RetentionConfig, type_ref: str) -> int | None:
    if "@" not in type_ref:
        return config.beans.get("*")
    name, ver = type_ref.split("@", 1)
    type_id = f"{name}@{ver.split('.', 1)[0]}"
    if type_id in config.beans:
        return config.beans[type_id]
    return config.beans.get("*")


def _referenced(ctx: Any) -> set[str]:
    found: set[str] = set()
    jobs = ctx.db.query(
        "SELECT inputs FROM jobs WHERE state NOT IN (" + ",".join("?" * len(_JOB_TERMINAL)) + ")",
        _JOB_TERMINAL,
    )
    runs = ctx.db.query(
        "SELECT inputs FROM runs WHERE state NOT IN (" + ",".join("?" * len(_RUN_TERMINAL)) + ")",
        _RUN_TERMINAL,
    )
    approvals = ctx.db.query("SELECT payload FROM approvals WHERE status = 'pending'")
    for row in list(jobs) + list(runs):
        found.update(_strings(_loads(row["inputs"])))
    for row in approvals:
        found.update(_strings(_loads(row["payload"])))
    return found


def _sweep_blobs(ctx: Any, extra: set[str], *, dry_run: bool) -> tuple[int, int]:
    deleted = 0
    freed = 0
    while True:
        dead = [
            (digest, size)
            for digest, size in _dead_digests(ctx, extra)
            if ctx.warehouse.blob_path(digest).is_file()
        ][:1000]
        if not dead:
            return deleted, freed
        with ctx.db.tx():
            for digest, size in dead:
                if dry_run or ctx.warehouse.delete_blob(digest):
                    deleted += 1
                    freed += size
        if dry_run:
            return deleted, freed


def _dead_digests(ctx: Any, extra: set[str]) -> list[tuple[str, int]]:
    rows = ctx.db.query(
        "SELECT pf.digest, pf.size, pf.bean_id, b.expired_at FROM payload_files pf "
        "JOIN beans b ON b.id = pf.bean_id"
    )
    grouped: dict[str, list[tuple[bool, int]]] = {}
    for row in rows:
        expired = row["expired_at"] is not None or str(row["bean_id"]) in extra
        grouped.setdefault(str(row["digest"]), []).append((expired, int(row["size"])))
    dead = [
        (digest, items[0][1]) for digest, items in grouped.items() if items and all(item[0] for item in items)
    ]
    dead.sort()
    return dead


def _sweep_dirs(
    ctx: Any, config: RetentionConfig, now: Any, *, dry_run: bool
) -> tuple[list[str], list[str], list[str]]:
    workspaces: list[str] = []
    logs: list[str] = []
    sessions: list[str] = []
    jobs_root = ctx.root / "jobs"
    if config.workspaces_days is not None:
        cutoff = utc_iso(now - timedelta(days=config.workspaces_days))
        for row in ctx.db.query(
            "SELECT id, attempt, updated_at FROM jobs WHERE state IN ("
            + ",".join("?" * len(_JOB_TERMINAL))
            + ") AND updated_at <= ?",
            (*_JOB_TERMINAL, cutoff),
        ):
            if _clean_job(
                ctx,
                jobs_root,
                str(row["id"]),
                int(row["attempt"]),
                ("in", "out", "workspace", "tmp"),
                dry_run,
            ):
                workspaces.append(str(row["id"]))
    if config.logs_days is not None:
        cutoff = utc_iso(now - timedelta(days=config.logs_days))
        for row in ctx.db.query(
            "SELECT id, attempt FROM jobs WHERE state IN ("
            + ",".join("?" * len(_JOB_TERMINAL))
            + ") AND updated_at <= ?",
            (*_JOB_TERMINAL, cutoff),
        ):
            if _clean_job(ctx, jobs_root, str(row["id"]), int(row["attempt"]), ("logs",), dry_run):
                logs.append(str(row["id"]))
    if config.sessions_days is not None:
        cutoff = utc_iso(now - timedelta(days=config.sessions_days))
        for row in ctx.db.query(
            "SELECT id FROM foreman_sessions WHERE state != 'running' AND ended_at IS NOT NULL "
            "AND ended_at <= ?",
            (cutoff,),
        ):
            target = ctx.root / "foreman" / "sessions" / str(row["id"])
            if _remove_contained(target, ctx.root / "foreman" / "sessions", dry_run):
                sessions.append(str(row["id"]))
    return sorted(workspaces), sorted(logs), sorted(sessions)


def _clean_job(
    ctx: Any, jobs_root: Path, job_id: str, attempt: int, names: tuple[str, ...], dry_run: bool
) -> bool:
    removed = False
    job_dir = jobs_root / job_id
    attempts = [job_dir / str(number) for number in range(1, attempt + 1) if (job_dir / str(number)).exists()]
    if not attempts and job_dir.is_dir():
        attempts = [path for path in job_dir.iterdir() if path.name.isdigit()]
    for attempt_dir in attempts:
        for name in names:
            if _remove_contained(attempt_dir / name, jobs_root, dry_run):
                removed = True
        if "workspace" in names and not dry_run:
            _remove_volumes(ctx, job_id, attempt_dir)
    return removed


def _remove_volumes(ctx: Any, job_id: str, attempt_dir: Path) -> None:
    provider = ctx.provider
    if not hasattr(provider, "remove_volume"):
        return
    number = attempt_dir.name
    names = [f"mao-{job_id}-ws"]
    if number != "1":
        names.append(f"mao-{job_id}-ws-{number}")
    for name in names:
        provider.remove_volume(name)


def _remove_contained(path: Path, root: Path, dry_run: bool) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    try:
        path.parent.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    if dry_run:
        return True
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    _remove_tree(path)
    return True


def _remove_tree(path: Path) -> None:
    for child in list(path.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            _remove_tree(child)
    path.rmdir()


def _section(root: Path) -> dict[str, Any] | None:
    path = Path(root) / "factory.json"
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    section = body.get("retention") if isinstance(body, dict) else None
    if not isinstance(section, dict):
        return None
    return section


def _days(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FactoryError("RETENTION_CONFIG_INVALID", "天数不合法", field=field)
    return value


def _loads(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found.update(_strings(item))
        return found
    if isinstance(value, dict):
        found = set()
        for item in value.values():
            found.update(_strings(item))
        return found
    return set()
