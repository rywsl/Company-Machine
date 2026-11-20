"""M9-04 验收：S3 请求带可校验的 SigV4、命令按指针使用后端、Postgres 只经 psql（spec/system/closure.md 5.4）。

假 S3 用测试里的密钥按 AWS SigV4 重算签名，签名不符回 403。假 psql 是放在 PATH 前面的标准库脚本，
逐次追加记录 argv 与 stdin。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mao.backends")

from mao.backends import PostgresStore, S3BlobStore  # noqa: E402
from mao.errors import FactoryError  # noqa: E402

ACCESS = "AKIDCLOSURE"
SECRET = "closure-secret-value-7"
REPO = Path(__file__).resolve().parents[3]
AUTH_RE = re.compile(
    r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request,\s*"
    r"SignedHeaders=([a-z0-9;-]+),\s*Signature=([0-9a-f]{64})"
)


def _signature_ok(handler: BaseHTTPRequestHandler, body: bytes) -> bool:
    match = AUTH_RE.fullmatch(handler.headers.get("Authorization", ""))
    if match is None:
        return False
    access, day, region, signed, signature = match.groups()
    amz_date = handler.headers.get("x-amz-date", "")
    payload = handler.headers.get("x-amz-content-sha256", "")
    names = signed.split(";")
    if access != ACCESS or region != "us-east-1" or not amz_date.startswith(day + "T"):
        return False
    if payload != hashlib.sha256(body).hexdigest():
        return False
    if names != sorted(names) or not {"host", "x-amz-content-sha256", "x-amz-date"} <= set(names):
        return False
    headers = "".join(f"{name}:{' '.join(handler.headers.get(name, '').split())}\n" for name in names)
    path, _, query = handler.path.partition("?")
    canonical = "\n".join([handler.command, path, query, headers, signed, payload])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + SECRET).encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest(), signature)


class S3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.accepted = 0
        self.rejected = 0
        self.headers: list[str] = []
        s3 = self

        class Handler(BaseHTTPRequestHandler):
            def _check(self, body: bytes) -> bool:
                s3.headers.append(json.dumps(dict(self.headers.items())))
                if _signature_ok(self, body):
                    s3.accepted += 1
                    return True
                s3.rejected += 1
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False

            def do_HEAD(self) -> None:  # noqa: N802
                if not self._check(b""):
                    return
                body = s3.objects.get(self.path)
                self.send_response(404 if body is None else 200)
                if body is not None:
                    self.send_header("x-amz-meta-sha256", hashlib.sha256(body).hexdigest())
                self.send_header("Content-Length", "0" if body is None else str(len(body)))
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                if not self._check(b""):
                    return
                body = s3.objects.get(self.path)
                self.send_response(404 if body is None else 200)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def do_PUT(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if not self._check(body):
                    return
                s3.objects[self.path] = body
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _store(endpoint: str) -> S3BlobStore:
    return S3BlobStore(
        endpoint=endpoint,
        bucket="b",
        region="us-east-1",
        access_key_env="CLOSURE_S3_KEY",
        secret_key_env="CLOSURE_S3_SECRET",
        timeout_s=5,
    )


def test_s3_requests_signed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSURE_S3_KEY", ACCESS)
    monkeypatch.setenv("CLOSURE_S3_SECRET", SECRET)
    s3 = S3()
    try:
        store = _store(s3.endpoint)
        blob = tmp_path / "blob"
        blob.write_bytes(b"signed-bytes")
        digest = store.put_file(blob)
        assert store.put_file(blob) == digest
        out = tmp_path / "out"
        store.get_file(digest, out)
    finally:
        s3.close()
    assert digest == "sha256:" + hashlib.sha256(b"signed-bytes").hexdigest()
    assert out.read_bytes() == b"signed-bytes"
    assert list(s3.objects.values()) == [b"signed-bytes"]
    assert s3.rejected == 0
    assert s3.accepted >= 4
    assert all(SECRET not in line for line in s3.headers)


def test_s3_bad_secret_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSURE_S3_KEY", ACCESS)
    monkeypatch.setenv("CLOSURE_S3_SECRET", "wrong-secret-value-3")
    s3 = S3()
    try:
        blob = tmp_path / "blob"
        blob.write_bytes(b"never-stored")
        with pytest.raises(FactoryError) as ei:
            _store(s3.endpoint).put_file(blob)
    finally:
        s3.close()
    assert ei.value.code == "BACKEND_UNAVAILABLE"
    assert ei.value.detail["reason"] == "http-403"
    assert "wrong-secret-value-3" not in str(ei.value)
    assert "wrong-secret-value-3" not in json.dumps(ei.value.detail, default=str)
    assert s3.objects == {}


def test_commands_use_active_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mao.cli import main
    from mao.commands.common import open_context
    from mao.context import Context
    from mao.sandbox import LocalProvider

    monkeypatch.setenv("CLOSURE_S3_KEY", ACCESS)
    monkeypatch.setenv("CLOSURE_S3_SECRET", SECRET)
    root = tmp_path / "factory"
    local = Context.open(root, provider=LocalProvider(root))
    seed = tmp_path / "seed"
    seed.write_bytes(b"before-switch")
    local.warehouse.put_file(seed)
    local.close()
    s3 = S3()
    try:
        config = tmp_path / "backend.json"
        blobs = {
            "kind": "s3",
            "endpoint": s3.endpoint,
            "bucket": "b",
            "region": "us-east-1",
            "addressing": "path",
            "access_key_env": "CLOSURE_S3_KEY",
            "secret_key_env": "CLOSURE_S3_SECRET",
            "timeout": "5s",
        }
        config.write_text(json.dumps({"blobs": blobs}), encoding="utf-8")
        assert main(["--root", str(root), "backend", "migrate", "--from", "local", "--to", str(config)]) == 0
        assert (root / "backend-active.json").is_file()
        assert b"before-switch" in s3.objects.values()
        config.unlink()
        ctx = open_context(argparse.Namespace(root=str(root), provider="local", local_commands=None))
        try:
            fresh = tmp_path / "fresh"
            fresh.write_bytes(b"after-switch")
            digest = ctx.warehouse.put_file(fresh)
            cached = ctx.warehouse.blob_path(digest)
            assert cached.read_bytes() == b"after-switch"
        finally:
            ctx.close()
    finally:
        s3.close()
    assert b"after-switch" in s3.objects.values()
    hexdigest = digest.split(":", 1)[1]
    assert not (root / "cas" / "sha256" / hexdigest[:2] / hexdigest[2:]).exists()
    assert s3.rejected == 0


def _fake_psql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> Path:
    log = tmp_path / "psql-log.jsonl"
    script = tmp_path / "psql_fake.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "stdin = sys.stdin.read()\n"
        "entry = {'argv': sys.argv[1:], 'stdin': stdin, 'pgpassfile': os.environ.get('PGPASSFILE', '')}\n"
        f"with Path({str(log)!r}).open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(entry) + '\\n')\n"
        "if 'row_to_json' in stdin:\n"
        f"    for row in {rows!r}:\n"
        "        sys.stdout.write(json.dumps(row) + '\\n')\n",
        encoding="utf-8",
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "psql.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    posix = bindir / "psql"
    posix.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8", newline="\n")
    posix.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("CLOSURE_DSN", "postgresql://factory:pw-secret-9@db.internal:5432/factory")
    return log


def _entries(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_postgres_migrate_through_psql(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_psql(tmp_path, monkeypatch, rows=[])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("FACTORY_ROOT", str(tmp_path / "factory-root"))
    store = PostgresStore(dsn_env="CLOSURE_DSN", psql="psql", timeout_s=10)
    try:
        version = store.migrate()
    finally:
        store.close()
    migrations = sorted((REPO / "src" / "mao" / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    assert version == int(migrations[-1].name[:3])
    entries = _entries(log)
    sent = "\n".join(entry["stdin"] for entry in entries)
    assert "CREATE TABLE" in sent and "schema_migrations" in sent and "egress_plans" in sent
    assert "PRAGMA" not in sent.upper() and "AUTOINCREMENT" not in sent.upper()
    assert "GENERATED BY DEFAULT AS IDENTITY" in sent
    for entry in entries:
        assert "-X" in entry["argv"] and "--no-password" in entry["argv"]
        assert "ON_ERROR_STOP=1" in entry["argv"]
        assert "pw-secret-9" not in json.dumps(entry["argv"]) and "pw-secret-9" not in entry["stdin"]
        assert entry["pgpassfile"]
    created = [p for p in tmp_path.rglob("*") if p.suffix in {".db", ".sqlite", ".sqlite3"}]
    assert created == []
    assert not (tmp_path / "factory-root").exists()


def test_postgres_query_binds_and_parses_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_psql(tmp_path, monkeypatch, rows=[{"id": "r1", "name": "o'brien", "n": 2}])
    store = PostgresStore(dsn_env="CLOSURE_DSN", psql="psql", timeout_s=10)
    try:
        rows = store.query("SELECT id, name, n FROM acceptance_rows WHERE name = ? AND n > ?", ["o'brien", 1])
    finally:
        store.close()
    assert [(row["id"], row["name"], row["n"]) for row in rows] == [("r1", "o'brien", 2)]
    sent = _entries(log)[-1]["stdin"]
    assert "row_to_json" in sent
    assert "$1" in sent and "$2" in sent and "\\bind" in sent
    assert "o'brien" not in sent


FORBIDDEN = ("INSERT OR REPLACE", "INSERT OR IGNORE", "sqlite_master", "AUTOINCREMENT", "PRAGMA")
ALLOWED_FILES = {"db.py", "backends.py", "backup.py"}


def test_runtime_sql_is_portable() -> None:
    found = []
    for path in sorted((REPO / "src" / "mao").rglob("*.py")):
        if path.name in ALLOWED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                hits = [token for token in FORBIDDEN if token in text]
                if re.search(r"\browid\b", text):
                    hits.append("rowid")
                found += [f"{path.relative_to(REPO).as_posix()}:{node.lineno}:{token}" for token in hits]
    assert found == []
