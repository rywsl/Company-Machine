"""人与厂长的持久对话（spec/system/talk.md）。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError

_HUMAN_BY = re.compile(r"^[A-Za-z0-9._-]+$")
_LIMIT = 64 * 1024
_REF_KINDS = frozenset({"changeset", "inbox", "run"})


@dataclass(frozen=True)
class TalkMessage:
    id: str
    role: str
    kind: str
    by: str
    body: str
    reply_state: str
    in_reply_to: str | None
    card_type: str | None
    ref_kind: str | None
    ref_id: str | None
    card: dict[str, Any]
    mailbox_id: str | None
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "kind": self.kind,
            "by": self.by,
            "body": self.body,
            "reply_state": self.reply_state,
            "in_reply_to": self.in_reply_to,
            "card_type": self.card_type,
            "ref_kind": self.ref_kind,
            "ref_id": self.ref_id,
            "card": dict(self.card),
            "mailbox_id": self.mailbox_id,
            "created_at": self.created_at,
        }


def list_messages(ctx: Any) -> list[TalkMessage]:
    rows = ctx.db.query("SELECT * FROM talk_messages ORDER BY created_at, id")
    return [_message(row) for row in rows]


def show(ctx: Any, message_id: str) -> TalkMessage:
    return _load(ctx, message_id)


def post_human(ctx: Any, text: str, *, by: str) -> TalkMessage:
    _check_actor(by, human=True)
    stored = _prepared_text(ctx, text)
    _check_ready(ctx)
    from mao.agents import Mailbox

    message_id = ctx.ids.new("tm")
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        _insert(
            conn,
            ctx,
            message_id=message_id,
            role="human",
            kind="text",
            by=by,
            body=stored,
            reply_state="open",
            created_at=now,
        )
        mail = Mailbox(ctx).post(
            "foreman",
            "talk",
            "talk:" + message_id,
            {"message": message_id, "by": by, "text": stored},
        )
        conn.execute(
            "UPDATE talk_messages SET mailbox_id = ? WHERE id = ?",
            (mail.id, message_id),
        )
    return _load(ctx, message_id)


def talk_reply(
    ctx: Any,
    text: str,
    *,
    by: str,
    turn_id: str,
    in_reply_to: str | None = None,
) -> TalkMessage:
    _check_actor(by, human=False)
    stored = _prepared_text(ctx, text)
    _check_ready(ctx)
    _require_running_turn(ctx, turn_id, "talk_reply")
    if in_reply_to is not None:
        _load(ctx, in_reply_to)
    message_id = ctx.ids.new("tm")
    now = utc_iso(ctx.clock.now())
    with ctx.db.tx() as conn:
        _insert(
            conn,
            ctx,
            message_id=message_id,
            role="foreman",
            kind="text",
            by=by,
            body=stored,
            reply_state="na",
            in_reply_to=in_reply_to,
            created_at=now,
        )
        if in_reply_to is not None:
            _answer_human(conn, ctx, in_reply_to, by="talk")
    return _load(ctx, message_id)


def settle_foreman_turn(ctx: Any, pending: list[Any], state: str, talk_replied: bool) -> None:
    talks = [item for item in pending if getattr(item, "kind", None) == "talk"]
    if not talks:
        return
    if state != "ok":
        for item in talks:
            rows = ctx.db.query("SELECT state FROM agent_messages WHERE id = ?", (item.id,))
            if rows and str(rows[0]["state"]) == "dropped":
                _mark_unanswered(ctx, str((item.body or {}).get("message") or ""))
        return
    for item in talks:
        message_id = str((item.body or {}).get("message") or "")
        if talk_replied:
            _mark_answered_if_open(ctx, message_id)
        else:
            _mark_unanswered(ctx, message_id)


def propose_run(
    ctx: Any,
    line: str,
    inputs: Mapping[str, str],
    *,
    by: str,
    turn_id: str,
    sink_mode: str = "execute",
) -> TalkMessage:
    _check_actor(by, human=False)
    _check_ready(ctx)
    _require_running_turn(ctx, turn_id, "talk_propose_run")
    if sink_mode not in ("execute", "dry-run"):
        raise FactoryError("TALK_RUN_INVALID", "sink_mode 不合法", field="sink_mode")
    if (
        not isinstance(inputs, Mapping)
        or isinstance(inputs, (str, bytes))
        or any(not isinstance(value, str) for value in inputs.values())
    ):
        raise FactoryError("TALK_RUN_INVALID", "inputs 不合法", field="inputs")
    message_id = ctx.ids.new("tm")
    card = {
        "type": "run",
        "line": str(line),
        "inputs": {str(key): value for key, value in inputs.items()},
        "sink_mode": sink_mode,
        "status": "proposed",
        "run_id": None,
        "by": by,
    }
    with ctx.db.tx() as conn:
        _insert(
            conn,
            ctx,
            message_id=message_id,
            role="system",
            kind="card",
            by="control",
            body=_line(ctx, "提议运行 " + str(line)),
            reply_state="na",
            card_type="run",
            ref_kind="run",
            ref_id="",
            card=card,
            created_at=utc_iso(ctx.clock.now()),
        )
        _open_run_notice(ctx, message_id, str(line))
    return _load(ctx, message_id)


def answer(ctx: Any, message_id: str, *, by: str, confirm: bool) -> TalkMessage:
    _check_actor(by, human=True)
    _check_ready(ctx)
    if confirm is not True:
        raise FactoryError("TALK_CONFIRM_REQUIRED", "需要确认", id=message_id)
    current = _load(ctx, message_id)
    if current.card_type != "run" or current.kind != "card":
        raise FactoryError("TALK_NOT_CONFIRMABLE", "不是运行卡片", id=message_id, kind=current.kind)
    if current.card.get("status") != "proposed" or current.ref_id not in (None, ""):
        raise FactoryError("TALK_ALREADY_ANSWERED", "运行卡片已经确认", id=message_id)
    from mao.governance.epochs import EpochStore
    from mao.governance.rollout import EpochRouter
    from mao.runs import open_line_runner

    line = str(current.card["line"])
    inputs = dict(current.card["inputs"])
    key = line + ":" + canonical_json(inputs)
    epochs = EpochStore(ctx)
    commit = EpochRouter(ctx, epochs).choose(key, line=line)
    snapshot = epochs.materialize(commit)
    status_id = ctx.ids.new("tm")
    with ctx.db.tx() as conn:
        run = open_line_runner(ctx, snapshot).start_run(
            line,
            inputs,
            trigger={"on": "talk", "by": by, "message": message_id},
            sink_mode=str(current.card["sink_mode"]),
        )
        run_id = run.id
        run_state = run.state.value if hasattr(run.state, "value") else str(run.state)
        original = dict(current.card)
        original["run_id"] = run_id
        conn.execute(
            "UPDATE talk_messages SET ref_id = ?, card = ? WHERE id = ?",
            (run_id, canonical_json(original), message_id),
        )
        status_card = dict(original)
        status_card["status"] = run_state
        status_card["by"] = by
        _insert(
            conn,
            ctx,
            message_id=status_id,
            role="system",
            kind="card",
            by="control",
            body=_line(ctx, "运行 " + run_id + " 状态 " + run_state),
            reply_state="na",
            card_type="run",
            ref_kind="run",
            ref_id=run_id,
            card=status_card,
            created_at=utc_iso(ctx.clock.now()),
        )
        _cancel_talk_item(ctx, message_id, by=by)
    return _load(ctx, status_id)


def note_status(ctx: Any, kind: str, ref_id: str, status: str) -> TalkMessage | None:
    if kind not in _REF_KINDS or not ref_id:
        return None
    rows = ctx.db.query(
        "SELECT * FROM talk_messages WHERE ref_kind = ? AND ref_id = ? ORDER BY created_at, id",
        (kind, ref_id),
    )
    if not rows:
        return None
    latest = _message(rows[-1])
    field = "state" if kind == "changeset" else "status"
    if latest.card.get(field) == status:
        return None
    card = {key: value for key, value in latest.card.items() if key not in ("value", "answer")}
    card[field] = status
    message_id = ctx.ids.new("tm")
    with ctx.db.tx() as conn:
        _insert(
            conn,
            ctx,
            message_id=message_id,
            role="system",
            kind="card",
            by="control",
            body=_line(ctx, "状态变为 " + status),
            reply_state="na",
            card_type=latest.card_type,
            ref_kind=latest.ref_kind,
            ref_id=ref_id,
            card=card,
            created_at=utc_iso(ctx.clock.now()),
        )
    return _load(ctx, message_id)


def append_changeset_card(ctx: Any, changeset: Any) -> TalkMessage:
    card = {
        "type": "changeset",
        "id": changeset.id,
        "state": changeset.state,
        "commit": changeset.commit,
    }
    return _append_card(ctx, "changeset", "changeset", changeset.id, card, "提议变更 " + changeset.id)


def append_escalate_card(ctx: Any, request: Any) -> TalkMessage:
    summary = _line(ctx, str(request.summary))
    card = {
        "type": "escalate",
        "request_id": request.id,
        "status": "open",
        "summary": summary,
    }
    return _append_card(ctx, "escalate", "inbox", request.id, card, "上报 " + request.id)


def append_secret_card(ctx: Any, request: Any) -> TalkMessage:
    payload = request.payload if isinstance(request.payload, dict) else {}
    card = {
        "type": "secret",
        "request_id": request.id,
        "status": "open",
        "name": payload.get("name"),
        "station": payload.get("station"),
    }
    return _append_card(
        ctx, "secret", "inbox", request.id, card, "申请秘密 " + str(payload.get("name") or "")
    )


def _append_card(
    ctx: Any, card_type: str, ref_kind: str, ref_id: str, card: Mapping[str, Any], body: str
) -> TalkMessage:
    message_id = ctx.ids.new("tm")
    with ctx.db.tx() as conn:
        _insert(
            conn,
            ctx,
            message_id=message_id,
            role="system",
            kind="card",
            by="control",
            body=_line(ctx, body),
            reply_state="na",
            card_type=card_type,
            ref_kind=ref_kind,
            ref_id=ref_id,
            card=card,
            created_at=utc_iso(ctx.clock.now()),
        )
    return _load(ctx, message_id)


def _line(ctx: Any, text: str) -> str:
    from mao.egress import SecretStore

    stored = SecretStore(ctx.root).redact_obj(text)
    return stored if isinstance(stored, str) else str(stored)


def _open_run_notice(ctx: Any, message_id: str, line: str) -> None:
    origin = "talk:" + message_id
    if _open_origin(ctx, origin):
        return
    from mao.inbox import Inbox

    Inbox(ctx).open(
        "question",
        subject=ctx.ids.new("hr"),
        origin=origin,
        summary="厂长提议运行 " + line,
        payload={"options": ["知道了"], "message": message_id, "card_type": "run"},
    )


def _check_actor(by: str, *, human: bool) -> None:
    if human:
        if not isinstance(by, str) or _HUMAN_BY.fullmatch(by) is None:
            raise FactoryError("TALK_ACTOR_INVALID", "人名不合法", by=by)
        return
    if not isinstance(by, str) or by == "":
        raise FactoryError("TALK_ACTOR_INVALID", "厂长身份为空", by=by)


def _prepared_text(ctx: Any, text: str) -> str:
    if not isinstance(text, str) or text.strip() == "":
        raise FactoryError("TALK_EMPTY", "文本为空", field="text")
    from mao.egress import SecretStore

    stored = SecretStore(ctx.root).redact_obj(text.strip())
    if not isinstance(stored, str):
        stored = str(stored)
    raw = canonical_json(stored).encode("utf-8")
    if len(raw) > _LIMIT:
        raise FactoryError("TALK_TOO_LARGE", "文本过大", bytes=len(raw))
    return stored


def _check_ready(ctx: Any) -> None:
    from mao.agents import AgentsConfig
    from mao.durable import LeaseStore
    from mao.supervisor import SupervisorConfig

    config = SupervisorConfig.load(ctx.root)
    if config.mode != "resident":
        raise FactoryError("TALK_FOREMAN_ABSENT", "厂长不是常驻", mode=config.mode)
    AgentsConfig.load(ctx.root)
    if LeaseStore(ctx).valid("serve") is None:
        raise FactoryError("TALK_SERVE_DOWN", "serve 没有在跑", resource="serve")


def _require_running_turn(ctx: Any, turn_id: str, tool: str) -> None:
    rows = ctx.db.query(
        "SELECT id FROM agent_turns WHERE id = ? AND agent = 'foreman' AND state = 'running'",
        (turn_id,),
    )
    if not rows:
        raise FactoryError("TALK_NOT_IN_TURN", "不在正在跑的厂长轮次里", tool=tool)


def _load(ctx: Any, message_id: str) -> TalkMessage:
    rows = ctx.db.query("SELECT * FROM talk_messages WHERE id = ?", (message_id,))
    if not rows:
        raise FactoryError("TALK_NOT_FOUND", "找不到线程消息", id=message_id)
    return _message(rows[0])


def _insert(
    conn: Any,
    ctx: Any,
    *,
    message_id: str,
    role: str,
    kind: str,
    by: str,
    body: str,
    reply_state: str,
    created_at: str,
    in_reply_to: str | None = None,
    card_type: str | None = None,
    ref_kind: str | None = None,
    ref_id: str | None = None,
    card: Mapping[str, Any] | None = None,
    mailbox_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO talk_messages (
               id, role, kind, by_name, body, reply_state, in_reply_to, card_type,
               ref_kind, ref_id, card, mailbox_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            role,
            kind,
            by,
            body,
            reply_state,
            in_reply_to,
            card_type,
            ref_kind,
            ref_id,
            canonical_json(dict(card or {})),
            mailbox_id,
            created_at,
        ),
    )


def _answer_human(conn: Any, ctx: Any, message_id: str, *, by: str) -> None:
    rows = conn.execute("SELECT * FROM talk_messages WHERE id = ?", (message_id,)).fetchall()
    if not rows:
        return
    current = _message(rows[0])
    if current.role != "human" or current.reply_state not in ("open", "unanswered"):
        return
    conn.execute(
        "UPDATE talk_messages SET reply_state = 'answered' WHERE id = ?",
        (message_id,),
    )
    _cancel_talk_item(ctx, message_id, by=by)


def _mark_answered_if_open(ctx: Any, message_id: str) -> None:
    if not message_id:
        return
    rows = ctx.db.query("SELECT reply_state FROM talk_messages WHERE id = ?", (message_id,))
    if not rows or str(rows[0]["reply_state"]) != "open":
        return
    with ctx.db.tx() as conn:
        conn.execute(
            "UPDATE talk_messages SET reply_state = 'answered' WHERE id = ? AND reply_state = 'open'",
            (message_id,),
        )


def _mark_unanswered(ctx: Any, message_id: str) -> None:
    if not message_id:
        return
    rows = ctx.db.query(
        "SELECT role, reply_state FROM talk_messages WHERE id = ?",
        (message_id,),
    )
    if not rows or str(rows[0]["role"]) != "human":
        return
    if str(rows[0]["reply_state"]) == "open":
        with ctx.db.tx() as conn:
            conn.execute(
                "UPDATE talk_messages SET reply_state = 'unanswered' WHERE id = ? AND reply_state = 'open'",
                (message_id,),
            )
    _open_unanswered(ctx, message_id)


def _open_unanswered(ctx: Any, message_id: str) -> None:
    origin = "talk:" + message_id
    if _open_origin(ctx, origin):
        return
    from mao.inbox import Inbox

    Inbox(ctx).open(
        "question",
        subject=ctx.ids.new("hr"),
        origin=origin,
        summary="厂长没有回复 " + message_id,
        payload={"options": ["知道了"], "message": message_id},
    )


def _cancel_talk_item(ctx: Any, message_id: str, *, by: str) -> None:
    origin = "talk:" + message_id
    rows = ctx.db.query(
        "SELECT id FROM human_requests WHERE origin = ? AND status = 'open'",
        (origin,),
    )
    if not rows:
        return
    from mao.inbox import Inbox

    Inbox(ctx).close(str(rows[0]["id"]), status="cancelled", answer=None, by=by)


def _open_origin(ctx: Any, origin: str) -> bool:
    rows = ctx.db.query(
        "SELECT id FROM human_requests WHERE origin = ? AND status = 'open'",
        (origin,),
    )
    return bool(rows)


def _message(row: Any) -> TalkMessage:
    card = json.loads(row["card"]) if row["card"] else {}
    if not isinstance(card, dict):
        card = {}
    return TalkMessage(
        str(row["id"]),
        str(row["role"]),
        str(row["kind"]),
        str(row["by_name"]),
        str(row["body"]),
        str(row["reply_state"]),
        None if row["in_reply_to"] is None else str(row["in_reply_to"]),
        None if row["card_type"] is None else str(row["card_type"]),
        None if row["ref_kind"] is None else str(row["ref_kind"]),
        None if row["ref_id"] is None else str(row["ref_id"]),
        card,
        None if row["mailbox_id"] is None else str(row["mailbox_id"]),
        str(row["created_at"]),
    )
