"""image 组命令。"""

from __future__ import annotations

import argparse
import os

from mao.commands.common import finish, open_context
from mao.governance.epochs import EpochStore
from mao.images import ImageBuilder, ImageConfig


def register(sub: argparse._SubParsersAction) -> None:
    image = sub.add_parser("image")
    commands = image.add_subparsers(dest="image_cmd", required=True)
    build = commands.add_parser("build")
    build.add_argument("path")
    build.add_argument("--commit", default=None)
    build.set_defaults(func=cmd_build)
    listed = commands.add_parser("ls")
    listed.set_defaults(func=cmd_ls)


def _by() -> str:
    return os.environ.get("FACTORY_ACTOR") or "human:cli"


def cmd_build(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        epochs = EpochStore(ctx)
        record = ImageBuilder(ctx, epochs, ImageConfig.load(ctx.root)).build(
            args.path, commit=args.commit, by=_by()
        )
    return finish(args, record, code=1 if record.state == "failed" else 0)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        rows = ImageBuilder(ctx, EpochStore(ctx), ImageConfig.load(ctx.root)).list()
    return finish(args, rows)
