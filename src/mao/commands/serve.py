"""serve 命令。"""

from __future__ import annotations

import argparse
import sys
import threading

from mao.commands.common import GLOBAL, finish, install_stop_signals, open_context
from mao.daemon import Daemon, ProcessExecutor, ServeConfig, run_serve


def register(sub: argparse._SubParsersAction) -> None:
    serve = sub.add_parser("serve", parents=[GLOBAL])
    serve.add_argument("--once", action="store_true")
    serve.set_defaults(func=cmd_serve)


def cmd_serve(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        config = ServeConfig.load(ctx.root)
        prefix = [sys.executable, "-m", "mao", "--root", str(ctx.root), "--provider", args.provider]
        if args.provider == "local":
            if args.local_commands:
                prefix += ["--local-commands", args.local_commands]
            if args.registry:
                prefix += ["--registry", args.registry]
        executor = ProcessExecutor(
            ctx.root,
            prefix,
            max_parallel=config.max_parallel_jobs,
            log_dir=ctx.root / "logs" / "jobs",
        )
        daemon = Daemon(ctx, config, executor=executor, registry=args.registry)
        if args.once:
            daemon.start()
            report = daemon.tick()
            session = None
            if config.supervise:
                from mao.governance.epochs import EpochStore
                from mao.supervisor import ForemanSupervisor, SupervisorConfig

                sup = ForemanSupervisor(
                    ctx,
                    EpochStore(ctx),
                    SupervisorConfig.load(ctx.root),
                    pipeline=daemon.pipeline,
                )
                sup.start()
                record = sup.run_once()
                sup.shutdown()
                session = None if record is None else record.to_json()
            daemon.shutdown(timeout_s=60)
            if config.supervise:
                from mao.base import canonical_json

                if args.json:
                    print(canonical_json({"tick": report.to_json(), "session": session}))
                    return 0
            return finish(args, report)
        stop = threading.Event()
        install_stop_signals(stop)
        run_serve(ctx, config, executor=executor, stop=stop, registry=args.registry)
    return 0
