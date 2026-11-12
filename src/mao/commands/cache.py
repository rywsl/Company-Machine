"""cache 组命令。"""

from __future__ import annotations

import argparse

from mao.cache import JobCache
from mao.commands.common import finish, human_by, open_context


def register(sub: argparse._SubParsersAction) -> None:
    cache = sub.add_parser("cache")
    commands = cache.add_subparsers(dest="cache_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.add_argument("--station", default=None)
    listed.set_defaults(func=cmd_ls)
    explain = commands.add_parser("explain")
    explain.add_argument("job")
    explain.set_defaults(func=cmd_explain)
    invalidate = commands.add_parser("invalidate")
    group = invalidate.add_mutually_exclusive_group(required=True)
    group.add_argument("--key")
    group.add_argument("--job")
    group.add_argument("--station")
    group.add_argument("--bean")
    invalidate.add_argument("--reason", required=True)
    invalidate.set_defaults(func=cmd_invalidate)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        JobCache(ctx)
    return finish(args, [])


def cmd_explain(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        body = JobCache(ctx).explain(args.job)
    return finish(args, body)


def cmd_invalidate(args: argparse.Namespace) -> int:
    if args.key is not None:
        scope, value = "key", args.key
    elif args.job is not None:
        scope, value = "job", args.job
    elif args.station is not None:
        scope, value = "station", args.station
    else:
        scope, value = "bean", args.bean
    with open_context(args) as ctx:
        invalid_id = JobCache(ctx).invalidate(scope, value, reason=args.reason, by=human_by())
    return finish(args, {"id": invalid_id})
