#!/usr/bin/env python3
"""PreToolUse：拒绝编辑受保护路径，拒绝 lock --update 与破坏性 git 命令。

stdin 是一行 JSON（Claude Code / Grok 的 PreToolUse 事件），按 UTF-8 字节解码，
不用控制台的区域编码（Windows 上是 GBK，会把中文内容解成坏 JSON）。命中时：
- 原因写到 stderr（Claude Code 在退出码 2 时把 stderr 交给模型）；
- 同时向 stdout 写 {"decision": "deny", "reason": "..."}（兼容只读 stdout 的 CLI）；
- 以退出码 2 结束。
未命中则退出码 0。

路径匹配与 tools/harness.py 的 pattern_regex 完全相同（tests/test_hooks.py 守护）：
`**/` 匹配零个或多个目录，`*` 与 `?` 不跨 `/`。模式来自 plan/protected.lock，读不到时用默认模式。
本脚本只用标准库，不依赖当前工作目录（.claude/settings.json 用 $CLAUDE_PROJECT_DIR 调用它）。
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "plan" / "protected.lock"

DEFAULT_PROTECTED = [
    "spec/**/*.md",
    "spec/schemas/*.json",
    "spec/mrl/constitution/*.mrl",
    "spec/mrl/governance/*.mrl",
    "spec/examples/registry/**/*",
    "plan/*.yaml",
    "plan/*.md",
    "plan/tasks/*.md",
    "plan/scenarios/*.yaml",
    "tests/conftest.py",
    "tests/test_hooks.py",
    "tests/acceptance/**/*",
    "tests/fakes/**/*",
    "tests/fixtures/**/*",
    "tools/harness.py",
    "tools/hooks/*.py",
    "tools/build_test_images.py",
    "docs/adr/*.md",
    "pyproject.toml",
    ".gitattributes",
    ".claude/settings.json",
    "AGENTS.md",
    "CLAUDE.md",
    "docs/*.md",
    "tests/test_codebase_policy.py",
    "tests/**/conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "spec/runtimes/*.yaml",
]

EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "search_replace", "strreplace"}
BASH_TOOLS = {"bash", "run_terminal_command", "shell", "powershell"}
PATH_KEYS = ("file_path", "path", "notebook_path", "target_file")

# 破坏性 git 操作（plan/README.md 第 6 节）。按 git 之后的词序列匹配，不按子串，
# 所以 `git commit -m "reset --hard"` 不会被误拦。
DESTRUCTIVE_GIT = [
    (("reset", "--hard"), "git reset --hard 会丢掉工作区的修改"),
    (("clean",), "git clean 会删除未跟踪文件"),
    (("checkout", "."), "git checkout . 会丢掉工作区的修改"),
    (("checkout", "--", "."), "git checkout -- . 会丢掉工作区的修改"),
    (("restore", "."), "git restore . 会丢掉工作区的修改"),
    (("push", "--force"), "禁止强推"),
    (("push", "-f"), "禁止强推"),
]


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """与 tools/harness.py 的同名函数逐字一致。"""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def patterns() -> list[str]:
    try:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return list(DEFAULT_PROTECTED)
    found = data.get("patterns") if isinstance(data, dict) else None
    return list(found) if isinstance(found, list) and found else list(DEFAULT_PROTECTED)


def rel_of(path: str) -> str | None:
    """把工具给出的路径换成仓库内的 POSIX 相对路径；仓库外的路径返回 None。

    相对路径按仓库根解析，不按当前工作目录（不要用 lstrip("./")，它会吃掉 `.claude` 的点）。
    """
    text = path.strip().strip('"').replace("\\", "/")
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        return candidate.resolve().relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def is_protected(rel: str, pats: list[str] | None = None) -> bool:
    return any(pattern_regex(p).match(rel) for p in (pats if pats is not None else patterns()))


def paths_from(payload: dict) -> list[str]:
    found: list[str] = []
    for key in PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            found.append(value)
    for item in payload.get("edits") or payload.get("files") or []:
        if isinstance(item, dict):
            found.extend(item[k] for k in PATH_KEYS if isinstance(item.get(k), str))
        elif isinstance(item, str):
            found.append(item)
    return found


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _contains_seq(words: list[str], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    return any(tuple(words[i : i + n]) == seq for i in range(len(words) - n + 1))


def bash_problem(command: str) -> str | None:
    compact = " ".join(command.split())
    if "harness.py" in compact and "lock" in compact and "--update" in compact:
        return "禁止运行 harness.py lock --update。只有人可以重新登记受保护文件。"
    # 按 shell 分隔符切成子命令，逐个看 git 的参数。
    for part in re.split(r"&&|\|\||;|\||\n", command):
        words = _words(part)
        if "git" not in words:
            continue
        rest = words[words.index("git") + 1 :]
        for seq, reason in DESTRUCTIVE_GIT:
            if _contains_seq(rest, seq):
                return f"禁止的 git 操作：{reason}。需要时写 plan/journal/ 并请人处理。"
    return None


def decide(event: dict) -> tuple[str, str]:
    tool = str(event.get("tool_name") or event.get("toolName") or "").strip()
    payload = event.get("tool_input") or event.get("toolInput") or {}
    if not isinstance(payload, dict):
        payload = {}
    lowered = tool.lower()
    if lowered in BASH_TOOLS:
        problem = bash_problem(str(payload.get("command") or ""))
        return ("deny", problem) if problem else ("allow", "")
    if lowered in EDIT_TOOLS:
        pats = patterns()
        for raw in paths_from(payload):
            rel = rel_of(raw)
            if rel and is_protected(rel, pats):
                return "deny", (
                    f"受保护文件，禁止编辑：{rel}（plan/README.md 第 6 节；"
                    "有疑问写 plan/journal/ 并 task block）"
                )
        return "allow", ""
    return "allow", ""


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        event = None
    if not isinstance(event, dict):
        decision, reason = "deny", "hook 输入不是 JSON 对象"
    else:
        decision, reason = decide(event)
    if decision == "deny":
        sys.stderr.buffer.write((reason + "\n").encode("utf-8"))
        sys.stdout.buffer.write(
            (json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False) + "\n").encode("utf-8")
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
