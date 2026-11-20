"""工厂根目录装配。"""

from __future__ import annotations

import os
import socket
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mao.base import RandomIds, SystemClock
from mao.db import Database
from mao.warehouse import Warehouse

FACTORYCTL_SOURCE = Path(__file__).resolve().parent / "ctl" / "factoryctl.py"
_OPEN: dict[str, list[weakref.ReferenceType[Any]]] = {}
_OPEN_LOCK = threading.Lock()


@dataclass
class Context:
    root: Path
    clock: Any
    ids: Any
    db: Database
    warehouse: Warehouse
    provider: Any
    proxies: Any = None
    owner: str = ""
    tokens: Any = None

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        provider: Any,
        clock: Any = None,
        ids: Any = None,
        proxies: Any = None,
        tokens: Any = None,
    ) -> Context:
        from mao.egress import SecretStore

        root = Path(root).resolve()
        SecretStore(root).install_log_filter()
        for path in (root, root / "cas", root / "jobs", root / "tmp", root / "tools" / "factory" / "bin"):
            path.mkdir(parents=True, exist_ok=True)
        db = Database(root / "factory.db")
        db.migrate()
        install_factoryctl(root)
        clock = clock if clock is not None else SystemClock()
        ids = ids if ids is not None else RandomIds()
        owner = f"{socket.gethostname()}:{os.getpid()}:{ids.new('o')}"
        ctx = cls(
            root,
            clock,
            ids,
            db,
            Warehouse(root, db, clock, ids),
            provider,
            proxies,
            owner,
            tokens,
        )
        _remember(ctx)
        return ctx

    def close(self) -> None:
        from mao.egress import stop_owned_forwarder

        stop_owned_forwarder(self)
        _forget(self)
        self.db.close()

    def __enter__(self) -> Context:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def factory_clock(root: Path) -> Any | None:
    """同一进程里已经打开的工厂时钟。命令行另开上下文时用它判断租约是否过期。"""
    key = str(Path(root).resolve())
    with _OPEN_LOCK:
        found = _OPEN.get(key, [])
        live = [item for ref in found if (item := ref()) is not None]
        _OPEN[key] = [weakref.ref(item) for item in live]
        if not live:
            _OPEN.pop(key, None)
            return None
        return live[0].clock


def _remember(ctx: Context) -> None:
    key = str(ctx.root)
    with _OPEN_LOCK:
        found = [ref for ref in _OPEN.get(key, []) if ref() is not None and ref() is not ctx]
        found.append(weakref.ref(ctx))
        _OPEN[key] = found


def _forget(ctx: Context) -> None:
    key = str(ctx.root)
    with _OPEN_LOCK:
        found = [ref for ref in _OPEN.get(key, []) if ref() is not None and ref() is not ctx]
        if found:
            _OPEN[key] = found
        else:
            _OPEN.pop(key, None)


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
