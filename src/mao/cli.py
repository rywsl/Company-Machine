"""factory 命令行。退出码见 spec/system/cli.md。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import mao
from mao.base import canonical_json
from mao.context import Context
from mao.errors import FactoryError
from mao.gate import Gate
from mao.jobs import dump_job, load_job
from mao.runner import Runner
from mao.sandbox import DockerProvider, LocalProvider
from mao.snapshot import Snapshot

_FACILITY = {"DOCKER_UNAVAILABLE", "DOCKER_FAILED", "IMAGE_NOT_FOUND"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory")
    parser.add_argument("--version", action="version", version=mao.__version__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--registry", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--provider", choices=("docker", "local"), default="docker")
    parser.add_argument("--local-commands", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.add_argument("root")

    bean = sub.add_parser("bean")
    bean_sub = bean.add_subparsers(dest="bean_cmd", required=True)
    imported = bean_sub.add_parser("import")
    imported.add_argument("--type", required=True)
    imported.add_argument("--file", action="append", required=True)
    lineage = bean_sub.add_parser("lineage")
    lineage.add_argument("bean")
    lineage.add_argument("--direction", required=True, choices=("up", "down"))

    single = sub.add_parser("run")
    single.add_argument("station", nargs="?")
    single.add_argument("--cache", action="store_true")
    single.add_argument("--variant", default=None)
    single.add_argument("--input", action="append", default=[])

    job = sub.add_parser("job")
    job_sub = job.add_subparsers(dest="job_cmd", required=True)
    run = job_sub.add_parser("run")
    run.add_argument("station")
    run.add_argument("--input", action="append", default=[])
    show = job_sub.add_parser("show")
    show.add_argument("job")

    backend = sub.add_parser("backend")
    backend_sub = backend.add_subparsers(dest="backend_cmd", required=True)
    migrate = backend_sub.add_parser("migrate")
    migrate.add_argument("--from", dest="source", required=True)
    migrate.add_argument("--to", dest="target", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)
    try:
        if args.local_commands and args.provider != "local":
            return _usage(args, argv, "--local-commands 只能与 --provider local 一起使用")
        if args.command == "check":
            return _check(args)
        if args.command == "bean" and args.bean_cmd == "import":
            return _import(args)
        if args.command == "bean" and args.bean_cmd == "lineage":
            return _lineage(args)
        if args.command == "job" and args.job_cmd == "run":
            return _run(args)
        if args.command == "job" and args.job_cmd == "show":
            return _show(args)
        if args.command == "backend" and args.backend_cmd == "migrate":
            return _backend_migrate(args)
        return _usage(args, argv, "未知子命令")
    except FactoryError as exc:
        return _factory_error(exc)
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 5


def _backend_migrate(args: argparse.Namespace) -> int:
    from mao.backends import migrate_local_to

    root = Path(os.environ.get("FACTORY_ROOT", args.source))
    try:
        migrate_local_to(root, Path(args.target))
    except FactoryError as exc:
        if exc.code == "MIGRATE_VERIFY_FAILED":
            return 1
        raise
    return 0


def _check(args: argparse.Namespace) -> int:
    Snapshot.load(Path(args.root))
    return 0


def _import(args: argparse.Namespace) -> int:
    files: dict[str, Path] = {}
    for item in args.file:
        if "=" not in item:
            return _usage(args, [], "--file 必须是 PATH=HOST")
        rel, host = item.split("=", 1)
        files[rel] = Path(host)
    with _open(args) as ctx:
        snapshot = Snapshot.load(_registry(args))
        gate = Gate(
            snapshot,
            ctx.warehouse,
            ctx.provider,
            ctx.clock,
            ctx.ids,
            workdir=ctx.root / "tmp" / "gate",
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


def _run(args: argparse.Namespace) -> int:
    inputs: dict[str, str] = {}
    for item in args.input:
        if "=" not in item:
            return _usage(args, [], "--input 必须是 PORT=BEAN")
        port, bean_id = item.split("=", 1)
        inputs[port] = bean_id
    with _open(args) as ctx:
        snapshot = Snapshot.load(_registry(args))
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


def _show(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        job = load_job(ctx.db, args.job)
    print(canonical_json(dump_job(job)))
    return 0


def _lineage(args: argparse.Namespace) -> int:
    with _open(args) as ctx:
        payload = ctx.warehouse.lineage(args.bean, args.direction)
    print(canonical_json(payload))
    return 0


def _open(args: argparse.Namespace) -> Context:
    root = Path(args.root or os.environ.get("FACTORY_ROOT") or ".factory")
    if args.provider == "local":
        commands = _commands(args.local_commands) if args.local_commands else {}
        provider: Any = LocalProvider(root, commands=commands)
    else:
        provider = DockerProvider(root)
    return Context.open(root, provider=provider)


def _registry(args: argparse.Namespace) -> Path:
    return Path(args.registry or os.environ.get("FACTORY_REGISTRY") or "registry")


def _commands(path: str) -> dict[str, list[str]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("CLI_USAGE", "local-commands 不是 JSON 对象", path=path) from exc
    if not isinstance(data, dict):
        raise FactoryError("CLI_USAGE", "local-commands 不是 JSON 对象", path=path)
    commands: dict[str, list[str]] = {}
    for key, value in data.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise FactoryError("CLI_USAGE", "local-commands 的值必须是字符串列表", path=path)
        commands[key] = list(value)
    return commands


def _usage(args: argparse.Namespace, argv: list[str], message: str) -> int:
    if getattr(args, "json", False):
        print(canonical_json({"code": "CLI_USAGE", "message": message, "detail": {"argv": argv}}))
    else:
        print(message, file=sys.stderr)
    return 2


def _factory_error(exc: FactoryError) -> int:
    print(f"{exc.code}: {exc.message}", file=sys.stderr)
    if exc.code == "REGISTRY_INVALID":
        return 3
    if exc.code in _FACILITY:
        return 4
    if exc.code == "CLI_USAGE":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
