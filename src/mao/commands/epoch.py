"""epoch 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, human_by, open_context
from mao.errors import FactoryError
from mao.governance.epochs import EpochStore


def register(sub: argparse._SubParsersAction) -> None:
    epoch = sub.add_parser("epoch")
    commands = epoch.add_subparsers(dest="epoch_cmd", required=True)
    init = commands.add_parser("init")
    init.add_argument("commit")
    init.add_argument("--by", default=None)
    init.set_defaults(func=cmd_init)
    listed = commands.add_parser("ls")
    listed.set_defaults(func=cmd_ls)
    show = commands.add_parser("show")
    show.add_argument("commit", nargs="?")
    show.set_defaults(func=cmd_show)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("commit", nargs="?")
    rollback.add_argument("--by", default=None)
    rollback.set_defaults(func=cmd_rollback)
    adopt = commands.add_parser("adopt")
    adopt.add_argument("commit")
    adopt.add_argument("--by", default=None)
    adopt.set_defaults(func=cmd_adopt)


def _by(args: argparse.Namespace) -> str:
    return args.by or human_by()


def cmd_init(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        commit = EpochStore(ctx).init(args.commit, by=_by(args))
    return finish(args, {"commit": commit})


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = EpochStore(ctx).list()
    return finish(args, found)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        store = EpochStore(ctx)
        commit = args.commit or store.current()
        found = [item for item in store.list() if item.commit == commit or item.commit.startswith(commit)]
        if not found:
            raise FactoryError("COMMIT_NOT_FOUND", "找不到提交", commit=commit)
        epoch = found[0]
        body = {
            "commit": epoch.commit,
            "tree": epoch.tree,
            "built_at": epoch.built_at,
            "retain": epoch.retain,
            "pointers": [name for name, value in store.pointers().items() if value == epoch.commit],
        }
    return finish(args, body)


def cmd_rollback(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        commit = EpochStore(ctx).rollback(args.commit, by=_by(args))
    return finish(args, {"commit": commit})


def cmd_adopt(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        commit = EpochStore(ctx).adopt(args.commit, by=_by(args))
    return finish(args, {"commit": commit})
