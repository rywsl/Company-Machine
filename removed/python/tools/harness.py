#!/usr/bin/env python3
"""提交和推送前的 spec 一致性校验。"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

SYSTEM_SPEC_CHAPTERS = (
    "目的与范围",
    "依赖",
    "接口",
    "数据格式",
    "行为",
    "错误",
    "验收测试",
    "错误示范与正确示范",
    "风险与注意事项",
)

ACCEPT_FILE_RE = re.compile(r"`(tests/acceptance(?:/[\w.-]+)*/test_[\w.-]+\.py)`")
TEST_NAME_RE = re.compile(r"`(test_[\w]+)`")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _run_subprocess(argv: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _parse_json_output(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index in range(len(stripped) - 1, -1, -1):
        if stripped[index] != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return value
    return None


def _run_json_command(argv: list[str], *, timeout: int | None = None) -> tuple[int, Any | None, str]:
    try:
        result = _run_subprocess(argv, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, None, str(exc)
    output = result.stdout or ""
    if result.stderr:
        output = f"{output}\n{result.stderr}"
    return result.returncode, _parse_json_output(result.stdout or ""), output.strip()


def _system_spec_problems() -> list[str]:
    """检查 spec/system 组件规范的章节结构和顺序。"""
    out: list[str] = []
    system_dir = ROOT / "spec" / "system"
    for path in sorted(system_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        heads = re.findall(r"^##\s*(\d+)\s*[.、]?\s*(.+?)\s*$", path.read_text(encoding="utf-8"), re.M)
        names = [name for _number, name in heads]
        position = -1
        for number, chapter in enumerate(SYSTEM_SPEC_CHAPTERS, 1):
            hits = [
                index
                for index, (heading_number, heading_name) in enumerate(heads)
                if heading_number == str(number) and heading_name.startswith(chapter)
            ]
            if not hits:
                out.append(f"{_rel(path)} 缺少第 {number} 章「{chapter}」（现有：{'、'.join(names)}）")
            elif hits[0] < position:
                out.append(f"{_rel(path)} 的第 {number} 章「{chapter}」顺序不对")
            else:
                position = hits[0]
    return out


def _error_code_problems() -> list[str]:
    """检查 mao 错误码都在 system spec 中有说明。"""
    try:
        from mao.errors import CODES
    except ImportError:
        return []
    text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "spec" / "system").glob("*.md"))
    return [
        f"错误码 {code} 已登记在 mao/errors.py，但没有任何 spec/system 文档提到它"
        for code in CODES
        if f"`{code}`" not in text
    ]


def _mrl_spec_problems() -> list[str]:
    """检查 MRL 规范覆盖实现登记的错误码和标准库函数。"""
    from mrl.errors import CODES
    from mrl.stdlib import FUNCTIONS

    path = ROOT / "spec" / "mrl" / "mrl-1.0.md"
    if not path.exists():
        return [f"缺少规范文档 {_rel(path)}"]
    text = path.read_text(encoding="utf-8")
    out = [f"mrl-1.0.md 没有提到错误码 {code}" for code in CODES if code not in text]
    for name in FUNCTIONS:
        if not re.search(rf"`{re.escape(name)}\b|\b{re.escape(name)}\(", text):
            out.append(f"mrl-1.0.md 没有提到标准库函数 {name}")
    return out


def _factory_profile_problems() -> list[str]:
    """检查 factory profile 覆盖诊断码和输入关系。"""
    from mrl.factory.check import kernel_program

    path = ROOT / "spec" / "mrl" / "factory-profile.md"
    if not path.exists():
        return [f"缺少规范文档 {_rel(path)}"]
    text = path.read_text(encoding="utf-8")
    program = kernel_program()
    out: list[str] = []
    for code, _severity, _title in program.evaluate({}).rows("code"):
        if f"`{code}`" not in text:
            out.append(f"factory-profile.md 没有提到诊断码 {code}")
    for name, info in sorted(program.relations.items()):
        if info.input and f"`{name}(" not in text and f"`{name}`" not in text:
            out.append(f"factory-profile.md 没有提到输入关系 {name}")
    return out


def _test_defs(path: Path) -> list[str]:
    """读取验收文件中的顶层测试函数，不导入测试文件。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]


def _spec_acceptance_tables() -> dict[str, dict[str, list[str]]]:
    """解析 spec/system 第 7 章中的验收文件和测试函数映射。"""
    out: dict[str, dict[str, list[str]]] = {}
    for path in sorted((ROOT / "spec" / "system").glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        start = re.search(r"^##\s*7\s*[.、]?.*$", text, re.M)
        if not start:
            continue
        end = re.search(r"^##\s*8\s*[.、]?", text[start.end() :], re.M)
        chapter = text[start.end() : start.end() + end.start()] if end else text[start.end() :]
        current: str | None = None
        files: dict[str, list[str]] = {}
        for line in chapter.splitlines():
            found = ACCEPT_FILE_RE.findall(line)
            if found:
                current = found[-1]
                files.setdefault(current, [])
            if current and line.lstrip().startswith("|"):
                cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                for cell in cells[:2]:
                    names = TEST_NAME_RE.findall(cell)
                    if names:
                        files[current].extend(name for name in names if name not in files[current])
                        break
        out[_rel(path)] = files
    return out


def _acceptance_problems() -> list[str]:
    """检查 spec 第 7 章与 tests/acceptance 的双向映射。"""
    out: list[str] = []
    claims: dict[str, set[str]] = defaultdict(set)
    claim_specs: dict[str, set[str]] = defaultdict(set)

    for spec, files in _spec_acceptance_tables().items():
        for rel, names in files.items():
            path = ROOT / rel
            if not path.exists():
                out.append(f"{spec} 第 7 章引用的验收文件 {rel} 不存在")
                continue
            try:
                definitions = set(_test_defs(path))
            except (OSError, SyntaxError) as exc:
                out.append(f"无法解析验收文件 {rel}：{exc}")
                continue
            claims[rel].update(names)
            claim_specs[rel].add(spec)
            for name in names:
                if name not in definitions:
                    out.append(f"{spec} 第 7 章列出的 {name} 不在 {rel} 里")

    acceptance_dir = ROOT / "tests" / "acceptance"
    for path in sorted(acceptance_dir.rglob("test_*.py")):
        rel = _rel(path)
        if rel not in claims:
            out.append(f"验收文件 {rel} 没有被任何 spec/system 文档的第 7 章引用")
            continue
        try:
            definitions = set(_test_defs(path))
        except (OSError, SyntaxError) as exc:
            out.append(f"无法解析验收文件 {rel}：{exc}")
            continue
        missing = sorted(definitions - claims[rel])
        if missing:
            specs = "、".join(sorted(claim_specs[rel]))
            for name in missing:
                out.append(f"{rel} 的 {name} 没有写进 {specs} 第 7 章")
    return out


def _acceptance_files() -> list[str]:
    """返回 spec 第 7 章引用且实际存在的验收文件。"""
    files = {rel for tables in _spec_acceptance_tables().values() for rel in tables if (ROOT / rel).is_file()}
    return sorted(files)


def doc_problems() -> list[str]:
    """执行不依赖任务状态的规范一致性检查。"""
    return [
        *_mrl_spec_problems(),
        *_factory_profile_problems(),
        *_system_spec_problems(),
        *_error_code_problems(),
        *_acceptance_problems(),
    ]


def pytest_summary(junit_xml: Path, exit_code: int) -> dict[str, Any]:
    """从 JUnit XML 汇总 pytest 结果。"""
    import xml.etree.ElementTree as ET

    summary: dict[str, Any] = {
        "exit": exit_code,
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "failures": [],
        "skips": [],
    }
    if not junit_xml.exists():
        return summary
    for case in ET.parse(junit_xml).getroot().iter("testcase"):
        nodeid = "::".join(value for value in (case.get("classname", ""), case.get("name", "")) if value)
        summary["total"] += 1
        if case.find("failure") is not None:
            summary["failed"] += 1
            summary["failures"].append(nodeid)
        elif case.find("error") is not None:
            summary["errors"] += 1
            summary["failures"].append(nodeid)
        elif (skipped := case.find("skipped")) is not None:
            summary["skipped"] += 1
            summary["skips"].append(f"{nodeid}: {skipped.get('message', '')}")
        else:
            summary["passed"] += 1
    return summary


def run_pytest(paths: list[str], label: str) -> dict[str, Any]:
    """运行一组 pytest 文件，并返回稳定的结果汇总。"""
    if not paths:
        return {
            "exit": 1,
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "failures": [f"{label}: 没有测试文件"],
            "skips": [],
        }
    with tempfile.TemporaryDirectory(prefix="mao-harness-") as directory:
        junit_xml = Path(directory) / "junit.xml"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            *paths,
            f"--junitxml={junit_xml}",
            "-p",
            "no:cacheprovider",
            "-rfEs",
        ]
        try:
            result = _run_subprocess(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "exit": 1,
                "total": 0,
                "passed": 0,
                "failed": 0,
                "errors": 1,
                "skipped": 0,
                "failures": [f"{label}: {exc}"],
                "skips": [],
            }
        if result.stdout:
            sys.stderr.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        return pytest_summary(junit_xml, result.returncode)


def _pytest_failed(summary: dict[str, Any], *, reject_skips: bool) -> bool:
    return bool(
        summary.get("exit")
        or summary.get("failed")
        or summary.get("errors")
        or (reject_skips and summary.get("skipped"))
    )


def _format_summary(label: str, summary: dict[str, Any]) -> str:
    return (
        f"{label}: exit={summary.get('exit')} total={summary.get('total')} "
        f"passed={summary.get('passed')} failed={summary.get('failed')} "
        f"errors={summary.get('errors')} skipped={summary.get('skipped')}"
    )


def _verify_mrl_command(label: str, argv: list[str], predicate: Any) -> list[str]:
    code, data, detail = _run_json_command(argv)
    if code != 0:
        return [f"{label} 失败（exit={code}）：{detail}"]
    if not isinstance(data, dict):
        return [f"{label} 没有返回 JSON 对象：{detail}"]
    try:
        ok, reason = predicate(data)
    except (AttributeError, KeyError, TypeError) as exc:
        return [f"{label} 输出结构不符合预期：{exc}"]
    if not ok:
        return [f"{label} 未通过：{reason}"]
    return []


def cmd_verify(_args: argparse.Namespace) -> int:
    """执行提交和推送前的完整一致性校验。"""
    problems = doc_problems()
    if problems:
        for problem in problems:
            print(f"✖ {problem}")
        return EXIT_FAIL
    print("✔ spec、实现和验收测试映射一致")

    acceptance = _acceptance_files()
    acceptance_summary = run_pytest(acceptance, "spec 关联的验收测试")
    if _pytest_failed(acceptance_summary, reject_skips=True):
        print(f"✖ {_format_summary('spec 关联的验收测试', acceptance_summary)}")
        return EXIT_FAIL
    print(f"✔ {_format_summary('spec 关联的验收测试', acceptance_summary)}")

    problems = _verify_mrl_command(
        "MRL conformance",
        ["uv", "run", "mrl", "conformance", "--json"],
        lambda data: (
            not data.get("failures"),
            f"failures={data.get('failures')}",
        ),
    )
    if problems:
        for problem in problems:
            print(f"✖ {problem}")
        return EXIT_FAIL
    print("✔ MRL conformance")

    problems = _verify_mrl_command(
        "MRL coverage",
        ["uv", "run", "mrl", "coverage", "--json"],
        lambda data: (
            not data.get("unexercised_codes") and not data.get("unfired_rules"),
            f"unexercised_codes={data.get('unexercised_codes')}, unfired_rules={data.get('unfired_rules')}",
        ),
    )
    if problems:
        for problem in problems:
            print(f"✖ {problem}")
        return EXIT_FAIL
    print("✔ MRL coverage")

    policy_summary = run_pytest(["tests/test_codebase_policy.py"], "代码策略测试")
    if _pytest_failed(policy_summary, reject_skips=False):
        print(f"✖ {_format_summary('代码策略测试', policy_summary)}")
        return EXIT_FAIL
    print(f"✔ {_format_summary('代码策略测试', policy_summary)}")
    print("✔ verify 通过")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harness", description="提交和推送前的 spec 一致性校验")
    subparsers = parser.add_subparsers(dest="cmd", required=True)
    subparsers.add_parser("verify", help="校验 spec、实现和验收测试的一致性")
    subparsers.choices["verify"].set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
