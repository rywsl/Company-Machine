"""receipt 组命令。"""

from __future__ import annotations

import argparse

from mao.commands.common import finish, governance_registry, open_context, registry_path
from mao.connectors import ReceiptStore, verify
from mao.jobs import load_job
from mao.snapshot import Snapshot


def register(sub: argparse._SubParsersAction) -> None:
    receipt = sub.add_parser("receipt")
    commands = receipt.add_subparsers(dest="receipt_cmd", required=True)
    listed = commands.add_parser("ls")
    listed.add_argument("--station", default=None)
    listed.add_argument("--status", default=None)
    listed.set_defaults(func=cmd_ls)
    show = commands.add_parser("show")
    show.add_argument("receipt")
    show.set_defaults(func=cmd_show)
    check = commands.add_parser("verify")
    check.add_argument("receipt")
    check.set_defaults(func=cmd_verify)
    void = commands.add_parser("void")
    void.add_argument("receipt")
    void.add_argument("--by", required=True)
    void.add_argument("--reason", required=True)
    void.set_defaults(func=cmd_void)


def cmd_ls(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _filter(ReceiptStore(ctx).list(), args.station, args.status)
    return finish(args, found)


def cmd_show(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        receipt = ReceiptStore(ctx).get(args.receipt)
    return finish(args, receipt)


def cmd_verify(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        store = ReceiptStore(ctx)
        receipt = store.get(args.receipt)
        job = load_job(ctx.db, receipt.job_id)
        snapshot = _snapshot_for_job(ctx, args, job.epoch)
        ok, detail = verify(ctx, snapshot, receipt)
        settled = store.settle(receipt.id, ok=ok, detail=detail)
    code = 0 if settled.status.value == "verified" else 1
    return finish(args, settled, code=code)


def cmd_void(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        receipt = ReceiptStore(ctx).void(args.receipt, by=args.by, reason=args.reason)
    return finish(args, receipt)


def _filter(items: list, station: str | None, status: str | None) -> list:
    found = list(items)
    if station:
        if "@" in station:
            key = f"Station:{station}"
            found = [item for item in found if item.station == key]
        else:
            found = [item for item in found if item.station_name == station]
    if status:
        found = [item for item in found if item.status.value == status]
    found.sort(key=lambda item: (item.created_at, item.id))
    return found


def _snapshot_for_job(ctx: object, args: argparse.Namespace, epoch: str) -> Snapshot:
    if governance_registry(ctx):
        from mao.governance.epochs import EpochStore

        return EpochStore(ctx).materialize(epoch)
    return Snapshot.load(registry_path(args))
