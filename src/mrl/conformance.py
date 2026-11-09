"""一致性测试运行器（spec/mrl/conformance/ 的格式见 spec/mrl/mrl-1.0.md 第 11 章）。

每个 YAML 文件是一个测试套件：
    suite: <套件 id>
    kind: expr | template | rules | factory
    cases: [ {id, ..., expect: {...}} ]
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from . import types as T
from .errors import EvalError, MRLError, StaticError
from .expr import compile_expr, compile_template
from .rules import Program
from .values import display, from_py, ident_key
from .yamlio import YamlError, load_file

CASE_KINDS = ("expr", "template", "rules", "factory")


@dataclass
class CaseResult:
    suite: str
    case: str
    ok: bool
    detail: str = ""
    file: str = ""

    @property
    def name(self) -> str:
        return f"{self.suite}/{self.case}"


@dataclass
class Case:
    suite: str
    kind: str
    data: dict
    file: str

    @property
    def id(self) -> str:
        return str(self.data.get("id", "?"))


def same_value(a: Any, b: Any) -> bool:
    return ident_key(a) == ident_key(b)


def discover(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in names:
                    if n.endswith((".yaml", ".yml")):
                        files.append(os.path.join(root, n))
        else:
            files.append(p)
    return sorted(files)


def load_cases(paths: list[str]) -> Iterator[Case]:
    seen: set[str] = set()
    for f in discover(paths):
        try:
            doc = load_file(f)
        except YamlError as e:
            yield Case(
                os.path.basename(f),
                "invalid",
                {"id": "<load>", "error": f"第 {e.line} 行第 {e.col} 列：{e}"},
                f,
            )
            continue
        except OSError as e:
            yield Case(os.path.basename(f), "invalid", {"id": "<load>", "error": str(e)}, f)
            continue
        if not isinstance(doc, dict) or "suite" not in doc or "cases" not in doc:
            yield Case(os.path.basename(f), "invalid", {"id": "<load>", "error": "缺少 suite 或 cases"}, f)
            continue
        suite = str(doc["suite"])
        kind = str(doc.get("kind", ""))
        if kind not in CASE_KINDS:
            yield Case(suite, "invalid", {"id": "<load>", "error": f"未知的 kind {kind!r}"}, f)
            continue
        if suite in seen:
            yield Case(suite, "invalid", {"id": "<load>", "error": f"套件 id {suite} 重复"}, f)
            continue
        seen.add(suite)
        ids: set[str] = set()
        for c in doc["cases"]:
            cid = str(c.get("id", "?"))
            if cid in ids:
                yield Case(suite, "invalid", {"id": cid, "error": "用例 id 重复"}, f)
                continue
            ids.add(cid)
            if kind == "factory" and "base" in doc:
                c = {"base": doc["base"], **c}
            yield Case(suite, kind, c, f)


def _types(spec: dict | None, values: dict | None) -> dict[str, T.Type]:
    env: dict[str, T.Type] = {k: T.DYN for k in (values or {})}
    for k, v in (spec or {}).items():
        env[k] = T.parse_type(str(v))
    return env


def _check_warnings(expect: dict, warnings: list) -> str:
    if "warnings" not in expect:
        return ""
    got = sorted(w.code for w in warnings)
    want = sorted(expect["warnings"])
    return "" if got == want else f"警告不符：期望 {want}，得到 {got}"


def run_expr_case(c: Case) -> CaseResult:
    d = c.data
    expect = d.get("expect", {})
    values = d.get("env") or {}
    try:
        env = _types(d.get("types"), values)
        if c.kind == "template":
            compiled: Any = compile_template(str(d["template"]), env)
        else:
            compiled = compile_expr(str(d["expr"]), env)
    except StaticError as e:
        if "static_error" in expect:
            codes = [x.code for x in e.diagnostics if x.severity == "error"]
            if expect["static_error"] in codes:
                return CaseResult(c.suite, c.id, True, file=c.file)
            return CaseResult(
                c.suite, c.id, False, f"期望静态错误 {expect['static_error']}，得到 {codes}", c.file
            )
        return CaseResult(c.suite, c.id, False, f"意外的静态错误：{e}", c.file)
    if "static_error" in expect:
        return CaseResult(c.suite, c.id, False, f"期望静态错误 {expect['static_error']}，但编译通过", c.file)
    msg = _check_warnings(expect, compiled.warnings)
    if msg:
        return CaseResult(c.suite, c.id, False, msg, c.file)
    if "type" in expect and c.kind == "expr":
        want_t = T.parse_type(str(expect["type"]))
        if compiled.type != want_t:
            return CaseResult(
                c.suite, c.id, False, f"静态类型不符：期望 {want_t}，得到 {compiled.type}", c.file
            )
    if "value" not in expect and "error" not in expect:
        return CaseResult(c.suite, c.id, True, file=c.file)
    try:
        budget = int(d.get("budget", 100_000))
        if c.kind == "template":
            got = compiled.render(values, budget=budget)
        else:
            got = compiled.evaluate(values, budget=budget)
    except EvalError as e:
        if "error" in expect:
            if e.code == expect["error"]:
                return CaseResult(c.suite, c.id, True, file=c.file)
            return CaseResult(
                c.suite, c.id, False, f"期望运行时错误 {expect['error']}，得到 {e.code}：{e.message}", c.file
            )
        return CaseResult(c.suite, c.id, False, f"意外的运行时错误 {e.code}：{e.message}", c.file)
    if "error" in expect:
        return CaseResult(
            c.suite, c.id, False, f"期望运行时错误 {expect['error']}，得到值 {display(got)}", c.file
        )
    want = from_py(expect["value"])
    if same_value(got, want):
        return CaseResult(c.suite, c.id, True, file=c.file)
    return CaseResult(c.suite, c.id, False, f"值不符：期望 {display(want)}，得到 {display(got)}", c.file)


def _rows(model: Any, rel: str) -> list[tuple]:
    return model.rows(rel)


def _norm_rows(model: Any, rel: str, rows: list) -> list[tuple]:
    from .rules import normalize

    info = model.program.relations[rel]
    out = []
    for r in rows:
        r = r if isinstance(r, list) else [r]
        out.append(tuple(normalize(from_py(v), t) for v, (_, t) in zip(r, info.columns, strict=True)))
    return out


def run_rules_case(c: Case) -> CaseResult:
    d = c.data
    expect = d.get("expect", {})
    if "modules" in d:
        sources = [(m["name"], m["text"]) for m in d["modules"]]
    else:
        sources = [(f"{c.id}.mrl", d["program"])]
    try:
        program = Program.from_sources(sources)
    except StaticError as e:
        if "static_error" in expect:
            codes = [x.code for x in e.diagnostics if x.severity == "error"]
            if expect["static_error"] in codes:
                return CaseResult(c.suite, c.id, True, file=c.file)
            return CaseResult(
                c.suite, c.id, False, f"期望静态错误 {expect['static_error']}，得到 {codes}", c.file
            )
        return CaseResult(c.suite, c.id, False, f"意外的静态错误：{e}", c.file)
    if "static_error" in expect:
        return CaseResult(c.suite, c.id, False, f"期望静态错误 {expect['static_error']}，但编译通过", c.file)
    msg = _check_warnings(expect, program.warnings)
    if msg:
        return CaseResult(c.suite, c.id, False, msg, c.file)
    try:
        model = program.evaluate(d.get("facts") or {})
    except MRLError as e:
        if "error" in expect:
            if e.code == expect["error"]:
                return CaseResult(c.suite, c.id, True, file=c.file)
            return CaseResult(
                c.suite, c.id, False, f"期望运行时错误 {expect['error']}，得到 {e.code}：{e.message}", c.file
            )
        return CaseResult(c.suite, c.id, False, f"意外的运行时错误 {e.code}：{e.message}", c.file)
    if "error" in expect:
        return CaseResult(c.suite, c.id, False, f"期望运行时错误 {expect['error']}，但求值成功", c.file)
    try:
        for rel, rows in (expect.get("relations") or {}).items():
            want = sorted(ident_key_tuple(r) for r in _norm_rows(model, rel, rows))
            got = sorted(ident_key_tuple(r) for r in _rows(model, rel))
            if want != got:
                return CaseResult(
                    c.suite,
                    c.id,
                    False,
                    f"关系 {rel} 不符：期望 {fmt_rows(_norm_rows(model, rel, rows))}，得到 {fmt_rows(_rows(model, rel))}",
                    c.file,
                )
        for rel, rows in (expect.get("contains") or {}).items():
            got_keys = {ident_key_tuple(r) for r in _rows(model, rel)}
            for r in _norm_rows(model, rel, rows):
                if ident_key_tuple(r) not in got_keys:
                    return CaseResult(c.suite, c.id, False, f"关系 {rel} 缺少 {fmt_rows([r])}", c.file)
        for rel, rows in (expect.get("excludes") or {}).items():
            got_keys = {ident_key_tuple(r) for r in _rows(model, rel)}
            for r in _norm_rows(model, rel, rows):
                if ident_key_tuple(r) in got_keys:
                    return CaseResult(c.suite, c.id, False, f"关系 {rel} 不应包含 {fmt_rows([r])}", c.file)
    except (KeyError, EvalError, ValueError) as e:
        return CaseResult(c.suite, c.id, False, f"期望写法有误：{e}", c.file)
    return CaseResult(c.suite, c.id, True, file=c.file)


def ident_key_tuple(row: tuple) -> tuple:
    return tuple(ident_key(v) for v in row)


def fmt_rows(rows: list[tuple]) -> str:
    return "[" + ", ".join("(" + ", ".join(display(v) for v in r) + ")" for r in rows) + "]"


def run_case(c: Case) -> CaseResult:
    if c.kind == "invalid":
        return CaseResult(c.suite, c.id, False, f"套件无效：{c.data.get('error')}", c.file)
    try:
        if c.kind in ("expr", "template"):
            return run_expr_case(c)
        if c.kind == "rules":
            return run_rules_case(c)
        from .factory.conformance import run_factory_case

        return run_factory_case(c)
    except Exception as e:  # noqa: BLE001 - 运行器必须把任何异常记为失败，而不是中断
        return CaseResult(c.suite, c.id, False, f"运行器异常：{type(e).__name__}: {e}", c.file)


def run(paths: list[str]) -> list[CaseResult]:
    return [run_case(c) for c in load_cases(paths)]
