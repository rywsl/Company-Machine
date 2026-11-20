"""MRL 抽象语法树。表达式节点与规则节点。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Span


@dataclass(frozen=True, eq=False)
class Node:
    span: Span = field(repr=False, kw_only=True)


# ---------- 表达式 ----------


@dataclass(frozen=True, eq=False)
class Lit(Node):
    value: object  # MRL 值（null 用 None）


@dataclass(frozen=True, eq=False)
class Ident(Node):
    name: str


@dataclass(frozen=True, eq=False)
class Select(Node):
    operand: Node
    field: str


@dataclass(frozen=True, eq=False)
class Index(Node):
    operand: Node
    index: Node


@dataclass(frozen=True, eq=False)
class Call(Node):
    func: str
    args: tuple[Node, ...]
    receiver_style: bool = False  # 以 x.f(...) 形式书写


@dataclass(frozen=True, eq=False)
class Macro(Node):
    kind: str  # all | exists | exists_one | map | filter
    target: Node
    var: str
    pred: Node | None  # filter/all/exists/exists_one 的谓词；map 的可选过滤条件
    body: Node | None  # map 的变换表达式


@dataclass(frozen=True, eq=False)
class Has(Node):
    operand: Node
    field: str


@dataclass(frozen=True, eq=False)
class Unary(Node):
    op: str  # ! | -
    operand: Node


@dataclass(frozen=True, eq=False)
class Binary(Node):
    op: str  # + - * / % == != < <= > >= in
    left: Node
    right: Node


@dataclass(frozen=True, eq=False)
class And(Node):
    left: Node
    right: Node


@dataclass(frozen=True, eq=False)
class Or(Node):
    left: Node
    right: Node


@dataclass(frozen=True, eq=False)
class Cond(Node):
    cond: Node
    then: Node
    otherwise: Node


@dataclass(frozen=True, eq=False)
class ListLit(Node):
    items: tuple[Node, ...]


@dataclass(frozen=True, eq=False)
class MapLit(Node):
    entries: tuple[tuple[Node, Node], ...]


# ---------- 模板 ----------


@dataclass(frozen=True, eq=False)
class Template(Node):
    parts: tuple[str | Node, ...]  # 文本片段或插值表达式


# ---------- 规则 ----------


@dataclass(frozen=True, eq=False)
class TypeRef(Node):
    name: str  # bool int float string any list map
    arg: TypeRef | None = None

    def __str__(self) -> str:
        return f"{self.name}<{self.arg}>" if self.arg else self.name


@dataclass(frozen=True, eq=False)
class Column(Node):
    name: str
    type: TypeRef


@dataclass(frozen=True, eq=False)
class RelDecl(Node):
    name: str
    columns: tuple[Column, ...]
    input: bool = False
    open: bool = False
    doc: str = ""


@dataclass(frozen=True, eq=False)
class Agg(Node):
    func: str  # count | sum | min | max | set
    expr: Node | None


@dataclass(frozen=True, eq=False)
class Head(Node):
    rel: str
    terms: tuple[Node, ...]  # 表达式或 Agg


@dataclass(frozen=True, eq=False)
class AtomLit(Node):
    rel: str
    args: tuple[Node, ...]
    negated: bool = False


@dataclass(frozen=True, eq=False)
class Assign(Node):
    var: str
    expr: Node


@dataclass(frozen=True, eq=False)
class Guard(Node):
    expr: Node


@dataclass(frozen=True, eq=False)
class Rule(Node):
    head: Head
    body: tuple[Node, ...]  # AtomLit | Assign | Guard
    module: str = ""

    @property
    def is_fact(self) -> bool:
        return not self.body


@dataclass(frozen=True, eq=False)
class Module(Node):
    name: str
    decls: tuple[RelDecl, ...]
    rules: tuple[Rule, ...]
    source: str = ""


def is_var_name(name: str) -> bool:
    """规则中的变量：以大写字母或下划线开头。"""
    return bool(name) and (name[0] == "_" or ("A" <= name[0] <= "Z"))
