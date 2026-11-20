"""maintain 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context, open_snapshot
from mao.foreman import bootstrap_check


def register(sub: argparse._SubParsersAction) -> None:
    maintain = sub.add_parser("maintain")
    commands = maintain.add_subparsers(dest="maintain_cmd", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.set_defaults(func=cmd_bootstrap)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        report = bootstrap_check(snapshot, ())
    return finish(args, report)
