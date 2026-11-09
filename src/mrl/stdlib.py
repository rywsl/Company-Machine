"""MRL 标准库函数（spec/mrl/mrl-1.0.md 第 6 章）。

每个函数有两部分：
- sig(arg_types) -> Type：静态检查，参数类型不对时抛 SigError；
- impl(*args) -> 值：运行期实现，参数种类不对时抛 EvalError("MRL-TYPE")。
所有函数都是全函数：要么返回值，要么抛出规定的错误；没有副作用，结果只取决于参数。
"""

from __future__ import annotations

import decimal
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import types as T
from .errors import EvalError
from .values import (
    MapValue,
    check_int,
    compare,
    eq_key,
    is_number,
    kind,
    norm_float,
    order_key,
    to_float,
    to_string,
    type_name,
)


class SigError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Function:
    name: str
    min_args: int
    max_args: int | None  # None 表示可变参数
    sig: Callable[[list[T.Type]], T.Type]
    impl: Callable[..., Any]
    doc: str = ""


# ---------- 静态检查工具 ----------

_SHAPES = {
    "string": lambda t: t == T.STRING,
    "int": lambda t: t == T.INT,
    "num": T.is_numeric,
    "bool": lambda t: t == T.BOOL,
    "list": lambda t: isinstance(t, T.ListT),
    "map": lambda t: isinstance(t, (T.MapT, T.RecordT)),
    "any": lambda t: True,
}


def _need(fname: str, i: int, t: T.Type, *shapes: str) -> None:
    if t == T.DYN:
        return
    if any(_SHAPES[s](t) for s in shapes):
        return
    raise SigError("MRL-TYPE", f"{fname}() 的第 {i + 1} 个参数应为 {' 或 '.join(shapes)}，得到 {t}")


def _elem(t: T.Type) -> T.Type:
    return t.elem if isinstance(t, T.ListT) else T.DYN


def _map_elem(t: T.Type) -> T.Type:
    if isinstance(t, T.MapT):
        return t.elem
    if isinstance(t, T.RecordT):
        elem: T.Type | None = None
        for _, ft, _ in t.fields:
            elem = ft if elem is None else T.lub(elem, ft)
        if t.open or elem is None:
            return T.DYN
        return elem
    return T.DYN


# ---------- 运行期工具 ----------


def _rt(fname: str, v: Any, *kinds: str) -> None:
    k = kind(v)
    ok = k in kinds or ("num" in kinds and k in ("int", "float"))
    if not ok:
        raise EvalError("MRL-TYPE", f"{fname}() 不接受 {type_name(v)} 类型的参数")


# \Z 而不是 $：尾随换行也必须拒绝（规范要求整串匹配，不允许空白）。
_INT_RE = re.compile(r"[+-]?[0-9]+\Z", re.ASCII)
_FLOAT_RE = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?\Z", re.ASCII)
_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _f_size(x: Any) -> int:
    _rt("size", x, "string", "list", "map")
    return len(x)


def _f_int(x: Any) -> int:
    k = kind(x)
    if k == "int":
        return x
    if k == "float":
        return check_int(math.trunc(x))
    if k == "string":
        if not _INT_RE.match(x):
            raise EvalError("MRL-CONVERT", f"无法把 {x!r} 转换为 int")
        return check_int(int(x))
    raise EvalError("MRL-TYPE", f"int() 不接受 {k} 类型的参数")


def _f_float(x: Any) -> float:
    k = kind(x)
    if k == "float":
        return x
    if k == "int":
        return to_float(x)
    if k == "string":
        if not _FLOAT_RE.match(x):
            raise EvalError("MRL-CONVERT", f"无法把 {x!r} 转换为 float")
        return norm_float(float(x))
    raise EvalError("MRL-TYPE", f"float() 不接受 {k} 类型的参数")


def _f_abs(x: Any) -> Any:
    _rt("abs", x, "num")
    if isinstance(x, int):
        return check_int(abs(x))
    return norm_float(abs(x))


def _f_floor(x: Any) -> int:
    _rt("floor", x, "num")
    return x if isinstance(x, int) else check_int(math.floor(x))


def _f_ceil(x: Any) -> int:
    _rt("ceil", x, "num")
    return x if isinstance(x, int) else check_int(math.ceil(x))


def _f_round(x: Any) -> int:
    """四舍五入，恰好一半时远离零。按 binary64 的精确数学值判断。"""
    _rt("round", x, "num")
    if isinstance(x, int):
        return x
    if not math.isfinite(x):
        raise EvalError("MRL-DOMAIN", "round() 的参数不是有限数")
    exact = decimal.Decimal.from_float(x)
    if exact == 0:
        return 0
    sign = -1 if exact < 0 else 1
    mag = abs(exact)
    whole = int(mag)
    frac = mag - whole
    if frac >= decimal.Decimal("0.5"):
        whole += 1
    return check_int(sign * whole)


def _minmax(fname: str, args: tuple, want: int) -> Any:
    if len(args) == 1:
        seq = args[0]
        _rt(fname, seq, "list")
        if not seq:
            raise EvalError("MRL-DOMAIN", f"{fname}() 的列表为空")
    else:
        seq = args
    best = None
    for v in seq:
        if not (is_number(v) or isinstance(v, str)):
            raise EvalError("MRL-TYPE", f"{fname}() 只接受数或 string，得到 {type_name(v)}")
        if best is None or compare(v, best) == want:
            best = v
    return best


def _f_min(*args: Any) -> Any:
    return _minmax("min", args, -1)


def _f_max(*args: Any) -> Any:
    return _minmax("max", args, 1)


def _f_sum(seq: Any) -> Any:
    _rt("sum", seq, "list")
    total: int | float = 0
    for v in seq:
        if not is_number(v):
            raise EvalError("MRL-TYPE", f"sum() 只接受数，得到 {type_name(v)}")
        if isinstance(total, int) and isinstance(v, int):
            total = check_int(total + v)
        else:
            total = norm_float(to_float(total) + to_float(v))
    return total


def _str2(fname: str, a: Any, b: Any) -> None:
    _rt(fname, a, "string")
    _rt(fname, b, "string")


def _f_starts(a: Any, b: Any) -> bool:
    _str2("startsWith", a, b)
    return a.startswith(b)


def _f_ends(a: Any, b: Any) -> bool:
    _str2("endsWith", a, b)
    return a.endswith(b)


def _f_contains(a: Any, b: Any) -> bool:
    _str2("contains", a, b)
    return b in a


def _f_lower(a: Any) -> str:
    _rt("lower", a, "string")
    return a.translate(_LOWER)


def _f_upper(a: Any) -> str:
    _rt("upper", a, "string")
    return a.translate(_UPPER)


def _f_trim(a: Any) -> str:
    _rt("trim", a, "string")
    return a.strip(" \t\r\n")


def _f_split(a: Any, sep: Any) -> tuple:
    _str2("split", a, sep)
    if sep == "":
        raise EvalError("MRL-DOMAIN", "split() 的分隔符不能为空")
    return tuple(a.split(sep))


def _f_join(seq: Any, sep: Any) -> str:
    _rt("join", seq, "list")
    _rt("join", sep, "string")
    for v in seq:
        if not isinstance(v, str):
            raise EvalError("MRL-TYPE", f"join() 的列表元素必须是 string，得到 {type_name(v)}")
    return sep.join(seq)


def _f_replace(a: Any, old: Any, new: Any) -> str:
    _str2("replace", a, old)
    _rt("replace", new, "string")
    if old == "":
        raise EvalError("MRL-DOMAIN", "replace() 的被替换串不能为空")
    return a.replace(old, new)


def _glob_tokens(p: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    i = 0
    while i < len(p):
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            toks.append(("lit", p[i + 1]))
            i += 2
        elif p.startswith("**", i):
            toks.append(("dstar", ""))
            i += 2
        elif c == "*":
            toks.append(("star", ""))
            i += 1
        elif c == "?":
            toks.append(("one", ""))
            i += 1
        else:
            toks.append(("lit", c))
            i += 1
    return toks


def _f_glob(s: Any, pattern: Any) -> bool:
    """通配符匹配，时间复杂度 O(len(s)·len(pattern))。
    *：任意个非 '/' 字符；**：任意个字符（含 '/'）；?：一个非 '/' 字符；\\x：字面 x。"""
    _str2("glob", s, pattern)
    toks = _glob_tokens(pattern)
    m = len(toks)

    def closure(states: list[bool]) -> list[bool]:
        for j in range(m):
            if states[j] and toks[j][0] in ("star", "dstar"):
                states[j + 1] = True
        return states

    cur = closure([True] + [False] * m)
    for ch in s:
        nxt = [False] * (m + 1)
        for j in range(m):
            if not cur[j]:
                continue
            kind_, val = toks[j]
            if kind_ == "lit" and val == ch:
                nxt[j + 1] = True
            elif kind_ == "one" and ch != "/":
                nxt[j + 1] = True
            elif kind_ == "star" and ch != "/":
                nxt[j] = True
            elif kind_ == "dstar":
                nxt[j] = True
        cur = closure(nxt)
        if not any(cur):
            return False
    return cur[m]


def _f_chars_in(s: Any, allowed: Any) -> bool:
    _str2("charsIn", s, allowed)
    pool = set(allowed)
    return all(ch in pool for ch in s)


def _f_keys(m: Any) -> tuple:
    _rt("keys", m, "map")
    return tuple(m)


def _f_values(m: Any) -> tuple:
    _rt("values", m, "map")
    return tuple(m[k] for k in m)


def _f_get(m: Any, k: Any, default: Any) -> Any:
    _rt("get", m, "map")
    _rt("get", k, "string")
    return m[k] if k in m else default


def _f_sort(seq: Any) -> tuple:
    _rt("sort", seq, "list")
    if all(is_number(v) for v in seq) or all(isinstance(v, str) for v in seq):
        return tuple(sorted(seq))  # Python 对 int/float 的比较是精确的；sorted 是稳定排序
    raise EvalError("MRL-TYPE", "sort() 的元素必须全是数或全是 string")


def _f_unique(seq: Any) -> tuple:
    _rt("unique", seq, "list")
    seen: set = set()
    out = []
    for v in seq:
        k = eq_key(v)
        if k not in seen:
            seen.add(k)
            out.append(v)
    return tuple(out)


def _setop(fname: str, a: Any, b: Any, op: str) -> tuple:
    _rt(fname, a, "list")
    _rt(fname, b, "list")
    bkeys = {eq_key(v) for v in b}
    chosen: dict = {}
    if op == "union":
        for v in (*a, *b):
            chosen.setdefault(eq_key(v), v)
    elif op == "intersect":
        for v in a:
            if eq_key(v) in bkeys:
                chosen.setdefault(eq_key(v), v)
    else:
        for v in a:
            if eq_key(v) not in bkeys:
                chosen.setdefault(eq_key(v), v)
    return tuple(sorted(chosen.values(), key=order_key))


def _f_union(a: Any, b: Any) -> tuple:
    return _setop("union", a, b, "union")


def _f_intersect(a: Any, b: Any) -> tuple:
    return _setop("intersect", a, b, "intersect")


def _f_difference(a: Any, b: Any) -> tuple:
    return _setop("difference", a, b, "difference")


def _f_is_subset(a: Any, b: Any) -> bool:
    _rt("isSubset", a, "list")
    _rt("isSubset", b, "list")
    bkeys = {eq_key(v) for v in b}
    return all(eq_key(v) in bkeys for v in a)


def _f_type(x: Any) -> str:
    return kind(x)


def _f_string(x: Any) -> str:
    return to_string(x)


# ---------- 静态签名 ----------


def _s_size(a: list[T.Type]) -> T.Type:
    _need("size", 0, a[0], "string", "list", "map")
    return T.INT


def _s_int(a: list[T.Type]) -> T.Type:
    _need("int", 0, a[0], "num", "string")
    return T.INT


def _s_float(a: list[T.Type]) -> T.Type:
    _need("float", 0, a[0], "num", "string")
    return T.FLOAT


def _s_string(a: list[T.Type]) -> T.Type:
    _need("string", 0, a[0], "string", "num", "bool")
    return T.STRING


def _s_abs(a: list[T.Type]) -> T.Type:
    _need("abs", 0, a[0], "num")
    return a[0] if a[0] != T.DYN else T.NUM


def _s_to_int(name: str) -> Callable[[list[T.Type]], T.Type]:
    def sig(a: list[T.Type]) -> T.Type:
        _need(name, 0, a[0], "num")
        return T.INT

    return sig


def _s_minmax(name: str) -> Callable[[list[T.Type]], T.Type]:
    def sig(a: list[T.Type]) -> T.Type:
        if len(a) == 1:
            _need(name, 0, a[0], "list")
            e = _elem(a[0])
            if e != T.DYN and not (T.is_numeric(e) or e == T.STRING):
                raise SigError("MRL-TYPE", f"{name}() 的列表元素必须是数或 string，得到 {e}")
            return e
        result: T.Type | None = None
        for i, t in enumerate(a):
            _need(name, i, t, "num", "string")
            result = t if result is None else T.lub(result, t)
        assert result is not None
        has_str = any(t == T.STRING for t in a)
        has_num = any(T.is_numeric(t) for t in a)
        if has_str and has_num:
            raise SigError("MRL-TYPE", f"{name}() 的参数必须全是数或全是 string")
        return result

    return sig


def _s_sum(a: list[T.Type]) -> T.Type:
    _need("sum", 0, a[0], "list")
    e = _elem(a[0])
    if e == T.INT:
        return T.INT
    if e == T.FLOAT:
        return T.FLOAT
    if e in (T.NUM, T.DYN):
        return T.NUM
    raise SigError("MRL-TYPE", f"sum() 的列表元素必须是数，得到 {e}")


def _s_str_pred(name: str) -> Callable[[list[T.Type]], T.Type]:
    def sig(a: list[T.Type]) -> T.Type:
        _need(name, 0, a[0], "string")
        _need(name, 1, a[1], "string")
        return T.BOOL

    return sig


def _s_str_unary(name: str) -> Callable[[list[T.Type]], T.Type]:
    def sig(a: list[T.Type]) -> T.Type:
        _need(name, 0, a[0], "string")
        return T.STRING

    return sig


def _s_split(a: list[T.Type]) -> T.Type:
    _need("split", 0, a[0], "string")
    _need("split", 1, a[1], "string")
    return T.ListT(T.STRING)


def _s_join(a: list[T.Type]) -> T.Type:
    _need("join", 0, a[0], "list")
    _need("join", 1, a[1], "string")
    e = _elem(a[0])
    if e not in (T.STRING, T.DYN):
        raise SigError("MRL-TYPE", f"join() 的列表元素必须是 string，得到 {e}")
    return T.STRING


def _s_replace(a: list[T.Type]) -> T.Type:
    for i in range(3):
        _need("replace", i, a[i], "string")
    return T.STRING


def _s_keys(a: list[T.Type]) -> T.Type:
    _need("keys", 0, a[0], "map")
    return T.ListT(T.STRING)


def _s_values(a: list[T.Type]) -> T.Type:
    _need("values", 0, a[0], "map")
    return T.ListT(_map_elem(a[0]))


def _s_get(a: list[T.Type]) -> T.Type:
    _need("get", 0, a[0], "map")
    _need("get", 1, a[1], "string")
    return T.lub(_map_elem(a[0]), a[2])


def _s_sort(a: list[T.Type]) -> T.Type:
    _need("sort", 0, a[0], "list")
    e = _elem(a[0])
    if e != T.DYN and not (T.is_numeric(e) or e == T.STRING):
        raise SigError("MRL-TYPE", f"sort() 的元素必须是数或 string，得到 {e}")
    return a[0] if a[0] != T.DYN else T.ListT(T.DYN)


def _s_unique(a: list[T.Type]) -> T.Type:
    _need("unique", 0, a[0], "list")
    return a[0] if a[0] != T.DYN else T.ListT(T.DYN)


def _s_setop(name: str) -> Callable[[list[T.Type]], T.Type]:
    def sig(a: list[T.Type]) -> T.Type:
        _need(name, 0, a[0], "list")
        _need(name, 1, a[1], "list")
        return T.ListT(T.lub(_elem(a[0]), _elem(a[1])))

    return sig


def _s_is_subset(a: list[T.Type]) -> T.Type:
    _need("isSubset", 0, a[0], "list")
    _need("isSubset", 1, a[1], "list")
    return T.BOOL


def _s_type(a: list[T.Type]) -> T.Type:
    return T.STRING


def _s_glob(a: list[T.Type]) -> T.Type:
    _need("glob", 0, a[0], "string")
    _need("glob", 1, a[1], "string")
    return T.BOOL


FUNCTIONS: dict[str, Function] = {
    f.name: f
    for f in [
        Function("size", 1, 1, _s_size, _f_size, "string 的码点数；list 或 map 的元素数"),
        Function("int", 1, 1, _s_int, _f_int, "转换为 int：float 向零截断；string 按十进制解析"),
        Function("float", 1, 1, _s_float, _f_float, "转换为 float"),
        Function("string", 1, 1, _s_string, _f_string, "转换为 string（float 用最短往返表示）"),
        Function("type", 1, 1, _s_type, _f_type, "值的种类名：null bool int float string list map"),
        Function("abs", 1, 1, _s_abs, _f_abs, "绝对值"),
        Function("floor", 1, 1, _s_to_int("floor"), _f_floor, "向下取整，返回 int"),
        Function("ceil", 1, 1, _s_to_int("ceil"), _f_ceil, "向上取整，返回 int"),
        Function("round", 1, 1, _s_to_int("round"), _f_round, "四舍五入（一半时远离零），返回 int"),
        Function("min", 1, None, _s_minmax("min"), _f_min, "最小值：min(list) 或 min(a, b, ...)"),
        Function("max", 1, None, _s_minmax("max"), _f_max, "最大值：max(list) 或 max(a, b, ...)"),
        Function("sum", 1, 1, _s_sum, _f_sum, "按列表顺序求和；全是 int 时结果为 int"),
        Function("startsWith", 2, 2, _s_str_pred("startsWith"), _f_starts, "前缀判断"),
        Function("endsWith", 2, 2, _s_str_pred("endsWith"), _f_ends, "后缀判断"),
        Function("contains", 2, 2, _s_str_pred("contains"), _f_contains, "子串判断"),
        Function("lower", 1, 1, _s_str_unary("lower"), _f_lower, "ASCII 小写"),
        Function("upper", 1, 1, _s_str_unary("upper"), _f_upper, "ASCII 大写"),
        Function("trim", 1, 1, _s_str_unary("trim"), _f_trim, "去掉两端的空格、制表符、回车、换行"),
        Function("split", 2, 2, _s_split, _f_split, "按分隔符切分"),
        Function("join", 2, 2, _s_join, _f_join, "用分隔符连接"),
        Function("replace", 3, 3, _s_replace, _f_replace, "替换全部出现"),
        Function("glob", 2, 2, _s_glob, _f_glob, "通配符匹配：* ** ? \\x"),
        Function(
            "charsIn",
            2,
            2,
            _s_str_pred("charsIn"),
            _f_chars_in,
            "s 的每个字符都出现在 allowed 中（空串为 true）",
        ),
        Function("keys", 1, 1, _s_keys, _f_keys, "map 的键，按码点序"),
        Function("values", 1, 1, _s_values, _f_values, "map 的值，按键的码点序"),
        Function("get", 3, 3, _s_get, _f_get, "get(m, k, 默认值)"),
        Function("sort", 1, 1, _s_sort, _f_sort, "稳定升序排序"),
        Function("unique", 1, 1, _s_unique, _f_unique, "去重，保留第一次出现"),
        Function("union", 2, 2, _s_setop("union"), _f_union, "并集，结果按规范序排序"),
        Function("intersect", 2, 2, _s_setop("intersect"), _f_intersect, "交集，结果按规范序排序"),
        Function("difference", 2, 2, _s_setop("difference"), _f_difference, "差集，结果按规范序排序"),
        Function("isSubset", 2, 2, _s_is_subset, _f_is_subset, "a 的每个元素都在 b 中"),
    ]
}


def make_table(extra: dict[str, Function] | None = None) -> dict[str, Function]:
    table = dict(FUNCTIONS)
    if extra:
        for name, fn in extra.items():
            if name in table:
                raise ValueError(f"函数 {name} 与标准库重名")
            table[name] = fn
    return table


__all__ = ["FUNCTIONS", "Function", "MapValue", "SigError", "make_table"]
