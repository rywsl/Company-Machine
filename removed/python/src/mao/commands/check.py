"""check 命令。"""

from __future__ import annotations

import argparse
from pathlib import Path

from mao.snapshot import Snapshot


def register(sub: argparse._SubParsersAction) -> None:
    check = sub.add_parser("check")
    check.add_argument("check_root", metavar="ROOT")
    check.set_defaults(func=cmd_check)


def cmd_check(args: argparse.Namespace) -> int:
    Snapshot.load(Path(args.check_root))
    return 0
