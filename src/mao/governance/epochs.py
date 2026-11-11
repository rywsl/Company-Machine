"""Epoch 存储、指针、回滚与宪法采纳。"""

from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError
from mao.governance.git import git, git_bytes
from mao.snapshot import Snapshot

_POINTERS = ("active", "candidate", "previous")
_RUN_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
_JOB_TERMINAL = ("SEALED", "DEAD")
_CS_TERMINAL = ("rejected", "rolled-back", "withdrawn")
_COL = '"commit"'


@dataclass(frozen=True)
class Epoch:
    commit: str
    tree: str
    built_at: str
    retain: bool


class EpochStore:
    def __init__(
        self, ctx: Any, *, registry: Path | None = None, git_bin: str = "git", timeout_s: float | None = None
    ) -> None:
        del git_bin
        self.ctx = ctx
        self._cache: dict[str, Snapshot] = {}
        config = _governance(ctx.root)
        self.registry = Path(registry) if registry is not None else Path(str(config.get("registry", "")))
        self.timeout_s = float(timeout_s if timeout_s is not None else config.get("git_timeout_s", 60))
        if not self.registry.is_dir() or not (self.registry / ".git").exists():
            raise FactoryError(
                "REGISTRY_NOT_CONFIGURED", "Registry 没有配置或不是 Git 仓库", path=str(self.registry)
            )

    def build(self, commit: str) -> Epoch:
        commit = self._verify(commit)
        existing = self.ctx.db.query(f"SELECT * FROM epochs WHERE {_COL} = ?", (commit,))
        if existing:
            return self._epoch(existing[0])
        dest = self.ctx.root / "epochs" / commit
        part = self.ctx.root / "epochs" / (commit + ".part")
        if part.exists():
            shutil.rmtree(part)
        registry = part / "registry"
        registry.mkdir(parents=True)
        try:
            self._extract(commit, registry)
            snapshot = Snapshot.load(registry, epoch=commit)
        except FactoryError:
            shutil.rmtree(part, ignore_errors=True)
            raise
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(part, dest)
        frozen = dest / "resolutions.json"
        if not frozen.is_file():
            payload = [{**dict(item), "origin": "precomputed"} for item in snapshot.result.resolutions]
            _atomic_write(frozen, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        tree = git(self.registry, "rev-parse", f"{commit}^{{tree}}", timeout_s=self.timeout_s).strip()
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(f"INSERT INTO epochs ({_COL}, tree, built_at) VALUES (?, ?, ?)", (commit, tree, now))
            _event(conn, self.ctx, "epoch.built", {"commit": commit, "tree": tree})
        self._cache[commit] = snapshot
        return self._epoch(self.ctx.db.query(f"SELECT * FROM epochs WHERE {_COL} = ?", (commit,))[0])

    def materialize(self, commit: str) -> Snapshot:
        built = self.build(commit)
        if built.commit not in self._cache:
            self._cache[built.commit] = Snapshot.load(
                self.ctx.root / "epochs" / built.commit / "registry", epoch=built.commit
            )
        return self._cache[built.commit]

    def init(self, commit: str, *, by: str) -> str:
        if self.ctx.db.query("SELECT name FROM epoch_pointers WHERE name = 'active'"):
            raise FactoryError("EPOCH_STATE_INVALID", "active 指针已经存在", op="init")
        built = self.build(commit)
        self.set_pointer("active", built.commit, by=by, reason="init")
        return built.commit

    def adopt(self, commit: str, *, by: str) -> str:
        commit = self._verify(commit)
        active = self.current()
        parent = git(self.registry, "rev-parse", f"{commit}^1", timeout_s=self.timeout_s).strip()
        if parent != active:
            raise FactoryError("EPOCH_ADOPT_INVALID", "父提交不是当前 active", reason="parent")
        names = [
            line
            for line in git(
                self.registry, "diff", "--name-only", active, commit, timeout_s=self.timeout_s
            ).splitlines()
            if line
        ]
        if not names or any(not line.startswith("constitution/") for line in names):
            raise FactoryError("EPOCH_ADOPT_INVALID", "采纳只能改宪法", reason="paths", paths=names)
        try:
            git(self.registry, "verify-commit", commit, timeout_s=self.timeout_s)
        except FactoryError as exc:
            raise FactoryError("EPOCH_ADOPT_INVALID", "提交没有签名", reason="unsigned") from exc
        self.build(commit)
        self.set_pointer("previous", active, by=by, reason="adopt")
        self.set_pointer("active", commit, by=by, reason="adopt")
        return commit

    def current(self) -> str:
        rows = self.ctx.db.query(f"SELECT {_COL} FROM epoch_pointers WHERE name = 'active'")
        if not rows:
            raise FactoryError("EPOCH_UNSET", "没有 active epoch")
        return str(rows[0]["commit"])

    def pointers(self) -> dict[str, str | None]:
        found = {
            row["name"]: row["commit"]
            for row in self.ctx.db.query(f"SELECT name, {_COL} FROM epoch_pointers")
        }
        return {name: found.get(name) for name in _POINTERS}

    def set_pointer(self, name: str, commit: str | None, *, by: str, reason: str) -> None:
        if name not in _POINTERS:
            raise ValueError("指针名不合法")
        if commit is not None:
            commit = self._verify(commit)
            if not self.ctx.db.query(f"SELECT {_COL} FROM epochs WHERE {_COL} = ?", (commit,)):
                raise FactoryError("EPOCH_NOT_FOUND", "epoch 还没有构建", commit=commit)
        previous = self.pointers().get(name)
        with self.ctx.db.tx() as conn:
            if commit is None:
                conn.execute("DELETE FROM epoch_pointers WHERE name = ?", (name,))
            else:
                conn.execute(
                    f"INSERT INTO epoch_pointers (name, {_COL}) VALUES (?, ?) "
                    f"ON CONFLICT(name) DO UPDATE SET {_COL} = excluded.{_COL}",
                    (name, commit),
                )
            _event(
                conn,
                self.ctx,
                "epoch.pointer",
                {"name": name, "from": previous, "to": commit, "by": by, "reason": reason},
            )
        ref = f"refs/factory/{name}"
        if commit is None:
            git(self.registry, "update-ref", "-d", ref, timeout_s=self.timeout_s)
        else:
            git(self.registry, "update-ref", ref, commit, timeout_s=self.timeout_s)

    def rollback(self, commit: str | None = None, *, by: str) -> str:
        pointers = self.pointers()
        target = commit or pointers.get("previous")
        if not target:
            raise FactoryError("EPOCH_UNSET", "没有可回滚的 previous")
        target = self._verify(target)
        if not self.ctx.db.query(f"SELECT {_COL} FROM epochs WHERE {_COL} = ?", (target,)):
            raise FactoryError("EPOCH_NOT_FOUND", "回滚目标还没有构建", commit=target)
        active = pointers.get("active")
        if target == active:
            raise FactoryError("EPOCH_STATE_INVALID", "不能回滚到当前 active", commit=target)
        self.set_pointer("previous", active, by=by, reason="rollback")
        self.set_pointer("active", target, by=by, reason="rollback")
        if pointers.get("candidate"):
            self.set_pointer("candidate", None, by=by, reason="rollback")
        return target

    def retain(self, commit: str) -> bool:
        commit = self._verify(commit)
        if commit in {value for value in self.pointers().values() if value}:
            return True
        if self._bound("runs", "epoch", commit, "state", _RUN_TERMINAL):
            return True
        if self._bound("jobs", "epoch", commit, "state", _JOB_TERMINAL):
            return True
        if self.ctx.db.query(
            "SELECT id FROM changesets WHERE (base_epoch = ? OR "
            + _COL
            + " = ?) AND state NOT IN ("
            + ",".join("?" * len(_CS_TERMINAL))
            + ")",
            (commit, commit, *_CS_TERMINAL),
        ):
            return True
        return False

    def list(self) -> list[Epoch]:
        return [
            self._epoch(row) for row in self.ctx.db.query("SELECT * FROM epochs ORDER BY built_at, " + _COL)
        ]

    def _bound(
        self, table: str, column: str, commit: str, state_col: str, terminal: tuple[str, ...]
    ) -> list[Any]:
        marks = ",".join("?" * len(terminal))
        return self.ctx.db.query(
            f"SELECT id FROM {table} WHERE {column} = ? AND {state_col} NOT IN ({marks})",
            (commit, *terminal),
        )

    def _verify(self, commit: str) -> str:
        try:
            resolved = git(
                self.registry, "rev-parse", "--verify", f"{commit}^{{commit}}", timeout_s=self.timeout_s
            ).strip()
        except FactoryError as exc:
            raise FactoryError("COMMIT_NOT_FOUND", "找不到提交", commit=commit) from exc
        if len(resolved) != 40:
            raise FactoryError("COMMIT_NOT_FOUND", "提交不是 40 位", commit=commit)
        return resolved

    def _extract(self, commit: str, dest: Path) -> None:
        blob = git_bytes(self.registry, "archive", "--format=tar", commit, timeout_s=self.timeout_s)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
            for member in archive.getmembers():
                name = member.name.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    raise FactoryError("GIT_FAILED", "归档包含不安全路径", args=["archive"], stderr=name)
                archive.extract(member, dest, filter="data")

    def _epoch(self, row: Any) -> Epoch:
        return Epoch(row["commit"], row["tree"], row["built_at"], self.retain(row["commit"]))


def _governance(root: Path) -> dict[str, Any]:
    path = Path(root) / "factory.json"
    if not path.is_file():
        raise FactoryError("REGISTRY_NOT_CONFIGURED", "缺少 factory.json", path=str(path))
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FactoryError("REGISTRY_NOT_CONFIGURED", "factory.json 无法解析", path=str(path)) from exc
    governance = body.get("governance") if isinstance(body, dict) else None
    if not isinstance(governance, dict) or not governance.get("registry"):
        raise FactoryError("REGISTRY_NOT_CONFIGURED", "没有配置 Registry", path=str(path))
    return governance


def _event(conn: Any, ctx: Any, kind: str, data: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
        (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(data)),
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    part = path.with_name(path.name + ".part")
    part.write_bytes(payload)
    os.replace(part, path)
