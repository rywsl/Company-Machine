"""factory gc。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, human_by, open_context
from mao.retention import RetentionConfig, collect


def register(sub: argparse._SubParsersAction) -> None:
    gc = sub.add_parser("gc")
    gc.add_argument("--dry-run", action="store_true")
    gc.set_defaults(func=cmd_gc)


def cmd_gc(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        report = collect(ctx, RetentionConfig.load(ctx.root), dry_run=args.dry_run, by=human_by())
    return finish(args, report)
