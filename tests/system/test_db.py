"""M1-01 单元：mao.db（spec/system/foundation.md 5.8–5.10）。模块缺失时整文件跳过。"""

from __future__ import annotations

import pytest

pytest.importorskip("mao.db")

from mao.db import Database  # noqa: E402


def test_journal_mode_is_wal(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        assert db.query("PRAGMA journal_mode")[0][0].lower() == "wal"
    finally:
        db.close()


def test_query_returns_row_mapping(tmp_path) -> None:
    db = Database(tmp_path / "factory.db")
    try:
        db.query("CREATE TABLE t (n INTEGER)")
        with db.tx() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
        assert db.query("SELECT n FROM t")[0]["n"] == 1
    finally:
        db.close()


def test_tx_rollback_on_exception(tmp_path) -> None:
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


def test_nested_tx_rolls_back_inner_only(tmp_path) -> None:
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


def test_migrate_applies_gaps_and_is_idempotent(tmp_path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_a.sql").write_text("CREATE TABLE a (n INTEGER);", encoding="utf-8")
    (mig / "003_b.sql").write_text("CREATE TABLE b (n INTEGER);", encoding="utf-8")
    db = Database(tmp_path / "factory.db")
    try:
        assert db.migrate(mig) == 3
        assert db.migrate(mig) == 3
        assert db.query("SELECT name FROM sqlite_master WHERE name = 'b'")
    finally:
        db.close()
