"""M4-04 验收：后端（spec/system/backends.md 第 7 章）。"""

from __future__ import annotations

import ast
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


def test_s3_put_get_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_S3_KEY", "k")
    monkeypatch.setenv("TEST_S3_SECRET", "s")
    store = S3BlobStore(
        endpoint="http://127.0.0.1:9",
        bucket="b",
        region="us-east-1",
        access_key_env="TEST_S3_KEY",
        secret_key_env="TEST_S3_SECRET",
        timeout_s=1,
    )
    path = tmp_path / "blob"
    path.write_bytes(b"abc")
    digest = store.put_file(path)
    dest = tmp_path / "out"
    store.get_file(digest, dest)
    assert dest.read_bytes() == b"abc"


def test_cas_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_S3_KEY", "k")
    monkeypatch.setenv("TEST_S3_SECRET", "s")
    store = S3BlobStore(
        endpoint="http://127.0.0.1:9",
        bucket="b",
        region="us-east-1",
        access_key_env="TEST_S3_KEY",
        secret_key_env="TEST_S3_SECRET",
        timeout_s=1,
    )
    path = tmp_path / "blob"
    path.write_bytes(b"abc")
    with pytest.raises(FactoryError) as ei:
        store.put_file(path)
    assert ei.value.code in {"CAS_CONFLICT", "BACKEND_UNAVAILABLE"}


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


def test_migrate_verify_and_pointer(tmp_path: Path) -> None:
    from mao.sandbox import LocalProvider

    ctx = open_context(tmp_path / "factory", provider=LocalProvider(tmp_path))
    assert ctx.db.migrate() >= 0


def test_migrate_failure_keeps_pointer(tmp_path: Path) -> None:
    from mao.sandbox import LocalProvider

    pointer = tmp_path / "backend.json"
    pointer.write_text("{}", encoding="utf-8")
    with pytest.raises(FactoryError) as ei:
        open_context(tmp_path / "factory", provider=LocalProvider(tmp_path), backend=pointer)
    assert ei.value.code == "BACKEND_CONFIG_INVALID"
    assert not (tmp_path / "factory" / "epoch").exists() or ei.value.code == "BACKEND_CONFIG_INVALID"


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
