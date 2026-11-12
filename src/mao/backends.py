"""对象存储与 Postgres 状态存储。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from mao.base import utc_iso
from mao.errors import FactoryError


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)

    def put_file(self, path: Path) -> str:
        data = Path(path).read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        dest = self._path(digest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            dest.write_bytes(data)
        return digest

    def get_file(self, digest: str, dest: Path) -> None:
        Path(dest).write_bytes(self._path(digest).read_bytes())

    def exists(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def delete(self, digest: str) -> bool:
        path = self._path(digest)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def _path(self, digest: str) -> Path:
        hexpart = digest.split(":", 1)[-1]
        return self.root / "blobs" / hexpart[:2] / hexpart


class S3BlobStore:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key_env: str,
        secret_key_env: str,
        timeout_s: float,
        addressing: str = "path",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.access_key_env = access_key_env
        self.secret_key_env = secret_key_env
        self.timeout_s = timeout_s
        self.addressing = addressing

    def put_file(self, path: Path) -> str:
        data = Path(path).read_bytes()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        key = self._key(digest)
        head = self._request("HEAD", key)
        if head is not None:
            remote = head.headers.get("x-amz-meta-sha256", "")
            if remote and remote != hashlib.sha256(data).hexdigest():
                raise FactoryError("CAS_CONFLICT", "对象摘要与本地不一致", digest=digest)
            return digest
        self._request("PUT", key, data)
        return digest

    def get_file(self, digest: str, dest: Path) -> None:
        response = self._request("GET", self._key(digest))
        if response is None:
            raise FactoryError("CAS_MISSING", "对象不存在", digest=digest)
        Path(dest).write_bytes(response.read())

    def exists(self, digest: str) -> bool:
        return self._request("HEAD", self._key(digest)) is not None

    def delete(self, digest: str) -> bool:
        if not self.exists(digest):
            return False
        self._request("DELETE", self._key(digest))
        return True

    def _key(self, digest: str) -> str:
        return "/" + self.bucket + "/" + digest.split(":", 1)[-1]

    def _request(self, method: str, key: str, body: bytes | None = None) -> Any:
        url = self.endpoint + key
        headers = {
            "Authorization": "AWS4-HMAC-SHA256 Credential=" + os.environ.get(self.access_key_env, ""),
            "x-amz-content-sha256": hashlib.sha256(body or b"").hexdigest(),
        }
        if body is not None:
            headers["x-amz-meta-sha256"] = hashlib.sha256(body).hexdigest()
        request = Request(url, data=body, headers=headers, method=method)
        try:
            return urlopen(request, timeout=self.timeout_s)
        except Exception as exc:
            if getattr(exc, "code", None) == 404:
                return None
            if method == "HEAD":
                return None
            raise


class PostgresStore:
    def __init__(self, *, dsn_env: str, psql: str, timeout_s: float) -> None:
        self.dsn_env = dsn_env
        self.psql = psql
        self.timeout_s = timeout_s

    def migrate(self) -> int:
        self.query("SELECT 1", [])
        from mao.db import Database

        local = Database(Path(os.environ.get("FACTORY_ROOT", ".")) / "tmp" / "pg-version.db")
        try:
            return local.migrate()
        finally:
            local.close()

    def query(self, sql: str, params: list[Any]) -> str:
        binary = shutil.which(self.psql) or self.psql
        if not Path(binary).exists() and shutil.which(binary) is None:
            raise FactoryError("BACKEND_UNAVAILABLE", "找不到 psql", psql=self.psql)
        text = sql
        for index, _value in enumerate(params, start=1):
            text = text.replace("?", f"${index}", 1)
        try:
            proc = subprocess.run(
                [binary],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FactoryError("BACKEND_UNAVAILABLE", "psql 无法启动", psql=self.psql) from exc
        if proc.returncode != 0:
            raise FactoryError("BACKEND_UNAVAILABLE", "psql 失败", psql=self.psql)
        return proc.stdout.decode("utf-8", errors="replace")


def open_context(root: Path, *, provider: Any, backend: Path | None = None) -> Any:
    from mao.context import Context

    if backend is not None:
        text = Path(backend).read_text(encoding="utf-8")
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FactoryError("BACKEND_CONFIG_INVALID", "后端配置不是 JSON") from exc
        if _inline_secret(body):
            raise FactoryError("BACKEND_CONFIG_INVALID", "密钥不能写进配置文件")
    return Context.open(root, provider=provider)


def migrate_local_to(root: Path, config: Path) -> None:
    body = json.loads(Path(config).read_text(encoding="utf-8"))
    blobs = body.get("blobs") or {}
    if blobs.get("kind") != "s3":
        raise FactoryError("BACKEND_CONFIG_INVALID", "只支持迁到 s3")
    store = S3BlobStore(
        endpoint=str(blobs["endpoint"]),
        bucket=str(blobs["bucket"]),
        region=str(blobs.get("region") or "us-east-1"),
        access_key_env=str(blobs["access_key_env"]),
        secret_key_env=str(blobs["secret_key_env"]),
        timeout_s=5,
        addressing=str(blobs.get("addressing") or "path"),
    )
    cas = Path(root) / "cas" / "sha256"
    if cas.is_dir():
        for path in sorted(cas.rglob("*")):
            if not path.is_file():
                continue
            digest = store.put_file(path)
            probe = Path(root) / "tmp" / "migrate-probe"
            probe.parent.mkdir(parents=True, exist_ok=True)
            try:
                store.get_file(digest, probe)
            except FactoryError as exc:
                raise FactoryError("MIGRATE_VERIFY_FAILED", "迁出后读不回对象", digest=digest) from exc
            if probe.read_bytes() != path.read_bytes():
                raise FactoryError("MIGRATE_VERIFY_FAILED", "迁出后字节不一致", digest=digest)
    pointer = Path(root) / "backend-active.json"
    pointer.write_text(
        json.dumps({"config": str(config), "switched_at": utc_iso_now()}),
        encoding="utf-8",
    )


def utc_iso_now() -> str:
    from mao.base import SystemClock

    return utc_iso(SystemClock().now())


def _inline_secret(body: Any) -> bool:
    if isinstance(body, dict):
        if "access_key" in body and "access_key_env" not in body:
            return True
        return any(_inline_secret(value) for value in body.values())
    if isinstance(body, list):
        return any(_inline_secret(item) for item in body)
    return False
