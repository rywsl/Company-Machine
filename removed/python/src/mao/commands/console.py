"""本机只读控制台命令。"""

from __future__ import annotations

import argparse
import json
import signal
import threading
from pathlib import Path

from mao.commands.common import governance_registry, open_context, open_snapshot
from mao.console import DEFAULT_PORT, TOKEN_FILE, ConsoleServer, load_token
from mao.errors import FactoryError


def register(sub: argparse._SubParsersAction) -> None:
    console = sub.add_parser("console")
    console.add_argument("--port", type=int, default=None)
    console.set_defaults(func=cmd_console)


def cmd_console(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        token = load_token(ctx.root)
        epochs, pipeline = _bound(ctx)
        snapshot = None if epochs is not None else open_snapshot(ctx, args)
        server = ConsoleServer(ctx, token=token, epochs=epochs, pipeline=pipeline, snapshot=snapshot)
        port = args.port if args.port is not None else _configured_port(ctx.root)
        try:
            actual = server.start(port)
        except FactoryError:
            raise
        except OSError as exc:
            raise FactoryError("CONSOLE_BIND_FAILED", "端口绑定失败", port=port) from exc
        print(f"http://127.0.0.1:{actual}/")
        print(ctx.root / TOKEN_FILE)
        _serve_until_signal(server)
    return 0


def _bound(ctx: object) -> tuple[object, object]:
    if governance_registry(ctx) is None:
        return None, None
    from mao.governance.changesets import ChangePipeline
    from mao.governance.epochs import EpochStore

    epochs = EpochStore(ctx)
    return epochs, ChangePipeline(ctx, epochs)


def _configured_port(root: Path) -> int:
    path = Path(root) / "factory.json"
    if not path.is_file():
        return DEFAULT_PORT
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_PORT
    console = body.get("console") if isinstance(body, dict) else None
    if not isinstance(console, dict) or "port" not in console:
        return DEFAULT_PORT
    port = console["port"]
    if isinstance(port, bool) or not isinstance(port, int) or port < 0:
        raise FactoryError("CONSOLE_BIND_FAILED", "console.port 不合法", port=port)
    return port


def _serve_until_signal(server: ConsoleServer) -> None:
    def stop(*_args: object) -> None:
        threading.Thread(target=server.close, daemon=True).start()

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        signal.signal(signal.SIGINT, previous)
        server.close()
