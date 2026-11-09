"""MRL-E / MRL-T 的宿主 API：编译（解析 + 静态检查）一次，多次求值。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ast as A
from . import types as T
from .checker import ExprChecker
from .errors import Diagnostic
from .evaluator import DEFAULT_BUDGET, Evaluator
from .parser import parse_expr, parse_template
from .stdlib import Function, make_table
from .values import from_py


@dataclass
class CompiledExpr:
    source_text: str
    node: A.Node
    type: T.Type
    env: dict[str, T.Type]
    functions: dict[str, Function]
    warnings: list[Diagnostic] = field(default_factory=list)
    paths: set[str] = field(default_factory=set)

    def evaluate(self, values: dict[str, Any], budget: int = DEFAULT_BUDGET, convert: bool = True) -> Any:
        """求值。values 中的 Python 值默认先转换为 MRL 值（convert=False 表示已经是 MRL 值）。"""
        env = {k: (from_py(v) if convert else v) for k, v in values.items()}
        return Evaluator(self.functions, budget).eval(self.node, env)


@dataclass
class CompiledTemplate:
    source_text: str
    node: A.Template
    env: dict[str, T.Type]
    functions: dict[str, Function]
    warnings: list[Diagnostic] = field(default_factory=list)

    def render(self, values: dict[str, Any], budget: int = DEFAULT_BUDGET, convert: bool = True) -> str:
        env = {k: (from_py(v) if convert else v) for k, v in values.items()}
        return Evaluator(self.functions, budget).template(self.node, env)


def compile_expr(
    text: str,
    env: dict[str, T.Type] | None = None,
    functions: dict[str, Function] | None = None,
    source: str = "",
    expect: T.Type | None = None,
) -> CompiledExpr:
    fns = functions if functions is not None else make_table()
    node = parse_expr(text, source)
    env = dict(env or {})
    c = ExprChecker(env, fns, source)
    t = c.check(node, expect)
    c.finish()
    return CompiledExpr(text, node, t, env, fns, c.warnings, c.paths)


def compile_template(
    text: str,
    env: dict[str, T.Type] | None = None,
    functions: dict[str, Function] | None = None,
    source: str = "",
) -> CompiledTemplate:
    fns = functions if functions is not None else make_table()
    node = parse_template(text, source)
    env = dict(env or {})
    c = ExprChecker(env, fns, source)
    c.check_template(node)
    c.finish()
    return CompiledTemplate(text, node, env, fns, c.warnings)


def evaluate(text: str, values: dict[str, Any] | None = None, env: dict[str, T.Type] | None = None) -> Any:
    """便捷函数：未给出类型的根名字按 dyn 处理。"""
    values = values or {}
    types = {k: T.DYN for k in values}
    types.update(env or {})
    return compile_expr(text, types).evaluate(values)
