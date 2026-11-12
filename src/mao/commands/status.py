"""status、metrics、events 命令。"""

from __future__ import annotations

import argparse
from typing import Any

from mao.commands.common import finish, governance_registry, open_context
from mao.errors import FactoryError
from mao.status import factory_status, render_metrics, tail_events


def register(sub: argparse._SubParsersAction) -> None:
    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)
    metrics = sub.add_parser("metrics")
    metrics.set_defaults(func=cmd_metrics)
    events = sub.add_parser("events")
    events.add_argument("--since", default=None)
    events.add_argument("--kind", default=None)
    events.add_argument("--limit", type=int, default=100)
    events.set_defaults(func=cmd_events)


def _bound(ctx: Any) -> tuple[Any, Any]:
    if governance_registry(ctx) is None:
        return None, None
    from mao.governance.changesets import ChangePipeline
    from mao.governance.epochs import EpochStore

    epochs = EpochStore(ctx)
    return epochs, ChangePipeline(ctx, epochs)


def cmd_status(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        epochs, pipeline = _bound(ctx)
        status = factory_status(ctx, epochs=epochs, pipeline=pipeline)
    error = any(item["severity"] == "error" for item in status["alerts"])
    return finish(args, status, code=1 if error else 0)


def cmd_metrics(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        epochs, pipeline = _bound(ctx)
        text = render_metrics(ctx, epochs=epochs, pipeline=pipeline)
    print(text, end="")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    if args.limit < 1 or args.limit > 10000:
        raise FactoryError("CLI_USAGE", "limit 超出范围", field="limit")
    with open_context(args) as ctx:
        try:
            rows = tail_events(ctx, since=args.since, kind=args.kind, limit=args.limit)
        except FactoryError as exc:
            if exc.code == "EVENTS_QUERY_INVALID":
                raise FactoryError("CLI_USAGE", exc.message, field=exc.detail.get("field")) from exc
            raise
    return finish(args, rows)
