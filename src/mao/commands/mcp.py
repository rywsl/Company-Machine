"""mcp 命令。"""

from __future__ import annotations

import argparse
import os
import sys

from mao.commands.common import open_context
from mao.errors import FactoryError
from mao.governance.epochs import EpochStore
from mao.mcp import McpServer, Session


def register(sub: argparse._SubParsersAction) -> None:
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--role", required=True, choices=("foreman", "reviewer"))
    mcp.set_defaults(func=cmd_mcp)


def cmd_mcp(args: argparse.Namespace) -> int:
    actor = os.environ.get("FACTORY_ACTOR")
    if not actor:
        print("缺少 FACTORY_ACTOR", file=sys.stderr)
        return 2
    with open_context(args) as ctx:
        try:
            epochs = EpochStore(ctx)
        except FactoryError as exc:
            if exc.code == "REGISTRY_NOT_CONFIGURED":
                print(f"{exc.code}: {exc.message}", file=sys.stderr)
                return 4
            raise
        McpServer(ctx, epochs, Session(role=args.role, actor=actor)).serve_stdio(sys.stdin, sys.stdout)
    return 0
