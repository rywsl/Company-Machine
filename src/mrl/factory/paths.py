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


def kernel_sources() -> list[tuple[str, str]]:
    out = []
    for p in sorted(kernel_dir().glob("*.mrl")):
        out.append((f"kernel/{p.name}", p.read_text(encoding="utf-8")))
    return out
