"""inbox 组命令。"""

from __future__ import annotations

import argparse
import os
import sys

from mao.commands.common import finish, open_context
from mao.errors import FactoryError
from mao.inbox import Inbox, answer


def register(sub: argparse._SubParsersAction) -> None:
    inbox = sub.add_parser("inbox")
    commands = inbox.add_subparsers(dest="inbox_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.add_argument("--status", default=None)
    listed.add_argument("--kind", default=None)
    listed.set_defaults(func=cmd_ls)
    show = commands.add_parser("show")
    show.add_argument("request")
    show.set_defaults(func=cmd_show)
    answered = commands.add_parser("answer")
    answered.add_argument("request")
    answered.add_argument("--choice", default=None)
    answered.add_argument("--text", default=None)
    answered.add_argument("--approve", action="store_true")
    answered.add_argument("--deny", action="store_true")
    answered.add_argument("--secret-file", default=None)
    answered.add_argument("--secret-stdin", action="store_true")
    answered.add_argument("--point", default=None)
    answered.add_argument("--points", default=None)
    answered.add_argument("--offset", default=None)
    answered.add_argument("--done", action="store_true")
    answered.add_argument("--note", default=None)
    answered.set_defaults(func=cmd_answer)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("request")
    cancel.add_argument("--note", default="")
    cancel.set_defaults(func=cmd_cancel)


def cmd_ls(args: argparse.Namespace) -> int:
    status = "open" if args.status is None else args.status
    with open_context(args) as ctx:
        found = Inbox(ctx).list(status=status, kind=args.kind)
    return finish(args, found)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        item = Inbox(ctx).get(args.request)
    return finish(args, item)


def cmd_answer(args: argparse.Namespace) -> int:
    reply = _reply(args)
    with open_context(args) as ctx:
        item = answer(ctx, args.request, reply, by=_by())
    return finish(args, item)


def cmd_cancel(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        item = Inbox(ctx).cancel(args.request, by=_by(), note=args.note or "")
    return finish(args, item)


def _by() -> str:
    return os.environ.get("FACTORY_ACTOR") or "human:cli"


def _reply(args: argparse.Namespace) -> dict[str, object]:
    flags = [
        args.choice is not None,
        args.text is not None,
        args.approve,
        args.deny,
        args.secret_file is not None,
        args.secret_stdin,
        args.point is not None,
        args.points is not None,
        args.offset is not None,
        args.done,
    ]
    if sum(1 for item in flags if item) != 1:
        print("必须恰好给一个回答旗标", file=sys.stderr)
        raise SystemExit(2)
    if args.choice is not None:
        return {"choice": args.choice}
    if args.text is not None:
        return {"text": args.text}
    if args.approve:
        return {"decision": "approve", "note": args.note or ""}
    if args.deny:
        return {"decision": "deny", "note": args.note or ""}
    if args.secret_file is not None:
        return {"secret": _read_secret_bytes(args.secret_file)}
    if args.secret_stdin:
        return {"secret": _stdin_bytes()}
    if args.point is not None:
        return {"point": _point(args.point)}
    if args.points is not None:
        return {"points": _points(args.points)}
    if args.offset is not None:
        return {"offset": _offset(args.offset)}
    return {"done": True}


def _read_secret_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise FactoryError("CLI_USAGE", "读不到秘密文件", path=path) from exc


def _stdin_bytes() -> bytes:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read()
    return sys.stdin.read().encode("utf-8")


def _point(text: str) -> list[int]:
    parts = text.split(",")
    if len(parts) != 2:
        raise FactoryError("CLI_USAGE", "--point 必须是 X,Y")
    try:
        return [int(parts[0]), int(parts[1])]
    except ValueError as exc:
        raise FactoryError("CLI_USAGE", "--point 必须是 X,Y") from exc


def _points(text: str) -> list[list[int]]:
    if text == "":
        raise FactoryError("CLI_USAGE", "--points 写法不对")
    found = []
    for item in text.split(";"):
        found.append(_point(item))
    return found


def _offset(text: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise FactoryError("CLI_USAGE", "--offset 必须是整数") from exc
