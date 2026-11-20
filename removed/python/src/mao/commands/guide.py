"""factory guide / factory capabilities。"""

from __future__ import annotations

import argparse

from mao.command_help import capabilities, guide
from mao.commands.common import finish


def register(sub: argparse._SubParsersAction) -> None:
    guided = sub.add_parser("guide", description="查看工厂使用步骤")
    guided.add_argument(
        "topic",
        nargs="?",
        default="overview",
        choices=("overview", "setup", "goals", "recovery", "results", "permissions"),
    )
    guided.set_defaults(func=cmd_guide)
    listed = sub.add_parser("capabilities", description="查看已安装命令能力")
    listed.set_defaults(func=cmd_capabilities)


def cmd_guide(args: argparse.Namespace) -> int:
    from mao.cli import build_parser

    return finish(args, guide(args.topic, build_parser()))


def cmd_capabilities(args: argparse.Namespace) -> int:
    from mao.cli import build_parser

    return finish(args, capabilities(build_parser()))
