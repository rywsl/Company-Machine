"""MRL 语言级错误码与诊断。

语言级错误码统一以 ``MRL-`` 开头，含义见 spec/mrl/mrl-1.0.md 第 9 章。
工厂级诊断码（E-* / W-* / I-*）由内核规则模块定义，不在这里。
"""

from __future__ import annotations

from dataclasses import dataclass

# 代码 → (阶段, 说明)。阶段：static = 编译期，runtime = 求值期，both = 两者皆可能。
CODES: dict[str, tuple[str, str]] = {
    "MRL-SYNTAX": ("static", "词法或语法错误"),
    "MRL-LIMIT": ("static", "超出实现限制：源码长度、嵌套深度"),
    "MRL-UNDEFINED": ("both", "未定义的名字、函数或关系"),
    "MRL-ARITY": ("static", "参数个数或关系元数不符"),
    "MRL-MACRO": ("static", "宏的用法不正确"),
    "MRL-SHADOW": ("static", "宏变量或规则变量遮蔽了已有名字"),
    "MRL-DECL": ("static", "关系声明不合法"),
    "MRL-DUP": ("static", "重复定义：关系、模块，或与函数同名"),
    "MRL-OWNERSHIP": (
        "static",
        "违反关系归属：为别的模块的关系或 @input 关系写规则，或在规则体中读取 @open 关系",
    ),
    "MRL-UNSAFE": ("static", "规则不安全：变量在使用前没有被正向原子或赋值绑定"),
    "MRL-STRATIFY": ("static", "无法分层：否定或聚合出现在递归环上"),
    "MRL-INVENTION": ("static", "递归规则中构造了新值，可能不终止"),
    "MRL-MAYBE-ABSENT": ("static", "访问了可选字段，但没有用 has() 保护（警告）"),
    "MRL-UNUSED": ("static", "关系永远为空，或既没有定义也没有使用（警告）"),
    "MRL-TYPE": ("both", "类型错误"),
    "MRL-NO-FIELD": ("both", "字段不存在"),
    "MRL-OVERFLOW": ("both", "整数超出 64 位有符号范围"),
    "MRL-INDEX": ("runtime", "列表下标越界"),
    "MRL-DIV-ZERO": ("runtime", "除以零"),
    "MRL-DOMAIN": ("runtime", "结果不是有限浮点数，或参数超出函数定义域"),
    "MRL-CONVERT": ("runtime", "字符串无法转换为数值"),
    "MRL-COST": ("runtime", "求值步数超出预算"),
    "MRL-DUP-KEY": ("both", "map 字面量中出现重复的键"),
    "MRL-FACT": ("runtime", "输入事实不符合关系声明"),
}

WARNING_CODES = frozenset({"MRL-MAYBE-ABSENT", "MRL-UNUSED"})


@dataclass(frozen=True)
class Span:
    """源码位置。offset 从 0 开始；line、col 从 1 开始；end 为结束偏移（不含）。"""

    offset: int
    end: int
    line: int
    col: int

    def __str__(self) -> str:
        return f"{self.line}:{self.col}"


NO_SPAN = Span(0, 0, 1, 1)


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    span: Span = NO_SPAN
    source: str = ""

    @property
    def severity(self) -> str:
        return "warning" if self.code in WARNING_CODES else "error"

    def format(self) -> str:
        where = f"{self.source}:{self.span}" if self.source else str(self.span)
        return f"{where}: {self.severity} {self.code}: {self.message}"

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "line": self.span.line,
            "col": self.span.col,
        }


class MRLError(Exception):
    """所有 MRL 错误的基类。"""

    def __init__(self, code: str, message: str, span: Span | None = None, source: str = ""):
        if code not in CODES:
            raise ValueError(f"unknown MRL error code {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.span = span or NO_SPAN
        self.source = source

    def diagnostic(self) -> Diagnostic:
        return Diagnostic(self.code, self.message, self.span, self.source)


class StaticError(MRLError):
    """编译期错误。可以携带多条诊断（第一条是主错误）。"""

    def __init__(self, diagnostics: list[Diagnostic]):
        errors = [d for d in diagnostics if d.severity == "error"]
        first = errors[0] if errors else diagnostics[0]
        super().__init__(first.code, first.message, first.span, first.source)
        self.diagnostics = diagnostics


class EvalError(MRLError):
    """求值期错误。宿主必须把它当作“检查不通过”（失败即关闭）。"""
