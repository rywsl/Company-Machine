"""工厂根目录装配。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import RandomIds, SystemClock
from mao.db import Database
from mao.warehouse import Warehouse

FACTORYCTL_SOURCE = Path(__file__).resolve().parent / "ctl" / "factoryctl.py"


@dataclass
class Context:
    root: Path
    clock: Any
    ids: Any
    db: Database
    warehouse: Warehouse
    provider: Any

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        provider: Any,
        clock: Any = None,
        ids: Any = None,
    ) -> Context:
        root = Path(root).resolve()
        for path in (root, root / "cas", root / "jobs", root / "tmp", root / "tools" / "factory" / "bin"):
            path.mkdir(parents=True, exist_ok=True)
        db = Database(root / "factory.db")
        db.migrate()
        install_factoryctl(root)
        clock = clock if clock is not None else SystemClock()
        ids = ids if ids is not None else RandomIds()
        return cls(root, clock, ids, db, Warehouse(root, db, clock, ids), provider)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Context:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def install_factoryctl(root: Path, source: Path | None = None) -> Path | None:
    source = source or FACTORYCTL_SOURCE
    if not source.is_file():
        return None
    data = source.read_bytes().replace(b"\r\n", b"\n")
    target = Path(root) / "tools" / "factory" / "bin" / "factoryctl"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == data:
        return target
    part = Path(root) / "tmp" / f"factoryctl.{os.getpid()}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(data)
    os.replace(part, target)
    os.chmod(target, 0o755)
    return target
