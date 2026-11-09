"""MRL-R：关系规则（spec/mrl/mrl-1.0.md 第 8 章）。

语义要点：
- 程序 = 若干模块；关系必须先声明（列带类型）；@input 关系只能由宿主提供事实；
- 归属：规则只能定义本模块声明的关系；@open 关系任何模块都能添加规则，但不能在规则体中读取；
- 安全：规则体从左到右求值，变量在使用前必须被正向原子或赋值绑定；
- 分层：否定与聚合不能出现在递归环上；递归规则中不允许构造新值（保证终止）；
- 求值：按层自底向上、半朴素迭代；结果是唯一的完美模型，与求值顺序无关；
- 任何运行时错误都会终止整个求值（失败即关闭）。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import ast as A
from . import types as T
from .checker import ExprChecker
from .errors import Diagnostic, EvalError, MRLError, StaticError
from .evaluator import DEFAULT_BUDGET, Evaluator
from .parser import parse_module
from .stdlib import Function, make_table
from .values import (
    MapValue,
    check_int,
    compare,
    eq,
    eq_key,
    from_py,
    ident_key,
    is_number,
    kind,
    norm_float,
    order_key,
    to_float,
    type_name,
)

MAX_TUPLES = 5_000_000


# ---------- 编译后的结构 ----------


@dataclass
class RelInfo:
    name: str
    columns: list[tuple[str, T.Type]]
    input: bool
    open: bool
    module: str
    doc: str
    decl: A.RelDecl

    @property
    def arity(self) -> int:
        return len(self.columns)

    def signature(self) -> str:
        cols = ", ".join(f"{n}: {t}" for n, t in self.columns)
        flags = "".join(f" @{f}" for f, on in (("input", self.input), ("open", self.open)) if on)
        return f"{self.name}({cols}){flags}"


@dataclass
class Arg:
    kind: str  # var | anon | const | expr
    name: str = ""
    value: Any = None
    node: A.Node | None = None


@dataclass
class Lit:
    kind: str  # atom | neg | assign | guard
    node: A.Node
    rel: str = ""
    args: list[Arg] = field(default_factory=list)
    var: str = ""
    expr: A.Node | None = None


@dataclass
class HeadTerm:
    kind: str  # var | const | expr | agg
    name: str = ""
    value: Any = None
    node: A.Node | None = None
    agg: str = ""


@dataclass
class CRule:
    rule: A.Rule
    head_rel: str
    head: list[HeadTerm]
    body: list[Lit]
    variables: list[str]  # 全部变量（含匿名变量的唯一名），用于聚合的绑定去重
    source: str

    @property
    def is_agg(self) -> bool:
        return any(t.kind == "agg" for t in self.head)

    def invents(self) -> bool:
        if any(t.kind in ("expr", "agg") for t in self.head):
            return True
        return any(lit.kind == "assign" and not _trivial(lit.expr) for lit in self.body)


def _trivial(node: A.Node | None) -> bool:
    return isinstance(node, (A.Lit, A.Ident))


def _expr_vars(node: A.Node, bound_macro: frozenset[str] = frozenset()) -> set[str]:
    """表达式中引用的规则变量（以大写字母或下划线开头、且不是宏变量的名字）。"""
    out: set[str] = set()

    stack: list[tuple[A.Node, frozenset[str]]] = [(node, bound_macro)]
    while stack:
        n, macro = stack.pop()
        if isinstance(n, A.Ident):
            if A.is_var_name(n.name) and n.name not in macro:
                out.add(n.name)
            continue
        if isinstance(n, A.Macro):
            stack.append((n.target, macro))
            inner = macro | {n.var}
            if n.body is not None:
                stack.append((n.body, inner))
            if n.pred is not None:
                stack.append((n.pred, inner))
            continue
        for child in reversed(_children(n)):
            stack.append((child, macro))
    return out


def _children(n: A.Node) -> list[A.Node]:
    if isinstance(n, A.Select):
        return [n.operand]
    if isinstance(n, A.Has):
        return [n.operand]
    if isinstance(n, A.Index):
        return [n.operand, n.index]
    if isinstance(n, A.Call):
        return list(n.args)
    if isinstance(n, A.Unary):
        return [n.operand]
    if isinstance(n, (A.Binary, A.And, A.Or)):
        return [n.left, n.right]
    if isinstance(n, A.Cond):
        return [n.cond, n.then, n.otherwise]
    if isinstance(n, A.ListLit):
        return list(n.items)
    if isinstance(n, A.MapLit):
        return [x for kv in n.entries for x in kv]
    return []


# ---------- 值的列类型规范化 ----------


def normalize(v: Any, t: T.Type) -> Any:
    """把值放进类型为 t 的列：int 放进 float 列时转换为 float；种类不符时抛 MRL-TYPE。"""
    if t == T.DYN:
        return v
    if t == T.BOOL:
        if isinstance(v, bool):
            return v
    elif t == T.INT:
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    elif t == T.FLOAT:
        if isinstance(v, float):
            return v
        if isinstance(v, int) and not isinstance(v, bool):
            return to_float(v)
    elif t == T.STRING:
        if isinstance(v, str):
            return v
    elif isinstance(t, T.ListT):
        if isinstance(v, tuple):
            return tuple(normalize(e, t.elem) for e in v)
    elif isinstance(t, T.MapT):
        if isinstance(v, MapValue):
            return MapValue((k, normalize(v[k], t.elem)) for k in v)
    raise EvalError("MRL-TYPE", f"值 {type_name(v)} 不能放进类型为 {t} 的列")


class Relation:
    def __init__(self, info: RelInfo):
        self.info = info
        self.types = [t for _, t in info.columns]
        self.rows: dict[tuple, tuple] = {}
        self.indexes: dict[tuple[int, ...], dict[tuple, list[tuple]]] = {}

    def __len__(self) -> int:
        return len(self.rows)

    def add(self, tup: tuple) -> bool:
        key = tuple(ident_key(v) for v in tup)
        if key in self.rows:
            return False
        self.rows[key] = tup
        for positions, idx in self.indexes.items():
            idx.setdefault(tuple(key[p] for p in positions), []).append(tup)
        return True

    def lookup(self, positions: tuple[int, ...], keys: tuple) -> Iterable[tuple]:
        if not positions:
            return list(self.rows.values())
        idx = self.indexes.get(positions)
        if idx is None:
            idx = {}
            for k, tup in self.rows.items():
                idx.setdefault(tuple(k[p] for p in positions), []).append(tup)
            self.indexes[positions] = idx
        return idx.get(keys, ())

    def sorted_rows(self) -> list[tuple]:
        return sorted(self.rows.values(), key=lambda tup: tuple(order_key(v) for v in tup))


class RowSet:
    """半朴素迭代中的增量集合，接口与 Relation.lookup 相同。"""

    def __init__(self, rows: list[tuple]):
        self.rows = rows
        self.keyed = [(tuple(ident_key(v) for v in r), r) for r in rows]

    def lookup(self, positions: tuple[int, ...], keys: tuple) -> Iterable[tuple]:
        if not positions:
            return self.rows
        return [r for k, r in self.keyed if tuple(k[p] for p in positions) == keys]


# ---------- 程序 ----------


@dataclass
class Model:
    """求值结果：每个关系的元组集合。"""

    program: Program
    relations: dict[str, Relation]
    fired: dict[tuple[str, int], int] = field(
        default_factory=dict
    )  # (源名, 规则所在行) → 新增元组数，用于规则覆盖率

    def rows(self, name: str) -> list[tuple]:
        if name not in self.relations:
            raise KeyError(f"未声明的关系 {name}")
        return self.relations[name].sorted_rows()

    def contains(self, name: str, tup: Sequence) -> bool:
        rel = self.relations[name]
        try:
            norm = tuple(normalize(from_py(v), t) for v, t in zip(tup, rel.types, strict=True))
        except (EvalError, ValueError):
            return False
        return tuple(ident_key(v) for v in norm) in rel.rows


class Program:
    def __init__(self) -> None:
        self.modules: list[A.Module] = []
        self.relations: dict[str, RelInfo] = {}
        self.rules: list[CRule] = []
        self.strata: list[list[str]] = []
        self.functions: dict[str, Function] = {}
        self.warnings: list[Diagnostic] = []

    # ----- 构建与静态检查 -----

    @classmethod
    def from_sources(
        cls, sources: Sequence[tuple[str, str]], functions: dict[str, Function] | None = None
    ) -> Program:
        """sources: [(源名, 文本)]。"""
        diags: list[Diagnostic] = []
        modules: list[A.Module] = []
        for name, text in sources:
            try:
                modules.append(parse_module(text, name))
            except StaticError as e:
                diags.extend(e.diagnostics)
        if diags:
            raise StaticError(diags)
        return cls.build(modules, functions)

    @classmethod
    def build(cls, modules: Sequence[A.Module], functions: dict[str, Function] | None = None) -> Program:
        p = cls()
        p.modules = list(modules)
        p.functions = functions if functions is not None else make_table()
        diags: list[Diagnostic] = []

        def err(code: str, msg: str, node: A.Node, source: str) -> None:
            diags.append(Diagnostic(code, msg, node.span, source))

        seen_modules: dict[str, str] = {}
        for m in modules:
            if m.name in seen_modules:
                err("MRL-DUP", f"模块 {m.name} 重复定义（另见 {seen_modules[m.name]}）", m, m.source)
            seen_modules[m.name] = m.source
            for d in m.decls:
                if d.name in p.relations:
                    other = p.relations[d.name]
                    err("MRL-DUP", f"关系 {d.name} 重复声明（另见模块 {other.module}）", d, m.source)
                    continue
                if d.name in p.functions:
                    err("MRL-DUP", f"关系 {d.name} 与函数同名", d, m.source)
                    continue
                if d.input and d.open:
                    err("MRL-DECL", f"关系 {d.name} 不能同时是 @input 与 @open", d, m.source)
                cols: list[tuple[str, T.Type]] = []
                names: set[str] = set()
                for c in d.columns:
                    if c.name in names:
                        err("MRL-DECL", f"关系 {d.name} 的列名 {c.name} 重复", c, m.source)
                    names.add(c.name)
                    try:
                        cols.append((c.name, T.from_type_ref(c.type)))
                    except ValueError as e:
                        err("MRL-DECL", f"关系 {d.name} 的列 {c.name}：{e}", c, m.source)
                        cols.append((c.name, T.DYN))
                p.relations[d.name] = RelInfo(d.name, cols, d.input, d.open, m.name, d.doc, d)
        if diags:
            raise StaticError(diags)

        for m in modules:
            for r in m.rules:
                try:
                    p.rules.append(p._compile_rule(r, m))
                except StaticError as e:
                    diags.extend(e.diagnostics)
        if diags:
            raise StaticError(diags)

        p._stratify(diags)
        if diags:
            raise StaticError(diags)
        p._unused()
        return p

    def _compile_rule(self, r: A.Rule, m: A.Module) -> CRule:
        diags: list[Diagnostic] = []
        src = m.source

        def err(code: str, msg: str, node: A.Node) -> None:
            diags.append(Diagnostic(code, msg, node.span, src))

        def fail() -> StaticError:
            return StaticError(diags)

        info = self.relations.get(r.head.rel)
        if info is None:
            err("MRL-UNDEFINED", f"未声明的关系 {r.head.rel}", r.head)
            raise fail()
        if info.input:
            err("MRL-OWNERSHIP", f"关系 {info.name} 是 @input，只能由宿主提供事实，不能写规则或事实", r.head)
        elif info.module != m.name and not info.open:
            err(
                "MRL-OWNERSHIP",
                f"关系 {info.name} 属于模块 {info.module}，模块 {m.name} 不能为它写规则",
                r.head,
            )
        if len(r.head.terms) != info.arity:
            err("MRL-ARITY", f"关系 {info.name} 有 {info.arity} 列，这里给了 {len(r.head.terms)} 个", r.head)
            raise fail()

        bound: dict[str, T.Type] = {}
        variables: list[str] = []
        anon_counter = 0
        body: list[Lit] = []

        def new_var(name: str, t: T.Type) -> None:
            bound[name] = t
            variables.append(name)

        def check_expr(node: A.Node, expect: T.Type | None = None) -> T.Type:
            missing = sorted(_expr_vars(node) - set(bound))
            if missing:
                err("MRL-UNSAFE", f"变量 {', '.join(missing)} 在使用前没有被绑定", node)
                return T.DYN
            if any(isinstance(x, A.Ident) and x.name == "_" for x in _walk(node)):
                err("MRL-UNSAFE", "匿名变量 '_' 只能直接作为原子的参数", node)
                return T.DYN
            c = ExprChecker(dict(bound), self.functions, src)
            t = c.check(node, expect)
            for d in c.diags:
                diags.append(d)
            return t

        for lit in r.body:
            if isinstance(lit, A.Assign):
                t = check_expr(lit.expr)
                if lit.var == "_":
                    err("MRL-UNSAFE", "不能给匿名变量赋值", lit)
                elif lit.var in bound:
                    if not T.comparable_eq(bound[lit.var], t):
                        err("MRL-TYPE", f"变量 {lit.var} 的类型 {bound[lit.var]} 与 {t} 不可比较", lit)
                else:
                    new_var(lit.var, t)
                body.append(Lit("assign", lit, var=lit.var, expr=lit.expr))
                continue
            assert isinstance(lit, A.Guard)
            e = lit.expr
            negated = False
            call = e
            if isinstance(e, A.Unary) and e.op == "!" and isinstance(e.operand, A.Call):
                negated = True
                call = e.operand
            if isinstance(call, A.Call) and not call.receiver_style and call.func in self.relations:
                rinfo = self.relations[call.func]
                if rinfo.open:
                    err("MRL-OWNERSHIP", f"关系 {rinfo.name} 是 @open（只写），不能在规则体中读取", call)
                if len(call.args) != rinfo.arity:
                    err(
                        "MRL-ARITY",
                        f"关系 {rinfo.name} 有 {rinfo.arity} 列，这里给了 {len(call.args)} 个",
                        call,
                    )
                    continue
                args: list[Arg] = []
                first_seen: set[str] = set()
                for a, (_, ctype) in zip(call.args, rinfo.columns, strict=True):
                    if isinstance(a, A.Ident) and a.name == "_":
                        anon_counter += 1
                        name = f"_#{anon_counter}"
                        if not negated:
                            new_var(name, ctype)
                        args.append(Arg("anon", name=name))
                    elif isinstance(a, A.Ident) and A.is_var_name(a.name):
                        if a.name in bound:
                            if T.compat(bound[a.name], ctype) == "no":
                                err(
                                    "MRL-TYPE",
                                    f"变量 {a.name} 的类型 {bound[a.name]} 与列类型 {ctype} 不符",
                                    a,
                                )
                            args.append(Arg("var", name=a.name))
                        elif negated:
                            err("MRL-UNSAFE", f"否定原子中的变量 {a.name} 必须先被绑定", a)
                            args.append(Arg("var", name=a.name))
                        else:
                            if a.name in first_seen:
                                args.append(Arg("var", name=a.name))
                            else:
                                first_seen.add(a.name)
                                args.append(Arg("var", name=a.name))
                    elif isinstance(a, A.Lit):
                        lt = _lit_type(a.value)
                        if T.compat(lt, ctype) == "no":
                            err("MRL-TYPE", f"常量 {a.value!r} 与列类型 {ctype} 不符", a)
                        args.append(Arg("const", value=a.value))
                    else:
                        t = check_expr(a)
                        if T.compat(t, ctype) == "no":
                            err("MRL-TYPE", f"表达式类型 {t} 与列类型 {ctype} 不符", a)
                        args.append(Arg("expr", node=a))
                # 原子中首次出现的变量在原子之后才算绑定
                if not negated:
                    for a, (_, ctype) in zip(call.args, rinfo.columns, strict=True):
                        if (
                            isinstance(a, A.Ident)
                            and A.is_var_name(a.name)
                            and a.name != "_"
                            and a.name not in bound
                        ):
                            new_var(a.name, ctype)
                body.append(Lit("neg" if negated else "atom", lit, rel=rinfo.name, args=args))
                continue
            if isinstance(call, A.Call) and not call.receiver_style and call.func not in self.functions:
                err("MRL-UNDEFINED", f"{call.func} 既不是已声明的关系，也不是函数", call)
                continue
            check_expr(e, T.BOOL)
            body.append(Lit("guard", lit, expr=e))

        head: list[HeadTerm] = []
        for term, (cname, ctype) in zip(r.head.terms, info.columns, strict=True):
            if isinstance(term, A.Agg):
                if term.expr is None:
                    rt: T.Type = T.INT
                else:
                    et = check_expr(term.expr)
                    if term.func == "sum":
                        if not (T.is_numeric(et) or et == T.DYN):
                            err("MRL-TYPE", f"sum() 需要数，得到 {et}", term)
                        rt = et if et in (T.INT, T.FLOAT) else T.NUM
                    elif term.func in ("min", "max"):
                        if not (T.is_numeric(et) or et in (T.STRING, T.DYN)):
                            err("MRL-TYPE", f"{term.func}() 需要数或 string，得到 {et}", term)
                        rt = et
                    else:
                        rt = T.ListT(et)
                if T.compat(rt, ctype) == "no":
                    err("MRL-TYPE", f"聚合 {term.func}() 的类型 {rt} 与列 {cname}: {ctype} 不符", term)
                head.append(HeadTerm("agg", node=term.expr, agg=term.func))
            elif isinstance(term, A.Ident) and A.is_var_name(term.name):
                if term.name == "_":
                    err("MRL-UNSAFE", "规则头中不能使用匿名变量", term)
                elif term.name not in bound:
                    err("MRL-UNSAFE", f"规则头中的变量 {term.name} 没有在规则体中绑定", term)
                elif T.compat(bound[term.name], ctype) == "no":
                    err(
                        "MRL-TYPE",
                        f"变量 {term.name} 的类型 {bound[term.name]} 与列 {cname}: {ctype} 不符",
                        term,
                    )
                head.append(HeadTerm("var", name=term.name))
            elif isinstance(term, A.Lit):
                if T.compat(_lit_type(term.value), ctype) == "no":
                    err("MRL-TYPE", f"常量 {term.value!r} 与列 {cname}: {ctype} 不符", term)
                head.append(HeadTerm("const", value=term.value))
            else:
                t = check_expr(term)
                if T.compat(t, ctype) == "no":
                    err("MRL-TYPE", f"表达式类型 {t} 与列 {cname}: {ctype} 不符", term)
                head.append(HeadTerm("expr", node=term))

        if any(d.severity == "error" for d in diags):
            raise fail()
        self.warnings.extend(d for d in diags if d.severity == "warning")
        return CRule(r, info.name, head, body, variables, src)

    def _stratify(self, diags: list[Diagnostic]) -> None:
        names = sorted(self.relations)
        edges: dict[str, set[str]] = {n: set() for n in names}
        neg_edges: set[tuple[str, str]] = set()
        for cr in self.rules:
            for lit in cr.body:
                if lit.kind in ("atom", "neg"):
                    edges[lit.rel].add(cr.head_rel)
                    if lit.kind == "neg" or cr.is_agg:
                        neg_edges.add((lit.rel, cr.head_rel))
        comp = _tarjan(names, edges)
        comp_of = {n: i for i, c in enumerate(comp) for n in c}
        for a, b in sorted(neg_edges):
            if comp_of[a] == comp_of[b]:
                cyc = sorted(comp[comp_of[a]])
                rule = next(
                    cr
                    for cr in self.rules
                    if cr.head_rel == b
                    and any(lit.rel == a and (lit.kind == "neg" or cr.is_agg) for lit in cr.body)
                )
                kind_ = "聚合" if rule.is_agg else "否定"
                diags.append(
                    Diagnostic(
                        "MRL-STRATIFY",
                        f"{kind_}出现在递归环上：{' → '.join(cyc)}",
                        rule.rule.span,
                        rule.source,
                    )
                )
        for cr in self.rules:
            recursive = any(
                lit.kind == "atom" and comp_of[lit.rel] == comp_of[cr.head_rel] for lit in cr.body
            )
            if recursive and cr.invents():
                diags.append(
                    Diagnostic(
                        "MRL-INVENTION",
                        f"递归规则 {cr.head_rel} 中构造了新值；递归规则的头部与赋值只能使用变量或常量",
                        cr.rule.span,
                        cr.source,
                    )
                )
        # 拓扑排序：Tarjan 给出的是逆拓扑序
        order = list(reversed(comp))
        self.strata = [sorted(c) for c in order]

    def _unused(self) -> None:
        defined = {cr.head_rel for cr in self.rules}
        used = {lit.rel for cr in self.rules for lit in cr.body if lit.kind in ("atom", "neg")}
        for name, info in sorted(self.relations.items()):
            if info.input or info.open:
                continue
            m = next(mm for mm in self.modules if mm.name == info.module)
            if name not in defined and name in used:
                self.warnings.append(
                    Diagnostic(
                        "MRL-UNUSED", f"关系 {name} 没有任何规则或事实，永远为空", info.decl.span, m.source
                    )
                )
            elif name not in defined and name not in used:
                self.warnings.append(
                    Diagnostic(
                        "MRL-UNUSED", f"关系 {name} 既没有被定义也没有被使用", info.decl.span, m.source
                    )
                )

    # ----- 求值 -----

    def evaluate(
        self, facts: dict[str, Iterable[Sequence[Any]]] | None = None, budget: int = DEFAULT_BUDGET
    ) -> Model:
        rels = {name: Relation(info) for name, info in self.relations.items()}
        for name, rows in (facts or {}).items():
            info = self.relations.get(name)
            if info is None:
                raise EvalError("MRL-FACT", f"未声明的关系 {name}")
            if not info.input:
                raise EvalError("MRL-FACT", f"关系 {name} 不是 @input，不能由宿主提供事实")
            for row in rows:
                if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
                    raise EvalError(
                        "MRL-FACT",
                        f"关系 {name} 的每一行必须是列表，得到 {type(row).__name__}",
                    )
                row = list(row)
                if len(row) != info.arity:
                    raise EvalError(
                        "MRL-FACT", f"关系 {name} 有 {info.arity} 列，事实 {row!r} 有 {len(row)} 列"
                    )
                try:
                    tup = tuple(normalize(from_py(v), t) for v, (_, t) in zip(row, info.columns, strict=True))
                except EvalError as e:
                    raise EvalError("MRL-FACT", f"关系 {name} 的事实 {row!r} 不符合声明：{e.message}") from e
                if rels[name].add(tup):
                    _check_tuple_budget(sum(len(r) for r in rels.values()))
        ev = _RuleEvaluator(self, rels, budget)
        for stratum in self.strata:
            ev.run_stratum(set(stratum))
        return Model(self, rels, ev.fired)


def _check_tuple_budget(total: int) -> None:
    """每插入一个新元组就检查总数，宿主事实与派生元组同一上限（规范 8.6、10.1）。"""
    if total > MAX_TUPLES:
        raise EvalError("MRL-COST", f"元组总数超过上限 {MAX_TUPLES}（含宿主事实）")


def _walk(n: A.Node) -> Iterator[A.Node]:
    """迭代遍历，长的左结合链不会变成宿主递归。"""
    stack = [n]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, A.Macro):
            if cur.body is not None:
                stack.append(cur.body)
            if cur.pred is not None:
                stack.append(cur.pred)
            stack.append(cur.target)
            continue
        stack.extend(reversed(_children(cur)))


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


def _tarjan(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = 0

    def strong(v: str) -> None:
        nonlocal counter
        # 迭代实现，避免深递归
        work = [(v, iter(sorted(edges[v])))]
        index[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(edges[w]))))
                    advanced = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                result.append(comp)

    for n in nodes:
        if n not in index:
            strong(n)
    return result


class _RuleEvaluator:
    def __init__(self, program: Program, rels: dict[str, Relation], budget: int):
        self.p = program
        self.rels = rels
        self.ev = Evaluator(program.functions, budget)
        self.budget = budget
        self.total = sum(len(r) for r in rels.values())
        self.fired: dict[tuple[str, int], int] = {}

    def eval_expr(self, node: A.Node, binding: dict[str, Any], cr: CRule) -> Any:
        self.ev.steps = 0
        try:
            return self.ev.eval(node, binding)
        except EvalError as e:
            if not e.source:
                e.source = cr.source
            raise

    def run_stratum(self, stratum: set[str]) -> None:
        rules = [cr for cr in self.p.rules if cr.head_rel in stratum]
        recursive = [cr for cr in rules if any(lit.kind == "atom" and lit.rel in stratum for lit in cr.body)]
        delta: dict[str, list[tuple]] = {}
        for cr in rules:
            for tup in self.fire(cr, None, None):
                if self.insert(cr, tup):
                    delta.setdefault(cr.head_rel, []).append(self.last)
        if not recursive:
            return
        while delta:
            new_delta: dict[str, list[tuple]] = {}
            for cr in recursive:
                for pos, lit in enumerate(cr.body):
                    if lit.kind == "atom" and lit.rel in delta:
                        for tup in self.fire(cr, pos, RowSet(delta[lit.rel])):
                            if self.insert(cr, tup):
                                new_delta.setdefault(cr.head_rel, []).append(self.last)
            delta = new_delta

    def insert(self, cr: CRule, tup: tuple) -> bool:
        rel = self.rels[cr.head_rel]
        try:
            norm = tuple(normalize(v, t) for v, t in zip(tup, rel.types, strict=True))
        except EvalError as e:
            e.span = cr.rule.head.span
            e.source = cr.source
            raise
        self.last = norm
        added = rel.add(norm)
        if added:
            key = (cr.source, cr.rule.span.line)
            self.fired[key] = self.fired.get(key, 0) + 1
            self.total += 1
            if self.total > MAX_TUPLES:
                raise EvalError(
                    "MRL-COST",
                    f"元组总数超过上限 {MAX_TUPLES}（含宿主事实）",
                    cr.rule.span,
                    cr.source,
                )
        return added

    def fire(self, cr: CRule, delta_pos: int | None, delta: RowSet | None) -> Iterator[tuple]:
        if cr.is_agg:
            yield from self.fire_agg(cr)
            return
        for binding in self.solve(cr, 0, {}, delta_pos, delta):
            yield tuple(self.head_value(t, binding, cr) for t in cr.head)

    def head_value(self, t: HeadTerm, binding: dict[str, Any], cr: CRule) -> Any:
        if t.kind == "var":
            return binding[t.name]
        if t.kind == "const":
            return t.value
        assert t.node is not None
        return self.eval_expr(t.node, binding, cr)

    def solve(
        self, cr: CRule, i: int, binding: dict[str, Any], delta_pos: int | None, delta: RowSet | None
    ) -> Iterator[dict[str, Any]]:
        if i == len(cr.body):
            yield binding
            return
        lit = cr.body[i]
        if lit.kind == "assign":
            assert lit.expr is not None
            v = self.eval_expr(lit.expr, binding, cr)
            if lit.var in binding:
                if eq(binding[lit.var], v):
                    yield from self.solve(cr, i + 1, binding, delta_pos, delta)
                return
            nb = dict(binding)
            nb[lit.var] = v
            yield from self.solve(cr, i + 1, nb, delta_pos, delta)
            return
        if lit.kind == "guard":
            assert lit.expr is not None
            v = self.eval_expr(lit.expr, binding, cr)
            if not isinstance(v, bool):
                raise EvalError(
                    "MRL-TYPE", f"守卫表达式需要 bool，得到 {type_name(v)}", lit.node.span, cr.source
                )
            if v:
                yield from self.solve(cr, i + 1, binding, delta_pos, delta)
            return
        rel = self.rels[lit.rel]
        source = delta if (delta_pos == i and delta is not None) else rel
        positions: list[int] = []
        keys: list[tuple] = []
        free: list[tuple[int, str]] = []
        for pos, (a, ctype) in enumerate(zip(lit.args, rel.types, strict=True)):
            if a.kind == "anon":
                free.append((pos, a.name))
                continue
            if a.kind == "var" and a.name not in binding:
                free.append((pos, a.name))
                continue
            if a.kind == "var":
                v = binding[a.name]
            elif a.kind == "const":
                v = a.value
            else:
                assert a.node is not None
                v = self.eval_expr(a.node, binding, cr)
            try:
                nv = normalize(v, ctype)
            except EvalError:
                if lit.kind == "neg":
                    yield from self.solve(cr, i + 1, binding, delta_pos, delta)
                return
            positions.append(pos)
            keys.append(ident_key(nv))
        candidates = source.lookup(tuple(positions), tuple(keys))
        if lit.kind == "neg":
            if not candidates:
                yield from self.solve(cr, i + 1, binding, delta_pos, delta)
            return
        for row in list(candidates):
            nb = dict(binding)
            ok = True
            for pos, name in free:
                v = row[pos]
                if name in nb:
                    if ident_key(nb[name]) != ident_key(v):
                        ok = False
                        break
                else:
                    nb[name] = v
            if ok:
                yield from self.solve(cr, i + 1, nb, delta_pos, delta)

    def fire_agg(self, cr: CRule) -> Iterator[tuple]:
        seen: set[tuple] = set()
        groups: dict[tuple, tuple[tuple, list[list[Any]]]] = {}
        agg_terms = [t for t in cr.head if t.kind == "agg"]
        for binding in self.solve(cr, 0, {}, None, None):
            bkey = tuple(ident_key(binding[v]) for v in sorted(cr.variables) if v in binding)
            if bkey in seen:
                continue
            seen.add(bkey)
            gvals = tuple(self.head_value(t, binding, cr) for t in cr.head if t.kind != "agg")
            gkey = tuple(ident_key(v) for v in gvals)
            if gkey not in groups:
                groups[gkey] = (gvals, [[] for _ in agg_terms])
            vals = groups[gkey][1]
            for j, t in enumerate(agg_terms):
                vals[j].append(None if t.node is None else self.eval_expr(t.node, binding, cr))
        if not groups and all(t.kind == "agg" for t in cr.head):
            if all(t.agg in ("count", "sum", "set") for t in agg_terms):
                groups[()] = ((), [[] for _ in agg_terms])
        for gkey in sorted(groups, key=lambda k: k):
            gvals, vals = groups[gkey]
            results = []
            for t, vs in zip(agg_terms, vals, strict=True):
                results.append(self.aggregate(t.agg, vs, cr))
            it = iter(results)
            gi = iter(gvals)
            yield tuple(next(it) if t.kind == "agg" else next(gi) for t in cr.head)

    def aggregate(self, func: str, vs: list[Any], cr: CRule) -> Any:
        if func == "count":
            return len(vs)
        if func == "set":
            chosen: dict[tuple, Any] = {}
            for v in sorted(vs, key=order_key):
                chosen.setdefault(eq_key(v), v)
            return tuple(chosen.values())
        if func == "sum":
            total: int | float = 0
            for v in sorted(vs, key=order_key):
                if not is_number(v):
                    raise EvalError(
                        "MRL-TYPE", f"sum() 只接受数，得到 {type_name(v)}", cr.rule.span, cr.source
                    )
                if isinstance(total, int) and isinstance(v, int):
                    total = check_int(total + v)
                else:
                    total = norm_float(to_float(total) + to_float(v))
            return total
        best = None
        want = -1 if func == "min" else 1
        for v in vs:
            if not (is_number(v) or isinstance(v, str)):
                raise EvalError(
                    "MRL-TYPE", f"{func}() 只接受数或 string，得到 {kind(v)}", cr.rule.span, cr.source
                )
            try:
                better = best is None or compare(v, best) == want
            except EvalError as e:
                e.span = cr.rule.span
                e.source = cr.source
                raise
            if better:
                best = v
        return best


def load_program(paths: Sequence[str], functions: dict[str, Function] | None = None) -> Program:
    sources = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            sources.append((path, f.read()))
    return Program.from_sources(sources, functions)


__all__ = ["MRLError", "Model", "Program", "load_program", "normalize"]
