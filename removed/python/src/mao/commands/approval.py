"""approval 组命令。"""

from __future__ import annotations

import argparse

from mao.approvals import Approvals
from mao.commands.common import finish, open_context, open_snapshot
from mao.runner import open_runner


def register(sub: argparse._SubParsersAction) -> None:
    approval = sub.add_parser("approval")
    commands = approval.add_subparsers(dest="approval_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.add_argument("--job", default=None)
    listed.add_argument("--status", default=None)
    listed.set_defaults(func=cmd_ls)
    show = commands.add_parser("show")
    show.add_argument("approval")
    show.set_defaults(func=cmd_show)
    decide = commands.add_parser("decide")
    decide.add_argument("approval")
    decide.add_argument("decision", choices=("approve", "deny"))
    decide.add_argument("--by", required=True)
    decide.add_argument("--note", default="")
    decide.set_defaults(func=cmd_decide)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = Approvals(ctx, open_snapshot(ctx, args)).list(job_id=args.job, status=args.status)
    return finish(args, found)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = Approvals(ctx, open_snapshot(ctx, args)).get(args.approval)
    return finish(args, found)


def cmd_decide(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        open_runner(ctx, snapshot).decide(args.approval, args.decision, by=args.by, note=args.note)
    return finish(args, None)
