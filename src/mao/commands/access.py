"""factory access。"""

from __future__ import annotations

import argparse
from pathlib import Path

from mao.commands.common import finish, open_context
from mao.operations import issue_credential, revoke_credential


def register(sub: argparse._SubParsersAction) -> None:
    access = sub.add_parser("access")
    commands = access.add_subparsers(dest="access_cmd", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--subject", required=True)
    issue.add_argument("--expires-at", required=True)
    issue.add_argument("--out", required=True)
    issue.set_defaults(func=cmd_issue)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("credential_id")
    revoke.set_defaults(func=cmd_revoke)


def cmd_issue(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = issue_credential(ctx, args.subject, args.expires_at, Path(args.out))
    return finish(args, record)


def cmd_revoke(args: argparse.Namespace) -> int:
    with open_context(args) as ctx:
        record = revoke_credential(ctx, args.credential_id)
    return finish(args, record)
