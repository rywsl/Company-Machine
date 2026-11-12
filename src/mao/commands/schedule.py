"""schedule 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, human_by, open_context, open_snapshot
from mao.runner import Runner
from mao.scheduler import Scheduler


def register(sub: argparse._SubParsersAction) -> None:
    schedule = sub.add_parser("schedule")
    commands = schedule.add_subparsers(dest="schedule_cmd", required=True)
    tick = commands.add_parser("tick")
    tick.set_defaults(func=cmd_tick)
    dlq = commands.add_parser("dlq")
    dlq_commands = dlq.add_subparsers(dest="dlq_cmd", required=True)
    listed = dlq_commands.add_parser("ls")
    listed.set_defaults(func=cmd_dlq)
    quarantine = commands.add_parser("quarantine")
    quarantine.add_argument("bean")
    quarantine.add_argument("--reason", required=True)
    quarantine.set_defaults(func=cmd_quarantine)
    release = commands.add_parser("release")
    release.add_argument("bean")
    release.set_defaults(func=cmd_release)


def _scheduler(ctx: object, args: argparse.Namespace) -> Scheduler:
    snapshot = open_snapshot(ctx, args)
    return Scheduler(ctx, snapshot, Runner(ctx, snapshot))


def cmd_tick(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        body = _scheduler(ctx, args).tick()
    return finish(args, body)


def cmd_dlq(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _scheduler(ctx, args).list_dead()
    return finish(args, found)


def cmd_quarantine(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        _scheduler(ctx, args).quarantine(args.bean, by=human_by(), reason=args.reason)
    return finish(args, None)


def cmd_release(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        _scheduler(ctx, args).release(args.bean, by=human_by())
    return finish(args, None)
