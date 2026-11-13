"""对象存储与 Postgres 状态存储。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from mao.base import SystemClock, canonical_json, digest_bytes, parse_duration, utc_iso
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
        hexpart = digest.split(":", 1)[-1]
        return f"/{self.bucket}/cas/sha256/{hexpart[:2]}/{hexpart[2:]}"

    def _request(self, method: str, key: str, body: bytes | None = None) -> Any:
        payload = body if body is not None else b""
        headers = self._signed_headers(method, key, payload)
        secret = os.environ.get(self.secret_key_env, "")
        request = Request(self.endpoint + key, data=body, headers=headers, method=method)
        try:
            return urlopen(request, timeout=self.timeout_s)
        except HTTPError as exc:
            if exc.code == 404 and method in ("HEAD", "GET"):
                return None
            raise self._unavailable(f"http-{exc.code}", secret) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise self._unavailable("transport", secret) from exc

    def _signed_headers(self, method: str, key: str, payload: bytes) -> dict[str, str]:
        access = os.environ.get(self.access_key_env, "")
        secret = os.environ.get(self.secret_key_env, "")
        parts = urlsplit(self.endpoint)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        stamp = SystemClock().now().strftime("%Y%m%dT%H%M%SZ")
        day = stamp[:8]
        payload_hash = hashlib.sha256(payload).hexdigest()
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": stamp,
        }
        if method == "PUT":
            headers["x-amz-meta-sha256"] = payload_hash
            headers["content-type"] = "application/octet-stream"
        names = sorted(headers)
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in names)
        signed = ";".join(names)
        canonical_uri = "/" + "/".join(quote(segment, safe="") for segment in key.strip("/").split("/"))
        canonical = "\n".join([method, canonical_uri, "", canonical_headers, signed, payload_hash])
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()]
        )
        signing = ("AWS4" + secret).encode("utf-8")
        for part in (day, self.region, "s3", "aws4_request"):
            signing = hmac.new(signing, part.encode("utf-8"), hashlib.sha256).digest()
        signature = hmac.new(signing, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}"
        )
        return headers

    def _unavailable(self, reason: str, secret: str) -> FactoryError:
        detail = {"reason": reason}
        if secret and secret in json.dumps(detail):
            detail = {"reason": "redacted"}
        return FactoryError("BACKEND_UNAVAILABLE", "对象存储拒绝了请求", **detail)


class PostgresStore:
    def __init__(self, *, dsn_env: str, psql: str, timeout_s: float) -> None:
        self.dsn_env = dsn_env
        self.psql = psql
        self.timeout_s = timeout_s
        self._pgpass: Path | None = None

    def close(self) -> None:
        path = self._pgpass
        self._pgpass = None
        if path is not None:
            path.unlink(missing_ok=True)

    def migrate(self) -> int:
        directory = Path(__file__).resolve().parent / "migrations"
        files = sorted(path for path in directory.glob("[0-9][0-9][0-9]_*.sql"))
        try:
            applied = {int(row["version"]) for row in self.query("SELECT version FROM schema_migrations", [])}
        except FactoryError:
            applied = set()
        pending = [(int(path.name[:3]), path.read_text(encoding="utf-8")) for path in files]
        pending = [(number, text) for number, text in pending if number not in applied]
        statements = [
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        ]
        now = utc_iso(SystemClock().now())
        for number, text in pending:
            statements.append(_translate_migration(text))
            statements.append(
                f"INSERT INTO schema_migrations (version, applied_at) VALUES ({number}, '{now}')"
            )
        self._send("\n".join(statements) + "\n", bind=False)
        return max((number for number, _text in [(int(path.name[:3]), path) for path in files]), default=0)

    def query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        text, bound = _bind_sql(sql, list(params))
        wrapped = f"SELECT row_to_json(q)::text FROM ({text}) AS q\n\\bind {bound}\\g\n"
        stdout = self._send(wrapped, bind=True)
        rows = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows

    def _send(self, text: str, *, bind: bool) -> str:
        del bind
        binary = shutil.which(self.psql) or self.psql
        if shutil.which(binary) is None and not Path(binary).exists():
            raise FactoryError("BACKEND_UNAVAILABLE", "找不到 psql", reason="psql-missing")
        dsn = os.environ.get(self.dsn_env, "")
        argv, env = self._argv(dsn)
        try:
            proc = subprocess.run(
                argv,
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FactoryError("BACKEND_UNAVAILABLE", "psql 无法启动", reason="psql-failed") from exc
        if proc.returncode != 0:
            raise FactoryError("BACKEND_UNAVAILABLE", "psql 失败", reason="psql-failed")
        return proc.stdout.decode("utf-8")

    def _argv(self, dsn: str) -> tuple[list[str], dict[str, str]]:
        parsed = urlsplit(dsn)
        host = parsed.hostname or ""
        port = str(parsed.port or 5432)
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        dbname = unquote(parsed.path.lstrip("/"))
        pgpass = self._passfile(host, port, dbname, user, password)
        binary = shutil.which(self.psql) or self.psql
        argv = [
            binary,
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "--no-password",
            "-h",
            host,
            "-p",
            port,
            "-U",
            user,
            "-d",
            dbname,
        ]
        env = dict(os.environ)
        env["PGPASSFILE"] = str(pgpass)
        return argv, env

    def _passfile(self, host: str, port: str, dbname: str, user: str, password: str) -> Path:
        if self._pgpass is None:
            handle = tempfile.NamedTemporaryFile(prefix="mao-pgpass-", delete=False)
            handle.close()
            self._pgpass = Path(handle.name)
            try:
                os.chmod(self._pgpass, 0o600)
            except OSError:
                pass
        line = ":".join(_pgpass_field(item) for item in (host, port, dbname, user, password))
        self._pgpass.write_text(line + "\n", encoding="utf-8")
        return self._pgpass


def open_context(root: Path, *, provider: Any, backend: Path | None = None) -> Any:
    from mao.context import Context

    root = Path(root)
    pointer = Path(backend) if backend is not None else root / "backend-active.json"
    if not pointer.is_file():
        return Context.open(root, provider=provider)
    try:
        loaded = json.loads(pointer.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FactoryError("BACKEND_CONFIG_INVALID", "后端配置不是 JSON") from exc
    config = (
        loaded.get("config")
        if isinstance(loaded, dict) and isinstance(loaded.get("config"), dict)
        else loaded
    )
    if _inline_secret(config):
        raise FactoryError("BACKEND_CONFIG_INVALID", "密钥不能写进配置文件")
    ctx = Context.open(root, provider=provider)
    blobs = config.get("blobs") if isinstance(config, dict) else None
    if isinstance(blobs, dict) and blobs.get("kind") == "s3":
        ctx.warehouse.store = _s3_from_config(blobs)
    return ctx


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
            with tempfile.NamedTemporaryFile(prefix="mao-migrate-", delete=False) as handle:
                probe = Path(handle.name)
            try:
                store.get_file(digest, probe)
                if probe.read_bytes() != path.read_bytes():
                    raise FactoryError("MIGRATE_VERIFY_FAILED", "迁出后字节不一致", digest=digest)
            except FactoryError as exc:
                if exc.code == "MIGRATE_VERIFY_FAILED":
                    raise
                raise FactoryError("MIGRATE_VERIFY_FAILED", "迁出后读不回对象", digest=digest) from exc
            finally:
                probe.unlink(missing_ok=True)
    report = {"tables": [], "blobs": {"count": 0, "digest": digest_bytes(b"")}, "ok": True}
    pointer = Path(root) / "backend-active.json"
    pointer.write_text(
        canonical_json(
            {
                "config": body,
                "switched_at": utc_iso(SystemClock().now()),
                "report": digest_bytes(canonical_json(report).encode("utf-8")),
            }
        ),
        encoding="utf-8",
    )


def utc_iso_now() -> str:
    from mao.base import SystemClock

    return utc_iso(SystemClock().now())


def _s3_from_config(blobs: dict[str, Any]) -> S3BlobStore:
    timeout = blobs.get("timeout", "30s")
    return S3BlobStore(
        endpoint=str(blobs["endpoint"]),
        bucket=str(blobs["bucket"]),
        region=str(blobs.get("region") or "us-east-1"),
        access_key_env=str(blobs["access_key_env"]),
        secret_key_env=str(blobs["secret_key_env"]),
        timeout_s=parse_duration(str(timeout)),
        addressing=str(blobs.get("addressing") or "path"),
    )


def _translate_migration(sql: str) -> str:
    translated = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )
    if re.search(r"\bAUTOINCREMENT\b", translated, re.IGNORECASE):
        raise FactoryError("SQL_DIALECT", "无法翻译 AUTOINCREMENT")
    translated = re.sub(r"\bREAL\b", "DOUBLE PRECISION", translated)
    if re.search(r"\bPRAGMA\b", translated, re.IGNORECASE):
        raise FactoryError("SQL_DIALECT", "无法翻译 PRAGMA")
    return translated


def _bind_sql(sql: str, params: list[Any]) -> tuple[str, str]:
    pieces: list[str] = []
    bound: list[str] = []
    cursor = 0
    number = 0
    for value in params:
        pos = sql.find("?", cursor)
        if pos < 0:
            break
        pieces.append(sql[cursor:pos])
        if value is None:
            pieces.append("NULL")
        else:
            if isinstance(value, str) and "\x00" in value:
                raise FactoryError("SQL_DIALECT", "参数含 NUL")
            if isinstance(value, bool):
                text = "1" if value else "0"
            elif isinstance(value, float):
                text = format(value, ".16g")
            else:
                text = str(value)
            number += 1
            pieces.append(f"${number}")
            bound.append("'" + text.replace("\\", "\\\\").replace("'", "''") + "'")
        cursor = pos + 1
    pieces.append(sql[cursor:])
    return "".join(pieces), " ".join(bound)


def _pgpass_field(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:")


def _inline_secret(body: Any) -> bool:
    if isinstance(body, dict):
        if "access_key" in body and "access_key_env" not in body:
            return True
        return any(_inline_secret(value) for value in body.values())
    if isinstance(body, list):
        return any(_inline_secret(item) for item in body)
    return False
