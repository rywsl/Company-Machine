"""factory runtime qualify / factory machine qualify。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mao.commands.common import finish, open_context
from mao.errors import FactoryError
from mao.qualification import qualify_machine, qualify_runtime


def register(sub: argparse._SubParsersAction) -> None:
    runtime = sub.add_parser("runtime")
    commands = runtime.add_subparsers(dest="runtime_cmd", required=True)
    qualify = commands.add_parser("qualify")
    qualify.add_argument("runtime")
    qualify.add_argument("--profile", required=True, choices=("offline", "live"))
    qualify.add_argument("--out", required=True)
    qualify.add_argument("--allow-paid", action="store_true")
    qualify.add_argument("--budget", type=float, default=None)
    qualify.set_defaults(func=cmd_runtime)
    machine = sub.add_parser("machine")
    machine_commands = machine.add_subparsers(dest="machine_cmd", required=True)
    machine_qualify = machine_commands.add_parser("qualify")
    machine_qualify.add_argument("machine")
    machine_qualify.add_argument("--profile", required=True, choices=("offline", "live"))
    machine_qualify.add_argument("--out", required=True)
    machine_qualify.add_argument("--allow-paid", action="store_true")
    machine_qualify.add_argument("--budget", type=float, default=None)
    machine_qualify.set_defaults(func=cmd_machine)


def cmd_runtime(args: argparse.Namespace) -> int:
    return _qualify(args, qualify_runtime, args.runtime)


def cmd_machine(args: argparse.Namespace) -> int:
    return _qualify(args, qualify_machine, args.machine)


def _qualify(args: argparse.Namespace, func, key: str) -> int:
    path = Path(args.out)
    if path.exists():
        raise FactoryError("CLI_USAGE", "输出文件已存在", path=str(path))
    with open_context(args) as ctx:
        receipt = func(
            ctx,
            key,
            profile=args.profile,
            budget_usd=args.budget,
            allow_paid=args.allow_paid,
        )
    path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return finish(args, receipt)
