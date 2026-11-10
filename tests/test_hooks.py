"""PreToolUse hook 与 harness 的一致性（tools/hooks/protect_paths.py、.claude/settings.json）。

hook 拦截编辑，harness 的 lock/accept 事后核对。两者对“哪些文件受保护”必须给出同一个答案。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO / "tools" / "hooks" / "protect_paths.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


hook = _load("_protect_paths", HOOK_PATH)
harness = _load("_harness", REPO / "tools" / "harness.py")

SAMPLES = [
    "spec/system/runner.md",
    "spec/README.md",
    "spec/mrl/conformance/expr/arith.yaml",
    "spec/schemas/line.schema.json",
    "spec/schemas/sub/x.json",
    "spec/examples/registry/types/video.yaml",
    "plan/tasks.yaml",
    "plan/state.json",
    "plan/journal/2026-11-10-M1-01.md",
    "plan/tasks/M1-01.md",
    "tests/acceptance/m1/test_m1_01_foundation.py",
    "tests/system/test_base.py",
    "tests/test_hooks.py",
    "tools/hooks/protect_paths.py",
    "tools/harness.py",
    "src/mao/base.py",
    ".claude/settings.json",
    ".claude/settings.local.json",
    "docs/adr/0001-mrl.md",
    "docs/architecture.md",
    "AGENTS.md",
]


def test_hook_regex_matches_harness_regex() -> None:
    pats = hook.patterns()
    for rel in SAMPLES:
        assert hook.is_protected(rel, pats) == harness.matches_any(rel, pats), rel


def test_default_patterns_agree() -> None:
    assert hook.DEFAULT_PROTECTED == harness.DEFAULT_PROTECTED


@pytest.mark.parametrize(
    ("raw", "rel"),
    [
        (str(REPO / "spec" / "system" / "runner.md"), "spec/system/runner.md"),
        (str(REPO / "spec" / "system" / "runner.md").replace("/", "\\"), "spec/system/runner.md"),
        (".claude/settings.json", ".claude/settings.json"),
        ("./AGENTS.md", "AGENTS.md"),
    ],
)
def test_rel_of_is_relative_to_repo(raw: str, rel: str) -> None:
    assert hook.rel_of(raw) == rel


def test_paths_outside_repo_are_allowed() -> None:
    assert hook.rel_of(str(REPO.parent / "elsewhere" / "spec" / "x.md")) is None


@pytest.mark.parametrize(
    ("event", "decision"),
    [
        ({"tool_name": "Edit", "tool_input": {"file_path": str(REPO / "spec/system/README.md")}}, "deny"),
        ({"tool_name": "Write", "tool_input": {"file_path": str(REPO / "src/mao/base.py")}}, "allow"),
        ({"tool_name": "Write", "tool_input": {"file_path": str(REPO / "docs/adr/0099-new.md")}}, "deny"),
        ({"tool_name": "MultiEdit", "tool_input": {"file_path": str(REPO / "plan/gates.yaml")}}, "deny"),
        (
            {"tool_name": "Bash", "tool_input": {"command": "uv run python tools/harness.py lock --update"}},
            "deny",
        ),
        ({"tool_name": "Bash", "tool_input": {"command": "git status && git reset --hard HEAD"}}, "deny"),
        ({"tool_name": "Bash", "tool_input": {"command": "git clean -fdx"}}, "deny"),
        ({"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}, "deny"),
        ({"tool_name": "Bash", "tool_input": {"command": 'git commit -m "explain reset --hard"'}}, "allow"),
        (
            {"tool_name": "Bash", "tool_input": {"command": "uv run python tools/harness.py lock --verify"}},
            "allow",
        ),
        ({"tool_name": "Read", "tool_input": {"file_path": str(REPO / "spec/system/README.md")}}, "allow"),
    ],
)
def test_decide(event: dict, decision: str) -> None:
    assert hook.decide(event)[0] == decision


def test_hook_runs_from_any_cwd_with_utf8_input(tmp_path: Path) -> None:
    event = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(REPO / "spec/system/x.md"), "content": "中文内容"},
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "spec/system/x.md" in proc.stderr.decode("utf-8")

    allowed = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(REPO / "src/mao/x.py"), "content": "中文"},
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(allowed, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def test_settings_call_hook_by_project_dir() -> None:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [h["command"] for entry in settings["hooks"]["PreToolUse"] for h in entry["hooks"]]
    assert commands, "没有 PreToolUse hook"
    for command in commands:
        assert "$CLAUDE_PROJECT_DIR/tools/hooks/protect_paths.py" in command, command
