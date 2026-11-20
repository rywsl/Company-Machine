"""M17-12 真实 CLI 帮助、恢复和外部目标交接。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mao.cli import build_parser, main


def test_public_help_discovers_complete_lifecycle(tmp_path: Path) -> None:
    del tmp_path
    text = _run(["--help"])
    for name in ("doctor", "init", "guide", "capabilities", "talk", "objective", "access"):
        assert name in text
    goals = json.loads(_run(["guide", "goals", "--json"]))
    assert goals["schema_version"] == "mao-guide-1"
    assert any("talk" in step["commands"] for step in goals["steps"])


def test_capabilities_examples_match_installed_parser(tmp_path: Path) -> None:
    del tmp_path
    body = json.loads(_run(["capabilities", "--json"]))
    assert body["schema_version"] == "mao-capabilities-1"
    installed = {path for path, _action in _paths(build_parser())}
    listed = {item["path"] for item in body["commands"]}
    assert installed == listed
    for item in body["commands"]:
        example = item["examples"][0]
        assert example[0] == "factory"
        assert " ".join(example[1:-1]) == item["path"]
        assert example[-1] == "--help"


def test_error_recovery_handles_pending_unknown_and_permission(tmp_path: Path) -> None:
    code, body = _json(tmp_path, ["objective", "show", "missing"])
    assert code != 0
    assert body["recovery"]["category"] in {"business", "permission", "dependency", "usage"}
    assert body["recovery"]["actions"]
    from mao.command_help import recovery_for

    unknown = recovery_for("TRANSPORT_TIMEOUT")
    assert unknown["category"] == "outcome_unknown" and unknown["retryable"] is False
    assert "重新提交" not in unknown["actions"][0]["reason"] or "不要" in unknown["actions"][0]["reason"]
    pending = recovery_for("OPERATION_FORBIDDEN")
    assert pending["category"] == "permission"


def test_skill_packaging_version_and_truthful_capabilities(tmp_path: Path) -> None:
    del tmp_path
    skill = Path("skills/mao-factory/SKILL.md")
    text = skill.read_text(encoding="utf-8")
    assert "factoryctl" in text and "外部操作者" in text
    body = json.loads(_run(["capabilities", "--json"]))
    assert body["skill"]["version"] == body["version"]
    assert body["skill"]["sha256"]
    assert "line run" not in text


def test_help_only_operator_hands_goal_to_factory(tmp_path: Path) -> None:
    guide = json.loads(_run(["guide", "goals", "--json"]))
    command = next(step["commands"] for step in guide["steps"] if "talk" in step["commands"])
    assert "run" not in command and "authorize" not in command
    assert command[1] == "talk"
    log = tmp_path / "operator.log"
    log.write_text(" ".join(command) + "\n", encoding="utf-8")
    assert "objective authorize" not in log.read_text(encoding="utf-8")


def test_skill_operator_hands_goal_to_factory(tmp_path: Path) -> None:
    skill = Path("skills/mao-factory/SKILL.md").read_text(encoding="utf-8")
    assert "factory talk send" in skill
    assert "盲目重新提交" in skill
    chosen = ["factory", "talk", "send"]
    assert all(part in skill for part in ("talk send",))
    log = tmp_path / "skill-operator.log"
    log.write_text(" ".join(chosen) + "\n", encoding="utf-8")
    assert "line run" not in log.read_text(encoding="utf-8")
    code = main(["guide", "goals", "--json"])
    assert code == 0


def _run(args: list[str]) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "mao.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _json(tmp_path: Path, args: list[str]) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "mao.cli", "--root", str(tmp_path), "--json", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, json.loads(proc.stdout or "{}")


def _paths(parser):
    found = []
    actions = getattr(parser, "_subparsers", None)
    if actions is None:
        return found
    for action in actions._group_actions:
        for name, sub in action.choices.items():
            found.append((name, sub))
            for child, nested in _paths(sub):
                found.append((f"{name} {child}", nested))
    return found
