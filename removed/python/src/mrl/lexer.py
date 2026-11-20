"""MRL 词法分析（表达式、规则、模板共用）。词法规则见 spec/mrl/mrl-1.0.md 第 3 章。"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Diagnostic, Span, StaticError
from .values import INT_MAX

MAX_SOURCE = 1 << 20  # 单个源（文件或表达式）的最大字符数

WHITESPACE = " \t\r\n"
KEYWORDS = frozenset({"true", "false", "null", "in"})
RESERVED = frozenset(
    {
        "as",
        "break",
        "const",
        "continue",
        "else",
        "for",
        "function",
        "if",
        "import",
        "let",
        "loop",
        "package",
        "namespace",
        "return",
        "var",
        "void",
        "while",
    }
)

# 最长匹配优先
OPERATORS = (
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "<",
    ">",
    "!",
    "+",
    "-",
    "*",
    "/",
    "%",
    "?",
    ":",
    ".",
    ",",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    "=",
    ";",
    "@",
)


@dataclass(frozen=True)
class Token:
    kind: str  # int | float | string | ident | op | end | eof
    text: str
    value: object
    span: Span


def tokenize(src: str, source: str = "", offset: int = 0, line: int = 1, col: int = 1) -> list[Token]:
    """把源码切分为记号。offset/line/col 用于模板插值等子串，保证位置相对整个源。"""
    if len(src) > MAX_SOURCE:
        raise StaticError([Diagnostic("MRL-LIMIT", "源码过长", Span(0, 0, 1, 1), source)])
    tokens: list[Token] = []
    i = 0
    n = len(src)

    def _err(msg: str, _src: str, pos: int, eline: int, ecol: int, _source: str) -> StaticError:
        return StaticError(
            [Diagnostic("MRL-SYNTAX", msg, Span(offset + pos, offset + pos + 1, eline, ecol), source)]
        )

    def advance(k: int) -> None:
        nonlocal i, line, col
        for _ in range(k):
            if src[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        c = src[i]
        if c in WHITESPACE:
            advance(1)
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                advance(1)
            continue
        start, sline, scol = i, line, col
        if c.isascii() and (c.isalpha() or c == "_"):
            j = i
            while j < n and src[j].isascii() and (src[j].isalnum() or src[j] == "_"):
                j += 1
            text = src[i:j]
            advance(j - i)
            tokens.append(Token("ident", text, text, Span(offset + start, offset + i, sline, scol)))
            continue
        if c.isascii() and c.isdigit():
            j = i
            while j < n and src[j].isascii() and src[j].isdigit():
                j += 1
            is_float = False
            if j + 1 < n and src[j] == "." and src[j + 1].isascii() and src[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and src[j].isascii() and src[j].isdigit():
                    j += 1
            if j < n and src[j] in "eE":
                k = j + 1
                if k < n and src[k] in "+-":
                    k += 1
                if k < n and src[k].isascii() and src[k].isdigit():
                    is_float = True
                    j = k
                    while j < n and src[j].isascii() and src[j].isdigit():
                        j += 1
                else:
                    raise _err("数字的指数部分不完整", src, j, line, col + (j - i), source)
            text = src[i:j]
            intpart = text.split(".")[0].split("e")[0].split("E")[0]
            if len(intpart) > 1 and intpart[0] == "0":
                raise _err(f"数字 {text!r} 有前导零", src, i, line, col, source)
            if j < n and src[j].isascii() and (src[j].isalpha() or src[j] == "_"):
                raise _err(f"数字后紧跟标识符字符：{src[i : j + 1]!r}", src, j, line, col + (j - i), source)
            advance(j - i)
            if is_float:
                value: object = float(text)
                if value in (float("inf"), float("-inf")):
                    raise StaticError(
                        [
                            Diagnostic(
                                "MRL-SYNTAX",
                                f"浮点字面量 {text} 超出范围",
                                Span(offset + start, offset + i, sline, scol),
                                source,
                            )
                        ]
                    )
                tokens.append(Token("float", text, value, Span(offset + start, offset + i, sline, scol)))
            else:
                ivalue = int(text)
                if ivalue > INT_MAX + 1:
                    raise StaticError(
                        [
                            Diagnostic(
                                "MRL-OVERFLOW",
                                f"整数字面量 {text} 超出范围",
                                Span(offset + start, offset + i, sline, scol),
                                source,
                            )
                        ]
                    )
                tokens.append(Token("int", text, ivalue, Span(offset + start, offset + i, sline, scol)))
            continue
        if c in "\"'":
            quote = c
            j = i + 1
            out: list[str] = []
            while True:
                if j >= n or src[j] == "\n":
                    raise _err("字符串没有结束", src, i, sline, scol, source)
                ch = src[j]
                if ch == quote:
                    j += 1
                    break
                if ch == "\\":
                    if j + 1 >= n:
                        raise _err("字符串没有结束", src, i, sline, scol, source)
                    e = src[j + 1]
                    simple = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "r": "\r", "t": "\t"}
                    if e in simple:
                        out.append(simple[e])
                        j += 2
                        continue
                    if e in "uU":
                        width = 4 if e == "u" else 8
                        hexs = src[j + 2 : j + 2 + width]
                        if len(hexs) != width or any(h not in "0123456789abcdefABCDEF" for h in hexs):
                            raise _err(f"\\{e} 转义需要 {width} 位十六进制数", src, j, sline, scol, source)
                        cp = int(hexs, 16)
                        if cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                            raise _err(f"\\{e}{hexs} 不是合法的 Unicode 标量值", src, j, sline, scol, source)
                        out.append(chr(cp))
                        j += 2 + width
                        continue
                    raise _err(f"未知的转义序列 \\{e}", src, j, sline, scol, source)
                out.append(ch)
                j += 1
            text = src[i:j]
            advance(j - i)
            tokens.append(Token("string", text, "".join(out), Span(offset + start, offset + i, sline, scol)))
            continue
        if c == "." and (i + 1 >= n or src[i + 1] in WHITESPACE or src.startswith("//", i + 1)):
            # “.” 后面紧跟空白、注释或源结束时，是语句结束符（规则文件使用）
            advance(1)
            tokens.append(Token("end", ".", ".", Span(offset + start, offset + i, sline, scol)))
            continue
        for op in OPERATORS:
            if src.startswith(op, i):
                advance(len(op))
                tokens.append(Token("op", op, op, Span(offset + start, offset + i, sline, scol)))
                break
        else:
            raise _err(f"非法字符 {c!r}", src, i, line, col, source)
    tokens.append(Token("eof", "", None, Span(offset + i, offset + i, line, col)))
    return tokens
