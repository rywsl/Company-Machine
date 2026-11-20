"""类型表达式：any | name@M[.m] | list<类型表达式> | ref<类型表达式>。

规范串（id）去掉 minor：video.mp4@1.2 的 id 是 video.mp4@1（同一个 major 内 minor 双向兼容）。
钉住的 minor 只用于引用检查（E-REF）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

_NAMED = re.compile(r"^([a-z][a-z0-9]*(?:[.-][a-z0-9]+)*)(?:@([0-9]+)(?:\.([0-9]+))?)?$")
RESERVED_NAMES = frozenset({"any", "list", "ref"})


@dataclass(frozen=True)
class TExpr:
    form: str  # named | list | ref | any
    arg: TExpr | None = None
    name: str = ""
    major: int | None = None
    minor: int | None = None

    @property
    def id(self) -> str:
        if self.form == "any":
            return "any"
        if self.form in ("list", "ref"):
            assert self.arg is not None
            return f"{self.form}<{self.arg.id}>"
        return self.type_id

    @property
    def type_id(self) -> str:
        return f"{self.name}@{self.major}" if self.major is not None else self.name

    @property
    def raw_ref(self) -> str:
        if self.major is None:
            return self.name
        if self.minor is None:
            return f"{self.name}@{self.major}"
        return f"{self.name}@{self.major}.{self.minor}"

    def walk(self) -> Iterator[TExpr]:
        yield self
        if self.arg is not None:
            yield from self.arg.walk()


def parse_texpr(text: str) -> TExpr:
    s = text.strip()
    if s == "any":
        return TExpr("any")
    for form in ("list", "ref"):
        if s.startswith(form + "<"):
            if not s.endswith(">"):
                raise ValueError(f"类型表达式 {text!r} 缺少结尾的 '>'")
            return TExpr(form, parse_texpr(s[len(form) + 1 : -1]))
    m = _NAMED.match(s)
    if not m:
        raise ValueError(f"无法解析类型表达式 {text!r}")
    name, major, minor = m.group(1), m.group(2), m.group(3)
    if name in RESERVED_NAMES:
        raise ValueError(f"{name!r} 是保留的类型名")
    return TExpr(
        "named",
        None,
        name,
        int(major) if major is not None else None,
        int(minor) if minor is not None else None,
    )
