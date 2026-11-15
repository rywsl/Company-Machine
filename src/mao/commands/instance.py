"""instance 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context, open_snapshot
from mao.instances import Instances


def register(sub: argparse._SubParsersAction) -> None:
    instance = sub.add_parser("instance")
    commands = instance.add_subparsers(dest="instance_cmd", required=True)
    listed = commands.add_parser("list")
    listed.add_argument("--station", default=None)
    listed.add_argument("--state", default=None)
    listed.set_defaults(func=cmd_list)
    restart = commands.add_parser("restart")
    restart.add_argument("ref")
    restart.set_defaults(func=cmd_restart)
    stop = commands.add_parser("stop")
    stop.add_argument("ref")
    stop.set_defaults(func=cmd_stop)


def _manager(ctx: object, args: argparse.Namespace) -> Instances:
    return Instances(ctx, open_snapshot(ctx, args))


def cmd_list(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        rows = _manager(ctx, args).list(station=args.station, state=args.state)
    return finish(args, rows)


def cmd_restart(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = _manager(ctx, args).restart(args.ref)
    return finish(args, record)


def cmd_stop(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = _manager(ctx, args).stop(args.ref)
    return finish(args, record)
