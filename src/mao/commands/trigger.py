"""trigger 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context, open_snapshot
from mao.runner import Runner
from mao.runs import LineRunner, TriggerEngine


def register(sub: argparse._SubParsersAction) -> None:
    trigger = sub.add_parser("trigger")
    commands = trigger.add_subparsers(dest="trigger_cmd", required=True)
    process = commands.add_parser("process")
    process.set_defaults(func=cmd_process)


def cmd_process(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        lines = LineRunner(ctx, snapshot, Runner(ctx, snapshot))
        created = TriggerEngine(ctx, snapshot, lines).process()
    return finish(args, created)
