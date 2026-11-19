"""factory 命令行。退出码见 spec/system/cli.md 与 commands.md。"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

import mao
from mao.commands.common import GLOBAL, GLOBAL_DEFAULTS, report_error
from mao.errors import FactoryError

_COMMAND_MODULES = (
    "access",
    "check",
    "bean",
    "job",
    "line",
    "receipt",
    "trigger",
    "cache",
    "agent",
    "asset",
    "approval",
    "autonomy",
    "secret",
    "source",
    "egress",
    "console",
    "web",
    "contract",
    "deployment",
    "schedule",
    "epoch",
    "changeset",
    "governance",
    "guide",
    "image",
    "inbox",
    "knowledge",
    "objective",
    "instance",
    "audit",
    "drift",
    "foreman",
    "maintain",
    "gateway",
    "gc",
    "backend",
    "backup",
    "mcp",
    "package",
    "qualification",
    "serve",
    "status",
    "talk",
)


def _registrars(commands: tuple[str, ...] | None = None) -> tuple[Any, ...]:
    names = _COMMAND_MODULES if commands is None else commands
    return tuple(importlib.import_module(f"mao.commands.{name}").register for name in names)


def build_parser(commands: tuple[str, ...] | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", parents=[GLOBAL])
    parser.set_defaults(**GLOBAL_DEFAULTS)
    parser.add_argument("--version", action="version", version=mao.__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for register in _registrars(commands):
        register(sub)
    return parser


def _preparse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--root", default=None)
    pre.add_argument("--registry", default=None)
    pre.add_argument("--json", action="store_true")
    pre.add_argument("--provider", choices=("docker", "local"), default="docker")
    pre.add_argument("--local-commands", default=None)
    pre.add_argument("--epoch", default=None)
    return pre.parse_known_args(argv)


def _apply_globals(args: argparse.Namespace, known: argparse.Namespace) -> None:
    args.root = known.root
    args.registry = known.registry
    args.json = known.json
    args.provider = known.provider
    args.local_commands = known.local_commands
    args.epoch = known.epoch


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        known, rest = _preparse(argv)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)
    try:
        command = next((item for item in rest if item in _COMMAND_MODULES), None)
        # `serve` is the resident path and only needs its own parser.  Keeping
        # the other command modules lazy removes a large import cost before the
        # daemon can acquire its lease and wake agents.  The public parser with
        # no argument still registers every command for help and library users.
        # Cross-process job execution also only needs the job parser.  Loading
        # every command module in each short-lived worker adds enough startup
        # latency to miss the resident pump's first observation window.
        selected = (command,) if command in {"job", "serve"} else None
        args = build_parser(selected).parse_args(rest)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)
    _apply_globals(args, known)
    if args.local_commands and args.provider != "local":
        print("--local-commands 只能与 --provider local 一起使用", file=sys.stderr)
        return 2
    func: Any = getattr(args, "func", None)
    if func is None:
        print("未知子命令", file=sys.stderr)
        return 2
    try:
        return int(func(args))
    except FactoryError as exc:
        return report_error(args, exc)
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
