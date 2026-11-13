"""内容寻址仓库：CAS、Bean、封存与血缘。"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from mao.base import canonical_bytes, canonical_json, digest_bytes, digest_file, utc_iso
from mao.errors import FactoryError

_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*@[0-9]+\.[0-9]+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEDIA = {
    "txt": "text/plain",
    "json": "application/json",
    "md": "text/markdown",
    "mp4": "video/mp4",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}


def reject_expired(warehouse: Any, inputs: Mapping[str, Any]) -> None:
    for ids in inputs.values():
        values = ids if isinstance(ids, (list, tuple)) else [ids]
        for bean_id in values:
            bean = warehouse.get(str(bean_id))
            if bean.expired_at is not None:
                raise FactoryError("BEAN_EXPIRED", "Bean 已过期", bean_id=bean.id)


def media_type_of(path: str) -> str:
    name = PurePosixPath(path.replace("\\", "/")).name
    if "." not in name:
        return "application/octet-stream"
    suffix = name.rsplit(".", 1)[1].lower()
    return _MEDIA.get(suffix, "application/octet-stream")


@dataclass
class FileRef:
    path: str
    digest: str
    size: int
    media_type: str


@dataclass
class Bean:
    id: str
    type: str
    meta: dict[str, Any]
    facts: dict[str, Any]
    payload: list[FileRef]
    sealed: bool
    created_at: str
    expired_at: str | None = None


@dataclass
class BeanDraft:
    type: str
    meta: dict[str, Any] = field(default_factory=dict)
    files: dict[str, Path] = field(default_factory=dict)


class Warehouse:
    def __init__(self, root: Path, db: Any, clock: Any, ids: Any = None) -> None:
        self.root = Path(root)
        self.db = db
        self.clock = clock
        self.ids = ids
        self.store = None

    def blob_path(self, digest: str) -> Path:
        if self.store is not None:
            return self._cached_blob(digest)
        if _DIGEST.fullmatch(digest) is None:
            raise FactoryError("DIGEST_INVALID", "摘要格式不合法", digest=digest)
        hexpart = digest.split(":", 1)[1]
        return self.root / "cas" / "sha256" / hexpart[:2] / hexpart[2:]

    def _cache_path(self, digest: str) -> Path:
        hexpart = digest.split(":", 1)[1]
        return self.root / "cas-cache" / "sha256" / hexpart[:2] / hexpart[2:]

    def _cached_blob(self, digest: str) -> Path:
        if _DIGEST.fullmatch(digest) is None:
            raise FactoryError("DIGEST_INVALID", "摘要格式不合法", digest=digest)
        cache = self._cache_path(digest)
        if cache.is_file() and digest_file(cache) != digest:
            cache.unlink()
            raise FactoryError("CAS_MISSING", "缓存摘要不符", digest=digest)
        if not cache.is_file():
            cache.parent.mkdir(parents=True, exist_ok=True)
            self.store.get_file(digest, cache)
            if digest_file(cache) != digest:
                cache.unlink()
                raise FactoryError("CAS_MISSING", "缓存摘要不符", digest=digest)
        return cache

    def put_file(self, path: Path) -> str:
        if self.store is not None:
            return self.store.put_file(Path(path))
        digest = digest_file(Path(path))
        dest = self.blob_path(digest)
        if dest.exists():
            return digest
        tmp_dir = self.root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        part = tmp_dir / f"{digest.split(':', 1)[1]}.{os.getpid()}.part"
        with Path(path).open("rb") as src, part.open("wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            part.unlink(missing_ok=True)
            return digest
        os.replace(part, dest)
        return digest

    def add(self, draft: BeanDraft) -> Bean:
        if _TYPE.fullmatch(draft.type) is None:
            raise FactoryError("BEAN_TYPE_INVALID", "Bean 类型必须带 minor", type=draft.type)
        canonical_json(draft.meta)
        records = []
        sources: dict[str, Path] = {}
        for rel, host in draft.files.items():
            _check_payload_path(rel)
            digest = self.put_file(Path(host))
            sources[digest] = Path(host)
            records.append(
                {
                    "path": rel,
                    "digest": digest,
                    "size": Path(host).stat().st_size,
                    "media_type": media_type_of(rel),
                }
            )
        records.sort(key=lambda item: item["path"])
        bean_id = digest_bytes(canonical_bytes({"type": draft.type, "meta": draft.meta, "payload": records}))
        with self.db.tx() as conn:
            for digest, host in sources.items():
                if not self.blob_path(digest).is_file():
                    self.put_file(host)
            existing = conn.execute("SELECT id FROM beans WHERE id = ?", (bean_id,)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO beans (id, type, meta_json, facts_json, sealed, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                    (
                        bean_id,
                        draft.type,
                        canonical_json(draft.meta),
                        canonical_json({}),
                        utc_iso(self.clock.now()),
                    ),
                )
                for item in records:
                    conn.execute(
                        "INSERT INTO payload_files (bean_id, path, digest, size, media_type) VALUES (?, ?, ?, ?, ?)",
                        (bean_id, item["path"], item["digest"], item["size"], item["media_type"]),
                    )
        return self.get(bean_id)

    def get(self, bean_id: str) -> Bean:
        rows = self.db.query("SELECT * FROM beans WHERE id = ?", (bean_id,))
        if not rows:
            raise FactoryError("BEAN_NOT_FOUND", f"Bean {bean_id} 不存在", bean_id=bean_id)
        row = rows[0]
        payload_rows = self.db.query(
            "SELECT path, digest, size, media_type FROM payload_files WHERE bean_id = ? ORDER BY path",
            (bean_id,),
        )
        return Bean(
            id=row["id"],
            type=row["type"],
            meta=json.loads(row["meta_json"]),
            facts=json.loads(row["facts_json"]),
            payload=[
                FileRef(
                    path=item["path"], digest=item["digest"], size=item["size"], media_type=item["media_type"]
                )
                for item in payload_rows
            ],
            sealed=bool(row["sealed"]),
            created_at=row["created_at"],
            expired_at=None if row["expired_at"] is None else str(row["expired_at"]),
        )

    def list(
        self, type_prefix: str | None = None, sealed_only: bool = True, include_expired: bool = False
    ) -> list[Bean]:
        sql = "SELECT id FROM beans"
        where: list[str] = []
        if sealed_only:
            where.append("sealed = 1")
        if not include_expired:
            where.append("expired_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, id"
        rows = self.db.query(sql)
        beans = [self.get(row["id"]) for row in rows]
        if type_prefix is not None:
            beans = [bean for bean in beans if bean.type.startswith(type_prefix)]
        return beans

    def expire(self, bean_id: str, *, by: str, reason: str) -> Bean:
        bean = self.get(bean_id)
        if not bean.sealed:
            raise FactoryError("BEAN_NOT_SEALED", "未封存的 Bean 不能过期", bean_id=bean_id)
        if bean.expired_at is not None:
            return bean
        now = utc_iso(self.clock.now())
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE beans SET expired_at = ? WHERE id = ? AND expired_at IS NULL",
                (now, bean_id),
            )
            if self.ids is not None:
                conn.execute(
                    "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'bean.expired', NULL, ?)",
                    (
                        self.ids.new("ev"),
                        now,
                        canonical_json({"bean_id": bean_id, "type": bean.type, "by": by, "reason": reason}),
                    ),
                )
        return self.get(bean_id)

    def delete_blob(self, digest: str) -> bool:
        if self.store is not None:
            cache = self._cache_path(digest)
            if cache.is_file():
                cache.unlink()
            return bool(self.store.delete(digest))
        path = self.blob_path(digest)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def seal_many(
        self, items: list[tuple[str, dict[str, Any]]], job_id: str | None, report_digest: str
    ) -> None:
        self.blob_path(report_digest)
        if not items:
            return
        with self.db.tx() as conn:
            for bean_id, facts in items:
                row = conn.execute("SELECT facts_json, sealed FROM beans WHERE id = ?", (bean_id,)).fetchone()
                if row is None:
                    raise FactoryError("BEAN_NOT_FOUND", f"Bean {bean_id} 不存在", bean_id=bean_id)
                facts_json = canonical_json(facts)
                sealed = (
                    conn.execute("SELECT 1 FROM seals WHERE bean_id = ?", (bean_id,)).fetchone() is not None
                )
                if not sealed and not row["sealed"]:
                    conn.execute(
                        "UPDATE beans SET facts_json = ?, sealed = 1 WHERE id = ?",
                        (facts_json, bean_id),
                    )
                    conn.execute(
                        "INSERT INTO seals (bean_id, job_id, report_digest, facts_json, sealed_at) VALUES (?, ?, ?, ?, ?)",
                        (bean_id, job_id, report_digest, facts_json, utc_iso(self.clock.now())),
                    )
                    continue
                if row["facts_json"] != facts_json and sealed:
                    raise FactoryError("FACTS_CONFLICT", "封存 facts 与首次封存不一致", bean_id=bean_id)
                if not sealed:
                    raise FactoryError("FACTS_CONFLICT", "封存 facts 与首次封存不一致", bean_id=bean_id)
                dup = conn.execute(
                    "SELECT 1 FROM seals WHERE bean_id = ? AND report_digest = ? AND ifnull(job_id, '') = ifnull(?, '')",
                    (bean_id, report_digest, job_id),
                ).fetchone()
                if dup is None:
                    conn.execute(
                        "INSERT INTO seals (bean_id, job_id, report_digest, facts_json, sealed_at) VALUES (?, ?, ?, ?, ?)",
                        (bean_id, job_id, report_digest, facts_json, utc_iso(self.clock.now())),
                    )

    def label(self, bean_id: str, key: str, value: str, *, by: str) -> None:
        self.get(bean_id)
        now = utc_iso(self.clock.now())
        with self.db.tx() as conn:
            conn.execute(
                """INSERT INTO bean_labels (bean_id, key, value, set_by, at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(bean_id, key) DO UPDATE SET value = excluded.value, set_by = excluded.set_by, at = excluded.at""",
                (bean_id, key, value, by, now),
            )

    def labels(self, bean_id: str) -> dict[str, str]:
        self.get(bean_id)
        rows = self.db.query("SELECT key, value FROM bean_labels WHERE bean_id = ?", (bean_id,))
        return {row["key"]: row["value"] for row in rows}

    def _bean_sealed_event(self, conn: Any, bean_id: str, job_id: str | None) -> None:
        if self.ids is None:
            return
        bean = self.get(bean_id)
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'bean.sealed', ?, ?)",
            (
                self.ids.new("ev"),
                utc_iso(self.clock.now()),
                job_id,
                canonical_json({"bean": bean_id, "job": job_id, "type": bean.type, "station": ""}),
            ),
        )

    def materialize(self, bean_id: str, dest: Path, *, layout: str) -> None:
        bean = self.get(bean_id)
        if bean.expired_at is not None:
            raise FactoryError("BEAN_EXPIRED", "Bean 已过期", bean_id=bean_id)
        if layout not in ("port", "bean"):
            raise ValueError("layout 必须是 port 或 bean")
        dest = Path(dest)
        if dest.exists() and not dest.is_dir():
            raise FactoryError("DEST_INVALID", "物化目标不是目录", dest=str(dest))
        dest.mkdir(parents=True, exist_ok=True)
        prefix = dest / "payload" if layout == "bean" else dest
        for item in bean.payload:
            target = prefix / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.blob_path(item.digest), target)
        payload = [
            {"path": item.path, "digest": item.digest, "size": item.size, "media_type": item.media_type}
            for item in bean.payload
        ]
        body = canonical_bytes(
            {"id": bean.id, "type": bean.type, "facts": bean.facts, "meta": bean.meta, "payload": payload}
        )
        (dest / "bean.json").write_bytes(body)

    def record_lineage(
        self, job_id: str, inputs: Mapping[str, list[str]], outputs: Mapping[str, list[str]]
    ) -> None:
        with self.db.tx() as conn:
            for ids in list(inputs.values()) + list(outputs.values()):
                for bean_id in ids:
                    row = conn.execute("SELECT 1 FROM beans WHERE id = ?", (bean_id,)).fetchone()
                    if row is None:
                        raise FactoryError("BEAN_NOT_FOUND", f"Bean {bean_id} 不存在", bean_id=bean_id)
            conn.execute("DELETE FROM lineage WHERE job_id = ?", (job_id,))
            for port in sorted(inputs):
                for ordinal, bean_id in enumerate(inputs[port]):
                    conn.execute(
                        "INSERT INTO lineage (job_id, direction, port, ordinal, bean_id) VALUES (?, 'in', ?, ?, ?)",
                        (job_id, port, ordinal, bean_id),
                    )
            for port in sorted(outputs):
                for ordinal, bean_id in enumerate(outputs[port]):
                    conn.execute(
                        "INSERT INTO lineage (job_id, direction, port, ordinal, bean_id) VALUES (?, 'out', ?, ?, ?)",
                        (job_id, port, ordinal, bean_id),
                    )

    def lineage(self, bean_id: str, direction: str) -> dict[str, Any]:
        if direction not in ("up", "down"):
            raise ValueError("direction 必须是 up 或 down")
        receipts = self._receipt_ids(bean_id) if direction == "down" else []
        try:
            self.get(bean_id)
        except FactoryError:
            if not receipts:
                raise
            return {
                "id": bean_id,
                "direction": direction,
                "jobs": [],
                "beans": [],
                "receipts": receipts,
            }
        visited_beans = {bean_id}
        visited_jobs: set[str] = set()
        queue = [bean_id]
        while queue:
            current = queue.pop(0)
            if direction == "up":
                rows = self.db.query(
                    "SELECT job_id FROM lineage WHERE bean_id = ? AND direction = 'out'",
                    (current,),
                )
            else:
                rows = self.db.query(
                    "SELECT job_id FROM lineage WHERE bean_id = ? AND direction = 'in'",
                    (current,),
                )
            for row in rows:
                job_id = row["job_id"]
                if job_id in visited_jobs:
                    continue
                visited_jobs.add(job_id)
                nxt = self.db.query(
                    "SELECT bean_id FROM lineage WHERE job_id = ? AND direction = ?",
                    (job_id, "in" if direction == "up" else "out"),
                )
                for item in nxt:
                    if item["bean_id"] not in visited_beans:
                        visited_beans.add(item["bean_id"])
                        queue.append(item["bean_id"])
        jobs = []
        for job_id in sorted(visited_jobs):
            jobs.append(
                {"job_id": job_id, "inputs": self._ports(job_id, "in"), "outputs": self._ports(job_id, "out")}
            )
        body = {
            "id": bean_id,
            "direction": direction,
            "jobs": jobs,
            "beans": sorted(visited_beans - {bean_id}),
        }
        subject = self.get(bean_id)
        if subject.expired_at is not None:
            body["expired"] = True
        if direction == "down" and receipts:
            body["receipts"] = receipts
        return body

    def _receipt_ids(self, bean_id: str) -> list[str]:
        rows = self.db.query("SELECT id, inputs FROM receipts")
        found: list[str] = []
        for row in rows:
            try:
                inputs = json.loads(row["inputs"])
            except (TypeError, json.JSONDecodeError):
                continue
            if any(bean_id in [str(item) for item in value] for value in inputs.values()):
                found.append(str(row["id"]))
        return found

    def verify(self) -> list[dict[str, str]]:
        problems: list[dict[str, str]] = []
        cas = self.root / "cas" / "sha256"
        if cas.is_dir():
            for path in cas.rglob("*"):
                if not path.is_file():
                    continue
                expected = "sha256:" + path.parent.name + path.name
                if digest_file(path) != expected:
                    problems.append(
                        {"kind": "digest_mismatch", "path": str(path), "bean_id": "", "digest": expected}
                    )
        expired = {row["id"] for row in self.db.query("SELECT id FROM beans WHERE expired_at IS NOT NULL")}
        for row in self.db.query("SELECT bean_id, path, digest FROM payload_files"):
            if row["bean_id"] in expired:
                continue
            if not self.blob_path(row["digest"]).is_file():
                problems.append(
                    {
                        "kind": "missing_blob",
                        "path": row["path"],
                        "bean_id": row["bean_id"],
                        "digest": row["digest"],
                    }
                )
        for row in self.db.query("SELECT id, type, meta_json, facts_json, sealed FROM beans"):
            payload = self.db.query(
                "SELECT path, digest, size, media_type FROM payload_files WHERE bean_id = ? ORDER BY path",
                (row["id"],),
            )
            records = [
                {
                    "path": item["path"],
                    "digest": item["digest"],
                    "size": item["size"],
                    "media_type": item["media_type"],
                }
                for item in payload
            ]
            expected = digest_bytes(
                canonical_bytes(
                    {"type": row["type"], "meta": json.loads(row["meta_json"]), "payload": records}
                )
            )
            if expected != row["id"]:
                problems.append({"kind": "id_mismatch", "path": "", "bean_id": row["id"], "digest": expected})
            has_seal = bool(self.db.query("SELECT 1 FROM seals WHERE bean_id = ?", (row["id"],)))
            if bool(row["sealed"]) != has_seal:
                problems.append({"kind": "seal_flag", "path": "", "bean_id": row["id"], "digest": ""})
            if not row["sealed"] and row["facts_json"] != canonical_json({}):
                problems.append({"kind": "facts_unsealed", "path": "", "bean_id": row["id"], "digest": ""})
        problems.sort(key=lambda item: (item["kind"], item["path"], item["bean_id"], item["digest"]))
        return problems

    def _ports(self, job_id: str, direction: str) -> dict[str, list[str]]:
        rows = self.db.query(
            "SELECT port, bean_id FROM lineage WHERE job_id = ? AND direction = ? ORDER BY port, ordinal",
            (job_id, direction),
        )
        ports: dict[str, list[str]] = {}
        for row in rows:
            ports.setdefault(row["port"], []).append(row["bean_id"])
        return ports


def _check_payload_path(path: str) -> None:
    if not path or any(ch in path for ch in '\\:*?"<>|') or path.startswith("/"):
        raise FactoryError("PAYLOAD_PATH_INVALID", "payload 路径不合法", path=path)
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise FactoryError("PAYLOAD_PATH_INVALID", "payload 路径不合法", path=path)
