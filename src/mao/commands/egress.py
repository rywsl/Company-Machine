"""egress 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context, open_snapshot
from mao.egress import SecretStore, build_egress


def register(sub: argparse._SubParsersAction) -> None:
    egress = sub.add_parser("egress")
    commands = egress.add_subparsers(dest="egress_cmd", required=True)
    check = commands.add_parser("check")
    check.add_argument("station")
    check.set_defaults(func=cmd_check)


def cmd_check(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        plan = build_egress(snapshot, args.station, SecretStore(ctx.root))
    return finish(args, plan)
