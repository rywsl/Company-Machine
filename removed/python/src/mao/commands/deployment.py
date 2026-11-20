"""factory doctor / factory init。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mao.commands.common import finish
from mao.deployment import apply_deployment, diagnose, plan_deployment
from mao.errors import FactoryError


def register(sub: argparse._SubParsersAction) -> None:
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--profile", required=True, choices=("resident", "local-test"))
    doctor.add_argument("--manifest", default=None)
    doctor.set_defaults(func=cmd_doctor)
    init = sub.add_parser("init")
    init.add_argument("--manifest", required=True)
    mode = init.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    init.add_argument("--plan-digest", default=None)
    init.set_defaults(func=cmd_init)


def cmd_doctor(args: argparse.Namespace) -> int:
    manifest = _load(args.manifest) if args.manifest else None
    report = diagnose(Path(args.root or "."), profile=args.profile, manifest=manifest)
    return finish(args, report)


def cmd_init(args: argparse.Namespace) -> int:
    manifest = _load(args.manifest)
    root = Path(args.root or ".")
    if args.plan:
        return finish(args, plan_deployment(root, manifest))
    if not args.plan_digest:
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "apply 需要 plan digest", field="plan_digest")
    return finish(args, apply_deployment(root, manifest, plan_digest=args.plan_digest))


def _load(path: str) -> dict:
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise FactoryError("DEPLOY_MANIFEST_INVALID", "manifest 必须是对象", field="manifest")
    return body
