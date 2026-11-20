"""MRL 语法分析：表达式（MRL-E）、模板（MRL-T）、规则模块（MRL-R）。

文法见 spec/mrl/mrl-1.0.md 第 4、7、8 章。
"""

from __future__ import annotations

from . import ast as A
from .errors import Diagnostic, Span, StaticError
from .lexer import KEYWORDS, RESERVED, Token, tokenize
from .values import INT_MAX

MAX_DEPTH = 64
MACROS = frozenset({"all", "exists", "exists_one", "map", "filter"})
AGGREGATES = frozenset({"count", "sum", "min", "max", "set"})
REL_OPS = frozenset({"<", "<=", ">", ">=", "==", "!=", "in"})


def _join(a: Span, b: Span) -> Span:
    return Span(a.offset, b.end, a.line, a.col)


class Parser:
    def __init__(self, tokens: list[Token], source: str = ""):
        self.toks = tokens
        self.pos = 0
        self.source = source
        self.depth = 0

    # ---------- 工具 ----------

    @property
    def tok(self) -> Token:
        return self.toks[self.pos]

    def peek(self, k: int = 1) -> Token:
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def error(self, msg: str, tok: Token | None = None, code: str = "MRL-SYNTAX") -> StaticError:
        t = tok or self.tok
        return StaticError([Diagnostic(code, msg, t.span, self.source)])

    def at_op(self, text: str) -> bool:
        return self.tok.kind == "op" and self.tok.text == text

    def at_ident(self, text: str | None = None) -> bool:
        return self.tok.kind == "ident" and (text is None or self.tok.text == text)

    def expect_op(self, text: str) -> Token:
        if not self.at_op(text):
            raise self.error(f"期望 {text!r}，得到 {self.describe(self.tok)}")
        t = self.tok
        self.pos += 1
        return t

    def expect_ident(self, what: str = "标识符") -> Token:
        if self.tok.kind != "ident":
            raise self.error(f"期望{what}，得到 {self.describe(self.tok)}")
        t = self.tok
        self.pos += 1
        return t

    @staticmethod
    def describe(t: Token) -> str:
        if t.kind == "eof":
            return "源结束"
        if t.kind == "end":
            return "语句结束符 '.'"
        return repr(t.text)

    def enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.error("表达式嵌套过深", code="MRL-LIMIT")

    def leave(self) -> None:
        self.depth -= 1

    # ---------- 表达式 ----------

    def expr(self) -> A.Node:
        self.enter()
        try:
            return self.conditional()
        finally:
            self.leave()

    def conditional(self) -> A.Node:
        c = self.or_()
        if self.at_op("?"):
            self.pos += 1
            a = self.or_()
            self.expect_op(":")
            b = self.expr()
            return A.Cond(c, a, b, span=_join(c.span, b.span))
        return c

    def or_(self) -> A.Node:
        left = self.and_()
        while self.at_op("||"):
            self.pos += 1
            right = self.and_()
            left = A.Or(left, right, span=_join(left.span, right.span))
        return left

    def and_(self) -> A.Node:
        left = self.relation()
        while self.at_op("&&"):
            self.pos += 1
            right = self.relation()
            left = A.And(left, right, span=_join(left.span, right.span))
        return left

    def at_relop(self) -> bool:
        t = self.tok
        return (t.kind == "op" and t.text in REL_OPS) or (t.kind == "ident" and t.text == "in")

    def relation(self) -> A.Node:
        left = self.addition()
        if self.at_relop():
            op = self.tok.text
            self.pos += 1
            right = self.addition()
            node = A.Binary(op, left, right, span=_join(left.span, right.span))
            if self.at_relop():
                raise self.error("比较运算符不能连用，请加括号")
            return node
        return left

    def addition(self) -> A.Node:
        left = self.mult()
        while self.tok.kind == "op" and self.tok.text in ("+", "-"):
            op = self.tok.text
            self.pos += 1
            right = self.mult()
            left = A.Binary(op, left, right, span=_join(left.span, right.span))
        return left

    def mult(self) -> A.Node:
        left = self.unary()
        while self.tok.kind == "op" and self.tok.text in ("*", "/", "%"):
            op = self.tok.text
            self.pos += 1
            right = self.unary()
            left = A.Binary(op, left, right, span=_join(left.span, right.span))
        return left

    def unary(self) -> A.Node:
        t = self.tok
        if self.at_op("!"):
            self.pos += 1
            self.enter()
            try:
                operand = self.unary()
            finally:
                self.leave()
            return A.Unary("!", operand, span=_join(t.span, operand.span))
        if self.at_op("-"):
            self.pos += 1
            nxt = self.tok
            if nxt.kind == "int" and not (self.peek().kind == "op" and self.peek().text in (".", "[")):
                self.pos += 1
                return A.Lit(-nxt.value, span=_join(t.span, nxt.span))  # 负整数字面量（允许 -2^63）
            self.enter()
            try:
                operand = self.unary()
            finally:
                self.leave()
            return A.Unary("-", operand, span=_join(t.span, operand.span))
        return self.member()

    def member(self) -> A.Node:
        node = self.primary()
        while True:
            if self.at_op("."):
                self.pos += 1
                if self.tok.kind != "ident":
                    raise self.error(f"'.' 后面期望字段名，得到 {self.describe(self.tok)}")
                name_tok = self.tok
                self.pos += 1
                if self.at_op("("):
                    args, end = self.call_args()
                    node = self.make_call(name_tok, node, args, _join(node.span, end))
                else:
                    node = A.Select(node, name_tok.text, span=_join(node.span, name_tok.span))
            elif self.at_op("["):
                self.pos += 1
                idx = self.expr()
                end = self.expect_op("]")
                node = A.Index(node, idx, span=_join(node.span, end.span))
            else:
                return node

    def call_args(self) -> tuple[list[A.Node], Span]:
        self.expect_op("(")
        args: list[A.Node] = []
        if not self.at_op(")"):
            args.append(self.expr())
            while self.at_op(","):
                self.pos += 1
                args.append(self.expr())
        end = self.expect_op(")")
        return args, end.span

    def make_call(self, name_tok: Token, receiver: A.Node | None, args: list[A.Node], span: Span) -> A.Node:
        name = name_tok.text
        if receiver is not None and name in MACROS:
            if name == "map":
                if len(args) not in (2, 3):
                    raise self.error(
                        "map 宏需要 2 或 3 个参数：map(x, 变换) 或 map(x, 条件, 变换)", name_tok, "MRL-MACRO"
                    )
            elif len(args) != 2:
                raise self.error(f"{name} 宏需要 2 个参数：{name}(变量, 表达式)", name_tok, "MRL-MACRO")
            var = args[0]
            if not isinstance(var, A.Ident):
                raise self.error(f"{name} 宏的第一个参数必须是变量名", name_tok, "MRL-MACRO")
            if name == "map":
                pred = args[1] if len(args) == 3 else None
                body = args[-1]
                return A.Macro("map", receiver, var.name, pred, body, span=span)
            return A.Macro(name, receiver, var.name, args[1], None, span=span)
        if receiver is None and name == "has":
            if len(args) != 1 or not isinstance(args[0], A.Select):
                raise self.error(
                    "has() 的参数必须是字段选择，例如 has(bean.meta.style)", name_tok, "MRL-MACRO"
                )
            sel = args[0]
            return A.Has(sel.operand, sel.field, span=span)
        if receiver is not None:
            return A.Call(name, (receiver, *args), receiver_style=True, span=span)
        return A.Call(name, tuple(args), span=span)

    def primary(self) -> A.Node:
        t = self.tok
        if t.kind in ("int", "float", "string"):
            if t.kind == "int" and t.value > INT_MAX:  # type: ignore[operator]
                raise self.error(f"整数字面量 {t.text} 超出范围", code="MRL-OVERFLOW")
            self.pos += 1
            return A.Lit(t.value, span=t.span)
        if t.kind == "ident":
            if t.text == "true" or t.text == "false":
                self.pos += 1
                return A.Lit(t.text == "true", span=t.span)
            if t.text == "null":
                self.pos += 1
                return A.Lit(None, span=t.span)
            if t.text in KEYWORDS or t.text in RESERVED:
                raise self.error(f"{t.text!r} 是保留字，不能用作标识符")
            self.pos += 1
            if self.at_op("("):
                args, end = self.call_args()
                return self.make_call(t, None, args, _join(t.span, end))
            return A.Ident(t.text, span=t.span)
        if self.at_op("("):
            self.pos += 1
            e = self.expr()
            self.expect_op(")")
            return e
        if self.at_op("["):
            start = self.tok
            self.pos += 1
            items: list[A.Node] = []
            while not self.at_op("]"):
                items.append(self.expr())
                if self.at_op(","):
                    self.pos += 1
                elif not self.at_op("]"):
                    raise self.error(f"列表中期望 ',' 或 ']'，得到 {self.describe(self.tok)}")
            end = self.expect_op("]")
            return A.ListLit(tuple(items), span=_join(start.span, end.span))
        if self.at_op("{"):
            start = self.tok
            self.pos += 1
            entries: list[tuple[A.Node, A.Node]] = []
            while not self.at_op("}"):
                k = self.expr()
                self.expect_op(":")
                v = self.expr()
                entries.append((k, v))
                if self.at_op(","):
                    self.pos += 1
                elif not self.at_op("}"):
                    raise self.error(f"map 中期望 ',' 或 '}}'，得到 {self.describe(self.tok)}")
            end = self.expect_op("}")
            return A.MapLit(tuple(entries), span=_join(start.span, end.span))
        raise self.error(f"期望表达式，得到 {self.describe(t)}")

    # ---------- 规则模块 ----------

    def at_arrow(self) -> bool:
        """':-' 由紧邻的 ':' 与 '-' 两个记号组成。"""
        return (
            self.at_op(":")
            and self.peek().kind == "op"
            and self.peek().text == "-"
            and self.peek().span.offset == self.tok.span.end
        )

    def expect_end(self) -> Token:
        if self.tok.kind != "end":
            raise self.error(f"期望语句结束符 '.'（其后需要空白或换行），得到 {self.describe(self.tok)}")
        t = self.tok
        self.pos += 1
        return t

    def module(self) -> A.Module:
        start = self.tok
        if not self.at_ident("module"):
            raise self.error("规则文件必须以 'module <名字>.' 开头")
        self.pos += 1
        parts = [self.expect_ident("模块名").text]
        while self.at_op("."):
            self.pos += 1
            parts.append(self.expect_ident("模块名").text)
        self.expect_end()
        name = ".".join(parts)
        decls: list[A.RelDecl] = []
        rules: list[A.Rule] = []
        while self.tok.kind != "eof":
            annotations: list[tuple[str, Token, str]] = []
            while self.at_op("@"):
                at = self.tok
                self.pos += 1
                ann = self.expect_ident("注解名")
                arg = ""
                if ann.text == "doc":
                    self.expect_op("(")
                    if self.tok.kind != "string":
                        raise self.error("@doc 需要一个字符串参数")
                    arg = self.tok.value  # type: ignore[assignment]
                    self.pos += 1
                    self.expect_op(")")
                elif ann.text not in ("input", "open"):
                    raise self.error(f"未知注解 @{ann.text}", ann, "MRL-DECL")
                annotations.append((ann.text, at, arg))
            if self.at_ident("rel"):
                decls.append(self.rel_decl(annotations))
            else:
                if annotations:
                    raise self.error("注解只能用在 rel 声明前", annotations[0][1], "MRL-DECL")
                rules.append(self.clause(name))
        return A.Module(name, tuple(decls), tuple(rules), self.source, span=start.span)

    def type_ref(self) -> A.TypeRef:
        t = self.expect_ident("类型名")
        if t.text in ("list", "map"):
            self.expect_op("<")
            arg = self.type_ref()
            end = self.expect_op(">")
            return A.TypeRef(t.text, arg, span=_join(t.span, end.span))
        return A.TypeRef(t.text, span=t.span)

    def rel_decl(self, annotations: list[tuple[str, Token, str]]) -> A.RelDecl:
        start = self.tok
        self.pos += 1  # rel
        name = self.expect_ident("关系名")
        if name.text in KEYWORDS or name.text in RESERVED:
            raise self.error(f"{name.text!r} 是保留字，不能用作关系名", name)
        self.expect_op("(")
        cols: list[A.Column] = []
        if not self.at_op(")"):
            while True:
                cname = self.expect_ident("列名")
                self.expect_op(":")
                ctype = self.type_ref()
                cols.append(A.Column(cname.text, ctype, span=cname.span))
                if self.at_op(","):
                    self.pos += 1
                    continue
                break
        self.expect_op(")")
        end = self.expect_end()
        kinds = {a[0] for a in annotations}
        doc = next((a[2] for a in annotations if a[0] == "doc"), "")
        return A.RelDecl(
            name.text,
            tuple(cols),
            input="input" in kinds,
            open="open" in kinds,
            doc=doc,
            span=_join(start.span, end.span),
        )

    def clause(self, module: str) -> A.Rule:
        head = self.head()
        body: list[A.Node] = []
        if self.at_arrow():
            self.pos += 2
            body.append(self.body_literal())
            while self.at_op(","):
                self.pos += 1
                body.append(self.body_literal())
        end = self.expect_end()
        return A.Rule(head, tuple(body), module, span=_join(head.span, end.span))

    def head(self) -> A.Head:
        name = self.expect_ident("关系名")
        if name.text in KEYWORDS or name.text in RESERVED:
            raise self.error(f"{name.text!r} 是保留字", name)
        self.expect_op("(")
        terms: list[A.Node] = []
        if not self.at_op(")"):
            while True:
                term = self.expr()
                if isinstance(term, A.Call) and not term.receiver_style and term.func in AGGREGATES:
                    if term.func == "count":
                        if term.args:
                            raise self.error("count() 不接受参数", code="MRL-ARITY")
                        term = A.Agg("count", None, span=term.span)
                    else:
                        if len(term.args) != 1:
                            raise self.error(f"{term.func}() 聚合需要 1 个参数", code="MRL-ARITY")
                        term = A.Agg(term.func, term.args[0], span=term.span)
                terms.append(term)
                if self.at_op(","):
                    self.pos += 1
                    continue
                break
        end = self.expect_op(")")
        return A.Head(name.text, tuple(terms), span=_join(name.span, end.span))

    def body_literal(self) -> A.Node:
        t = self.tok
        if (
            t.kind == "ident"
            and A.is_var_name(t.text)
            and self.peek().kind == "op"
            and self.peek().text == "="
        ):
            self.pos += 2
            e = self.expr()
            return A.Assign(t.text, e, span=_join(t.span, e.span))
        e = self.expr()
        return A.Guard(e, span=e.span)  # 是原子还是守卫，在装配程序时按名字区分


def parse_expr(src: str, source: str = "", offset: int = 0, line: int = 1, col: int = 1) -> A.Node:
    p = Parser(tokenize(src, source, offset, line, col), source)
    e = p.expr()
    if p.tok.kind != "eof":
        raise p.error(f"表达式后有多余内容：{p.describe(p.tok)}")
    return e


def parse_module(src: str, source: str = "") -> A.Module:
    p = Parser(tokenize(src, source), source)
    return p.module()


def _line_col(src: str, offset: int) -> tuple[int, int]:
    line = src.count("\n", 0, offset) + 1
    last = src.rfind("\n", 0, offset)
    col = offset + 1 if last < 0 else offset - last
    return line, col


def parse_template(src: str, source: str = "") -> A.Template:
    """模板：普通文本 + {{ 表达式 }}。插值在第一个不在括号或字符串内的 '}}' 处结束。"""
    parts: list[str | A.Node] = []
    i = 0
    n = len(src)
    text: list[str] = []
    while i < n:
        if src.startswith("{{", i):
            if text:
                parts.append("".join(text))
                text = []
            j = i + 2
            depth = 0
            end = -1
            while j < n:
                c = src[j]
                if c in "\"'":
                    k = j + 1
                    while k < n and src[k] != c:
                        k += 2 if src[k] == "\\" else 1
                    j = k + 1
                    continue
                if c in "{[(":
                    depth += 1
                elif c in ")]":
                    depth -= 1
                elif c == "}":
                    if depth == 0 and j + 1 < n and src[j + 1] == "}":
                        end = j
                        break
                    depth -= 1
                j += 1
            if end < 0:
                line, col = _line_col(src, i)
                raise StaticError(
                    [Diagnostic("MRL-SYNTAX", "模板插值缺少结束的 '}}'", Span(i, i + 2, line, col), source)]
                )
            inner = src[i + 2 : end]
            line, col = _line_col(src, i)
            if not inner.strip():
                raise StaticError(
                    [Diagnostic("MRL-SYNTAX", "模板插值为空", Span(i, end + 2, line, col), source)]
                )
            iline, icol = _line_col(src, i + 2)
            parts.append(parse_expr(inner, source, offset=i + 2, line=iline, col=icol))
            i = end + 2
        else:
            text.append(src[i])
            i += 1
    if text:
        parts.append("".join(text))
    return A.Template(tuple(parts), span=Span(0, n, 1, 1))


def is_template(s: str) -> bool:
    return "{{" in s
