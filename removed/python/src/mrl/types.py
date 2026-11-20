"""MRL 静态类型。

类型：bool、int、float、num（int 或 float）、string、null、dyn（静态未知）、
list<T>、map<T>（键为 string）、record{...}（宿主提供的结构化类型，字段可选/必需，开放/封闭）。
"""

from __future__ import annotations

from dataclasses import dataclass


class Type:
    def __str__(self) -> str:  # pragma: no cover - 子类实现
        raise NotImplementedError


@dataclass(frozen=True)
class Prim(Type):
    name: str

    def __str__(self) -> str:
        return self.name


BOOL = Prim("bool")
INT = Prim("int")
FLOAT = Prim("float")
NUM = Prim("num")
STRING = Prim("string")
NULL = Prim("null")
DYN = Prim("dyn")


@dataclass(frozen=True)
class ListT(Type):
    elem: Type

    def __str__(self) -> str:
        return f"list<{self.elem}>"


@dataclass(frozen=True)
class MapT(Type):
    elem: Type

    def __str__(self) -> str:
        return f"map<{self.elem}>"


@dataclass(frozen=True)
class RecordT(Type):
    """记录类型。fields: (名字, 类型, 是否必需)。open=True 时允许未声明的字段（类型为 dyn）。"""

    fields: tuple[tuple[str, Type, bool], ...]
    open: bool = False

    def field(self, name: str) -> tuple[Type, bool] | None:
        for n, t, req in self.fields:
            if n == name:
                return t, req
        return None

    def __str__(self) -> str:
        inner = ", ".join(f"{n}{'' if r else '?'}: {t}" for n, t, r in self.fields)
        if self.open:
            inner = inner + (", ..." if inner else "...")
        return "{" + inner + "}"


def record(fields: dict[str, Type], required: set[str] | None = None, open: bool = False) -> RecordT:
    req = set(fields) if required is None else required
    return RecordT(tuple((k, fields[k], k in req) for k in sorted(fields)), open)


def is_numeric(t: Type) -> bool:
    return t in (INT, FLOAT, NUM)


def is_dyn(t: Type) -> bool:
    return t == DYN


def numeric_result(a: Type, b: Type) -> Type:
    """+ - * 的数值结果类型。"""
    if a == INT and b == INT:
        return INT
    if a == FLOAT or b == FLOAT:
        return FLOAT
    return NUM


def lub(a: Type, b: Type) -> Type:
    """最小上界，用于条件表达式与列表元素。"""
    if a == b:
        return a
    if NULL in (a, b):
        return DYN  # 可能为 null 的值没有更精确的静态类型
    if is_numeric(a) and is_numeric(b):
        return NUM
    if isinstance(a, ListT) and isinstance(b, ListT):
        return ListT(lub(a.elem, b.elem))
    if isinstance(a, MapT) and isinstance(b, MapT):
        return MapT(lub(a.elem, b.elem))
    if isinstance(a, (MapT, RecordT)) and isinstance(b, (MapT, RecordT)):
        return MapT(DYN)
    return DYN


def compat(src: Type, dst: Type) -> str:
    """src 的值能否放进 dst：yes / maybe（需要运行期检查）/ no。"""
    if dst == DYN or src == dst:
        return "yes"
    if src == DYN:
        return "maybe"
    if dst == FLOAT and src == INT:
        return "yes"  # int 会被规范化为 float
    if dst == NUM and src in (INT, FLOAT):
        return "yes"
    if src == NUM and dst in (INT, FLOAT):
        return "maybe"
    if isinstance(dst, ListT) and isinstance(src, ListT):
        return compat(src.elem, dst.elem)
    if isinstance(dst, MapT) and isinstance(src, MapT):
        return compat(src.elem, dst.elem)
    if isinstance(dst, MapT) and isinstance(src, RecordT):
        results = [compat(t, dst.elem) for _, t, _ in src.fields]
        if src.open:
            results.append(compat(DYN, dst.elem))
        if "no" in results:
            return "no"
        return "maybe" if "maybe" in results else "yes"
    return "no"


def comparable_eq(a: Type, b: Type) -> bool:
    """== / != 的静态检查：两边种类明显不同（如 string 与 int）时视为类型错误。"""
    if DYN in (a, b) or NULL in (a, b) or a == b:
        return True
    if is_numeric(a) and is_numeric(b):
        return True
    if isinstance(a, ListT) and isinstance(b, ListT):
        return comparable_eq(a.elem, b.elem)
    if isinstance(a, (MapT, RecordT)) and isinstance(b, (MapT, RecordT)):
        return True
    return False


COLUMN_TYPES = {"bool": BOOL, "int": INT, "float": FLOAT, "string": STRING, "any": DYN}


def from_type_ref(ref) -> Type:
    """把规则声明中的列类型（ast.TypeRef）转换为 Type。列类型不允许 num。"""
    if ref.name in COLUMN_TYPES:
        if ref.arg is not None:
            raise ValueError(f"{ref.name} 不接受类型参数")
        return COLUMN_TYPES[ref.name]
    if ref.name in ("list", "map"):
        if ref.arg is None:
            raise ValueError(f"{ref.name} 需要类型参数")
        inner = from_type_ref(ref.arg)
        return ListT(inner) if ref.name == "list" else MapT(inner)
    raise ValueError(f"未知的列类型 {ref.name!r}（可用：bool int float string any list<T> map<T>）")


_SIMPLE = {
    "bool": BOOL,
    "int": INT,
    "float": FLOAT,
    "num": NUM,
    "string": STRING,
    "null": NULL,
    "dyn": DYN,
    "any": DYN,
}


def parse_type(text: str) -> Type:
    """解析类型字符串，供宿主与测试使用。

    语法见 spec/mrl/mrl-1.0.md 第 2.7 节：记号之间只允许空格；名字与第 3.2 节
    标识符相同（ASCII，不能以数字开头）；记录不允许末尾逗号。
    """
    pos = 0
    s = text

    def skip() -> None:
        nonlocal pos
        while pos < len(s) and s[pos] == " ":
            pos += 1

    def ident() -> str:
        nonlocal pos
        skip()
        start = pos
        # 与第 3.2 节的标识符相同：只限 ASCII，且不能以数字开头。
        if pos < len(s) and s[pos].isascii() and (s[pos].isalpha() or s[pos] == "_"):
            pos += 1
            while pos < len(s) and s[pos].isascii() and (s[pos].isalnum() or s[pos] == "_"):
                pos += 1
        if start == pos:
            raise ValueError(f"无法解析类型 {text!r}：位置 {pos} 期望名字")
        return s[start:pos]

    def expect(ch: str) -> None:
        nonlocal pos
        skip()
        if not s.startswith(ch, pos):
            raise ValueError(f"无法解析类型 {text!r}：位置 {pos} 期望 {ch!r}")
        pos += len(ch)

    def parse() -> Type:
        nonlocal pos
        skip()
        if s.startswith("{", pos):
            pos += 1
            fields: dict[str, Type] = {}
            required: set[str] = set()
            is_open = False
            skip()
            while not s.startswith("}", pos):
                skip()
                if s.startswith("...", pos):
                    pos += 3
                    is_open = True
                    skip()
                    break
                name = ident()
                optional = False
                if s.startswith("?", pos):
                    optional = True
                    pos += 1
                expect(":")
                fields[name] = parse()
                if not optional:
                    required.add(name)
                skip()
                if s.startswith(",", pos):
                    pos += 1
                    skip()
                    if pos >= len(s) or s.startswith("}", pos):
                        raise ValueError(f"无法解析类型 {text!r}：记录类型不允许末尾逗号")
                    continue
                break
            expect("}")
            return record(fields, required, is_open)
        name = ident()
        if name in ("list", "map"):
            expect("<")
            inner = parse()
            expect(">")
            return ListT(inner) if name == "list" else MapT(inner)
        if name in _SIMPLE:
            return _SIMPLE[name]
        raise ValueError(f"无法解析类型 {text!r}：未知类型 {name!r}")

    t = parse()
    skip()
    if pos != len(s):
        raise ValueError(f"无法解析类型 {text!r}：多余的内容 {s[pos:]!r}")
    return t
