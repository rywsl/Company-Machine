"""job 组与 M1 的单工位 run。"""

from __future__ import annotations

import argparse
import sys

from mao.base import canonical_json
from mao.cache import CACHEABLE_ROLES, JobCache
from mao.commands.common import (
    coerce_params,
    finish,
    governance_registry,
    open_context,
    open_snapshot,
    registry_path,
    split_pair,
)
from mao.errors import FactoryError
from mao.jobs import dump_job, load_job
from mao.runner import open_runner
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
    single.add_argument("--param", action="append", default=[])
    single.set_defaults(func=cmd_station_run)


def cmd_station_run(args: argparse.Namespace) -> int:
    if not args.station:
        print("缺少工位", file=sys.stderr)
        return 2
    inputs = dict(split_pair(item, "--input 必须是 PORT=BEAN") for item in args.input)
    pairs = [split_pair(item, "--param 必须是 NAME=VALUE") for item in args.param]
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        station_key = snapshot.key("Station", args.station)
        params = coerce_params(snapshot, station_key, pairs)
        runner = open_runner(ctx, snapshot)
        done, cache_hit = _run_station(
            ctx, snapshot, runner, station_key, inputs, params, cache=args.cache, variant=args.variant
        )
    body = {"job": dump_job(done), "cache_hit": cache_hit}
    return finish(args, body, code=0 if done.state.value == "SEALED" else 1)


def _run_station(
    ctx: object,
    snapshot: object,
    runner: object,
    station_key: str,
    inputs: dict[str, str],
    params: dict,
    *,
    cache: bool,
    variant: str | None,
) -> tuple[object, bool]:
    role = str(snapshot.spec(station_key).get("role") or "")
    if role in CACHEABLE_ROLES:
        store = JobCache(ctx)
        material = store.material(snapshot, station_key, inputs, params, variant=variant)
        if cache:
            hit = store.lookup(material.key())
            if hit is not None:
                return hit, True
        job = runner.submit(station_key, inputs, params)
        store.bind(job.id, material)
        return runner.run(job.id), False
    job = runner.submit(station_key, inputs, params)
    return runner.run(job.id), False


def cmd_run(args: argparse.Namespace) -> int:
    inputs = dict(split_pair(item, "--input 必须是 PORT=BEAN") for item in args.input)
    with open_context(args) as ctx:
        snapshot = Snapshot.load(registry_path(args))
        runner = open_runner(ctx, snapshot)
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
        runner = open_runner(ctx, snapshot)
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
