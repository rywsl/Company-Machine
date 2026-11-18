"""从已安装的命令解析器生成帮助、guide 和 capabilities。"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

import mao

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "mao-factory" / "SKILL.md"


def capabilities(parser: argparse.ArgumentParser) -> dict[str, Any]:
    commands = [_command(path, action) for path, action in _walk(parser)]
    skill = _skill_record()
    return {
        "schema_version": "mao-capabilities-1",
        "version": mao.__version__,
        "commands": commands,
        "profiles": ["resident", "local-test"],
        "guides": ["overview", "setup", "goals", "recovery", "results", "permissions"],
        "skill": skill,
    }


def guide(topic: str, parser: argparse.ArgumentParser) -> dict[str, Any]:
    known = {item["path"] for item in capabilities(parser)["commands"]}
    steps = {
        "overview": [
            _step(
                "查看工厂能做什么",
                ["factory", "capabilities", "--json"],
                "mao-capabilities-1",
                "factory guide goals --json",
            )
        ],
        "setup": [
            _step(
                "只读预检",
                ["factory", "doctor", "--profile", "local-test", "--json"],
                "status",
                "factory init --manifest FILE --plan",
            )
        ],
        "goals": [
            _step(
                "把宏观目标交给工厂",
                ["factory", "talk", "send", "--by", "NAME", "TEXT"],
                "message",
                "factory objective ls --json",
            )
        ],
        "recovery": [
            _step(
                "查看未完成请求",
                ["factory", "operation", "show", "REQUEST_ID", "--json"],
                "pending",
                "factory guide recovery --json",
            )
        ],
        "results": [
            _step("查看运行结果", ["factory", "job", "show", "JOB", "--json"], "job", "factory receipt ls")
        ],
        "permissions": [
            _step(
                "查看外部凭据边界",
                ["factory", "access", "issue", "--help"],
                "help",
                "factory guide permissions --json",
            )
        ],
    }
    if topic not in steps:
        topic = "overview"
    chosen = [
        item
        for item in steps[topic]
        if item["commands"][0] == "factory"
        and " ".join(item["commands"][:2]) in known
        or item["commands"][1] in {path.split()[0] for path in known}
    ]
    return {
        "schema_version": "mao-guide-1",
        "topic": topic,
        "steps": chosen or steps[topic],
        "limitations": ["外部操作者只转达目标、查看状态和转交真正需要人的事项。"],
    }


def recovery_for(code: str) -> dict[str, Any]:
    category = _category(code)
    retryable = category not in {"permission", "outcome_unknown", "usage"}
    actions = [
        {"command": ["factory", "guide", "recovery", "--json"], "reason": "查看恢复步骤", "requires": "cli"}
    ]
    if category == "outcome_unknown":
        actions = [
            {
                "command": ["factory", "operation", "show", "REQUEST_ID", "--json"],
                "reason": "对账未知结果，不要重新提交",
                "requires": "request_id",
            }
        ]
        retryable = False
    if category == "pending":
        actions = [
            {
                "command": ["factory", "operation", "show", "REQUEST_ID", "--json"],
                "reason": "请求仍在处理",
                "requires": "request_id",
            }
        ]
        retryable = False
    return {
        "category": category,
        "state": category,
        "retryable": retryable,
        "actions": actions,
        "correlation_ids": [],
    }


def _category(code: str) -> str:
    if code in {"CLI_USAGE", "DEPLOY_MANIFEST_INVALID"}:
        return "usage"
    if code in {"OPERATION_FORBIDDEN", "SERVICE_LOCATION_DENIED"}:
        return "permission"
    if code in {"TRANSPORT_TIMEOUT", "SERVICE_OUTCOME_UNKNOWN"}:
        return "outcome_unknown"
    if code in {"DOCKER_UNAVAILABLE", "RUNTIME_UNQUALIFIED"}:
        return "dependency"
    if code in {"OPERATION_CREDENTIAL_EXPIRED"}:
        return "permission"
    return "business"


def _walk(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> list[tuple[str, argparse.ArgumentParser]]:
    found = []
    actions = getattr(parser, "_subparsers", None)
    if actions is None:
        return found
    for action in actions._group_actions:
        for name, sub in action.choices.items():
            path = " ".join((*prefix, name))
            found.append((path, sub))
            found.extend(_walk(sub, (*prefix, name)))
    return found


def _command(path: str, parser: argparse.ArgumentParser) -> dict[str, Any]:
    parts = path.split()
    return {
        "path": path,
        "summary": parser.description or path,
        "roles": ["human", "external"]
        if parts[0] in {"guide", "capabilities", "doctor", "status", "talk", "objective"}
        else ["human"],
        "mutates": parts[0] in {"init", "access", "talk", "objective"},
        "may_charge": False,
        "prerequisites": [],
        "next": f"factory {path} --help",
        "examples": [["factory", *parts, "--help"]],
    }


def _step(purpose: str, commands: list[str], expected: str, nxt: str) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "commands": commands,
        "expected": expected,
        "next": nxt,
        "on_error": "factory guide recovery --json",
    }


def _skill_record() -> dict[str, Any]:
    if not _SKILL.is_file():
        return {
            "name": "mao-factory",
            "path": "skills/mao-factory/SKILL.md",
            "version": mao.__version__,
            "sha256": "",
        }
    digest = hashlib.sha256(_SKILL.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return {
        "name": "mao-factory",
        "path": "skills/mao-factory/SKILL.md",
        "version": mao.__version__,
        "sha256": digest,
    }
