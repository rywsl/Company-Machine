"""bean 组命令。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mao.base import canonical_json
from mao.commands.common import finish, human_by, open_context, open_snapshot, split_pair
from mao.gate import Gate


def register(sub: argparse._SubParsersAction) -> None:
    bean = sub.add_parser("bean")
    commands = bean.add_subparsers(dest="bean_cmd", required=True)
    imported = commands.add_parser("import")
    imported.add_argument("--type", required=True)
    imported.add_argument("--file", action="append", required=True)
    imported.set_defaults(func=cmd_import)
    lineage = commands.add_parser("lineage")
    lineage.add_argument("bean")
    lineage.add_argument("--direction", required=True, choices=("up", "down"))
    lineage.set_defaults(func=cmd_lineage)
    label = commands.add_parser("label")
    label.add_argument("bean")
    label.add_argument("pair")
    label.set_defaults(func=cmd_label)


def cmd_import(args: argparse.Namespace) -> int:
    files: dict[str, Path] = {}
    for item in args.file:
        rel, host = split_pair(item, "--file 必须是 PATH=HOST")
        files[rel] = Path(host)
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        gate = Gate(
            snapshot, ctx.warehouse, ctx.provider, ctx.clock, ctx.ids, workdir=ctx.root / "tmp" / "gate"
        )
        report, bean = gate.admit_external(args.type, files, {})
        if report.verdict != "pass" or bean is None:
            print(report.feedback or report.verdict, file=sys.stderr)
            return 1
        ctx.warehouse.seal_many([(bean.id, report.facts.get(bean.id, {}))], None, report.digest())
        if args.json:
            print(canonical_json({"id": bean.id}))
        else:
            print(bean.id)
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        payload = ctx.warehouse.lineage(args.bean, args.direction)
    print(canonical_json(payload))
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    key, value = split_pair(args.pair, "标签必须是 KEY=VALUE")
    with open_context(args) as ctx:
        ctx.warehouse.label(args.bean, key, value, by=human_by())
    return finish(args, None) if args.json else 0
