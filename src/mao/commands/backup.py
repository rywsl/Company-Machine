"""factory backup。"""

from __future__ import annotations

import argparse
from pathlib import Path

from mao.backup import create_backup, restore_backup, verify_backup
from mao.commands.common import finish, human_by, open_context


def register(sub: argparse._SubParsersAction) -> None:
    backup = sub.add_parser("backup")
    commands = backup.add_subparsers(dest="backup_cmd", required=True)
    create = commands.add_parser("create")
    create.add_argument("dest")
    create.add_argument("--with-logs", action="store_true")
    create.set_defaults(func=cmd_create)
    verify = commands.add_parser("verify")
    verify.add_argument("dest")
    verify.set_defaults(func=cmd_verify)
    restore = commands.add_parser("restore")
    restore.add_argument("dest")
    restore.add_argument("restore_root", metavar="ROOT")
    restore.add_argument("--registry-dest", default=None)
    restore.set_defaults(func=cmd_restore)


def cmd_create(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        manifest = create_backup(ctx, Path(args.dest), with_logs=args.with_logs, by=human_by())
    return finish(args, manifest)


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_backup(Path(args.dest))
    return finish(args, report, code=0 if report["ok"] else 1)


def cmd_restore(args: argparse.Namespace) -> int:
    registry = None if args.registry_dest is None else Path(args.registry_dest)
    report = restore_backup(Path(args.dest), Path(args.restore_root), registry_dest=registry)
    return finish(args, report)
