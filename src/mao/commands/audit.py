"""audit 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context
from mao.governance.epochs import EpochStore
from mao.reconcile import Reconciler


def register(sub: argparse._SubParsersAction) -> None:
    audit = sub.add_parser("audit")
    commands = audit.add_subparsers(dest="audit_cmd", required=True)
    once = commands.add_parser("once")
    once.set_defaults(func=cmd_once)
    listed = commands.add_parser("ls")
    listed.add_argument("--state", default=None)
    listed.set_defaults(func=cmd_ls)


def cmd_once(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        store = EpochStore(ctx)
        found = Reconciler(ctx, store).audit_once(timeout_s=store.timeout_s)
    bad = [item for item in found if item.state.value in {"open", "wake"}]
    return finish(args, found, code=1 if bad else 0)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = Reconciler(ctx, EpochStore(ctx)).findings(state=args.state)
    return finish(args, found)
