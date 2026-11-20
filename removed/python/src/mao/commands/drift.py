"""drift 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context
from mao.governance.epochs import EpochStore
from mao.reconcile import Reconciler


def register(sub: argparse._SubParsersAction) -> None:
    drift = sub.add_parser("drift")
    commands = drift.add_subparsers(dest="drift_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.set_defaults(func=cmd_ls)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = [
            item
            for item in Reconciler(ctx, EpochStore(ctx)).findings(state=None)
            if item.invariant == "drift"
        ]
    return finish(args, found)
