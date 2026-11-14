"""secret 组命令。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mao.commands.common import finish, open_context
from mao.egress import SecretStore
from mao.errors import FactoryError
from mao.secrets import (
    direct_label,
    normalize_value,
    remember_stored,
    station_snapshot,
    store_secret,
    verify_secret,
)


def register(sub: argparse._SubParsersAction) -> None:
    secret = sub.add_parser("secret")
    commands = secret.add_subparsers(dest="secret_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.set_defaults(func=cmd_ls)
    setter = commands.add_parser("set")
    setter.add_argument("name")
    setter.add_argument("--class", dest="secret_class", required=True)
    setter.add_argument("--file", default=None)
    setter.add_argument("--stdin", action="store_true")
    setter.add_argument("--check", default=None)
    setter.set_defaults(func=cmd_set)
    verify = commands.add_parser("verify")
    verify.add_argument("name")
    verify.add_argument("--station", required=True)
    verify.set_defaults(func=cmd_verify)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        store = SecretStore(ctx.root)
        body = [{"name": name, "class": store.secret_class(name)} for name in store.names()]
    return finish(args, body)


def cmd_set(args: argparse.Namespace) -> int:
    if (args.file is None) == (not args.stdin):
        print("必须给 --file 或 --stdin", file=sys.stderr)
        return 2
    raw = Path(args.file).read_bytes() if args.file else _stdin_bytes()
    value = normalize_value(raw)
    with open_context(args) as ctx:
        if args.check:
            snapshot = _snapshot(ctx, args.check)
            result = verify_secret(
                ctx,
                snapshot,
                station=args.check,
                name=args.name,
                value=value,
                label=direct_label(ctx, args.name),
            )
            if not result.ok:
                raise FactoryError(
                    result.code or "SECRET_INVALID",
                    "秘密核验不通过",
                    exit_code=result.exit_code,
                    log=result.log,
                )
        store_secret(ctx.root, args.name, args.secret_class, value)
        remember_stored(ctx, args.name, args.secret_class, by=_by(), request=None)
    return finish(args, {"name": args.name, "class": args.secret_class, "verified": bool(args.check)})


def cmd_verify(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        store = SecretStore(ctx.root)
        injected = store.inject([args.name])
        if args.name not in injected:
            raise FactoryError("SECRET_NOT_FOUND", "找不到秘密", name=args.name)
        snapshot = _snapshot(ctx, args.station)
        result = verify_secret(
            ctx,
            snapshot,
            station=args.station,
            name=args.name,
            value=injected[args.name],
            label=direct_label(ctx, args.name),
        )
        if not result.ok:
            raise FactoryError(
                result.code or "SECRET_INVALID",
                "秘密核验不通过",
                exit_code=result.exit_code,
                log=result.log,
            )
        body = {"name": args.name, "ok": True, "code": "", "exit_code": 0}
    return finish(args, body)


def _snapshot(ctx: object, station: str) -> object:
    found = station_snapshot(ctx, station)
    if found is not None:
        return found
    from mao.governance.epochs import EpochStore

    epochs = EpochStore(ctx)
    return epochs.materialize(epochs.current())


def _by() -> str:
    return os.environ.get("FACTORY_ACTOR") or "human:cli"


def _stdin_bytes() -> bytes:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read()
    return sys.stdin.read().encode("utf-8")
