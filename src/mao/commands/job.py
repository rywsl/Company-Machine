"""job 组与 M1 的单工位 run。"""

from __future__ import annotations

import argparse
import sys

from mao.base import canonical_json
from mao.commands.common import governance_registry, open_context, registry_path, split_pair
from mao.errors import FactoryError
from mao.jobs import dump_job, load_job
from mao.runner import Runner
from mao.snapshot import Snapshot


def register(sub: argparse._SubParsersAction) -> None:
    job = sub.add_parser("job")
    commands = job.add_subparsers(dest="job_cmd", required=True)
    run = commands.add_parser("run")
    run.add_argument("station")
    run.add_argument("--input", action="append", default=[])
    run.set_defaults(func=cmd_run)
    show = commands.add_parser("show")
    show.add_argument("job")
    show.set_defaults(func=cmd_show)
    exec_job = commands.add_parser("exec")
    exec_job.add_argument("job")
    exec_job.set_defaults(func=cmd_exec)
    single = sub.add_parser("run")
    single.add_argument("station", nargs="?")
    single.add_argument("--cache", action="store_true")
    single.add_argument("--variant", default=None)
    single.add_argument("--input", action="append", default=[])
    single.set_defaults(func=cmd_unused)


def cmd_unused(args: argparse.Namespace) -> int:
    del args
    print("未知子命令", file=sys.stderr)
    return 2


def cmd_run(args: argparse.Namespace) -> int:
    inputs = dict(split_pair(item, "--input 必须是 PORT=BEAN") for item in args.input)
    with open_context(args) as ctx:
        snapshot = Snapshot.load(registry_path(args))
        runner = Runner(ctx, snapshot)
        job = runner.run(runner.submit(args.station, inputs, {}).id)
    if args.json:
        print(canonical_json(dump_job(job)))
    elif job.state.value == "SEALED":
        for port in sorted(job.outputs):
            for bean_id in job.outputs[port]:
                print(f"{port}\t{bean_id}")
    else:
        error = job.error or {}
        print(f"{error.get('code', 'DEAD')}: {error.get('message', '')}", file=sys.stderr)
    return 0 if job.state.value == "SEALED" else 1


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        job = load_job(ctx.db, args.job)
    print(canonical_json(dump_job(job)))
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        job = load_job(ctx.db, args.job)
        if governance_registry(ctx):
            from mao.governance.epochs import EpochStore

            snapshot = EpochStore(ctx).materialize(job.epoch)
        else:
            snapshot = Snapshot.load(registry_path(args))
        runner = Runner(ctx, snapshot)
        try:
            done = runner.run(job.id)
        except FactoryError as exc:
            if exc.code != "JOB_LEASED":
                raise
            if args.json:
                print(canonical_json({"error": exc.to_json()}))
            else:
                print(f"{exc.code}: {exc.message}", file=sys.stderr)
            return 1
    if args.json:
        print(canonical_json(dump_job(done)))
    return 0 if done.state.value != "DEAD" else 1
