"""secret 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context
from mao.egress import SecretStore


def register(sub: argparse._SubParsersAction) -> None:
    secret = sub.add_parser("secret")
    commands = secret.add_subparsers(dest="secret_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.set_defaults(func=cmd_ls)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        store = SecretStore(ctx.root)
        body = [{"name": name, "class": store.secret_class(name)} for name in store.names()]
    return finish(args, body)
