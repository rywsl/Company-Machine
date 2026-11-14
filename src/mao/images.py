"""从 Registry 构建机器镜像。构建发生在宿主，沙箱不接触 docker。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError
from mao.foreman import redact

_PINNED = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_IID = re.compile(r"^sha256:[0-9a-f]{64}$")
_SLUG = re.compile(r"[^a-z0-9.-]")


@dataclass(frozen=True)
class ImageConfig:
    repo: str = "mao-local"
    network: str = "default"
    timeout_s: float = 1800
    max_context_mb: int = 200
    max_builds_per_day: int = 20
    allow_unpinned_bases: tuple[str, ...] = ()

    @classmethod
    def load(cls, root: Path) -> ImageConfig:
        path = Path(root) / "factory.json"
        if not path.is_file():
            return cls()
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FactoryError("IMAGE_CONFIG_INVALID", "factory.json 无法解析", field="images") from exc
        section = body.get("images") if isinstance(body, dict) else None
        if section is None:
            return cls()
        if not isinstance(section, dict):
            raise FactoryError("IMAGE_CONFIG_INVALID", "images 段类型不对", field="images")
        values: dict[str, Any] = {}
        if "repo" in section:
            if not isinstance(section["repo"], str) or not section["repo"]:
                raise FactoryError("IMAGE_CONFIG_INVALID", "images.repo 类型不对", field="repo")
            values["repo"] = section["repo"]
        if "network" in section:
            if section["network"] not in ("none", "default"):
                raise FactoryError("IMAGE_CONFIG_INVALID", "images.network 类型不对", field="network")
            values["network"] = section["network"]
        for name in ("timeout_s",):
            if name in section:
                raw = section[name]
                if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) <= 0:
                    raise FactoryError("IMAGE_CONFIG_INVALID", f"images.{name} 类型不对", field=name)
                values[name] = float(raw)
        for name in ("max_context_mb", "max_builds_per_day"):
            if name in section:
                raw = section[name]
                if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                    raise FactoryError("IMAGE_CONFIG_INVALID", f"images.{name} 类型不对", field=name)
                values[name] = raw
        if "allow_unpinned_bases" in section:
            bases = section["allow_unpinned_bases"]
            if not isinstance(bases, list) or any(not isinstance(item, str) for item in bases):
                raise FactoryError(
                    "IMAGE_CONFIG_INVALID",
                    "images.allow_unpinned_bases 类型不对",
                    field="allow_unpinned_bases",
                )
            values["allow_unpinned_bases"] = tuple(bases)
        return cls(**values)


@dataclass(frozen=True)
class ImageBuild:
    id: str
    path: str
    commit: str
    context_digest: str
    state: str
    image_id: str | None
    ref: str | None
    log: str
    error: dict[str, Any] | None
    by: str
    at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "commit": self.commit,
            "context_digest": self.context_digest,
            "state": self.state,
            "image_id": self.image_id,
            "ref": self.ref,
            "log": self.log,
            "error": self.error,
            "by": self.by,
            "at": self.at,
        }


class ImageBuilder:
    def __init__(self, ctx: Any, epochs: Any, config: ImageConfig, *, docker: str = "docker") -> None:
        self.ctx = ctx
        self.epochs = epochs
        self.config = config
        self.docker = docker

    def build(self, path: str, *, commit: str | None = None, by: str) -> ImageBuild:
        rel = _posix_path(path)
        commit = commit or self.epochs.current()
        timeout = float(self.epochs.timeout_s)
        _require_commit(self.epochs.registry, commit, timeout)
        if not _tree_exists(self.epochs.registry, commit, rel, timeout):
            raise FactoryError("IMAGE_CONTEXT_INVALID", "上下文目录不存在", reason="missing")
        build_id = self.ctx.ids.new("ib")
        dest = Path(self.ctx.root) / "images" / "ctx" / build_id
        dest.mkdir(parents=True, exist_ok=True)
        try:
            _extract(self.epochs.registry, commit, rel, dest, timeout)
            if not (dest / "Dockerfile").is_file():
                raise FactoryError("IMAGE_CONTEXT_INVALID", "缺少 Dockerfile", reason="no-dockerfile")
            _check_size(dest, self.config.max_context_mb)
            _check_bases((dest / "Dockerfile").read_text(encoding="utf-8"), self.config.allow_unpinned_bases)
            digest = context_digest(dest)
            cached = self._reuse(rel, commit, digest, by, build_id)
            if cached is not None:
                return cached
            self._limit()
            return self._docker_build(dest, rel, commit, digest, by, build_id)
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def list(self) -> list[ImageBuild]:
        rows = self.ctx.db.query("SELECT * FROM images ORDER BY id")
        return [_row(row) for row in rows]

    def _reuse(self, path: str, commit: str, digest: str, by: str, build_id: str) -> ImageBuild | None:
        rows = self.ctx.db.query(
            "SELECT * FROM images WHERE context_digest = ? AND state = 'ok' ORDER BY id",
            (digest,),
        )
        if not rows:
            return None
        image_id = str(rows[0]["image_id"])
        if not _image_present(self.docker, image_id):
            return None
        at = utc_iso(self.ctx.clock.now())
        log_rel = f"images/{build_id}.log"
        log_path = Path(self.ctx.root) / log_rel
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"reused {image_id}\n", encoding="utf-8")
        record = ImageBuild(
            build_id,
            path,
            commit,
            digest,
            "cached",
            image_id,
            str(rows[0]["ref"]),
            log_rel,
            None,
            by,
            at,
        )
        self._insert(record)
        return record

    def _limit(self) -> None:
        cutoff = utc_iso(self.ctx.clock.now() - timedelta(hours=24))
        rows = self.ctx.db.query(
            "SELECT COUNT(*) AS n FROM images WHERE at >= ? AND state IN ('ok', 'failed')",
            (cutoff,),
        )
        count = int(rows[0]["n"])
        if count >= self.config.max_builds_per_day:
            raise FactoryError("IMAGE_BUILD_LIMITED", "超过每日构建次数", count=count)

    def _docker_build(
        self, dest: Path, path: str, commit: str, digest: str, by: str, build_id: str
    ) -> ImageBuild:
        slug = _slug(path)
        tag = f"{self.config.repo}/{slug}:ctx-{digest[7:19]}"
        iid = dest.parent / f"{build_id}.iid"
        argv = build_argv(
            self.docker,
            context=dest,
            tag=tag,
            iidfile=iid,
            network=self.config.network,
            labels={"mao.context": digest, "mao.path": path, "mao.commit": commit},
        )
        timed_out = False
        exit_code = 1
        chunks: list[str] = []
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.config.timeout_s,
                check=False,
            )
            exit_code = int(proc.returncode)
            chunks.append(proc.stdout.decode("utf-8", errors="replace"))
            chunks.append(proc.stderr.decode("utf-8", errors="replace"))
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            if exc.stdout:
                chunks.append(
                    exc.stdout.decode("utf-8", errors="replace")
                    if isinstance(exc.stdout, bytes)
                    else str(exc.stdout)
                )
            if exc.stderr:
                chunks.append(
                    exc.stderr.decode("utf-8", errors="replace")
                    if isinstance(exc.stderr, bytes)
                    else str(exc.stderr)
                )
        log_rel = f"images/{build_id}.log"
        self._write_log(log_rel, "".join(chunks))
        image_id = None
        ref = None
        error = None
        state = "failed"
        if not timed_out and exit_code == 0 and iid.is_file():
            text = iid.read_text(encoding="utf-8").strip()
            if _IID.fullmatch(text):
                image_id = text
                ref = f"{self.config.repo}/{slug}@sha256:{text[7:]}"
                state = "ok"
        if state != "ok":
            error = {"code": "IMAGE_BUILD_FAILED", "detail": {"exit_code": exit_code, "timed_out": timed_out}}
        record = ImageBuild(
            build_id,
            path,
            commit,
            digest,
            state,
            image_id,
            ref,
            log_rel,
            error,
            by,
            utc_iso(self.ctx.clock.now()),
        )
        self._insert(record)
        return record

    def _write_log(self, rel: str, text: str) -> None:
        path = Path(self.ctx.root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [redact(self.ctx, None, line) for line in text.splitlines()]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _insert(self, record: ImageBuild) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute(
                'INSERT INTO images (id, path, "commit", context_digest, state, image_id, ref, log, error, by, at) '
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.path,
                    record.commit,
                    record.context_digest,
                    record.state,
                    record.image_id,
                    record.ref,
                    record.log,
                    None if record.error is None else canonical_json(record.error),
                    record.by,
                    record.at,
                ),
            )
            conn.execute(
                "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, 'image.built', NULL, ?)",
                (
                    self.ctx.ids.new("ev"),
                    record.at,
                    canonical_json(
                        {"id": record.id, "path": record.path, "state": record.state, "ref": record.ref}
                    ),
                ),
            )


def context_digest(path: Path) -> str:
    lines: list[str] = []
    root = Path(path)
    files = sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    for file in files:
        rel = file.relative_to(root).as_posix()
        mark = "-"
        if os.name != "nt" and file.stat().st_mode & 0o111:
            mark = "x"
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        lines.append(f"{rel}\t{mark}\t{digest}\n")
    return "sha256:" + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def dockerfile_bases(text: str) -> list[str]:
    found: list[str] = []
    for line in _logical_lines(text):
        parts = line.split()
        if not parts or parts[0].lower() != "from":
            continue
        words = parts[1:]
        image = None
        index = 0
        while index < len(words):
            word = words[index]
            if word.startswith("--"):
                index += 1
                continue
            image = word
            break
        if image is None:
            continue
        found.append(image)
    return found


def build_argv(
    docker: str,
    *,
    context: Path,
    tag: str,
    iidfile: Path,
    network: str,
    labels: Mapping[str, str],
) -> list[str]:
    argv = [docker, "build", "--pull=false", "--network", network, "--iidfile", str(iidfile)]
    for key in sorted(labels):
        argv.extend(["--label", f"{key}={labels[key]}"])
    argv.extend(["-t", tag, "-f", str(Path(context) / "Dockerfile"), str(context)])
    return argv


def _posix_path(path: str) -> str:
    text = str(path).replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text == "":
        raise FactoryError("IMAGE_CONTEXT_INVALID", "上下文路径不合法", reason="path")
    parts = [part for part in text.split("/") if part != "" and part != "."]
    if not parts or any(part == ".." for part in parts) or parts[0] == "constitution":
        raise FactoryError("IMAGE_CONTEXT_INVALID", "上下文路径不合法", reason="path")
    return "/".join(parts)


def _require_commit(repo: Path, commit: str, timeout_s: float) -> None:
    from mao.governance.git import git

    try:
        git(repo, "cat-file", "-e", f"{commit}^{{commit}}", timeout_s=timeout_s)
    except FactoryError as exc:
        if exc.code == "GIT_FAILED":
            raise FactoryError("COMMIT_NOT_FOUND", "提交不存在", commit=commit) from exc
        raise


def _tree_exists(repo: Path, commit: str, path: str, timeout_s: float) -> bool:
    from mao.governance.git import git

    listing = git(repo, "ls-tree", "-d", commit, "--", path, timeout_s=timeout_s)
    return bool(listing.strip())


def _extract(repo: Path, commit: str, path: str, dest: Path, timeout_s: float) -> None:
    from mao.governance.git import git_bytes

    blob = git_bytes(repo, "archive", "--format=tar", commit, "--", path, timeout_s=timeout_s)
    prefix = path.rstrip("/") + "/"
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        for member in archive.getmembers():
            name = member.name.replace("\\", "/")
            if name == path.rstrip("/") or name == prefix.rstrip("/"):
                continue
            if name.startswith(prefix):
                name = name[len(prefix) :]
            if name == "" or name.startswith("/") or ".." in Path(name).parts:
                raise FactoryError("IMAGE_CONTEXT_INVALID", "上下文含链接", reason="link")
            if member.issym() or member.islnk() or member.isdev() or member.ischr() or member.isfifo():
                raise FactoryError("IMAGE_CONTEXT_INVALID", "上下文含链接", reason="link")
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise FactoryError("IMAGE_CONTEXT_INVALID", "上下文含链接", reason="link")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = archive.extractfile(member)
            data = b"" if extracted is None else extracted.read()
            target.write_bytes(data)
            if os.name != "nt":
                os.chmod(target, member.mode & 0o777)


def _check_size(dest: Path, max_mb: int) -> None:
    total = sum(item.stat().st_size for item in dest.rglob("*") if item.is_file())
    if total > max_mb * 1024 * 1024:
        raise FactoryError("IMAGE_CONTEXT_INVALID", "上下文太大", reason="too-large")


def _check_bases(text: str, allowed: tuple[str, ...]) -> None:
    stages: set[str] = set()
    for line in _logical_lines(text):
        parts = line.split()
        if not parts or parts[0].lower() != "from":
            continue
        words = parts[1:]
        image = None
        index = 0
        while index < len(words):
            word = words[index]
            if word.startswith("--"):
                index += 1
                continue
            image = word
            index += 1
            break
        if image is None:
            continue
        if "$" in image and image not in allowed:
            raise FactoryError(
                "IMAGE_CONTEXT_INVALID", "基础镜像没有固定", reason="unpinned-base", base=image
            )
        if image in stages or image == "scratch" or image in allowed or _PINNED.fullmatch(image):
            if "AS" in [item.upper() for item in words[index:]]:
                alias_at = [item.upper() for item in words].index("AS")
                if alias_at + 1 < len(words):
                    stages.add(words[alias_at + 1])
            continue
        raise FactoryError("IMAGE_CONTEXT_INVALID", "基础镜像没有固定", reason="unpinned-base", base=image)
    # stage names are recorded above only when the base is accepted; record them in the same pass
    _record_stages(text, stages)


def _record_stages(text: str, stages: set[str]) -> None:
    for line in _logical_lines(text):
        parts = line.split()
        if not parts or parts[0].lower() != "from":
            continue
        upper = [item.upper() for item in parts]
        if "AS" not in upper:
            continue
        at = upper.index("AS")
        if at + 1 < len(parts):
            stages.add(parts[at + 1])


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buf and (stripped == "" or stripped.startswith("#")):
            continue
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        buf += raw
        if buf.strip() and not buf.strip().startswith("#"):
            lines.append(buf.strip())
        buf = ""
    if buf.strip():
        lines.append(buf.strip())
    return lines


def _slug(path: str) -> str:
    return _SLUG.sub("-", path.replace("/", "-").lower())


def _image_present(docker: str, image_id: str) -> bool:
    try:
        proc = subprocess.run(
            [docker, "image", "inspect", image_id],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _row(row: Mapping[str, Any]) -> ImageBuild:
    error = None
    if row["error"]:
        error = json.loads(str(row["error"]))
    return ImageBuild(
        str(row["id"]),
        str(row["path"]),
        str(row["commit"]),
        str(row["context_digest"]),
        str(row["state"]),
        None if row["image_id"] is None else str(row["image_id"]),
        None if row["ref"] is None else str(row["ref"]),
        str(row["log"]),
        error,
        str(row["by"]),
        str(row["at"]),
    )
