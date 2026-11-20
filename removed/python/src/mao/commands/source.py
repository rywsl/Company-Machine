"""source 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import coerce_params, finish, open_context, open_snapshot, split_pair
from mao.errors import FactoryError
from mao.jobs import dump_job
from mao.runner import open_runner


def register(sub: argparse._SubParsersAction) -> None:
    source = sub.add_parser("source")
    commands = source.add_subparsers(dest="source_cmd", required=True)
    run = commands.add_parser("run")
    run.add_argument("station")
    run.add_argument("--param", action="append", default=[])
    run.set_defaults(func=cmd_run)


def cmd_run(args: argparse.Namespace) -> int:
    pairs = [split_pair(item, "--param 必须是 NAME=VALUE") for item in args.param]
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        station_key = snapshot.key("Station", args.station)
        station = snapshot.spec(station_key)
        role = str(station.get("role") or "")
        if role != "source":
            raise FactoryError(
                "ROLE_MISMATCH",
                "工位角色不是 source",
                station=station_key,
                role=role,
                expected="source",
            )
        params = coerce_params(snapshot, station_key, pairs)
        runner = open_runner(ctx, snapshot)
        done = runner.run(runner.submit(station_key, {}, params).id)
    code = 0 if done.state.value == "SEALED" else 1
    return finish(args, dump_job(done), code=code)
