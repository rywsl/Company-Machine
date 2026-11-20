"""MRL 值域。

值有七种：null、bool、int、float、string、list、map。
- int：64 位有符号整数；
- float：IEEE-754 binary64，只允许有限值，零统一为 +0.0；
- list：Python tuple（不可变）；
- map：MapValue（不可变，键只能是 string，迭代按键的码点序）。

注意：Python 的 bool 是 int 的子类，所有判断都必须先判断 bool。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from typing import Any

from .errors import EvalError

INT_MIN = -(2**63)
INT_MAX = 2**63 - 1


class MapValue(Mapping):
    """不可变 map。键为 string；迭代顺序为键的 Unicode 码点序。"""

    __slots__ = ("_d", "_keys", "_hash")

    def __init__(self, items: Any = ()):
        d = dict(items)
        for k in d:
            if not isinstance(k, str):
                raise EvalError("MRL-TYPE", f"map 的键必须是 string，得到 {type_name(k)}")
        self._d = d
        self._keys = tuple(sorted(d))
        self._hash: int | None = None

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: object) -> bool:
        return key in self._d

    def __repr__(self) -> str:
        return "MapValue(" + ", ".join(f"{k!r}: {self._d[k]!r}" for k in self._keys) + ")"

    def __eq__(self, other: object) -> bool:  # 仅供 Python 内部使用；MRL 语义用 values.eq
        return isinstance(other, MapValue) and ident_key(self) == ident_key(other)

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(ident_key(self))
        return self._hash


NUMERIC_KINDS = ("int", "float")


def kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, tuple):
        return "list"
    if isinstance(v, MapValue):
        return "map"
    raise TypeError(f"not an MRL value: {v!r} ({type(v).__name__})")


def type_name(v: Any) -> str:
    try:
        return kind(v)
    except TypeError:
        return type(v).__name__


def is_number(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float))


def check_int(i: int) -> int:
    if i < INT_MIN or i > INT_MAX:
        raise EvalError("MRL-OVERFLOW", "整数运算结果超出 64 位有符号范围")
    return i


def norm_float(f: float) -> float:
    if not math.isfinite(f):
        raise EvalError("MRL-DOMAIN", "浮点结果不是有限数")
    if f == 0.0:
        return 0.0
    return f


def to_float(v: int | float) -> float:
    if isinstance(v, float):
        return v
    return float(v)  # |v| <= 2^63，按“就近、偶数优先”舍入，不会溢出


def from_py(x: Any) -> Any:
    """把 JSON/YAML 解析出的 Python 值转换为 MRL 值。"""
    if x is None or isinstance(x, (bool, str)):
        return x
    if isinstance(x, int):
        return check_int(x)
    if isinstance(x, float):
        return norm_float(x)
    if isinstance(x, (list, tuple)):
        return tuple(from_py(e) for e in x)
    if isinstance(x, MapValue):
        return x
    if isinstance(x, dict):
        return MapValue((k, from_py(v)) for k, v in x.items())
    raise EvalError("MRL-TYPE", f"无法转换为 MRL 值：{type(x).__name__}")


def to_py(v: Any) -> Any:
    """把 MRL 值转换为可 JSON 序列化的 Python 值（map 按键排序）。"""
    if isinstance(v, tuple):
        return [to_py(e) for e in v]
    if isinstance(v, MapValue):
        return {k: to_py(v[k]) for k in v}
    return v


def eq(a: Any, b: Any) -> bool:
    """MRL 的 == 语义：数值按精确数学值比较，int 与 float 可以相等；其他种类不同即不等。"""
    ka, kb = kind(a), kind(b)
    if ka in NUMERIC_KINDS and kb in NUMERIC_KINDS:
        return a == b  # Python 的 int/float 比较是精确的
    if ka != kb:
        return False
    if ka == "list":
        return len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b, strict=True))
    if ka == "map":
        return a._keys == b._keys and all(eq(a[k], b[k]) for k in a)
    return a == b


def compare(a: Any, b: Any) -> int:
    """有序比较：只对 (数, 数) 与 (string, string) 定义。"""
    if is_number(a) and is_number(b):
        return (a > b) - (a < b)
    if isinstance(a, str) and isinstance(b, str):
        return (a > b) - (a < b)
    raise EvalError("MRL-TYPE", f"无法比较 {type_name(a)} 与 {type_name(b)} 的大小")


_RANK = {"null": 0, "bool": 1, "int": 2, "float": 2, "string": 3, "list": 4, "map": 5}


def order_key(v: Any) -> tuple:
    """规范全序：null < bool < 数 < string < list < map；数值相等时 int 在 float 前。"""
    k = kind(v)
    r = _RANK[k]
    if k in NUMERIC_KINDS:
        return (r, v, 0 if k == "int" else 1)
    if k == "list":
        return (r, tuple(order_key(e) for e in v))
    if k == "map":
        return (r, tuple((key, order_key(v[key])) for key in v))
    if k == "null":
        return (r,)
    return (r, v)


def ident_key(v: Any) -> tuple:
    """身份键：区分种类（int 1 与 float 1.0 是不同的身份）。用于关系元组的去重。"""
    k = kind(v)
    if k == "null":
        return ("z",)
    if k == "bool":
        return ("b", v)
    if k == "int":
        return ("i", v)
    if k == "float":
        return ("f", v)
    if k == "string":
        return ("s", v)
    if k == "list":
        return ("l", tuple(ident_key(e) for e in v))
    return ("m", tuple((key, ident_key(v[key])) for key in v))


def eq_key(v: Any) -> tuple:
    """与 eq() 一致的可哈希键：int 与 float 数值相等时键相等。用于 unique 与集合函数。"""
    k = kind(v)
    if k in NUMERIC_KINDS:
        return ("n", v)
    if k == "null":
        return ("z",)
    if k == "bool":
        return ("b", v)
    if k == "string":
        return ("s", v)
    if k == "list":
        return ("l", tuple(eq_key(e) for e in v))
    return ("m", tuple((key, eq_key(v[key])) for key in v))


def format_float(f: float) -> str:
    """float 的规范字符串：最短往返表示，规则与 Python 3 的 repr(float) 相同。"""
    return repr(f)


def to_string(v: Any) -> str:
    """string() 与模板插值使用的转换。只接受 string、int、float、bool。"""
    k = kind(v)
    if k == "string":
        return v
    if k == "int":
        return str(v)
    if k == "float":
        return format_float(v)
    if k == "bool":
        return "true" if v else "false"
    raise EvalError("MRL-TYPE", f"无法把 {k} 转换为 string")


def dumps(v: Any) -> str:
    """规范 JSON 文本（键排序、UTF-8 原样输出）。"""
    return json.dumps(to_py(v), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def display(v: Any) -> str:
    """面向人的显示形式，用于诊断消息与 CLI 输出。"""
    k = kind(v)
    if k == "null":
        return "null"
    if k == "bool":
        return "true" if v else "false"
    if k == "float":
        return format_float(v)
    if k == "string":
        return json.dumps(v, ensure_ascii=False)
    if k == "list":
        return "[" + ", ".join(display(e) for e in v) + "]"
    if k == "map":
        return "{" + ", ".join(f"{json.dumps(key, ensure_ascii=False)}: {display(v[key])}" for key in v) + "}"
    return str(v)
