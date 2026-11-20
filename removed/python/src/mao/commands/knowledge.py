"""factory knowledge。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mao.commands.common import finish, open_context
from mao.errors import FactoryError
from mao.governance.epochs import EpochStore
from mao.learning import LearningStore


def register(sub: argparse._SubParsersAction) -> None:
    knowledge = sub.add_parser("knowledge")
    commands = knowledge.add_subparsers(dest="knowledge_cmd", required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("asset_id")
    authorize.add_argument("--by", required=True)
    authorize.set_defaults(func=cmd_authorize)
    trial = commands.add_parser("trial")
    trial.add_argument("target")
    trial.add_argument("--contract", required=True)
    trial.add_argument("--purpose", required=True, choices=("knowledge", "takeover", "release"))
    trial.add_argument("--by", required=True)
    trial.set_defaults(func=cmd_trial)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("trial")
    evaluate.add_argument("--evidence", required=True)
    evaluate.add_argument("--by", required=True)
    evaluate.set_defaults(func=cmd_evaluate)
    show = commands.add_parser("show")
    show.add_argument("trial")
    show.set_defaults(func=cmd_show)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("trial")
    cancel.add_argument("--reason", required=True)
    cancel.add_argument("--by", required=True)
    cancel.set_defaults(func=cmd_cancel)
    promote = commands.add_parser("promote")
    promote.add_argument("target")
    mode = promote.add_mutually_exclusive_group(required=True)
    mode.add_argument("--trial")
    mode.add_argument("--directive", action="store_true")
    promote.add_argument("--expect", required=True)
    promote.add_argument("--by", required=True)
    promote.set_defaults(func=cmd_promote)
    invalidate = commands.add_parser("invalidate")
    invalidate.add_argument("target")
    invalidate.add_argument("--reason", required=True)
    invalidate.add_argument("--by", required=True)
    invalidate.set_defaults(func=cmd_invalidate)
    heads = commands.add_parser("heads")
    heads.add_argument("subject")
    heads.set_defaults(func=cmd_heads)


def cmd_authorize(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = _store(ctx).authorize(args.asset_id, by=args.by)
    return finish(args, record)


def cmd_trial(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = _store(ctx).open_trial(args.target, args.contract, purpose=args.purpose, by=args.by)
    return finish(args, record)


def cmd_evaluate(args: argparse.Namespace) -> int:
    evidence = _object(args.evidence)
    with open_context(args) as ctx:
        record = _store(ctx).evaluate(args.trial, evidence, by=args.by)
    code = 1 if record.get("state") == "failed" else 0
    return finish(args, record, code=code)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = _store(ctx).get_trial(args.trial)
    return finish(args, record)


def cmd_cancel(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = _store(ctx).cancel_trial(args.trial, reason=args.reason, by=args.by)
    return finish(args, record)


def cmd_promote(args: argparse.Namespace) -> int:
    trial_id = None if args.directive else args.trial
    expected = None if args.expect == "none" else args.expect
    with open_context(args) as ctx:
        record = _store(ctx).promote(args.target, trial_id=trial_id, expected=expected, by=args.by)
    return finish(args, record)


def cmd_invalidate(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = _store(ctx).invalidate(args.target, reason=args.reason, by=args.by)
    return finish(args, record)


def cmd_heads(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        records = _store(ctx).heads(args.subject)
    return finish(args, records)


def _store(ctx: object) -> LearningStore:
    return LearningStore(ctx, EpochStore(ctx))


def _object(path: str) -> dict:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("CLI_USAGE", "JSON 文件无法读取", path=path) from exc
    if not isinstance(body, dict):
        raise FactoryError("CLI_USAGE", "JSON 文件必须是对象", path=path)
    return body
