"""SQLite：WAL、显式事务与编号迁移。"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from mao.errors import FactoryError

DEFAULT_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^([0-9]{3})_[a-z0-9_]+\.sql$")


class _Tx:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._savepoint = ""

    def __enter__(self) -> sqlite3.Connection:
        db = self._db
        db._lock.acquire()
        try:
            if db._depth == 0:
                try:
                    db._conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    text = str(exc).lower()
                    if "locked" in text or "busy" in text:
                        raise FactoryError("DB_BUSY", "数据库被其他进程锁住", error=str(exc)) from exc
                    raise
                db._depth = 1
            else:
                name = f"sp_{db._depth}"
                db._conn.execute(f"SAVEPOINT {name}")
                db._depth += 1
                self._savepoint = name
            return db._conn
        except BaseException:
            db._lock.release()
            raise

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        db = self._db
        try:
            if self._savepoint:
                name = self._savepoint
                if exc_type is None:
                    db._conn.execute(f"RELEASE {name}")
                else:
                    db._conn.execute(f"ROLLBACK TO {name}")
                    db._conn.execute(f"RELEASE {name}")
                db._depth -= 1
            else:
                if exc_type is None:
                    db._conn.execute("COMMIT")
                else:
                    db._conn.execute("ROLLBACK")
                db._depth = 0
        finally:
            db._lock.release()


class Database:
    path: Path

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        mode_row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        mode = "" if mode_row is None else str(mode_row[0])
        if mode.lower() != "wal":
            self._conn.close()
            raise FactoryError("DB_UNSUPPORTED", "无法启用 WAL 模式", mode=mode)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._depth = 0

    def tx(self) -> _Tx:
        return _Tx(self)

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def close(self) -> None:
        self._conn.close()

    def migrate(self, migrations_dir: Path | None = None) -> int:
        if self._depth > 0:
            raise RuntimeError("不能在事务里调用 migrate")
        directory = Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS
        with self._lock:
            files = self._migration_files(directory)
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            latest = max((n for n, _path, _sql in files), default=0)
            if current > latest:
                raise FactoryError("DB_TOO_NEW", "数据库版本高于现有迁移", version=current, latest=latest)
            for number, path, sql in files:
                if number <= current:
                    continue
                script = "BEGIN IMMEDIATE;\n" + sql + f"\n;PRAGMA user_version = {number};\nCOMMIT;"
                try:
                    self._conn.executescript(script)
                except sqlite3.Error as exc:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise FactoryError(
                        "DB_MIGRATION_FAILED",
                        "迁移执行失败",
                        file=path.name,
                        error=str(exc),
                    ) from exc
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def _migration_files(self, directory: Path) -> list[tuple[int, Path, str]]:
        if not directory.is_dir():
            return []
        found: list[tuple[int, Path, str]] = []
        seen: set[int] = set()
        for path in sorted(directory.glob("*.sql")):
            match = _NAME.fullmatch(path.name)
            if match is None or int(match.group(1)) == 0:
                raise FactoryError("DB_MIGRATION_INVALID", "迁移文件名不合法", file=path.name)
            number = int(match.group(1))
            if number in seen:
                raise FactoryError("DB_MIGRATION_INVALID", "迁移编号重复", file=path.name)
            seen.add(number)
            found.append((number, path, path.read_text(encoding="utf-8")))
        found.sort(key=lambda item: item[0])
        return found
