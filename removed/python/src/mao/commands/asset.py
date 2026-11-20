"""factory asset。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mao.assets import AssetStore
from mao.commands.common import finish, open_context
from mao.errors import FactoryError


def register(sub: argparse._SubParsersAction) -> None:
    asset = sub.add_parser("asset")
    commands = asset.add_subparsers(dest="asset_cmd", required=True)
    put = commands.add_parser("put")
    put.add_argument("file")
    put.add_argument("--by", required=True)
    put.set_defaults(func=cmd_put)
    show = commands.add_parser("show")
    show.add_argument("asset_id")
    show.set_defaults(func=cmd_show)
    listed = commands.add_parser("ls")
    listed.add_argument("--kind", default=None)
    listed.add_argument("--subject", default=None)
    listed.set_defaults(func=cmd_list)
    closure = commands.add_parser("closure")
    closure.add_argument("asset_id", nargs="+")
    closure.set_defaults(func=cmd_closure)
    release = commands.add_parser("release")
    release.add_argument("asset_id")
    release.add_argument("--reason", required=True)
    release.add_argument("--by", required=True)
    release.set_defaults(func=cmd_release)


def cmd_put(args: argparse.Namespace) -> int:
    document = _object(args.file)
    with open_context(args) as ctx:
        record = AssetStore(ctx).put(document, by=args.by)
    return finish(args, record)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = AssetStore(ctx).get(args.asset_id)
    return finish(args, record)


def cmd_list(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        records = AssetStore(ctx).list(kind=args.kind, subject=args.subject)
    return finish(args, records)


def cmd_closure(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = AssetStore(ctx).closure(args.asset_id)
    return finish(args, record)


def cmd_release(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = AssetStore(ctx).release(args.asset_id, reason=args.reason, by=args.by)
    return finish(args, record)


def _object(path: str) -> dict:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("CLI_USAGE", "JSON 文件无法读取", path=path) from exc
    if not isinstance(body, dict):
        raise FactoryError("CLI_USAGE", "JSON 文件必须是对象", path=path)
    return body
