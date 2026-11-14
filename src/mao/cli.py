"""factory 命令行。退出码见 spec/system/cli.md 与 commands.md。"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import mao
from mao.commands import (
    approval,
    audit,
    autonomy,
    backend,
    backup,
    bean,
    cache,
    changeset,
    check,
    console,
    contract,
    drift,
    egress,
    epoch,
    foreman,
    gateway,
    gc,
    governance,
    image,
    inbox,
    job,
    line,
    maintain,
    mcp,
    receipt,
    schedule,
    secret,
    serve,
    source,
    status,
    trigger,
)
from mao.commands.common import GLOBAL, GLOBAL_DEFAULTS, report_error
from mao.errors import FactoryError

_REGISTRARS = (
    check.register,
    bean.register,
    job.register,
    line.register,
    receipt.register,
    trigger.register,
    cache.register,
    approval.register,
    autonomy.register,
    secret.register,
    source.register,
    egress.register,
    console.register,
    contract.register,
    schedule.register,
    epoch.register,
    changeset.register,
    governance.register,
    image.register,
    inbox.register,
    audit.register,
    drift.register,
    foreman.register,
    maintain.register,
    gateway.register,
    gc.register,
    backend.register,
    backup.register,
    mcp.register,
    serve.register,
    status.register,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", parents=[GLOBAL])
    parser.set_defaults(**GLOBAL_DEFAULTS)
    parser.add_argument("--version", action="version", version=mao.__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    for register in _REGISTRARS:
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
        args = build_parser().parse_args(rest)
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
