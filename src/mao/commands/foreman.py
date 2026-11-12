"""foreman 组命令。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mao.commands.common import actor_from_env, finish, open_context
from mao.errors import FactoryError
from mao.foreman import ConversionStore, ForemanQuery, ForemanSubmit, WakeQueue
from mao.governance.changesets import ChangePipeline
from mao.governance.epochs import EpochStore


def register(sub: argparse._SubParsersAction) -> None:
    foreman = sub.add_parser("foreman")
    commands = foreman.add_subparsers(dest="foreman_cmd", required=True)
    nxt = commands.add_parser("next")
    nxt.set_defaults(func=cmd_next)
    done = commands.add_parser("done")
    done.add_argument("wake")
    done.set_defaults(func=cmd_done)
    get = commands.add_parser("get")
    get.add_argument("key")
    get.set_defaults(func=cmd_get)
    listed = commands.add_parser("list")
    listed.add_argument("kind")
    listed.set_defaults(func=cmd_list)
    diff = commands.add_parser("diff")
    diff.add_argument("base")
    diff.add_argument("commit")
    diff.set_defaults(func=cmd_diff)
    bean = commands.add_parser("bean")
    bean_commands = bean.add_subparsers(dest="bean_cmd", required=True)
    bean_get = bean_commands.add_parser("get")
    bean_get.add_argument("bean")
    bean_get.set_defaults(func=cmd_bean_get)
    lineage = bean_commands.add_parser("lineage")
    lineage.add_argument("bean")
    lineage.add_argument("direction", choices=("up", "down"))
    lineage.set_defaults(func=cmd_bean_lineage)
    job = commands.add_parser("job")
    job_commands = job.add_subparsers(dest="job_cmd", required=True)
    job_get = job_commands.add_parser("get")
    job_get.add_argument("job")
    job_get.set_defaults(func=cmd_job_get)
    transcript = job_commands.add_parser("transcript")
    transcript.add_argument("job")
    transcript.set_defaults(func=cmd_transcript)
    dlq = commands.add_parser("dlq")
    dlq_commands = dlq.add_subparsers(dest="dlq_cmd", required=True)
    dlq_ls = dlq_commands.add_parser("ls")
    dlq_ls.set_defaults(func=cmd_dlq)
    drift = commands.add_parser("drift")
    drift_commands = drift.add_subparsers(dest="drift_cmd", required=True)
    drift_ls = drift_commands.add_parser("ls")
    drift_ls.set_defaults(func=cmd_drift)
    invariant = commands.add_parser("invariant")
    invariant_commands = invariant.add_subparsers(dest="invariant_cmd", required=True)
    invariant_ls = invariant_commands.add_parser("ls")
    invariant_ls.set_defaults(func=cmd_invariant)
    metrics = commands.add_parser("metrics")
    metrics.add_argument("station")
    metrics.add_argument("metric")
    metrics.set_defaults(func=cmd_metrics)
    approval = commands.add_parser("approval")
    approval_commands = approval.add_subparsers(dest="approval_cmd", required=True)
    approval_ls = approval_commands.add_parser("ls")
    approval_ls.set_defaults(func=cmd_approval)
    changeset = commands.add_parser("changeset")
    cs_commands = changeset.add_subparsers(dest="changeset_cmd", required=True)
    propose = cs_commands.add_parser("propose")
    propose.add_argument("--doc", required=True)
    propose.add_argument("--commit", required=True)
    propose.set_defaults(func=cmd_propose)
    status = cs_commands.add_parser("status")
    status.add_argument("changeset")
    status.set_defaults(func=cmd_status)
    withdraw = cs_commands.add_parser("withdraw")
    withdraw.add_argument("changeset")
    withdraw.set_defaults(func=cmd_withdraw)
    review = commands.add_parser("review")
    review.add_argument("changeset")
    review.add_argument("--runtime", required=True)
    review.add_argument("--verdict", required=True, choices=("approve", "deny"))
    review.add_argument("--note", default="")
    review.set_defaults(func=cmd_review)
    conversion = commands.add_parser("conversion")
    conversion_commands = conversion.add_subparsers(dest="conversion_cmd", required=True)
    conversion_ls = conversion_commands.add_parser("ls")
    conversion_ls.add_argument("--status", default=None)
    conversion_ls.set_defaults(func=cmd_conversion)
    supervise = commands.add_parser("supervise")
    supervise.add_argument("--once", action="store_true")
    supervise.set_defaults(func=cmd_supervise)
    sessions = commands.add_parser("sessions")
    sessions.add_argument("--state", default=None)
    sessions.set_defaults(func=cmd_sessions)
    wakes = commands.add_parser("wakes")
    wakes.add_argument("--status", default=None, choices=("open", "taken", "done", "failed"))
    wakes.set_defaults(func=cmd_wakes)
    reopen = commands.add_parser("reopen")
    reopen.add_argument("wake")
    reopen.set_defaults(func=cmd_reopen)


def _query(ctx: object) -> ForemanQuery:
    return ForemanQuery(ctx, EpochStore(ctx))


def _submit(ctx: object) -> ForemanSubmit:
    return ForemanSubmit(ctx, ChangePipeline(ctx, EpochStore(ctx)))


def cmd_next(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = WakeQueue(ctx).next(by="cli")
    return finish(args, found, null=True)


def cmd_done(args: argparse.Namespace) -> int:
    by = actor_from_env()
    with open_context(args) as ctx:
        found = WakeQueue(ctx).done(args.wake, by=by)
    return finish(args, found)


def cmd_get(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).registry_get(args.key)
    return finish(args, found)


def cmd_list(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).registry_list(args.kind)
    return finish(args, found)


def cmd_diff(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        query = _query(ctx)
        found = query.registry_diff(args.base, args.commit, timeout_s=query.epochs.timeout_s)
    return finish(args, found)


def cmd_bean_get(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).bean_get(args.bean)
    return finish(args, found)


def cmd_bean_lineage(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).bean_lineage(args.bean, args.direction)
    return finish(args, found)


def cmd_job_get(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).job_get(args.job)
    return finish(args, found)


def cmd_transcript(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        text = _query(ctx).job_transcript(args.job)
    return finish(args, text)


def cmd_dlq(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).dlq_list()
    return finish(args, found)


def cmd_drift(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).drift_list()
    return finish(args, found)


def cmd_invariant(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).invariant_report()
    return finish(args, found)


def cmd_metrics(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).metrics_query(args.station, args.metric)
    return finish(args, found)


def cmd_approval(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _query(ctx).approval_list()
    return finish(args, found)


def cmd_propose(args: argparse.Namespace) -> int:
    by = actor_from_env()
    document = json.loads(Path(args.doc).read_text(encoding="utf-8"))
    with open_context(args) as ctx:
        store = EpochStore(ctx)
        found = _submit(ctx).propose(document, args.commit, by=by, timeout_s=store.timeout_s)
    return finish(args, found)


def cmd_status(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = _submit(ctx).status(args.changeset)
    return finish(args, found)


def cmd_withdraw(args: argparse.Namespace) -> int:
    by = actor_from_env()
    with open_context(args) as ctx:
        found = _submit(ctx).withdraw(args.changeset, by=by)
    return finish(args, found)


def cmd_review(args: argparse.Namespace) -> int:
    by = actor_from_env()
    if args.runtime != by:
        raise FactoryError("CLI_USAGE", "runtime 必须等于 FACTORY_ACTOR")
    with open_context(args) as ctx:
        review_id = _submit(ctx).review(
            args.changeset,
            {"runtime": args.runtime, "verdict": args.verdict, "note": args.note},
            by=by,
        )
    return finish(args, {"id": review_id})


def cmd_supervise(args: argparse.Namespace) -> int:
    from mao.governance.epochs import EpochStore
    from mao.supervisor import ForemanSupervisor, SupervisorConfig

    with open_context(args) as ctx:
        config = SupervisorConfig.load(ctx.root)
        sup = ForemanSupervisor(ctx, EpochStore(ctx), config)
        sup.start()
        if args.once:
            record = sup.run_once()
            sup.shutdown()
            return finish(args, None if record is None else record.to_json(), null=record is None)
        sup.shutdown()
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from mao.supervisor import list_sessions

    with open_context(args) as ctx:
        found = list_sessions(ctx, state=args.state)
    return finish(args, [item.to_json() for item in found])


def cmd_wakes(args: argparse.Namespace) -> int:
    from mao.console import listed_wakes

    with open_context(args) as ctx:
        found = listed_wakes(ctx, status=args.status)
    return finish(args, found)


def cmd_reopen(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = WakeQueue(ctx).retry(args.wake, by="human:cli")
    return finish(args, {"id": found.id, "status": found.status})


def cmd_conversion(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        found = ConversionStore(ctx).list(status=args.status)
    return finish(args, found)
