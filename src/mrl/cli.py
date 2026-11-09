"""mrl：MRL 的命令行工具。

    mrl eval EXPR [--types M] [--env M] [--budget N] [--json]   求值一个表达式
    mrl template TEXT [--types M] [--env M] [--json]             渲染一个模板
    mrl check FILE.mrl... [--json]                               静态检查规则模块
    mrl run FILE.mrl... [--facts F] [--rel NAME]... [--json]     求值规则模块
    mrl conformance [PATH...] [--json] [-q]                      运行一致性用例（默认 spec/mrl/conformance）
    mrl coverage [PATH...] [--json]                              factory 用例对诊断码与内核规则的覆盖
    mrl registry check ROOT [--json] [--strict] [--no-info]      检查 Registry（V1 + V2）
    mrl registry facts ROOT [--rel NAME]... [--json]             查看抽取的事实与派生关系
    mrl registry resolutions ROOT [--json]                       查看转换解析表
    mrl resolve ROOT --from U --to D [--guarantees a,b] [--requires x,y] [--via c1,c2] [--json]
    mrl explain CODE                                             解释诊断码（E-*/W-*/I-* 或 MRL-*）

M 是一个 YAML/JSON 映射，可以内联书写，也可以写成 @文件路径。

退出码：0 通过；1 检查未通过（有 error、有失败用例或覆盖缺口）；2 用法错误或输入无法读取；
3 MRL 静态错误；4 MRL 运行时错误；5 内部错误（内核规则本身有缺陷）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from . import types as T
from .errors import CODES, EvalError, StaticError
from .values import display, to_py
from .yamlio import YamlError, load_one

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_STATIC, EXIT_RUNTIME, EXIT_INTERNAL = 0, 1, 2, 3, 4, 5


class UsageError(Exception):
    pass


def _out(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _mapping(text: str | None, what: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        if text.startswith("@"):
            text = Path(text[1:]).read_text(encoding="utf-8")
        data = load_one(text)
    except (OSError, YamlError) as e:
        raise UsageError(f"{what} 无法读取：{e}") from e
    if not isinstance(data, dict):
        raise UsageError(f"{what} 必须是一个映射")
    return data


def _types(spec: dict[str, Any], values: dict[str, Any]) -> dict[str, T.Type]:
    env: dict[str, T.Type] = {k: T.DYN for k in values}
    for k, v in spec.items():
        try:
            env[k] = T.parse_type(str(v))
        except ValueError as e:
            raise UsageError(f"--types 中 {k} 的类型无法解析：{e}") from e
    return env


def _split(s: str | None) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def _static(e: StaticError, as_json: bool) -> int:
    if as_json:
        _out({"ok": False, "diagnostics": [d.to_json() for d in e.diagnostics]})
    else:
        for d in e.diagnostics:
            print(d.format())
    return EXIT_STATIC


def _runtime(e: EvalError, as_json: bool) -> int:
    if as_json:
        _out({"ok": False, "error": {"code": e.code, "message": e.message, "source": e.source}})
    else:
        print(f"运行时错误 {e.code}：{e.message}")
    return EXIT_RUNTIME


# ---------- 表达式 ----------


def cmd_eval(a: argparse.Namespace) -> int:
    from .expr import compile_expr, compile_template

    values = _mapping(a.env, "--env")
    env = _types(_mapping(a.types, "--types"), values)
    try:
        compiled: Any = compile_template(a.expr, env) if a.template else compile_expr(a.expr, env)
    except StaticError as e:
        return _static(e, a.json)
    try:
        if a.template:
            value: Any = compiled.render(values, budget=a.budget)
        else:
            value = compiled.evaluate(values, budget=a.budget)
    except EvalError as e:
        return _runtime(e, a.json)
    if a.json:
        out = {"ok": True, "value": to_py(value), "warnings": [w.to_json() for w in compiled.warnings]}
        if not a.template:
            out["type"] = str(compiled.type)
        _out(out)
    else:
        for w in compiled.warnings:
            print(w.format(), file=sys.stderr)
        print(value if a.template else display(value))
    return EXIT_OK


# ---------- 规则 ----------


def _load_sources(files: list[str]) -> list[tuple[str, str]]:
    out = []
    for f in files:
        try:
            out.append((f, Path(f).read_text(encoding="utf-8")))
        except OSError as e:
            raise UsageError(f"无法读取 {f}：{e}") from e
    return out


def cmd_check(a: argparse.Namespace) -> int:
    from .rules import Program

    try:
        program = Program.from_sources(_load_sources(a.files))
    except StaticError as e:
        return _static(e, a.json)
    if a.json:
        _out(
            {
                "ok": True,
                "relations": len(program.relations),
                "rules": len(program.rules),
                "warnings": [w.to_json() for w in program.warnings],
            }
        )
    else:
        for w in program.warnings:
            print(w.format())
        print(f"✔ {len(program.relations)} 个关系，{len(program.rules)} 条规则，{len(program.strata)} 层")
    return EXIT_OK


def cmd_run(a: argparse.Namespace) -> int:
    from .rules import Program

    try:
        program = Program.from_sources(_load_sources(a.files))
    except StaticError as e:
        return _static(e, a.json)
    facts = _mapping(a.facts, "--facts")
    try:
        model = program.evaluate(facts)
    except EvalError as e:
        return _runtime(e, a.json)
    names = a.rel or sorted(n for n, info in program.relations.items() if not info.input)
    unknown = [n for n in names if n not in program.relations]
    if unknown:
        raise UsageError(f"未声明的关系：{', '.join(unknown)}")
    if a.json:
        _out({n: [[to_py(v) for v in row] for row in model.rows(n)] for n in names})
    else:
        for n in names:
            for row in model.rows(n):
                print(f"{n}({', '.join(display(v) for v in row)}).")
    return EXIT_OK


# ---------- 一致性与覆盖 ----------


def _default_conformance() -> list[str]:
    from .factory.paths import spec_root

    return [str(spec_root() / "mrl" / "conformance")]


def cmd_conformance(a: argparse.Namespace) -> int:
    from .conformance import run

    results = run(a.paths or _default_conformance())
    bad = [r for r in results if not r.ok]
    if a.json:
        _out(
            {
                "total": len(results),
                "passed": len(results) - len(bad),
                "failures": [{"case": r.name, "file": r.file, "detail": r.detail} for r in bad],
            }
        )
    else:
        if not a.quiet:
            for r in bad:
                print(f"FAIL {r.name}  ({r.file})\n     {r.detail}")
        print(f"{len(results) - len(bad)}/{len(results)} 通过")
    return EXIT_FAIL if bad or not results else EXIT_OK


def cmd_coverage(a: argparse.Namespace) -> int:
    from .factory.conformance import coverage

    paths = a.paths or [str(Path(_default_conformance()[0]) / "factory")]
    c = coverage(paths)
    gaps = bool(c.unexercised_codes or c.unfired_rules)
    if a.json:
        _out(
            {
                "cases": c.cases,
                "codes": {k: sorted(v) for k, v in sorted(c.codes.items())},
                "unexercised_codes": c.unexercised_codes,
                "kernel_diag_rules": c.rules,
                "unfired_rules": [{"source": s, "line": n, "rule": t} for s, n, t in c.unfired_rules],
            }
        )
    else:
        print(f"factory 用例：{c.cases} 个")
        print(f"诊断码覆盖：{len(c.codes)} 个码被期望；未覆盖：{', '.join(c.unexercised_codes) or '无'}")
        print(f"内核 diag 规则：{c.rules - len(c.unfired_rules)}/{c.rules} 条至少触发过一次")
        for s, n, t in c.unfired_rules:
            print(f"  未触发 {s}:{n}  {t}")
    return EXIT_FAIL if gaps else EXIT_OK


# ---------- Registry ----------


def _check(root: str, extra: list[str] | None = None):
    from .factory.check import check_registry

    try:
        return check_registry(root, extra_texprs=extra)
    except FileNotFoundError as e:
        raise UsageError(str(e)) from e


def cmd_registry_check(a: argparse.Namespace) -> int:
    r = _check(a.root)
    if a.json:
        _out(r.to_json())
    else:
        print(r.format_text(show_info=not a.no_info))
    return r.exit_code(strict=a.strict)


def cmd_registry_facts(a: argparse.Namespace) -> int:
    r = _check(a.root)
    assert r.model is not None
    program = r.model.program
    if not a.rel:
        counts = {n: len(r.model.rows(n)) for n, info in sorted(program.relations.items()) if info.input}
        if a.json:
            _out(counts)
        else:
            for n, k in counts.items():
                print(f"{n:24} {k}")
        return EXIT_OK
    unknown = [n for n in a.rel if n not in program.relations]
    if unknown:
        raise UsageError(
            f"未声明的关系：{', '.join(unknown)}（可用 mrl registry facts ROOT 查看全部输入关系）"
        )
    if a.json:
        _out({n: [[to_py(v) for v in row] for row in r.model.rows(n)] for n in a.rel})
    else:
        for n in a.rel:
            for row in r.model.rows(n):
                print(f"{n}({', '.join(display(v) for v in row)}).")
    return EXIT_OK


def cmd_registry_resolutions(a: argparse.Namespace) -> int:
    r = _check(a.root)
    if a.json:
        _out(r.resolutions)
        return EXIT_OK
    for x in r.resolutions:
        line = x["line"].split(":", 1)[-1]
        chain = f" [{', '.join(x['chain'])}]" if x.get("chain") else ""
        print(f"{x['status']:<13} {line} {x['step']}.{x['port']}  {x.get('from')} → {x.get('to')}{chain}")
    return EXIT_OK


def cmd_resolve(a: argparse.Namespace) -> int:
    from .factory.functions import resolve, resolve_via
    from .values import from_py

    r = _check(a.root, [a.from_, a.to])
    assert r.model is not None
    m = r.model

    def one(rel: str) -> Any:
        rows = m.rows(rel)
        return rows[0][0] if rows else ()

    subs, always = one("sub_pairs"), one("always_pairs")
    g, req = from_py(_split(a.guarantees)), from_py(_split(a.requires))
    from .factory.texpr import parse_texpr

    try:
        u, d = parse_texpr(a.from_).id, parse_texpr(a.to).id
    except ValueError as e:
        raise UsageError(str(e)) from e
    try:
        if a.via:
            res = resolve_via(one("conv_lib_all"), subs, always, u, g, d, req, from_py(_split(a.via)))
        else:
            res = resolve(one("conv_lib"), subs, always, u, g, d, req)
    except EvalError as e:
        return _runtime(e, a.json)
    out = to_py(res)
    if a.json:
        _out(out)
    else:
        for k in ("status", "chain", "type", "props", "missing", "lossless", "detail"):
            print(f"{k:9} {out[k]}")
    return EXIT_OK if out["status"] in ("direct", "convert") else EXIT_FAIL


def cmd_explain(a: argparse.Namespace) -> int:
    code = a.code.upper()
    if code in CODES:
        phase, desc = CODES[code]
        print(f"{code}（MRL 语言级，阶段：{phase}）：{desc}")
        print("详见 spec/mrl/mrl-1.0.md 第 9 章。")
        return EXIT_OK
    from .factory.check import kernel_program

    program = kernel_program()
    table = {c: (sev, title) for c, sev, title in program.evaluate({}).rows("code")}
    if code not in table:
        raise UsageError(f"未知的诊断码 {a.code}")
    sev, title = table[code]
    print(f"{code}（{sev}）：{title}")
    rules = []
    for cr in program.rules:
        if cr.head_rel != "diag" or not cr.rule.head.terms:
            continue
        first = cr.rule.head.terms[0]
        if getattr(first, "value", None) == code:
            rules.append(f"{cr.source}:{cr.rule.span.line}")
    if code.startswith(
        ("E-YAML", "E-SCHEMA", "E-LAYOUT", "E-EXPR", "W-EXPR", "W-PROPERTY-META", "E-CONSTITUTION")
    ):
        rules.append("宿主阶段（src/mrl/factory/check.py、sites.py），经 kernel/host.mrl 输出")
    print("产生位置：" + ("；".join(rules) if rules else "宪法模块（constitution/*.mrl）"))
    print("详见 spec/mrl/factory-profile.md 第 7 章。")
    return EXIT_OK


# ---------- 入口 ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mrl", description="MRL（MAO Relation Language）工具")
    p.add_argument("--version", action="version", version=f"mrl {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("eval", help="求值一个表达式")
    e.add_argument("expr")
    e.add_argument("--types", help="根名字的类型（映射，或 @文件）")
    e.add_argument("--env", help="根名字的值（映射，或 @文件）")
    e.add_argument("--budget", type=int, default=100_000)
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_eval, template=False)

    t = sub.add_parser("template", help="渲染一个模板")
    t.add_argument("expr", metavar="TEXT")
    t.add_argument("--types")
    t.add_argument("--env")
    t.add_argument("--budget", type=int, default=100_000)
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_eval, template=True)

    c = sub.add_parser("check", help="静态检查规则模块")
    c.add_argument("files", nargs="+")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="求值规则模块")
    r.add_argument("files", nargs="+")
    r.add_argument("--facts", help="输入事实：{关系: [[列, ...], ...]}（映射，或 @文件）")
    r.add_argument("--rel", action="append", help="只输出这些关系（可重复）")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    k = sub.add_parser("conformance", help="运行一致性用例")
    k.add_argument("paths", nargs="*")
    k.add_argument("--json", action="store_true")
    k.add_argument("-q", "--quiet", action="store_true")
    k.set_defaults(func=cmd_conformance)

    v = sub.add_parser("coverage", help="factory 用例的覆盖情况")
    v.add_argument("paths", nargs="*")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_coverage)

    g = sub.add_parser("registry", help="Registry 检查与查询")
    gs = g.add_subparsers(dest="registry_cmd", required=True)
    gc = gs.add_parser("check", help="检查 Registry（V1 + V2）")
    gc.add_argument("root")
    gc.add_argument("--json", action="store_true")
    gc.add_argument("--strict", action="store_true", help="警告也视为未通过")
    gc.add_argument("--no-info", action="store_true", help="不输出 info 级诊断")
    gc.set_defaults(func=cmd_registry_check)
    gf = gs.add_parser("facts", help="查看抽取的事实与派生关系")
    gf.add_argument("root")
    gf.add_argument("--rel", action="append")
    gf.add_argument("--json", action="store_true")
    gf.set_defaults(func=cmd_registry_facts)
    gr = gs.add_parser("resolutions", help="查看转换解析表")
    gr.add_argument("root")
    gr.add_argument("--json", action="store_true")
    gr.set_defaults(func=cmd_registry_resolutions)

    s = sub.add_parser("resolve", help="对一条假想的边做转换解析")
    s.add_argument("root")
    s.add_argument("--from", dest="from_", required=True, help="上游类型表达式")
    s.add_argument("--to", required=True, help="下游类型表达式")
    s.add_argument("--guarantees", help="上游保证的属性，逗号分隔")
    s.add_argument("--requires", help="下游要求的属性，逗号分隔")
    s.add_argument("--via", help="显式转换链，逗号分隔")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_resolve)

    x = sub.add_parser("explain", help="解释诊断码")
    x.add_argument("code")
    x.set_defaults(func=cmd_explain)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    try:
        a = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0) and EXIT_USAGE
    try:
        return int(a.func(a))
    except UsageError as e:
        print(f"用法错误：{e}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as e:  # noqa: BLE001
        from .factory.check import InternalError

        if isinstance(e, InternalError):
            print(f"内部错误：{e}", file=sys.stderr)
            return EXIT_INTERNAL
        raise


if __name__ == "__main__":
    sys.exit(main())
