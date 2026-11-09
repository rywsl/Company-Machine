"""Registry 检查（V1 + V2 的确定性部分）：读取 → schema → 抽取 → 内核 + 宪法求值 → 表达式检查 → 诊断。

流程（spec/mrl/factory-profile.md 第 6 章）：
1. 按目录布局读取 Spec（E-YAML、E-LAYOUT）；
2. 每个文档按其 kind 的 JSON Schema 校验（E-SCHEMA）；未通过的文档不参与后续步骤；
3. 抽取事实（kernel/schema.mrl 声明的 @input 关系）；
4. 第一遍求值：内核 + 宪法；
5. 用第一遍的结果构造类型环境，检查嵌入的表达式（E-EXPR、W-EXPR、W-PROPERTY-META）；
6. 第二遍求值：把第 5 步的诊断作为 host_diag 加入，得到最终的 diag；
7. 校验诊断码都已登记，映射到文件位置，排序输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import EvalError, StaticError
from ..rules import Model, Program
from ..stdlib import make_table
from ..values import to_py
from .extract import Extraction, extract
from .functions import PROFILE_FUNCTIONS
from .paths import kernel_sources
from .registry import HostDiag, Registry, SpecDoc, load_registry
from .schema import KIND_SCHEMA, REGISTRY_KINDS, validate
from .sites import check_sites

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
SEVERITY_MARK = {"error": "✖", "warning": "⚠", "info": "ℹ"}


class InternalError(Exception):
    """内核本身无法编译或求值：这是实现或内核规则的缺陷，而不是 Registry 的问题。"""


@dataclass
class Diag:
    code: str
    severity: str
    subject: str
    message: str
    file: str = ""
    line: int = 0
    col: int = 0
    path: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "path": self.path,
        }

    def format(self) -> str:
        where = f"{self.file}:{self.line}" if self.file else "(registry)"
        return f"{SEVERITY_MARK.get(self.severity, '?')} {self.code:<16} {where}  {self.subject}\n    {self.message}"


@dataclass
class CheckResult:
    root: str
    diags: list[Diag] = field(default_factory=list)
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, list[tuple]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    model: Model | None = None
    extraction: Extraction | None = None

    def count(self, severity: str) -> int:
        return sum(1 for d in self.diags if d.severity == severity)

    @property
    def ok(self) -> bool:
        return self.count("error") == 0

    def exit_code(self, strict: bool = False) -> int:
        if self.count("error"):
            return 1
        if strict and self.count("warning"):
            return 1
        return 0

    def summary(self) -> str:
        specs = sum(v for k, v in self.stats.items() if k.startswith("kind:"))
        return (
            f"{self.count('error')} 个错误，{self.count('warning')} 个警告，{self.count('info')} 条信息"
            f" · {specs} 个 Spec · {self.stats.get('edges', 0)} 条边 · {self.stats.get('conversions', 0)} 处转换"
        )

    def format_text(self, show_info: bool = True) -> str:
        lines = [d.format() for d in self.diags if show_info or d.severity != "info"]
        verdict = "✔ 通过" if self.ok else "✖ 未通过"
        lines.append(f"{verdict}：{self.summary()}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "ok": self.ok,
            "counts": {s: self.count(s) for s in ("error", "warning", "info")},
            "stats": self.stats,
            "diagnostics": [d.to_json() for d in self.diags],
            "resolutions": self.resolutions,
        }


def _validate_docs(reg: Registry) -> tuple[list[SpecDoc], list[HostDiag]]:
    host: list[HostDiag] = []
    valid: list[SpecDoc] = []
    for doc in reg.docs:
        if not isinstance(doc.data, dict):
            host.append(HostDiag("E-SCHEMA", doc.subject, "文档必须是一个映射", doc.file, doc.index, ""))
            continue
        kind = doc.kind
        if kind in KIND_SCHEMA and kind not in REGISTRY_KINDS:
            host.append(
                HostDiag(
                    "E-LAYOUT",
                    doc.subject,
                    f"{kind} 是记录类 kind，不属于 Registry 的 Spec 目录",
                    doc.file,
                    doc.index,
                    "kind",
                )
            )
            continue
        if kind in REGISTRY_KINDS and kind != doc.expected_kind:
            host.append(
                HostDiag(
                    "E-LAYOUT",
                    doc.subject,
                    f"该位置只能放 {doc.expected_kind}，但文档的 kind 是 {kind}",
                    doc.file,
                    doc.index,
                    "kind",
                )
            )
            continue
        errors = validate(doc.data)
        if errors:
            for path, msg in errors:
                host.append(
                    HostDiag("E-SCHEMA", doc.subject, f"{path or '(根)'}：{msg}", doc.file, doc.index, path)
                )
            continue
        valid.append(doc)
    return valid, host


def _functions() -> dict:
    return make_table(PROFILE_FUNCTIONS)


def kernel_program() -> Program:
    try:
        return Program.from_sources(kernel_sources(), _functions())
    except StaticError as e:
        raise InternalError(
            "内核规则无法通过静态检查：\n" + "\n".join(d.format() for d in e.diagnostics)
        ) from e


def build_program(constitution: list[tuple[str, str]]) -> tuple[Program, list[HostDiag]]:
    """内核 + 宪法。宪法缺失或无法编译时报 E-CONSTITUTION，并退回到只有内核的程序。"""
    if not constitution:
        return kernel_program(), [
            HostDiag("E-CONSTITUTION", "Registry", "缺少宪法：constitution/ 目录下没有 .mrl 文件")
        ]
    try:
        return Program.from_sources(kernel_sources() + constitution, _functions()), []
    except StaticError as e:
        kernel_errors = [d for d in e.diagnostics if not d.source.startswith("constitution/")]
        program = kernel_program()  # 内核自身有问题时这里会抛出 InternalError
        diags = e.diagnostics if not kernel_errors else kernel_errors
        return program, [
            HostDiag(
                "E-CONSTITUTION",
                f"File:{d.source}",
                f"宪法无法通过 MRL 静态检查：{d.format()}",
                d.source,
                -1,
                "",
            )
            for d in diags
        ]


def _host_rows(host: list[HostDiag]) -> list[tuple[str, str, str]]:
    return [(h.code, h.subject, h.message) for h in host]


def check_loaded(
    reg: Registry, constitution: list[tuple[str, str]] | None = None, extra_texprs: list[str] | None = None
) -> CheckResult:
    """extra_texprs：额外登记的类型表达式（例如 mrl resolve 查询的类型），使它们参与子类型计算。"""
    valid, host = _validate_docs(reg)
    host = list(reg.diags) + host
    x = extract(valid, extra_texprs)
    host += x.diags
    bad = {(h.file, h.doc) for h in x.diags if h.code == "E-SCHEMA" and h.file}
    if bad:  # 类型表达式无法解析的文档与其他 schema 错误一样，整体不参与后续检查
        x = extract([d for d in valid if (d.file, d.index) not in bad], extra_texprs)
    program, cdiags = build_program(reg.constitution if constitution is None else constitution)
    host += cdiags

    facts: dict[str, list[tuple]] = {k: list(v) for k, v in x.facts.items()}
    facts["loc"] = [(s, f, d, p) for s, (f, d, p) in sorted(x.locs.items())]

    def run(extra: list[HostDiag]) -> tuple[Program, Model, list[HostDiag]]:
        nonlocal program
        facts["host_diag"] = _host_rows(host + extra)
        try:
            return program, program.evaluate(facts), []
        except EvalError as e:
            if not e.source.startswith("constitution/"):
                raise InternalError(f"内核求值失败：{e.code}：{e.message}（{e.source}:{e.span}）") from e
            note = HostDiag(
                "E-CONSTITUTION", f"File:{e.source}", f"宪法求值失败：{e.code}：{e.message}", e.source, -1, ""
            )
            program = kernel_program()
            facts["host_diag"] = _host_rows(host + extra + [note])
            return program, program.evaluate(facts), [note]

    program, model, notes = run([])
    host += notes
    site_diags = check_sites(x, model)
    if site_diags:
        host += site_diags
        program, model, notes = run([])
        host += notes

    return _collect(reg, x, model, host, facts)


def _collect(
    reg: Registry, x: Extraction, model: Model, host: list[HostDiag], facts: dict[str, list[tuple]]
) -> CheckResult:
    codes = {c: sev for c, sev, _title in model.rows("code")}
    docs = {(d.file, d.index): d for d in reg.docs}
    host_by_row: dict[tuple[str, str, str], HostDiag] = {}
    for h in host:
        host_by_row.setdefault((h.code, h.subject, h.message), h)

    def locate(subject: str) -> tuple[str, int, str]:
        """主体 → 位置：精确匹配；否则逐级去掉 "/" 之后的最后一段（"."、"/"、"[" 分隔），直到 Spec 键本身。"""
        s = subject
        while True:
            if s in x.locs:
                return x.locs[s]
            if "/" not in s:
                break
            s = s[: max(s.rfind("."), s.rfind("/"), s.rfind("["))]
        if subject.startswith("File:"):
            return subject[5:].split("#", 1)[0], -1, ""
        return "", -1, ""

    out: list[Diag] = []
    for code, subject, message in model.rows("diag"):
        sev = codes.get(code)
        if sev is None:
            sev, message, code = (
                "error",
                f"诊断码 {code} 没有在 kernel/codes.mrl 中登记（原消息：{message}）",
                "E-CONSTITUTION",
            )
        h = host_by_row.get((code, subject, message))
        file, doc_index, path = (h.file, h.doc, h.path) if h is not None and h.file else locate(subject)
        line = col = 0
        d = docs.get((file, doc_index))
        if h is not None and h.line:
            line, col = h.line, h.col
        elif d is not None:
            line, col = d.position(path)
        out.append(Diag(code, sev, subject, message, file, line, col, path))
    out.sort(key=lambda d: (SEVERITY_ORDER.get(d.severity, 9), d.file, d.line, d.code, d.subject, d.message))

    edges = {(r[0], r[1], r[2]): r for r in model.rows("edge")}
    resolutions = []
    for line_key, step, port, res in model.rows("resolution"):
        e = edges.get((line_key, step, port))
        item = {"line": line_key, "step": step, "port": port}
        if e is not None:
            item.update({"from": e[3], "guarantees": list(e[4]), "to": e[5], "requires": list(e[6])})
        item.update(to_py(res))
        resolutions.append(item)

    stats: dict[str, int] = {}
    for _key, kind, *_rest in facts.get("spec", []):
        stats[f"kind:{kind}"] = stats.get(f"kind:{kind}", 0) + 1
    stats["edges"] = len(resolutions)
    stats["conversions"] = sum(1 for r in resolutions if r.get("status") == "convert")
    return CheckResult(reg.root, out, resolutions, facts, stats, model, x)


def check_registry(
    root: str | Path, constitution: list[tuple[str, str]] | None = None, extra_texprs: list[str] | None = None
) -> CheckResult:
    rootp = Path(root)
    if not rootp.is_dir():
        raise FileNotFoundError(f"Registry 目录不存在：{root}")
    return check_loaded(load_registry(rootp), constitution, extra_texprs)
