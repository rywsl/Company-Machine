"""MRL-E 静态检查：名字解析、类型推导、宏、定义性分析（可选字段需 has() 保护）。

规则见 spec/mrl/mrl-1.0.md 第 5 章。
"""

from __future__ import annotations

from . import ast as A
from . import types as T
from .errors import Diagnostic, StaticError
from .stdlib import Function, SigError


def path_of(node: A.Node) -> str | None:
    """名字后接若干 `.字段`。成员链按迭代展开，避免长链变成宿主递归。"""
    fields: list[str] = []
    cur = node
    while isinstance(cur, A.Select):
        fields.append(cur.field)
        cur = cur.operand
    if not isinstance(cur, A.Ident):
        return None
    return ".".join([cur.name, *reversed(fields)])


def _has_paths(e: A.Has) -> frozenset[str]:
    base = path_of(e.operand)
    return frozenset() if base is None else frozenset({f"{base}.{e.field}"})


def _same_op_spine(e: A.Node, kind: type) -> tuple[A.Node, list[A.Node]]:
    rights: list[A.Node] = []
    cur = e
    while isinstance(cur, kind):
        rights.append(cur.right)
        cur = cur.left
    return cur, rights


def guards_true(e: A.Node) -> frozenset[str]:
    """e 为真时可知存在的字段路径。"""
    if isinstance(e, A.Has):
        return _has_paths(e)
    if isinstance(e, A.And):
        leaf, rights = _same_op_spine(e, A.And)
        acc = guards_true(leaf)
        for right in reversed(rights):
            acc = acc | guards_true(right)
        return acc
    if isinstance(e, A.Or):
        leaf, rights = _same_op_spine(e, A.Or)
        acc = guards_true(leaf)
        for right in reversed(rights):
            acc = acc & guards_true(right)
        return acc
    if isinstance(e, A.Unary) and e.op == "!":
        return guards_false(e.operand)
    return frozenset()


def guards_false(e: A.Node) -> frozenset[str]:
    """e 为假时可知存在的字段路径。"""
    if isinstance(e, A.Or):
        leaf, rights = _same_op_spine(e, A.Or)
        acc = guards_false(leaf)
        for right in reversed(rights):
            acc = acc | guards_false(right)
        return acc
    if isinstance(e, A.And):
        leaf, rights = _same_op_spine(e, A.And)
        acc = guards_false(leaf)
        for right in reversed(rights):
            acc = acc & guards_false(right)
        return acc
    if isinstance(e, A.Unary) and e.op == "!":
        return guards_true(e.operand)
    return frozenset()


def _lit_type(v: object) -> T.Type:
    if v is None:
        return T.NULL
    if isinstance(v, bool):
        return T.BOOL
    if isinstance(v, int):
        return T.INT
    if isinstance(v, float):
        return T.FLOAT
    return T.STRING


class ExprChecker:
    """检查一个表达式。env：根名字 → 类型；functions：可用函数表。"""

    def __init__(self, env: dict[str, T.Type], functions: dict[str, Function], source: str = ""):
        self.env = env
        self.functions = functions
        self.source = source
        self.diags: list[Diagnostic] = []
        self.paths: set[str] = set()

    def report(self, code: str, msg: str, node: A.Node) -> None:
        self.diags.append(Diagnostic(code, msg, node.span, self.source))

    def finish(self) -> None:
        if any(d.severity == "error" for d in self.diags):
            raise StaticError(self.diags)

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diags if d.severity == "warning"]

    # ---------- 入口 ----------

    def check(self, node: A.Node, expect: T.Type | None = None) -> T.Type:
        t = self.infer(node, {}, frozenset())
        if expect is not None and T.compat(t, expect) == "no":
            self.report("MRL-TYPE", f"表达式类型为 {t}，这里需要 {expect}", node)
        return t

    def check_template(self, tpl: A.Template) -> None:
        for part in tpl.parts:
            if isinstance(part, str):
                continue
            t = self.infer(part, {}, frozenset())
            if t not in (T.STRING, T.INT, T.FLOAT, T.NUM, T.BOOL, T.DYN):
                self.report("MRL-TYPE", f"模板插值必须是 string、数或 bool，得到 {t}", part)

    # ---------- 推导 ----------

    def lookup(self, name: str, scope: dict[str, T.Type]) -> T.Type | None:
        if name in scope:
            return scope[name]
        return self.env.get(name)

    def infer(self, n: A.Node, scope: dict[str, T.Type], guards: frozenset[str]) -> T.Type:
        # 二元运算链与成员链不增加嵌套深度（4.7），必须能在源长度限制内检查完。
        if isinstance(n, (A.Binary, A.And, A.Or, A.Select, A.Index)):
            return self._infer_spine(n, scope, guards)
        return self._infer_node(n, scope, guards)

    def _infer_spine(self, n: A.Node, scope: dict[str, T.Type], guards: frozenset[str]) -> T.Type:
        nodes: list[A.Node] = []
        cur: A.Node = n
        while isinstance(cur, (A.Binary, A.And, A.Or, A.Select, A.Index)):
            nodes.append(cur)
            cur = cur.left if isinstance(cur, (A.Binary, A.And, A.Or)) else cur.operand
        t = self.infer(cur, scope, guards)
        for node in reversed(nodes):
            t = self._infer_spine_step(node, t, scope, guards)
        return t

    def _infer_spine_step(
        self, n: A.Node, left_t: T.Type, scope: dict[str, T.Type], guards: frozenset[str]
    ) -> T.Type:
        if isinstance(n, A.Select):
            return self.select(n, scope, guards, operand_type=left_t)
        if isinstance(n, A.Index):
            return self.index(n, scope, guards, operand_type=left_t)
        if isinstance(n, A.Binary):
            return self.binary(n, scope, guards, left_type=left_t)
        extra = guards_true(n.left) if isinstance(n, A.And) else guards_false(n.left)
        rt = self.infer(n.right, scope, guards | extra)
        op = "&&" if isinstance(n, A.And) else "||"
        for side, t in (("左", left_t), ("右", rt)):
            if t not in (T.BOOL, T.DYN):
                self.report("MRL-TYPE", f"'{op}' 的{side}操作数需要 bool，得到 {t}", n)
        return T.BOOL

    def _infer_node(self, n: A.Node, scope: dict[str, T.Type], guards: frozenset[str]) -> T.Type:
        if isinstance(n, A.Lit):
            return _lit_type(n.value)
        if isinstance(n, A.Ident):
            t = self.lookup(n.name, scope)
            if t is None:
                self.report("MRL-UNDEFINED", f"未定义的名字 {n.name!r}", n)
                return T.DYN
            self.paths.add(n.name)
            return t
        if isinstance(n, A.Select):
            return self.select(n, scope, guards)
        if isinstance(n, A.Has):
            ot = self.infer(n.operand, scope, guards)
            if isinstance(ot, T.RecordT):
                if ot.field(n.field) is None and not ot.open:
                    self.report("MRL-NO-FIELD", f"{ot} 没有字段 {n.field!r}", n)
            elif not (isinstance(ot, T.MapT) or ot in (T.DYN, T.NULL)):
                self.report("MRL-TYPE", f"has() 只能用于 map 或记录，得到 {ot}", n)
            return T.BOOL
        if isinstance(n, A.Index):
            return self.index(n, scope, guards)
        if isinstance(n, A.Unary):
            ot = self.infer(n.operand, scope, guards)
            if n.op == "!":
                if ot not in (T.BOOL, T.DYN):
                    self.report("MRL-TYPE", f"'!' 需要 bool，得到 {ot}", n)
                return T.BOOL
            if ot == T.DYN:
                return T.NUM
            if not T.is_numeric(ot):
                self.report("MRL-TYPE", f"一元 '-' 需要数，得到 {ot}", n)
                return T.DYN
            return ot
        if isinstance(n, A.Binary):
            return self.binary(n, scope, guards)
        if isinstance(n, (A.And, A.Or)):
            lt = self.infer(n.left, scope, guards)
            extra = guards_true(n.left) if isinstance(n, A.And) else guards_false(n.left)
            rt = self.infer(n.right, scope, guards | extra)
            op = "&&" if isinstance(n, A.And) else "||"
            for side, t in (("左", lt), ("右", rt)):
                if t not in (T.BOOL, T.DYN):
                    self.report("MRL-TYPE", f"'{op}' 的{side}操作数需要 bool，得到 {t}", n)
            return T.BOOL
        if isinstance(n, A.Cond):
            ct = self.infer(n.cond, scope, guards)
            if ct not in (T.BOOL, T.DYN):
                self.report("MRL-TYPE", f"条件表达式的条件需要 bool，得到 {ct}", n.cond)
            a = self.infer(n.then, scope, guards | guards_true(n.cond))
            b = self.infer(n.otherwise, scope, guards | guards_false(n.cond))
            return T.lub(a, b)
        if isinstance(n, A.ListLit):
            elem: T.Type | None = None
            for item in n.items:
                it = self.infer(item, scope, guards)
                elem = it if elem is None else T.lub(elem, it)
            return T.ListT(elem if elem is not None else T.DYN)
        if isinstance(n, A.MapLit):
            return self.map_lit(n, scope, guards)
        if isinstance(n, A.Call):
            return self.call(n, scope, guards)
        if isinstance(n, A.Macro):
            return self.macro(n, scope, guards)
        self.report("MRL-SYNTAX", f"不支持的表达式节点 {type(n).__name__}", n)
        return T.DYN

    def select(
        self,
        n: A.Select,
        scope: dict[str, T.Type],
        guards: frozenset[str],
        operand_type: T.Type | None = None,
    ) -> T.Type:
        ot = self.infer(n.operand, scope, guards) if operand_type is None else operand_type
        p = path_of(n)
        if p is not None:
            self.paths.add(p)
        if isinstance(ot, T.RecordT):
            f = ot.field(n.field)
            if f is None:
                if ot.open:
                    return T.DYN
                self.report("MRL-NO-FIELD", f"{ot} 没有字段 {n.field!r}", n)
                return T.DYN
            ft, required = f
            if not required and (p is None or p not in guards):
                self.report("MRL-MAYBE-ABSENT", f"字段 {p or n.field!r} 可能不存在，请先用 has() 判断", n)
            return ft
        if isinstance(ot, T.MapT):
            return ot.elem
        if ot == T.DYN:
            return T.DYN
        self.report("MRL-TYPE", f"不能对 {ot} 取字段 {n.field!r}", n)
        return T.DYN

    def index(
        self,
        n: A.Index,
        scope: dict[str, T.Type],
        guards: frozenset[str],
        operand_type: T.Type | None = None,
    ) -> T.Type:
        ot = self.infer(n.operand, scope, guards) if operand_type is None else operand_type
        it = self.infer(n.index, scope, guards)
        if isinstance(ot, T.ListT):
            if it not in (T.INT, T.DYN, T.NUM):
                self.report("MRL-TYPE", f"列表下标需要 int，得到 {it}", n.index)
            return ot.elem
        if isinstance(ot, T.MapT):
            if it not in (T.STRING, T.DYN):
                self.report("MRL-TYPE", f"map 的键需要 string，得到 {it}", n.index)
            return ot.elem
        if isinstance(ot, T.RecordT):
            if isinstance(n.index, A.Lit) and isinstance(n.index.value, str):
                f = ot.field(n.index.value)
                if f is None:
                    if ot.open:
                        return T.DYN
                    self.report("MRL-NO-FIELD", f"{ot} 没有字段 {n.index.value!r}", n)
                    return T.DYN
                return f[0]
            if it not in (T.STRING, T.DYN):
                self.report("MRL-TYPE", f"记录的键需要 string，得到 {it}", n.index)
            return T.DYN
        if ot == T.DYN:
            return T.DYN
        self.report("MRL-TYPE", f"不能对 {ot} 使用下标", n)
        return T.DYN

    def binary(
        self,
        n: A.Binary,
        scope: dict[str, T.Type],
        guards: frozenset[str],
        left_type: T.Type | None = None,
    ) -> T.Type:
        lt = self.infer(n.left, scope, guards) if left_type is None else left_type
        rt = self.infer(n.right, scope, guards)
        op = n.op
        if op == "+":
            if T.is_numeric(lt) and T.is_numeric(rt):
                return T.numeric_result(lt, rt)
            if lt == rt == T.STRING:
                return T.STRING
            if isinstance(lt, T.ListT) and isinstance(rt, T.ListT):
                return T.ListT(T.lub(lt.elem, rt.elem))
            if T.DYN in (lt, rt):
                other = rt if lt == T.DYN else lt
                if other == T.DYN or T.is_numeric(other) or other == T.STRING or isinstance(other, T.ListT):
                    return T.NUM if T.is_numeric(other) else (other if other != T.DYN else T.DYN)
            self.report("MRL-TYPE", f"'+' 不能用于 {lt} 与 {rt}", n)
            return T.DYN
        if op in ("-", "*", "/", "%"):
            ok = all(t == T.DYN or T.is_numeric(t) for t in (lt, rt))
            if not ok:
                self.report("MRL-TYPE", f"'{op}' 需要数，得到 {lt} 与 {rt}", n)
                return T.DYN
            if op == "/":
                return T.FLOAT
            if op == "%":
                if T.FLOAT in (lt, rt):
                    self.report("MRL-TYPE", "'%' 只能用于 int", n)
                return T.INT
            return T.numeric_result(T.NUM if lt == T.DYN else lt, T.NUM if rt == T.DYN else rt)
        if op in ("<", "<=", ">", ">="):
            ok = (
                T.DYN in (lt, rt)
                or (T.is_numeric(lt) and T.is_numeric(rt))
                or (lt == T.STRING and rt == T.STRING)
            )
            if not ok:
                self.report("MRL-TYPE", f"'{op}' 不能比较 {lt} 与 {rt}", n)
            return T.BOOL
        if op in ("==", "!="):
            if not T.comparable_eq(lt, rt):
                self.report("MRL-TYPE", f"'{op}' 两边的类型不可比较：{lt} 与 {rt}", n)
            return T.BOOL
        if op == "in":
            if isinstance(rt, T.ListT):
                if not T.comparable_eq(lt, rt.elem):
                    self.report("MRL-TYPE", f"'in' 左边为 {lt}，列表元素为 {rt.elem}，不可比较", n)
            elif isinstance(rt, (T.MapT, T.RecordT)):
                if lt not in (T.STRING, T.DYN):
                    self.report("MRL-TYPE", f"判断 map 的键时左边需要 string，得到 {lt}", n)
            elif rt != T.DYN:
                self.report("MRL-TYPE", f"'in' 的右边需要 list 或 map，得到 {rt}", n)
            return T.BOOL
        self.report("MRL-SYNTAX", f"未知运算符 {op}", n)
        return T.DYN

    def map_lit(self, n: A.MapLit, scope: dict[str, T.Type], guards: frozenset[str]) -> T.Type:
        literal_keys = all(isinstance(k, A.Lit) and isinstance(k.value, str) for k, _ in n.entries)
        fields: dict[str, T.Type] = {}
        elem: T.Type | None = None
        for k, v in n.entries:
            kt = self.infer(k, scope, guards)
            if kt not in (T.STRING, T.DYN):
                self.report("MRL-TYPE", f"map 的键必须是 string，得到 {kt}", k)
            vt = self.infer(v, scope, guards)
            elem = vt if elem is None else T.lub(elem, vt)
            if isinstance(k, A.Lit) and isinstance(k.value, str):
                if k.value in fields:
                    self.report("MRL-DUP-KEY", f"map 字面量中重复的键 {k.value!r}", k)
                fields[k.value] = vt
        if literal_keys and n.entries:
            return T.record(fields)
        return T.MapT(elem if elem is not None else T.DYN)

    def call(self, n: A.Call, scope: dict[str, T.Type], guards: frozenset[str]) -> T.Type:
        arg_types = [self.infer(a, scope, guards) for a in n.args]
        fn = self.functions.get(n.func)
        if fn is None:
            self.report("MRL-UNDEFINED", f"未定义的函数 {n.func!r}", n)
            return T.DYN
        if len(arg_types) < fn.min_args or (fn.max_args is not None and len(arg_types) > fn.max_args):
            want = str(fn.min_args) if fn.max_args == fn.min_args else f"{fn.min_args}..{fn.max_args or 'n'}"
            self.report("MRL-ARITY", f"{n.func}() 需要 {want} 个参数，得到 {len(arg_types)} 个", n)
            return T.DYN
        try:
            return fn.sig(arg_types)
        except SigError as e:
            self.report(e.code, e.message, n)
            return T.DYN

    def macro(self, n: A.Macro, scope: dict[str, T.Type], guards: frozenset[str]) -> T.Type:
        tt = self.infer(n.target, scope, guards)
        if isinstance(tt, T.ListT):
            vt = tt.elem
        elif isinstance(tt, (T.MapT, T.RecordT)):
            vt = T.STRING
        elif tt == T.DYN:
            vt = T.DYN
        else:
            self.report("MRL-TYPE", f"{n.kind} 宏只能用于 list 或 map，得到 {tt}", n)
            vt = T.DYN
        if self.lookup(n.var, scope) is not None:
            self.report("MRL-SHADOW", f"宏变量 {n.var!r} 遮蔽了已有的名字", n)
        inner = dict(scope)
        inner[n.var] = vt
        if n.pred is not None:
            pt = self.infer(n.pred, inner, guards)
            if pt not in (T.BOOL, T.DYN):
                self.report("MRL-TYPE", f"{n.kind} 宏的条件需要 bool，得到 {pt}", n.pred)
        if n.kind in ("all", "exists", "exists_one"):
            return T.BOOL
        if n.kind == "filter":
            return T.ListT(vt)
        assert n.body is not None
        bt = self.infer(n.body, inner, guards)
        return T.ListT(bt)


def check_expr(
    node: A.Node,
    env: dict[str, T.Type],
    functions: dict[str, Function],
    source: str = "",
    expect: T.Type | None = None,
) -> tuple[T.Type, list[Diagnostic], set[str]]:
    """检查表达式，返回（类型、警告、访问过的路径）。有错误时抛 StaticError。"""
    c = ExprChecker(env, functions, source)
    t = c.check(node, expect)
    c.finish()
    return t, c.warnings, c.paths
