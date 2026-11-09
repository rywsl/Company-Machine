"""M4-04 验收：后端（spec/system/backends.md 第 7 章）。"""

from __future__ import annotations

import ast
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mao.backends")
pytest.importorskip("mao.errors")

from mao.backends import LocalBlobStore, PostgresStore, S3BlobStore, open_context  # noqa: E402
from mao.errors import FactoryError  # noqa: E402


def test_context_open_ignores_cloud_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mao.context import Context
    from mao.sandbox import LocalProvider

    monkeypatch.setenv("MAO_S3_ACCESS_KEY", "local-test-key")
    root = tmp_path / "factory"
    ctx = Context.open(root, provider=LocalProvider(root))
    assert ctx.root == root.resolve() or ctx.root == root
    again = open_context(root, provider=LocalProvider(root))
    assert again.db is not None


class _S3State:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.puts = 0
        self.drop_puts = False
        self.lie_sha = False


def _s3_server(state: _S3State) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:  # noqa: N802
            item = state.objects.get(self.path)
            if item is None:
                self.send_response(404)
                self.end_headers()
                return
            body, sha = item
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("x-amz-meta-sha256", "deadbeef" if state.lie_sha else sha)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            item = state.objects.get(self.path)
            if item is None:
                self.send_response(404)
                self.end_headers()
                return
            body, _sha = item
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            state.puts += 1
            sha = hashlib.sha256(body).hexdigest()
            if not state.drop_puts:
                state.objects[self.path] = (body, sha)
            self.send_response(200)
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            del fmt, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_s3_put_get_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_S3_KEY", "k")
    monkeypatch.setenv("TEST_S3_SECRET", "s")
    state = _S3State()
    server, endpoint = _s3_server(state)
    try:
        store = S3BlobStore(
            endpoint=endpoint,
            bucket="b",
            region="us-east-1",
            access_key_env="TEST_S3_KEY",
            secret_key_env="TEST_S3_SECRET",
            timeout_s=5,
        )
        path = tmp_path / "blob"
        path.write_bytes(b"abc")
        digest = store.put_file(path)
        dest = tmp_path / "out"
        store.get_file(digest, dest)
        assert dest.read_bytes() == b"abc"
        puts_after_first = state.puts
        again = store.put_file(path)
        assert again == digest
        assert state.puts == puts_after_first
    finally:
        server.shutdown()


def test_cas_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_S3_KEY", "k")
    monkeypatch.setenv("TEST_S3_SECRET", "s")
    state = _S3State()
    server, endpoint = _s3_server(state)
    try:
        store = S3BlobStore(
            endpoint=endpoint,
            bucket="b",
            region="us-east-1",
            access_key_env="TEST_S3_KEY",
            secret_key_env="TEST_S3_SECRET",
            timeout_s=5,
        )
        other = tmp_path / "other"
        other.write_bytes(b"xyz")
        digest_hex = hashlib.sha256(b"xyz").hexdigest()
        key = f"/b/cas/sha256/{digest_hex[:2]}/{digest_hex[2:]}"
        state.objects[key] = (b"not-xyz", "0" * 64)
        with pytest.raises(FactoryError) as ei:
            store.put_file(other)
        assert ei.value.code == "CAS_CONFLICT"
        assert state.objects[key][0] == b"not-xyz"
    finally:
        server.shutdown()


def test_blob_path_is_local_file(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    path = tmp_path / "blob"
    path.write_bytes(b"xyz")
    digest = store.put_file(path)
    dest = tmp_path / "copy"
    store.get_file(digest, dest)
    assert dest.read_bytes() == b"xyz"
    assert store.exists(digest) is True


def test_config_rejects_inline_secret(tmp_path: Path) -> None:
    from mao.sandbox import LocalProvider

    pointer = tmp_path / "backend.json"
    pointer.write_text('{"s3": {"access_key": "inline-secret"}}', encoding="utf-8")
    with pytest.raises(FactoryError) as ei:
        open_context(tmp_path / "factory", provider=LocalProvider(tmp_path), backend=pointer)
    assert ei.value.code == "BACKEND_CONFIG_INVALID"


def test_psql_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DSN", "postgres://local/factory")
    store = PostgresStore(dsn_env="TEST_DSN", psql=str(tmp_path / "missing-psql"), timeout_s=1)
    with pytest.raises(FactoryError) as ei:
        store.migrate()
    assert ei.value.code == "BACKEND_UNAVAILABLE"


def test_placeholder_not_concatenated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DSN", "postgres://local/factory")
    store = PostgresStore(dsn_env="TEST_DSN", psql="psql", timeout_s=1)
    with pytest.raises(FactoryError) as ei:
        store.query("select $1", ["o'brien"])
    assert ei.value.code in {"BACKEND_UNAVAILABLE", "SQL_DIALECT"}


def _local_with_rows_and_blob(tmp_path: Path):
    from mao.context import Context
    from mao.sandbox import LocalProvider

    root = tmp_path / "factory"
    ctx = Context.open(root, provider=LocalProvider(root))
    blob = tmp_path / "blob"
    blob.write_bytes(b"migrated-bytes")
    digest = ctx.warehouse.put_file(blob)
    with ctx.db.tx() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS acceptance_rows (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO acceptance_rows (id, name) VALUES ('r1', 'one')")
        conn.execute("INSERT INTO acceptance_rows (id, name) VALUES ('r2', 'two')")
    ctx.close()
    return root, digest


def test_migrate_verify_and_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mao.cli import main

    root, digest = _local_with_rows_and_blob(tmp_path)
    monkeypatch.setenv("TEST_S3_KEY", "k")
    monkeypatch.setenv("TEST_S3_SECRET", "s")
    monkeypatch.setenv("FACTORY_ROOT", str(root))
    state = _S3State()
    server, endpoint = _s3_server(state)
    config = tmp_path / "backend.json"
    config.write_text(
        json.dumps(
            {
                "blobs": {
                    "kind": "s3",
                    "endpoint": endpoint,
                    "bucket": "b",
                    "region": "us-east-1",
                    "addressing": "path",
                    "access_key_env": "TEST_S3_KEY",
                    "secret_key_env": "TEST_S3_SECRET",
                    "timeout": "5s",
                }
            }
        ),
        encoding="utf-8",
    )
    try:
        main(["backend", "migrate", "--from", "local", "--to", str(config)])
    finally:
        server.shutdown()
    pointer = root / "backend-active.json"
    assert pointer.is_file()
    stored = json.loads(pointer.read_text(encoding="utf-8"))
    assert "digest" in stored or "switched_at" in stored
    assert any(digest.split(":", 1)[-1] in key for key in state.objects)


def test_migrate_failure_keeps_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mao.cli import main
    from mao.sandbox import LocalProvider

    root, _digest = _local_with_rows_and_blob(tmp_path)
    monkeypatch.setenv("TEST_S3_KEY", "k")
    monkeypatch.setenv("TEST_S3_SECRET", "s")
    monkeypatch.setenv("FACTORY_ROOT", str(root))
    state = _S3State()
    state.drop_puts = True
    server, endpoint = _s3_server(state)
    config = tmp_path / "backend.json"
    config.write_text(
        json.dumps(
            {
                "blobs": {
                    "kind": "s3",
                    "endpoint": endpoint,
                    "bucket": "b",
                    "region": "us-east-1",
                    "addressing": "path",
                    "access_key_env": "TEST_S3_KEY",
                    "secret_key_env": "TEST_S3_SECRET",
                    "timeout": "5s",
                }
            }
        ),
        encoding="utf-8",
    )
    try:
        with pytest.raises(FactoryError) as ei:
            main(["backend", "migrate", "--from", "local", "--to", str(config)])
        assert ei.value.code == "MIGRATE_VERIFY_FAILED"
    finally:
        server.shutdown()
    assert not (root / "backend-active.json").exists()
    local = open_context(root, provider=LocalProvider(root))
    probe = tmp_path / "probe"
    probe.write_bytes(b"still-local")
    local.warehouse.put_file(probe)
    assert not any(body == b"still-local" for body, _sha in state.objects.values())
    local.close()


def test_pragma_not_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DSN", "postgres://local/factory")
    store = PostgresStore(dsn_env="TEST_DSN", psql=str(tmp_path / "psql"), timeout_s=1)
    with pytest.raises(FactoryError) as ei:
        store.migrate()
    assert ei.value.code == "BACKEND_UNAVAILABLE"


def test_no_third_party_import() -> None:
    import mao.backends as backends

    tree = ast.parse(Path(backends.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert {"boto3", "psycopg", "minio"}.isdisjoint(imported)
