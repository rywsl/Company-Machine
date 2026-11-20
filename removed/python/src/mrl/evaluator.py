"""MRL-E 求值（spec/mrl/mrl-1.0.md 第 6 章）。

- 严格从左到右；&&、||、?: 短路；
- 任何错误都以 EvalError 抛出，宿主必须视为“不通过”；
- 每访问一个节点计 1 步，超过预算抛 MRL-COST，保证求值有界。
"""

from __future__ import annotations

from typing import Any

from . import ast as A
from .errors import EvalError
from .stdlib import Function
from .values import (
    MapValue,
    check_int,
    compare,
    eq,
    is_number,
    kind,
    norm_float,
    to_float,
    to_string,
    type_name,
)

DEFAULT_BUDGET = 100_000


class Evaluator:
    def __init__(self, functions: dict[str, Function], budget: int = DEFAULT_BUDGET):
        self.functions = functions
        self.budget = budget
        self.steps = 0

    def tick(self, n: A.Node) -> None:
        self.steps += 1
        if self.steps > self.budget:
            raise EvalError("MRL-COST", f"求值步数超过预算 {self.budget}", n.span)

    def eval(self, n: A.Node, env: dict[str, Any]) -> Any:
        try:
            return self._eval(n, env)
        except EvalError as e:
            if e.span.offset == 0 and e.span.end == 0 and n.span is not None:
                e.span = n.span
            raise

    def _err(self, code: str, msg: str, n: A.Node) -> EvalError:
        return EvalError(code, msg, n.span)

    def _eval(self, n: A.Node, env: dict[str, Any]) -> Any:
        # 左结合链不增加嵌套深度（4.7），按左脊迭代，避免宿主 RecursionError。
        if isinstance(n, (A.Binary, A.And, A.Or, A.Select, A.Index)):
            return self._eval_spine(n, env)
        return self._eval_node(n, env)

    def _eval_spine(self, n: A.Node, env: dict[str, Any]) -> Any:
        nodes: list[A.Node] = []
        cur: A.Node = n
        while isinstance(cur, (A.Binary, A.And, A.Or, A.Select, A.Index)):
            self.tick(cur)
            nodes.append(cur)
            cur = cur.left if isinstance(cur, (A.Binary, A.And, A.Or)) else cur.operand
        val = self._eval(cur, env)
        for node in reversed(nodes):
            val = self._apply_spine(node, val, env)
        return val

    def _apply_spine(self, n: A.Node, left: Any, env: dict[str, Any]) -> Any:
        if isinstance(n, A.Select):
            if isinstance(left, MapValue):
                if n.field not in left:
                    raise self._err("MRL-NO-FIELD", f"字段 {n.field!r} 不存在", n)
                return left[n.field]
            raise self._err("MRL-TYPE", f"不能对 {type_name(left)} 取字段 {n.field!r}", n)
        if isinstance(n, A.Index):
            return self._index_on(n, left, self._eval(n.index, env))
        if isinstance(n, A.And):
            if not isinstance(left, bool):
                raise self._err("MRL-TYPE", f"'&&' 需要 bool，得到 {type_name(left)}", n)
            if not left:
                return False
            right = self._eval(n.right, env)
            if not isinstance(right, bool):
                raise self._err("MRL-TYPE", f"'&&' 需要 bool，得到 {type_name(right)}", n)
            return right
        if isinstance(n, A.Or):
            if not isinstance(left, bool):
                raise self._err("MRL-TYPE", f"'||' 需要 bool，得到 {type_name(left)}", n)
            if left:
                return True
            right = self._eval(n.right, env)
            if not isinstance(right, bool):
                raise self._err("MRL-TYPE", f"'||' 需要 bool，得到 {type_name(right)}", n)
            return right
        assert isinstance(n, A.Binary)
        return self._binary_apply(n, left, self._eval(n.right, env))

    def _eval_node(self, n: A.Node, env: dict[str, Any]) -> Any:
        self.tick(n)
        if isinstance(n, A.Lit):
            return n.value
        if isinstance(n, A.Ident):
            if n.name not in env:
                raise self._err("MRL-UNDEFINED", f"未绑定的名字 {n.name!r}", n)
            return env[n.name]
        if isinstance(n, A.Select):
            obj = self._eval(n.operand, env)
            if isinstance(obj, MapValue):
                if n.field not in obj:
                    raise self._err("MRL-NO-FIELD", f"字段 {n.field!r} 不存在", n)
                return obj[n.field]
            raise self._err("MRL-TYPE", f"不能对 {type_name(obj)} 取字段 {n.field!r}", n)
        if isinstance(n, A.Has):
            obj = self._eval(n.operand, env)
            if isinstance(obj, MapValue):
                return n.field in obj
            if obj is None:
                return False
            raise self._err("MRL-TYPE", f"has() 不能用于 {type_name(obj)}", n)
        if isinstance(n, A.Index):
            return self._index_on(n, self._eval(n.operand, env), self._eval(n.index, env))
        if isinstance(n, A.Unary):
            v = self._eval(n.operand, env)
            if n.op == "!":
                if not isinstance(v, bool):
                    raise self._err("MRL-TYPE", f"'!' 需要 bool，得到 {type_name(v)}", n)
                return not v
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise self._err("MRL-TYPE", f"一元 '-' 需要数，得到 {type_name(v)}", n)
            return check_int(-v) if isinstance(v, int) else norm_float(-v)
        if isinstance(n, A.And):
            left = self._eval(n.left, env)
            if not isinstance(left, bool):
                raise self._err("MRL-TYPE", f"'&&' 需要 bool，得到 {type_name(left)}", n)
            if not left:
                return False
            right = self._eval(n.right, env)
            if not isinstance(right, bool):
                raise self._err("MRL-TYPE", f"'&&' 需要 bool，得到 {type_name(right)}", n)
            return right
        if isinstance(n, A.Or):
            left = self._eval(n.left, env)
            if not isinstance(left, bool):
                raise self._err("MRL-TYPE", f"'||' 需要 bool，得到 {type_name(left)}", n)
            if left:
                return True
            right = self._eval(n.right, env)
            if not isinstance(right, bool):
                raise self._err("MRL-TYPE", f"'||' 需要 bool，得到 {type_name(right)}", n)
            return right
        if isinstance(n, A.Cond):
            c = self._eval(n.cond, env)
            if not isinstance(c, bool):
                raise self._err("MRL-TYPE", f"条件需要 bool，得到 {type_name(c)}", n)
            return self._eval(n.then if c else n.otherwise, env)
        if isinstance(n, A.Binary):
            return self.binary(n, env)
        if isinstance(n, A.ListLit):
            return tuple(self._eval(i, env) for i in n.items)
        if isinstance(n, A.MapLit):
            items: dict[str, Any] = {}
            for k, v in n.entries:
                key = self._eval(k, env)
                if not isinstance(key, str):
                    raise self._err("MRL-TYPE", f"map 的键必须是 string，得到 {type_name(key)}", k)
                if key in items:
                    raise self._err("MRL-DUP-KEY", f"map 字面量中重复的键 {key!r}", k)
                items[key] = self._eval(v, env)
            return MapValue(items)
        if isinstance(n, A.Call):
            fn = self.functions.get(n.func)
            if fn is None:
                raise self._err("MRL-UNDEFINED", f"未定义的函数 {n.func!r}", n)
            args = [self._eval(a, env) for a in n.args]
            if len(args) < fn.min_args or (fn.max_args is not None and len(args) > fn.max_args):
                raise self._err("MRL-ARITY", f"{n.func}() 的参数个数不对", n)
            try:
                return fn.impl(*args)
            except EvalError as e:
                e.span = n.span
                raise
        if isinstance(n, A.Macro):
            return self.macro(n, env)
        raise self._err("MRL-SYNTAX", f"不支持的表达式节点 {type(n).__name__}", n)

    def _index_on(self, n: A.Index, obj: Any, idx: Any) -> Any:
        if isinstance(obj, tuple):
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise self._err("MRL-TYPE", f"列表下标需要 int，得到 {type_name(idx)}", n)
            if idx < 0 or idx >= len(obj):
                raise self._err("MRL-INDEX", f"下标 {idx} 越界（长度 {len(obj)}）", n)
            return obj[idx]
        if isinstance(obj, MapValue):
            if not isinstance(idx, str):
                raise self._err("MRL-TYPE", f"map 的键需要 string，得到 {type_name(idx)}", n)
            if idx not in obj:
                raise self._err("MRL-NO-FIELD", f"键 {idx!r} 不存在", n)
            return obj[idx]
        raise self._err("MRL-TYPE", f"不能对 {type_name(obj)} 使用下标", n)

    def binary(self, n: A.Binary, env: dict[str, Any]) -> Any:
        return self._binary_apply(n, self._eval(n.left, env), self._eval(n.right, env))

    def _binary_apply(self, n: A.Binary, a: Any, b: Any) -> Any:
        op = n.op
        if op == "==":
            return eq(a, b)
        if op == "!=":
            return not eq(a, b)
        if op in ("<", "<=", ">", ">="):
            try:
                c = compare(a, b)
            except EvalError as e:
                e.span = n.span
                raise
            return {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[op]
        if op == "in":
            if isinstance(b, tuple):
                return any(eq(a, x) for x in b)
            if isinstance(b, MapValue):
                if not isinstance(a, str):
                    raise self._err("MRL-TYPE", f"判断 map 的键时左边需要 string，得到 {type_name(a)}", n)
                return a in b
            raise self._err("MRL-TYPE", f"'in' 的右边需要 list 或 map，得到 {type_name(b)}", n)
        if op == "+":
            if isinstance(a, str) and isinstance(b, str):
                return a + b
            if isinstance(a, tuple) and isinstance(b, tuple):
                return a + b
        if not (is_number(a) and is_number(b)):
            raise self._err("MRL-TYPE", f"'{op}' 不能用于 {type_name(a)} 与 {type_name(b)}", n)
        if op == "/":
            if b == 0:
                raise self._err("MRL-DIV-ZERO", "除以零", n)
            return self._float(to_float(a) / to_float(b), n)
        if op == "%":
            if not (isinstance(a, int) and isinstance(b, int)):
                raise self._err("MRL-TYPE", "'%' 只能用于 int", n)
            if b == 0:
                raise self._err("MRL-DIV-ZERO", "对零取模", n)
            return a % b  # Python 的 % 是向下取整的模，结果符号与除数相同
        if isinstance(a, int) and isinstance(b, int):
            r = {"+": a + b, "-": a - b, "*": a * b}[op]
            try:
                return check_int(r)
            except EvalError as e:
                e.span = n.span
                raise
        fa, fb = to_float(a), to_float(b)
        r = {"+": fa + fb, "-": fa - fb, "*": fa * fb}[op]
        return self._float(r, n)

    def _float(self, f: float, n: A.Node) -> float:
        try:
            return norm_float(f)
        except EvalError as e:
            e.span = n.span
            raise

    def macro(self, n: A.Macro, env: dict[str, Any]) -> Any:
        target = self._eval(n.target, env)
        if isinstance(target, tuple):
            items = target
        elif isinstance(target, MapValue):
            items = tuple(target)  # 键，按码点序
        else:
            raise self._err("MRL-TYPE", f"{n.kind} 宏只能用于 list 或 map，得到 {type_name(target)}", n)
        inner = dict(env)

        def test(pred: A.Node, item: Any) -> bool:
            inner[n.var] = item
            r = self._eval(pred, inner)
            if not isinstance(r, bool):
                raise self._err("MRL-TYPE", f"{n.kind} 宏的条件需要 bool，得到 {type_name(r)}", pred)
            return r

        if n.kind == "all":
            assert n.pred is not None
            return all(test(n.pred, x) for x in items)
        if n.kind == "exists":
            assert n.pred is not None
            return any(test(n.pred, x) for x in items)
        if n.kind == "exists_one":
            assert n.pred is not None
            return sum(1 for x in items if test(n.pred, x)) == 1
        if n.kind == "filter":
            assert n.pred is not None
            return tuple(x for x in items if test(n.pred, x))
        assert n.body is not None
        out = []
        for x in items:
            if n.pred is not None and not test(n.pred, x):
                continue
            inner[n.var] = x
            out.append(self._eval(n.body, inner))
        return tuple(out)

    def template(self, tpl: A.Template, env: dict[str, Any]) -> str:
        out: list[str] = []
        for part in tpl.parts:
            if isinstance(part, str):
                out.append(part)
                continue
            v = self.eval(part, env)
            if kind(v) not in ("string", "int", "float", "bool"):
                raise EvalError("MRL-TYPE", f"模板插值不能是 {kind(v)}", part.span)
            out.append(to_string(v))
        return "".join(out)
