"""line 组命令。"""

from __future__ import annotations

import argparse

from mao.base import canonical_json
from mao.commands.common import finish, human_by, open_context, open_snapshot, split_pair
from mao.runner import open_runner
from mao.runplan import compile_run_plan
from mao.runs import InlineExecutor, LineRunner, open_line_runner


def register(sub: argparse._SubParsersAction) -> None:
    line = sub.add_parser("line")
    commands = line.add_subparsers(dest="line_cmd", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("line")
    plan.set_defaults(func=cmd_plan)
    run = commands.add_parser("run")
    run.add_argument("line")
    run.add_argument("--in", dest="input", action="append", default=[])
    run.add_argument("--budget", default=None)
    run.add_argument("--variant", action="append", default=[])
    run.add_argument("--no-cache", action="store_true")
    run.set_defaults(func=cmd_run)
    listed = commands.add_parser("ls")
    listed.add_argument("--state", default=None)
    listed.set_defaults(func=cmd_ls)
    show = commands.add_parser("show")
    show.add_argument("run")
    show.set_defaults(func=cmd_show)
    nodes = commands.add_parser("nodes")
    nodes.add_argument("run")
    nodes.set_defaults(func=cmd_nodes)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("run")
    cancel.set_defaults(func=cmd_cancel)
    retry = commands.add_parser("retry")
    retry.add_argument("run")
    retry.add_argument("node")
    retry.set_defaults(func=cmd_retry)
    rebuild = commands.add_parser("rebuild")
    rebuild.add_argument("run")
    rebuild.set_defaults(func=cmd_rebuild)


def cmd_plan(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        plan = compile_run_plan(snapshot, args.line)
    return finish(args, plan.to_json())


def cmd_run(args: argparse.Namespace) -> int:
    inputs = dict(split_pair(item, "--in 必须是 PORT=BEAN") for item in args.input)
    variants = dict(split_pair(item, "--variant 必须是 STEP=V") for item in args.variant)
    budget = None if args.budget is None else float(args.budget)
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args, route_key=args.line + ":" + canonical_json(inputs))
        runner = open_runner(ctx, snapshot)
        lines = open_line_runner(ctx, snapshot, runner)
        started = lines.start_run(
            args.line,
            inputs,
            budget_usd=budget,
            variants=variants,
            use_cache=not args.no_cache,
            trigger={"on": "manual", "by": human_by()},
        )
        done = lines.run_to_completion(started.id, InlineExecutor(runner))
    code = 1 if done.state.value in {"FAILED", "CANCELLED"} else 0
    return finish(args, done, code=code)


def _lines(ctx: object, args: argparse.Namespace, epoch: str | None = None) -> LineRunner:
    snapshot = open_snapshot(ctx, args, epoch=epoch)
    return open_line_runner(ctx, snapshot)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _lines(ctx, args).list(state=args.state)
    return finish(args, found)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        run = _lines(ctx, args).get(args.run)
        if run.epoch:
            run = _lines(ctx, args, run.epoch).get(args.run)
    return finish(args, run)


def cmd_nodes(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        lines = _lines(ctx, args)
        run = lines.get(args.run)
        nodes = _lines(ctx, args, run.epoch).nodes(args.run)
    return finish(args, nodes)


def cmd_cancel(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        done = _lines(ctx, args).cancel(args.run, by=human_by())
    return finish(args, done)


def cmd_retry(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        lines = _lines(ctx, args)
        run = lines.get(args.run)
        done = _lines(ctx, args, run.epoch).retry_node(args.run, args.node, by=human_by())
    return finish(args, done)


def cmd_rebuild(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        done = _lines(ctx, args).rebuild(args.run, by=human_by())
    return finish(args, done)
