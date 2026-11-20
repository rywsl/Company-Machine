"""governance 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context
from mao.governance.changesets import ChangePipeline
from mao.governance.epochs import EpochStore


def register(sub: argparse._SubParsersAction) -> None:
    governance = sub.add_parser("governance")
    commands = governance.add_subparsers(dest="governance_cmd", required=True)
    tick = commands.add_parser("tick")
    tick.set_defaults(func=cmd_tick)
    safe = commands.add_parser("safe-mode")
    safe.set_defaults(func=cmd_safe)


def cmd_tick(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = ChangePipeline(ctx, EpochStore(ctx)).tick()
    return finish(args, found)


def cmd_safe(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        body = ChangePipeline(ctx, EpochStore(ctx)).safe_mode()
    return finish(args, body)
