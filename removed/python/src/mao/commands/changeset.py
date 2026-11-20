"""changeset 组命令。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mao.commands.common import finish, open_context
from mao.governance.changesets import ChangePipeline
from mao.governance.epochs import EpochStore


def register(sub: argparse._SubParsersAction) -> None:
    changeset = sub.add_parser("changeset")
    commands = changeset.add_subparsers(dest="changeset_cmd", required=True)
    propose = commands.add_parser("propose")
    propose.add_argument("file")
    propose.add_argument("--commit", required=True)
    propose.add_argument("--by", required=True)
    propose.set_defaults(func=cmd_propose)
    listed = commands.add_parser("ls")
    listed.add_argument("--state", default=None)
    listed.set_defaults(func=cmd_ls)
    show = commands.add_parser("show")
    show.add_argument("changeset")
    show.set_defaults(func=cmd_show)
    withdraw = commands.add_parser("withdraw")
    withdraw.add_argument("changeset")
    withdraw.add_argument("--by", required=True)
    withdraw.set_defaults(func=cmd_withdraw)
    advance = commands.add_parser("advance")
    advance.add_argument("changeset")
    advance.set_defaults(func=cmd_advance)
    signed = commands.add_parser("signoff")
    signed.add_argument("changeset")
    signed.add_argument("decision")
    signed.add_argument("--by", required=True)
    signed.add_argument("--note", default="")
    signed.set_defaults(func=cmd_signoff)


def _pipeline(ctx: object) -> ChangePipeline:
    return ChangePipeline(ctx, EpochStore(ctx))


def cmd_propose(args: argparse.Namespace) -> int:
    document = json.loads(Path(args.file).read_text(encoding="utf-8"))
    with open_context(args) as ctx:
        store = EpochStore(ctx)
        found = _pipeline(ctx).propose(document, args.commit, by=args.by, timeout_s=store.timeout_s)
    return finish(args, found)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _pipeline(ctx).list(state=args.state)
    return finish(args, found)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _pipeline(ctx).get(args.changeset)
    return finish(args, found)


def cmd_withdraw(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _pipeline(ctx).withdraw(args.changeset, by=args.by)
    return finish(args, found)


def cmd_signoff(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _pipeline(ctx).signoff(args.changeset, args.decision, by=args.by, note=args.note or "")
    return finish(args, found)


def cmd_advance(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _pipeline(ctx).advance(args.changeset)
    return finish(args, found)
