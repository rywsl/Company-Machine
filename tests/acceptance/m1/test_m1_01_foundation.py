"""M1-01 验收：基础设施（spec/system/foundation.md 第 7 章）。

模块还不存在时整个文件跳过。实现之后，下面每个测试都调用已交付的函数。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

pytest.importorskip("mao.base")
pytest.importorskip("mao.errors")
pytest.importorskip("mao.db")

from mao.base import (  # noqa: E402
    CounterIds,
    FakeClock,
    RandomIds,
    SystemClock,
    canonical_bytes,
    canonical_json,
    digest_bytes,
    digest_file,
    parse_duration,
    utc_iso,
)
from mao.db import Database  # noqa: E402
from mao.errors import CODES, FactoryError  # noqa: E402


def test_canonical_json_sorting_and_separators() -> None:
    assert canonical_json({"b": 1, "a": [1.5, "中"]}) == '{"a":[1.5,"中"],"b":1}'
    assert canonical_json((1, 2)) == "[1,2]"
    assert "\n" not in canonical_json({"a": 1})


def test_canonical_json_rejects_invalid() -> None:
    with pytest.raises(FactoryError) as ei:
        canonical_json({"a": float("nan")})
    assert ei.value.code == "JSON_INVALID"
    with pytest.raises(FactoryError) as ei:
        canonical_json({1: "x"})
    assert ei.value.code == "JSON_INVALID"


def test_canonical_bytes_is_utf8() -> None:
    assert canonical_bytes({"a": "中"}) == '{"a":"中"}'.encode()


def test_digest_bytes_known_vector() -> None:
    assert digest_bytes(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_digest_file_matches_bytes_and_streams(tmp_path) -> None:
    path = tmp_path / "blob"
    data = b"x" * (1024 * 1024 + 3)
    path.write_bytes(data)
    assert digest_file(path) == digest_bytes(data)


def test_utc_iso_formats_and_truncates() -> None:
    dt = datetime(2026, 1, 2, 3, 4, 5, 123999, tzinfo=UTC)
    assert utc_iso(dt) == "2026-01-02T03:04:05.123Z"
    shifted = datetime(2026, 1, 2, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    assert utc_iso(shifted) == "2026-01-02T00:00:00.000Z"


def test_utc_iso_rejects_naive() -> None:
    with pytest.raises(ValueError):
        utc_iso(datetime(2026, 1, 1))


def test_parse_duration_table() -> None:
    assert parse_duration("250ms") == 0.25
    assert parse_duration("90m") == 5400.0
    assert parse_duration("7d") == 604800.0
    assert parse_duration("007s") == 7.0


def test_parse_duration_invalid() -> None:
    for text in ("", "5", "5x", "-5s", "1.5h", " 5s", "5S", 5):
        with pytest.raises(FactoryError) as ei:
            parse_duration(text)  # type: ignore[arg-type]
        assert ei.value.code == "DURATION_INVALID"


def test_fake_clock() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FakeClock(start)
    assert clock.now() == start
    assert clock.now() == start
    clock.advance(1.5)
    clock.sleep(0.5)
    assert clock.monotonic() == 2.0
    assert clock.now() == start + timedelta(seconds=2)
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_fake_clock_requires_aware_start() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_counter_ids() -> None:
    ids = CounterIds()
    assert ids.new("j") == "j-0001"
    assert ids.new("j") == "j-0002"
    assert ids.new("a") == "a-0001"


def test_random_ids_format_and_uniqueness() -> None:
    ids = RandomIds()
    values = {ids.new("j") for _ in range(8)}
    assert len(values) == 8
    for value in values:
        prefix, suffix = value.split("-")
        assert prefix == "j" and len(suffix) == 10


def test_id_prefix_validation() -> None:
    with pytest.raises(ValueError):
        CounterIds().new("Job")


def test_factory_error_registered_and_unregistered() -> None:
    err = FactoryError("JSON_INVALID", "坏了", path="$.a")
    assert err.code == "JSON_INVALID"
    assert err.to_json()["detail"] == {"path": "$.a"}
    with pytest.raises(AssertionError):
        FactoryError("NO_SUCH_CODE", "x")


def test_codes_table_shape() -> None:
    assert "JSON_INVALID" in CODES
    assert "DURATION_INVALID" in CODES
    for code, text in CODES.items():
        assert code.isupper() and text


def test_codes_documented_in_specs() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    blob = "\n".join(p.read_text(encoding="utf-8") for p in (root / "spec" / "system").glob("*.md"))
    for code in CODES:
        assert f"`{code}`" in blob


def test_static_rules() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "mao"
    files = [path for path in root.rglob("*.py") if path.is_file()]
    assert files
    forbidden: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec"}
            ):
                forbidden.append(f"{path}:{node.lineno} {node.func.id}")
    assert forbidden == []


def test_database_pragmas_and_rows(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        rows = db.query("PRAGMA journal_mode")
        assert rows[0][0].lower() == "wal"
        db.query("CREATE TABLE t (n INTEGER)")
        with db.tx() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        got = db.query("SELECT n FROM t")
        assert got[0]["n"] == 1
    finally:
        db.close()


def test_tx_commit_and_rollback(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        db.query("CREATE TABLE t (n INTEGER)")
        with pytest.raises(RuntimeError):
            with db.tx() as conn:
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        assert db.query("SELECT COUNT(*) AS c FROM t")[0]["c"] == 0
    finally:
        db.close()


def test_nested_tx_uses_savepoints(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        db.query("CREATE TABLE t (n INTEGER)")
        with db.tx() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            try:
                with db.tx() as inner:
                    inner.execute("INSERT INTO t VALUES (2)")
                    raise RuntimeError("inner")
            except RuntimeError:
                pass
        assert [row["n"] for row in db.query("SELECT n FROM t")] == [1]
    finally:
        db.close()


def test_migrate_applies_in_order_with_gaps(tmp_path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_a.sql").write_text("CREATE TABLE a (n INTEGER);", encoding="utf-8")
    (mig / "003_b.sql").write_text("CREATE TABLE b (n INTEGER);", encoding="utf-8")
    db = Database(tmp_path / "factory.db")
    try:
        assert db.migrate(mig) == 3
        assert db.migrate(mig) == 3
    finally:
        db.close()


def test_migrate_invalid_names(tmp_path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "000_bad.sql").write_text("SELECT 1;", encoding="utf-8")
    db = Database(tmp_path / "factory.db")
    try:
        with pytest.raises(FactoryError) as ei:
            db.migrate(mig)
        assert ei.value.code == "DB_MIGRATION_INVALID"
    finally:
        db.close()


def test_migrate_failure_rolls_back(tmp_path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_bad.sql").write_text("THIS IS NOT SQL;", encoding="utf-8")
    db = Database(tmp_path / "factory.db")
    try:
        with pytest.raises(FactoryError) as ei:
            db.migrate(mig)
        assert ei.value.code == "DB_MIGRATION_FAILED"
        assert db.query("PRAGMA user_version")[0][0] == 0
    finally:
        db.close()


def test_migrate_too_new(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        with db.tx() as conn:
            conn.execute("PRAGMA user_version = 9")
        with pytest.raises(FactoryError) as ei:
            db.migrate(tmp_path / "missing")
        assert ei.value.code == "DB_TOO_NEW"
    finally:
        db.close()


def test_migrate_default_dir(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        version = db.migrate()
        assert isinstance(version, int)
    finally:
        db.close()
