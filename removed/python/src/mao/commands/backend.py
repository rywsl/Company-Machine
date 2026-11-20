"""backend 组命令。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mao.backends import migrate_local_to
from mao.commands.common import finish, open_context
from mao.errors import FactoryError


def register(sub: argparse._SubParsersAction) -> None:
    backend = sub.add_parser("backend")
    commands = backend.add_subparsers(dest="backend_cmd", required=True)
    status = commands.add_parser("status")
    status.set_defaults(func=cmd_status)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--from", dest="source", required=True)
    migrate.add_argument("--to", dest="target", required=True)
    migrate.add_argument("--dry-run", action="store_true")
    migrate.set_defaults(func=cmd_migrate)
    verify = commands.add_parser("verify")
    verify.set_defaults(func=cmd_verify)


def cmd_status(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        path = ctx.root / "backend.json"
        if path.is_file():
            body = json.loads(path.read_text(encoding="utf-8"))
        else:
            body = {"kind": "local"}
    return finish(args, body)


def cmd_migrate(args: argparse.Namespace) -> int:
    root = Path(args.root) if getattr(args, "root", None) else Path(os.environ.get("FACTORY_ROOT", "."))
    if args.dry_run:
        json.loads(Path(args.target).read_text(encoding="utf-8"))
        return finish(args, {"dry_run": True})
    try:
        migrate_local_to(root, Path(args.target))
    except FactoryError as exc:
        if exc.code == "MIGRATE_VERIFY_FAILED":
            return 1
        raise
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        path = ctx.root / "backend-active.json"
        body = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"ok": True}
    return finish(args, body)
