"""gateway 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, open_context, open_snapshot
from mao.gateway import mcp_manifest, openapi_to_tools


def register(sub: argparse._SubParsersAction) -> None:
    gateway = sub.add_parser("gateway")
    commands = gateway.add_subparsers(dest="gateway_cmd", required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("machine")
    manifest.set_defaults(func=cmd_manifest)


def cmd_manifest(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        machine = snapshot.spec(snapshot.key("Machine", args.machine))
        document = machine.get("openapi") or {"paths": {}}
        tools = openapi_to_tools(document, allow=None)
        body = mcp_manifest(tools, fmt="claude-json")
    return finish(args, body)
