"""autonomy 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context
from mao.delegation import AutonomyConfig


def register(sub: argparse._SubParsersAction) -> None:
    autonomy = sub.add_parser("autonomy")
    commands = autonomy.add_subparsers(dest="autonomy_cmd", required=True)
    show = commands.add_parser("show")
    show.set_defaults(func=cmd_show)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        body = AutonomyConfig.load(ctx.root).to_json()
    return finish(args, body)
