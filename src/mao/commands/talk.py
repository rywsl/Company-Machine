"""talk 组命令。只调用 mao.talk。"""

from __future__ import annotations

import argparse
from typing import Any

from mao.commands.common import finish, open_context
from mao.context import factory_clock
from mao.talk import answer, list_messages, post_human, show


def register(sub: argparse._SubParsersAction) -> None:
    talk = sub.add_parser("talk")
    commands = talk.add_subparsers(dest="talk_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.set_defaults(func=cmd_ls)
    shown = commands.add_parser("show")
    shown.add_argument("message")
    shown.set_defaults(func=cmd_show)
    sent = commands.add_parser("send")
    sent.add_argument("--by", required=True)
    sent.add_argument("text")
    sent.set_defaults(func=cmd_send)
    answered = commands.add_parser("answer")
    answered.add_argument("message")
    answered.add_argument("--by", required=True)
    answered.add_argument("--confirm", action="store_true")
    answered.set_defaults(func=cmd_answer)


def _open(args: argparse.Namespace) -> Any:
    ctx = open_context(args)
    clock = factory_clock(ctx.root)
    if clock is not None and clock is not ctx.clock:
        ctx.clock = clock
    return ctx


def cmd_ls(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        found = list_messages(ctx)
    return finish(args, found)


def cmd_show(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        item = show(ctx, args.message)
    return finish(args, item)


def cmd_send(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        item = post_human(ctx, args.text, by=args.by)
    return finish(args, item)


def cmd_answer(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        item = answer(ctx, args.message, by=args.by, confirm=bool(args.confirm))
    return finish(args, item)
