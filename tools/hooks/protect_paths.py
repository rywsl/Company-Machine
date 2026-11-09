#!/usr/bin/env python3
"""PreToolUse：拒绝编辑受保护路径，并拒绝 lock --update。

stdin 是一行 JSON（Claude Code / Grok PreToolUse）。命中时向 stdout 写
{"decision": "deny", "reason": "..."} 并以退出码 2 结束。未命中则 allow，退出码 0。

匹配语义与 tools/harness.py 的 pathlib glob 相同。lock 文件不存在时用同一份默认模式。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "plan" / "protected.lock"

DEFAULT_PROTECTED = [
    "spec/**/*.md",
    "spec/schemas/*.json",
    "spec/mrl/constitution/*.mrl",
    "spec/examples/registry/**/*",
    "plan/*.yaml",
    "plan/*.md",
    "plan/tasks/*.md",
    "plan/scenarios/*.yaml",
    "tests/conftest.py",
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
]

EDIT_TOOLS = {
    "edit",
    "write",
    "multiedit",
    "notebookedit",
    "search_replace",
    "strreplace",
}
BASH_TOOLS = {"bash", "run_terminal_command", "shell"}


def patterns() -> list[str]:
    if LOCK.exists():
        data = json.loads(LOCK.read_text(encoding="utf-8"))
        return list(data.get("patterns") or DEFAULT_PROTECTED)
    return list(DEFAULT_PROTECTED)


def rel_of(path: str) -> str | None:
    text = path.strip().strip('"').replace("\\", "/")
    if not text:
        return None
    candidate = Path(text)
    try:
        if candidate.is_absolute():
            return candidate.resolve().relative_to(ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    return text.lstrip("./")


def is_protected(rel: str) -> bool:
    probe = ROOT / rel
    for pat in patterns():
        for hit in ROOT.glob(pat):
            if hit.is_file() and hit.resolve() == probe.resolve():
                return True
        # 尚未存在的受保护路径也要拦住（例如新建 docs/adr/0001.md）。
        if _glob_match(rel, pat):
            return True
    return False


def _glob_match(rel: str, pat: str) -> bool:
    """pathlib 的 ** 语义：用一个虚拟文件探测模式是否能命中这条相对路径。"""
    if Path(rel).match(pat):
        return True
    # Path.match 不总是按完整相对路径匹配 **。再试一次从仓库根出发的 glob 结果。
    return any(p.as_posix() == rel for p in Path(".").glob(pat))


def paths_from(tool: str, payload: dict) -> list[str]:
    keys = ("file_path", "path", "notebook_path", "target_file", "file_path")
    found: list[str] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            found.append(value)
    for item in payload.get("edits") or payload.get("files") or []:
        if isinstance(item, dict):
            for key in keys:
                value = item.get(key)
                if isinstance(value, str):
                    found.append(value)
        elif isinstance(item, str):
            found.append(item)
    return found


def bash_update(command: str) -> bool:
    compact = " ".join(command.split())
    return "lock" in compact and "--update" in compact and "harness.py" in compact


def decide(event: dict) -> tuple[str, str]:
    tool = str(event.get("tool_name") or event.get("toolName") or "").strip()
    payload = event.get("tool_input") or event.get("toolInput") or {}
    if not isinstance(payload, dict):
        payload = {}
    lowered = tool.lower()
    if lowered in BASH_TOOLS or tool == "Bash":
        command = str(payload.get("command") or "")
        if bash_update(command):
            return "deny", "禁止运行 harness.py lock --update。只有人可以重新登记受保护文件。"
        return "allow", ""
    if lowered in EDIT_TOOLS or tool in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        for raw in paths_from(tool, payload):
            rel = rel_of(raw)
            if rel and is_protected(rel):
                return "deny", f"受保护文件，禁止编辑：{rel}"
        return "allow", ""
    return "allow", ""


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        print(json.dumps({"decision": "deny", "reason": "hook 输入不是 JSON"}, ensure_ascii=False))
        return 2
    decision, reason = decide(event)
    if decision == "deny":
        print(json.dumps({"decision": "deny", "reason": reason}, ensure_ascii=False))
        return 2
    print(json.dumps({"decision": "allow"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
