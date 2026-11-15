"""agent 组命令。"""

from __future__ import annotations

import argparse
import os
from typing import Any

from mao.agents import AgentHost, AgentsConfig, Mailbox, agent_names, agent_status
from mao.commands.common import finish, open_context, open_snapshot
from mao.errors import FactoryError


def register(sub: argparse._SubParsersAction) -> None:
    agent = sub.add_parser("agent")
    commands = agent.add_subparsers(dest="agent_cmd", required=True)
    listed = commands.add_parser("list")
    listed.set_defaults(func=cmd_list)
    show = commands.add_parser("show")
    show.add_argument("name")
    show.set_defaults(func=cmd_show)
    send = commands.add_parser("send")
    send.add_argument("name")
    send.add_argument("--text", required=True)
    send.set_defaults(func=cmd_send)
    pause = commands.add_parser("pause")
    pause.add_argument("name")
    pause.set_defaults(func=cmd_pause)
    resume = commands.add_parser("resume")
    resume.add_argument("name")
    resume.set_defaults(func=cmd_resume)
    turns = commands.add_parser("turns")
    turns.add_argument("name")
    turns.set_defaults(func=cmd_turns)
    rotate = commands.add_parser("rotate")
    rotate.add_argument("name")
    rotate.set_defaults(func=cmd_rotate)


def _host(ctx: Any, args: argparse.Namespace) -> AgentHost:
    from mao.governance.epochs import EpochStore

    return AgentHost(ctx, EpochStore(ctx), AgentsConfig.load(ctx.root))


def cmd_list(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        body = [agent_status(ctx, name) for name in agent_names(snapshot, ctx.root)]
    return finish(args, body)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        if args.name not in agent_names(snapshot, ctx.root):
            raise FactoryError("AGENT_NOT_FOUND", "没有这个 Agent", agent=args.name)
        body = agent_status(ctx, args.name)
    return finish(args, body)


def cmd_pause(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        host = _host(ctx, args)
        host.pause(args.name)
        body = agent_status(ctx, args.name)
    return finish(args, body)


def cmd_resume(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        host = _host(ctx, args)
        host.resume(args.name)
        body = agent_status(ctx, args.name)
    return finish(args, body)


def cmd_rotate(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        host = _host(ctx, args)
        host.rotate(args.name)
        body = agent_status(ctx, args.name)
    return finish(args, body)


def cmd_turns(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        host = _host(ctx, args)
        body = [item.to_json() for item in host.turns(args.name)]
    return finish(args, body)


def cmd_send(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        snapshot = open_snapshot(ctx, args)
        if args.name not in agent_names(snapshot, ctx.root):
            raise FactoryError("AGENT_NOT_FOUND", "没有这个 Agent", agent=args.name)
        actor = os.environ.get("FACTORY_ACTOR") or "human:cli"
        if not actor.startswith("human:"):
            actor = "human:" + actor
        message = Mailbox(ctx).post(args.name, "message", actor, {"text": args.text})
    return finish(args, message.to_json())
