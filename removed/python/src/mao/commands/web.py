"""factory web 命令。"""

from __future__ import annotations

import argparse
import json

from mao.commands.common import open_context, open_snapshot
from mao.webapp import DEFAULT_PORT, WebServer, serve_until_signal


def register(sub: argparse._SubParsersAction) -> None:
    web = sub.add_parser("web")
    web.add_argument("--port", type=int, default=None)
    web.set_defaults(func=cmd_web)


def cmd_web(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        server = WebServer(ctx, snapshot=snapshot)
        port = args.port if args.port is not None else _configured_port(ctx.root)
        actual = server.start(port)
        print(f"http://127.0.0.1:{actual}/", flush=True)
        serve_until_signal(server)
    return 0


def _configured_port(root: object) -> int:
    from pathlib import Path

    path = Path(root) / "factory.json"
    if not path.is_file():
        return DEFAULT_PORT
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_PORT
    web = body.get("web") if isinstance(body, dict) else None
    if not isinstance(web, dict) or "port" not in web:
        return DEFAULT_PORT
    port = web["port"]
    if isinstance(port, bool) or not isinstance(port, int) or port < 0 or port > 65535:
        raise ValueError("web.port 不合法")
    return port
