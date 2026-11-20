"""定位 spec 目录：schemas、内核规则、参考宪法都以 spec/ 为唯一来源。"""

from __future__ import annotations

import os
from pathlib import Path


def spec_root() -> Path:
    env = os.environ.get("MRL_SPEC_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    dev = here.parents[3] / "spec"  # <repo>/src/mrl/factory/paths.py → <repo>/spec
    if (dev / "schemas").is_dir():
        return dev
    packaged = here.parents[1] / "_spec"
    if (packaged / "schemas").is_dir():
        return packaged
    raise RuntimeError("找不到 spec 目录；请设置环境变量 MRL_SPEC_DIR")


def schemas_dir() -> Path:
    return spec_root() / "schemas"


def kernel_dir() -> Path:
    return spec_root() / "mrl" / "kernel"


def reference_constitution_dir() -> Path:
    return spec_root() / "mrl" / "constitution"


_KERNEL_STAMP: tuple[str, tuple[tuple[str, int, int], ...]] | None = None
_KERNEL_TEXTS: list[tuple[str, str]] = []


def kernel_sources() -> list[tuple[str, str]]:
    """内核源码。文件没变时复用已读文本；返回新列表，调用方可以再拼接宪法。"""
    global _KERNEL_STAMP, _KERNEL_TEXTS
    directory = kernel_dir()
    files = sorted(directory.glob("*.mrl"))
    stamp = (
        str(directory.resolve()),
        tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files),
    )
    if stamp != _KERNEL_STAMP:
        _KERNEL_TEXTS = [(f"kernel/{p.name}", p.read_text(encoding="utf-8")) for p in files]
        _KERNEL_STAMP = stamp
    return list(_KERNEL_TEXTS)
