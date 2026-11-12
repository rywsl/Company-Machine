"""contract 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context, open_snapshot, split_pair
from mao.contract import ContractRunner
from mao.runner import Runner


def register(sub: argparse._SubParsersAction) -> None:
    contract = sub.add_parser("contract")
    commands = contract.add_subparsers(dest="contract_cmd", required=True)
    validator = commands.add_parser("validator")
    validator.add_argument("type")
    validator.add_argument("validator")
    validator.add_argument("--timeout", required=True, type=float)
    validator.set_defaults(func=cmd_validator)
    golden = commands.add_parser("golden")
    golden.add_argument("golden")
    golden.add_argument("--baseline", default=None)
    golden.add_argument("--timeout", required=True, type=float)
    golden.set_defaults(func=cmd_golden)
    equivalence = commands.add_parser("equivalence")
    equivalence.add_argument("station")
    equivalence.add_argument("--timeout", default=None, type=float)
    equivalence.set_defaults(func=cmd_equivalence)
    diff = commands.add_parser("diff")
    diff.add_argument("station")
    diff.add_argument("--timeout", default=None, type=float)
    diff.set_defaults(func=cmd_diff)
    sink = commands.add_parser("sink-dry-run")
    sink.add_argument("station")
    sink.add_argument("--in", dest="input", action="append", default=[])
    sink.add_argument("--param", action="append", default=[])
    sink.add_argument("--timeout", default=None, type=float)
    sink.set_defaults(func=cmd_sink)
    listed = commands.add_parser("ls")
    listed.add_argument("--kind", default=None)
    listed.set_defaults(func=cmd_ls)


def _runner(ctx: object, args: argparse.Namespace) -> ContractRunner:
    snapshot = open_snapshot(ctx, args)
    return ContractRunner(ctx, snapshot, Runner(ctx, snapshot))


def _code(result: object) -> int:
    verdict = getattr(result, "verdict", None)
    value = verdict.value if hasattr(verdict, "value") else verdict
    return 1 if value == "fail" else 0


def cmd_validator(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        result = _runner(ctx, args).run_validator(args.type, args.validator, timeout_s=args.timeout)
    return finish(args, result, code=_code(result))


def cmd_golden(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        result = _runner(ctx, args).run_golden(args.golden, args.baseline, timeout_s=args.timeout)
    return finish(args, result, code=_code(result))


def cmd_equivalence(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        result = _runner(ctx, args).run_equivalence(args.station, timeout_s=args.timeout)
    return finish(args, result, code=_code(result))


def cmd_diff(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        result = _runner(ctx, args).run_diff(args.station, timeout_s=args.timeout)
    return finish(args, result, code=_code(result))


def cmd_sink(args: argparse.Namespace) -> int:
    inputs = dict(split_pair(item, "--in 必须是 PORT=BEAN") for item in args.input)
    params = dict(split_pair(item, "--param 必须是 K=V") for item in args.param)
    with open_context(args) as ctx:
        result = _runner(ctx, args).run_sink_dry_run(
            args.station, inputs=inputs, params=params, timeout_s=args.timeout
        )
    return finish(args, result, code=_code(result))


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _runner(ctx, args).list(kind=args.kind)
    return finish(args, found)
