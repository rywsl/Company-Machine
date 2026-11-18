"""factory objective。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mao.commands.common import finish, open_context
from mao.context import factory_clock
from mao.errors import FactoryError
from mao.governance.epochs import EpochStore
from mao.objectives import ObjectiveStore


def register(sub: argparse._SubParsersAction) -> None:
    objective = sub.add_parser("objective")
    commands = objective.add_subparsers(dest="objective_cmd", required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("asset")
    authorize.add_argument("--grant", required=True)
    authorize.add_argument("--by", required=True)
    authorize.set_defaults(func=cmd_authorize)
    listed = commands.add_parser("ls")
    listed.set_defaults(func=cmd_list)
    shown = commands.add_parser("show")
    shown.add_argument("objective_id")
    shown.set_defaults(func=cmd_show)
    pause = commands.add_parser("pause")
    pause.add_argument("objective_id")
    pause.add_argument("--reason", required=True)
    pause.add_argument("--by", required=True)
    pause.set_defaults(func=cmd_pause)
    resume = commands.add_parser("resume")
    resume.add_argument("objective_id")
    resume.add_argument("--by", required=True)
    resume.set_defaults(func=cmd_resume)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("objective_id")
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--by", required=True)
    revoke.set_defaults(func=cmd_revoke)


def cmd_authorize(args: argparse.Namespace) -> int:
    grant = _object(args.grant)
    with _open(args) as ctx:
        record = _store(ctx).authorize(args.asset, grant, by=args.by)
    return finish(args, record)


def cmd_list(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        records = _store(ctx).list()
    return finish(args, records)


def cmd_show(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        record = _store(ctx).get(args.objective_id)
    return finish(args, record)


def cmd_pause(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        record = _store(ctx).pause(args.objective_id, reason=args.reason, by=args.by)
    return finish(args, record)


def cmd_resume(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        record = _store(ctx).resume(args.objective_id, by=args.by)
    return finish(args, record)


def cmd_revoke(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        record = _store(ctx).revoke(args.objective_id, reason=args.reason, by=args.by)
    return finish(args, record)


def _open(args: argparse.Namespace) -> Any:
    ctx = open_context(args)
    clock = factory_clock(ctx.root)
    if clock is not None and clock is not ctx.clock:
        ctx.clock = clock
    return ctx


def _store(ctx: Any) -> ObjectiveStore:
    return ObjectiveStore(ctx, EpochStore(ctx))


def _object(path: str) -> dict:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("CLI_USAGE", "JSON 文件无法读取", path=path) from exc
    if not isinstance(body, dict):
        raise FactoryError("CLI_USAGE", "JSON 文件必须是对象", path=path)
    return body
