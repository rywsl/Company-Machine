"""工厂一致性用例（kind: factory）：以一个基准 Registry 为起点打补丁，检查得到的诊断与转换解析。

用例格式（spec/mrl/factory-profile.md 第 8 章）：

    suite: factory.lines
    kind: factory
    base: example                 # example = spec/examples/registry；empty = 空目录；否则为相对套件文件的目录
    cases:
      - id: ...
        patch:                    # 按顺序应用
          - {file: stations/x.yaml, set: {spec.limits.budget_usd: 50}}        # doc 默认 0
          - {file: stations/x.yaml, doc: 1, unset: [spec.params]}
          - {file: types/new.yaml, content: "apiVersion: ..."}                 # 写入整个文件
          - {file: lines/old.yaml, delete: true}
          - {file: stations/x.yaml, append: {apiVersion: mao/v1, ...}}       # 追加一个文档
          - {file: stations/x.yaml, doc: 2, remove_doc: true}
        expect:
          clean: true             # 没有 error 与 warning（info 不限）
          ok: true                # 没有 error
          codes: [E-PORT]         # error 与 warning 的诊断码（多重集合）必须恰好等于此列表
          diags:   [{code, subject, message, severity}]   # 每项都必须匹配至少一条诊断（message 为子串）
          absent:  [{code, subject}]                       # 不得匹配任何诊断
          resolutions: [{line, step, port, status, chain, type}]

路径写法：a.b.c、a.b[0].c；键中含 "." 时写成 a["x.y"].c。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..conformance import Case, CaseResult
from ..yamlio import YamlError, load_documents
from .check import CheckResult, check_loaded
from .paths import spec_root
from .registry import read_tree, registry_from_texts

_SEG = re.compile(r'\["((?:[^"\\]|\\.)*)"\]|\[(\d+)\]|([^.\[\]]+)')


def parse_path(path: str) -> list[str | int]:
    out: list[str | int] = []
    pos = 0
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        m = _SEG.match(path, pos)
        if not m:
            raise ValueError(f"无法解析路径 {path!r}（位置 {pos}）")
        if m.group(1) is not None:
            out.append(m.group(1).replace('\\"', '"'))
        elif m.group(2) is not None:
            out.append(int(m.group(2)))
        else:
            out.append(m.group(3))
        pos = m.end()
    if not out:
        raise ValueError("路径为空")
    return out


def _set(data: Any, path: list[str | int], value: Any) -> None:
    cur = data
    for i, seg in enumerate(path[:-1]):
        nxt = path[i + 1]
        if isinstance(seg, int):
            cur = cur[seg]
            continue
        if seg not in cur or cur[seg] is None:
            cur[seg] = [] if isinstance(nxt, int) else {}
        cur = cur[seg]
    last = path[-1]
    if isinstance(last, int) and isinstance(cur, list) and last == len(cur):
        cur.append(value)
    else:
        cur[last] = value


def _unset(data: Any, path: list[str | int]) -> None:
    cur = data
    for seg in path[:-1]:
        cur = cur[seg]
    del cur[path[-1]]


def _dump(docs: list[Any]) -> str:
    """补丁后的文档写成 JSON（JSON 是 YAML 1.2 的子集，不会有标量歧义）。"""
    return "\n---\n".join(json.dumps(d, ensure_ascii=False, indent=2) for d in docs) + "\n"


def apply_patch(files: dict[str, str], op: dict[str, Any]) -> None:
    f = op["file"]
    if "content" in op:
        files[f] = str(op["content"])
        return
    if op.get("delete"):
        if f not in files:
            raise ValueError(f"要删除的文件 {f} 不存在")
        del files[f]
        return
    docs = [d.data for d in load_documents(files[f])] if f in files else []
    if "append" in op:
        docs.append(op["append"])
        files[f] = _dump(docs)
        return
    index = int(op.get("doc", 0))
    if index >= len(docs):
        raise ValueError(f"文件 {f} 只有 {len(docs)} 个文档，没有第 {index} 个")
    if op.get("remove_doc"):
        del docs[index]
    for path, value in (op.get("set") or {}).items():
        _set(docs[index], parse_path(path), value)
    for path in op.get("unset") or []:
        _unset(docs[index], parse_path(path))
    files[f] = _dump(docs)


@lru_cache(maxsize=8)
def _base_files(base: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(read_tree(base).items())) if base else ()


def base_dir(base: str | None, suite_file: str) -> str:
    if base in (None, "example"):
        return str(spec_root() / "examples" / "registry")
    if base == "empty":
        return ""
    return str((Path(suite_file).parent / str(base)).resolve())


def _match(d: Any, want: dict[str, Any]) -> bool:
    for k in ("code", "subject", "severity"):
        if k in want and getattr(d, k) != want[k]:
            return False
    return not ("message" in want and str(want["message"]) not in d.message)


def _res_match(r: dict[str, Any], want: dict[str, Any]) -> bool:
    for k, v in want.items():
        if k == "line":
            if r["line"] != v and r["line"].split(":", 1)[-1] != v:
                return False
        elif r.get(k) != v:
            return False
    return True


def compare(result: CheckResult, expect: dict[str, Any]) -> str:
    """返回空串表示符合期望，否则返回第一处不符的说明。"""
    problems = [d for d in result.diags if d.severity in ("error", "warning")]

    def listing() -> str:
        return "；".join(f"{d.code} {d.subject}：{d.message}" for d in result.diags) or "（无诊断）"

    if expect.get("clean") and problems:
        return f"期望没有 error/warning，得到：{listing()}"
    if expect.get("ok") and result.count("error"):
        return f"期望没有 error，得到：{listing()}"
    if "codes" in expect:
        got = sorted(d.code for d in problems)
        want = sorted(expect["codes"])
        if got != want:
            return f"诊断码不符：期望 {want}，得到 {got}。全部诊断：{listing()}"
    for want in expect.get("diags") or []:
        if not any(_match(d, want) for d in result.diags):
            return f"缺少诊断 {want}。全部诊断：{listing()}"
    for want in expect.get("absent") or []:
        hit = [d for d in result.diags if _match(d, want)]
        if hit:
            return f"不应出现诊断 {want}，得到 {hit[0].code} {hit[0].subject}：{hit[0].message}"
    for want in expect.get("resolutions") or []:
        if not any(_res_match(r, want) for r in result.resolutions):
            got = [{k: r.get(k) for k in ("step", "port", "status", "chain")} for r in result.resolutions]
            return f"缺少解析结果 {want}。实际：{got}"
    return ""


def flatten(ops: Any) -> list[dict[str, Any]]:
    """补丁列表可以嵌套（便于用 YAML 锚点复用一组补丁），按顺序展开。"""
    out: list[dict[str, Any]] = []
    for op in ops or []:
        out.extend(flatten(op) if isinstance(op, list) else [op])
    return out


def run_factory_case(c: Case) -> CaseResult:
    d = c.data
    files = dict(_base_files(base_dir(d.get("base"), c.file)))
    try:
        for op in flatten(d.get("patch")):
            apply_patch(files, op)
    except (KeyError, IndexError, TypeError, ValueError, YamlError) as e:
        return CaseResult(c.suite, c.id, False, f"补丁无法应用：{type(e).__name__}: {e}", c.file)
    result = check_loaded(registry_from_texts(f"<{c.suite}/{c.id}>", files))
    msg = compare(result, d.get("expect") or {})
    return CaseResult(c.suite, c.id, not msg, msg, c.file)


@dataclass
class Coverage:
    codes: dict[str, set[str]]  # 诊断码 → 期望它的用例
    unexercised_codes: list[str]  # 码表中登记、但没有任何用例期望的诊断码
    rules: int  # 内核中写 diag 的规则数
    unfired_rules: list[tuple[str, int, str]]  # 没有在任何用例中触发过的 diag 规则：(源, 行, 规则原文首行)
    cases: int


def coverage(paths: list[str]) -> Coverage:
    """factory 用例对内核的覆盖：每个诊断码至少被一个用例期望；每条写 diag 的内核规则至少触发一次。"""
    from ..conformance import load_cases
    from .check import kernel_program
    from .paths import kernel_sources

    program = kernel_program()
    table = {c for c, _sev, _title in program.evaluate({}).rows("code")}
    diag_rules = {(cr.source, cr.rule.span.line) for cr in program.rules if cr.head_rel == "diag"}
    texts = dict(kernel_sources())
    seen: dict[str, set[str]] = {}
    fired: set[tuple[str, int]] = set()
    n = 0
    for c in load_cases(paths):
        if c.kind != "factory":
            continue
        n += 1
        e = c.data.get("expect") or {}
        for code in e.get("codes") or []:
            seen.setdefault(code, set()).add(f"{c.suite}/{c.id}")
        for d in e.get("diags") or []:
            if "code" in d:
                seen.setdefault(d["code"], set()).add(f"{c.suite}/{c.id}")
        files = dict(_base_files(base_dir(c.data.get("base"), c.file)))
        try:
            for op in flatten(c.data.get("patch")):
                apply_patch(files, op)
        except (KeyError, IndexError, TypeError, ValueError, YamlError):
            continue
        model = check_loaded(registry_from_texts("<coverage>", files)).model
        if model is not None:
            fired |= {k for k, v in model.fired.items() if v}
    unfired = []
    for src, line in sorted(diag_rules - fired):
        first = texts.get(src, "").split("\n")[line - 1].strip() if src in texts else ""
        unfired.append((src, line, first))
    return Coverage(seen, sorted(table - set(seen)), len(diag_rules), unfired, n)
