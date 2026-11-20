"""工厂 profile 函数：转换解析 resolve / resolveVia（规范性算法，见 spec/mrl/factory-profile.md 第 5 章）。

两个函数都是纯函数：结果只取决于参数，所以同一个 epoch 内同样的边总是解析到同样的转换链（不变量 I14）。

参数（均为 MRL 值）：
    lib     list<map>：转换库，每项 {id, from, to, lossless, requires, guarantees, preserves, cost, p50}
    subs    list<[a, b]>：类型表达式的子类型对 a <: b（即 kernel.types 的 tsub）
    always  list<[e, p]>：类型表达式 e 的恒真属性 p（即 kernel.types 的 texpr_always）
    u, g    上游类型与保证；d, r 下游类型与要求
    chain   list<string>：显式指定的转换链（只用于 resolveVia）

结果 map：
    status    direct | convert | contract | no-converter | via-invalid
    chain     插入的转换 id 列表（direct/contract/no-converter 为空）
    type      经过转换链之后的类型（无转换时为 u）
    props     到达下游时可提供的属性（升序、去重）
    missing   r 中没有被覆盖的属性（升序）
    lossless  转换链上的转换是否都无损（direct 为 true；contract/no-converter/via-invalid 为 false）
    detail    via-invalid 时的原因，其他情况为空串
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .. import types as T
from ..errors import EvalError
from ..stdlib import Function, SigError
from ..values import MapValue, kind

MAX_CHAIN = 4  # 自动解析时转换链的最大长度
MAX_PATHS = 100_000  # 自动解析时最多探索的路径数，超出时报 MRL-COST

RESULT_TYPE = T.record(
    {
        "status": T.STRING,
        "chain": T.ListT(T.STRING),
        "type": T.STRING,
        "props": T.ListT(T.STRING),
        "missing": T.ListT(T.STRING),
        "lossless": T.BOOL,
        "detail": T.STRING,
    }
)

_LIST_SHAPES = ("lib", "subs", "always")


def _sig(n: int, fname: str):
    def sig(args: list[T.Type]) -> T.Type:
        names = ["lib", "subs", "always", "u", "g", "d", "r", "chain"][:n]
        for i, (name, t) in enumerate(zip(names, args, strict=True)):
            if t == T.DYN:
                continue
            want_list = name in _LIST_SHAPES or name in ("g", "r", "chain")
            ok = isinstance(t, T.ListT) if want_list else t == T.STRING
            if not ok:
                shape = "list" if want_list else "string"
                raise SigError("MRL-TYPE", f"{fname}() 的第 {i + 1} 个参数（{name}）应为 {shape}，得到 {t}")
        return RESULT_TYPE

    return sig


class _Conv:
    __slots__ = ("id", "src", "dst", "lossless", "requires", "guarantees", "preserves", "cost", "p50")

    def __init__(self, m: Any):
        if not isinstance(m, MapValue):
            raise EvalError("MRL-TYPE", f"转换库的元素必须是 map，得到 {kind(m)}")
        try:
            self.id = _str(m["id"])
            self.src = _str(m["from"])
            self.dst = _str(m["to"])
            self.lossless = bool(m["lossless"])
            self.requires = frozenset(_strs(m["requires"]))
            self.guarantees = frozenset(_strs(m["guarantees"]))
            self.preserves = frozenset(_strs(m["preserves"]))
            self.cost = float(m["cost"])
            self.p50 = float(m["p50"])
        except KeyError as e:
            raise EvalError("MRL-NO-FIELD", f"转换库的元素缺少字段 {e.args[0]!r}") from e


def _str(v: Any) -> str:
    if not isinstance(v, str):
        raise EvalError("MRL-TYPE", f"期望 string，得到 {kind(v)}")
    return v


def _strs(v: Any) -> list[str]:
    if not isinstance(v, tuple):
        raise EvalError("MRL-TYPE", f"期望 list<string>，得到 {kind(v)}")
    return [_str(x) for x in v]


def _pairs(v: Any) -> list[tuple[str, str]]:
    if not isinstance(v, tuple):
        raise EvalError("MRL-TYPE", f"期望 list<list<string>>，得到 {kind(v)}")
    out = []
    for p in v:
        if not (isinstance(p, tuple) and len(p) == 2):
            raise EvalError("MRL-TYPE", "子类型对与恒真属性对必须是长度为 2 的 list")
        out.append((_str(p[0]), _str(p[1])))
    return out


class _Ctx:
    def __init__(self, subs: Any, always: Any):
        self.subs = set(_pairs(subs))
        self.always: dict[str, set[str]] = {}
        for e, p in _pairs(always):
            self.always.setdefault(e, set()).add(p)

    def sub(self, a: str, b: str) -> bool:
        return (a, b) in self.subs

    def props(self, t: str, base: frozenset[str] | set[str]) -> frozenset[str]:
        return frozenset(base) | frozenset(self.always.get(t, ()))

    def step(self, c: _Conv, props: frozenset[str]) -> frozenset[str]:
        """经过一个转换后的属性：该转换的 guarantees ∪ (进入时的属性 ∩ 该转换的 preserves) ∪ 目标类型的恒真属性。"""
        return self.props(c.dst, c.guarantees | (props & c.preserves))


def _result(
    status: str,
    chain: Sequence[str],
    t: str,
    props: frozenset[str],
    required: frozenset[str],
    lossless: bool,
    detail: str = "",
) -> MapValue:
    return MapValue(
        {
            "status": status,
            "chain": tuple(chain),
            "type": t,
            "props": tuple(sorted(props)),
            "missing": tuple(sorted(required - props)),
            "lossless": lossless,
            "detail": detail,
        }
    )


def resolve(lib: Any, subs: Any, always: Any, u: Any, g: Any, d: Any, r: Any) -> MapValue:
    convs = sorted((_Conv(m) for m in _tuple(lib, "lib")), key=lambda c: c.id)
    ctx = _Ctx(subs, always)
    u, d = _str(u), _str(d)
    required = frozenset(_strs(r))
    p0 = ctx.props(u, set(_strs(g)))
    # 1. 直接连接
    if ctx.sub(u, d) and required <= p0:
        return _result("direct", (), u, p0, required, True)
    # 2. 在转换图上搜索：每一步的转换必须接受当前类型（当前类型 <: from），且当前属性覆盖它的 requires
    best: tuple | None = None
    frontier: list[tuple[tuple[str, ...], str, frozenset[str], bool, float, float]] = [
        ((), u, p0, True, 0.0, 0.0)
    ]
    explored = 0
    for _depth in range(MAX_CHAIN):
        nxt = []
        for chain, t, props, lossless, cost, p50 in frontier:
            for c in convs:
                if c.id in chain or not ctx.sub(t, c.src) or not c.requires <= props:
                    continue
                explored += 1
                if explored > MAX_PATHS:
                    raise EvalError("MRL-COST", f"转换解析探索的路径超过 {MAX_PATHS} 条")
                entry = (
                    chain + (c.id,),
                    c.dst,
                    ctx.step(c, props),
                    lossless and c.lossless,
                    cost + c.cost,
                    p50 + c.p50,
                )
                nxt.append(entry)
                if ctx.sub(entry[1], d) and required <= entry[2]:
                    # 3. 排序：无损优先 → 步数最少 → 估计成本最低 → 估计耗时最短 → id 序列的字典序
                    key = (not entry[3], len(entry[0]), entry[4], entry[5], entry[0])
                    if best is None or key < best[0]:
                        best = (key, entry)
        frontier = nxt
    if best is not None:
        chain, t, props, lossless, _cost, _p50 = best[1]
        return _result("convert", chain, t, props, required, lossless)
    # 4. 没有路径
    if ctx.sub(u, d):
        return _result("contract", (), u, p0, required, False)
    return _result("no-converter", (), u, p0, required, False)


def resolve_via(lib: Any, subs: Any, always: Any, u: Any, g: Any, d: Any, r: Any, chain: Any) -> MapValue:
    convs = {c.id: c for c in (_Conv(m) for m in _tuple(lib, "lib"))}
    ctx = _Ctx(subs, always)
    u, d = _str(u), _str(d)
    required = frozenset(_strs(r))
    ids = _strs(chain)
    t, props, lossless = u, ctx.props(u, set(_strs(g))), True
    for i, cid in enumerate(ids):
        c = convs.get(cid)
        if c is None:
            return _result(
                "via-invalid",
                ids,
                t,
                props,
                required,
                False,
                f"via[{i}]：{cid} 不在转换库中（不存在、不是 converter 或已退役）",
            )
        if not ctx.sub(t, c.src):
            return _result(
                "via-invalid",
                ids,
                t,
                props,
                required,
                False,
                f"via[{i}]：{cid} 接受 {c.src}，但此处的类型是 {t}",
            )
        if not c.requires <= props:
            miss = ", ".join(sorted(c.requires - props))
            return _result(
                "via-invalid",
                ids,
                t,
                props,
                required,
                False,
                f"via[{i}]：{cid} 要求的属性 {{{miss}}} 在此处没有被覆盖",
            )
        t, props, lossless = c.dst, ctx.step(c, props), lossless and c.lossless
    if not ctx.sub(t, d):
        return _result(
            "via-invalid", ids, t, props, required, False, f"转换链的终点类型 {t} 不是 {d} 的子类型"
        )
    if not required <= props:
        return _result("contract", ids, t, props, required, False)
    return _result("convert", ids, t, props, required, lossless)


def _tuple(v: Any, name: str) -> tuple:
    if not isinstance(v, tuple):
        raise EvalError("MRL-TYPE", f"{name} 应为 list，得到 {kind(v)}")
    return v


PROFILE_FUNCTIONS: dict[str, Function] = {
    "resolve": Function(
        "resolve", 7, 7, _sig(7, "resolve"), resolve, "转换解析（自动）：见 factory-profile.md 第 5 章"
    ),
    "resolveVia": Function(
        "resolveVia",
        8,
        8,
        _sig(8, "resolveVia"),
        resolve_via,
        "转换解析（显式转换链）：见 factory-profile.md 第 5 章",
    ),
}
