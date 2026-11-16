"""factory package。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mao.commands.common import finish, open_context
from mao.errors import FactoryError
from mao.packages import create_checkpoint, export_package, import_package, restore_checkpoint, verify_package


def register(sub: argparse._SubParsersAction) -> None:
    package = sub.add_parser("package")
    commands = package.add_subparsers(dest="package_cmd", required=True)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("dest")
    checkpoint.add_argument("--factory", required=True)
    checkpoint.add_argument("--by", required=True)
    checkpoint.add_argument("--timeout", type=float, default=120.0)
    checkpoint.set_defaults(func=cmd_checkpoint)
    restore = commands.add_parser("restore")
    restore.add_argument("src")
    restore.add_argument("root", metavar="ROOT")
    restore.add_argument("--registry-dest", default=None)
    restore.add_argument("--timeout", type=float, default=120.0)
    restore.set_defaults(func=cmd_restore)
    export = commands.add_parser("export")
    export.add_argument("checkpoint")
    export.add_argument("dest")
    export.add_argument("--release", required=True)
    export.add_argument("--by", required=True)
    export.add_argument("--timeout", type=float, default=120.0)
    export.set_defaults(func=cmd_export)
    verify = commands.add_parser("verify")
    verify.add_argument("src")
    verify.add_argument("--timeout", type=float, default=120.0)
    verify.set_defaults(func=cmd_verify)
    imported = commands.add_parser("import")
    imported.add_argument("src")
    imported.add_argument("root", metavar="ROOT")
    imported.add_argument("--bindings", required=True)
    imported.add_argument("--by", required=True)
    imported.add_argument("--timeout", type=float, default=120.0)
    imported.set_defaults(func=cmd_import)


def cmd_checkpoint(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        manifest = create_checkpoint(
            ctx, Path(args.dest), factory_asset=args.factory, by=args.by, timeout_s=args.timeout
        )
    return finish(args, manifest)


def cmd_restore(args: argparse.Namespace) -> int:
    registry = None if args.registry_dest is None else Path(args.registry_dest)
    report = restore_checkpoint(
        Path(args.src), Path(args.root), registry_dest=registry, timeout_s=args.timeout
    )
    return finish(args, report)


def cmd_export(args: argparse.Namespace) -> int:
    release = _object(args.release)
    manifest = export_package(
        Path(args.checkpoint),
        Path(args.dest),
        release=release,
        by=args.by,
        timeout_s=args.timeout,
    )
    return finish(args, manifest)


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_package(Path(args.src), timeout_s=args.timeout)
    return finish(args, report, code=0 if report["ok"] else 1)


def cmd_import(args: argparse.Namespace) -> int:
    bindings = _object(args.bindings)
    report = import_package(
        Path(args.src), Path(args.root), bindings=bindings, by=args.by, timeout_s=args.timeout
    )
    return finish(args, report)


def _object(path: str) -> dict:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("CLI_USAGE", "JSON 文件无法读取", path=path) from exc
    if not isinstance(body, dict):
        raise FactoryError("CLI_USAGE", "JSON 文件必须是对象", path=path)
    return body
