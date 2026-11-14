"""人的收件箱：打开、回答、取消与通知钩子。"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from mao.base import canonical_json, utc_iso
from mao.errors import FactoryError

_KINDS = ("question", "approval", "signoff", "secret", "handoff")
_QUESTION_REPLIES = ({"choice"}, {"text"})
_USER = re.compile(r"^[A-Za-z0-9._-]+$")


class RequestKind(StrEnum):
    QUESTION = "question"
    APPROVAL = "approval"
    SIGNOFF = "signoff"
    SECRET = "secret"
    HANDOFF = "handoff"


class RequestStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class HumanRequest:
    id: str
    kind: str
    subject: str
    origin: str
    summary: str
    payload: dict[str, Any]
    status: str
    answer: dict[str, Any] | None
    answered_by: str | None
    deadline: str | None
    created_at: str
    answered_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "origin": self.origin,
            "summary": self.summary,
            "payload": dict(self.payload),
            "status": self.status,
            "answer": None if self.answer is None else dict(self.answer),
            "answered_by": self.answered_by,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
        }


class Inbox:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def open(
        self,
        kind: str,
        *,
        subject: str,
        origin: str,
        summary: str,
        payload: Mapping[str, Any],
        deadline: str | None = None,
    ) -> HumanRequest:
        notify = _notify_config(self.ctx.root)
        _check_open(kind, summary, payload)
        if kind == RequestKind.QUESTION.value:
            request_id = subject or self.ctx.ids.new("hr")
            subject = request_id
        else:
            request_id = self.ctx.ids.new("hr")
        store = _secrets(self.ctx)
        stored_payload = store.redact_obj(dict(payload))
        now = utc_iso(self.ctx.clock.now())
        fresh = False
        with self.ctx.db.tx() as conn:
            if kind != RequestKind.QUESTION.value:
                existing = conn.execute(
                    "SELECT * FROM human_requests WHERE kind = ? AND subject = ? AND status = 'open'",
                    (kind, subject),
                ).fetchone()
                if existing is not None:
                    return _request(existing)
            conn.execute(
                """INSERT INTO human_requests
                   (id, kind, subject, origin, summary, payload, status, answer, answered_by,
                    deadline, created_at, answered_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, NULL, ?, ?, NULL)""",
                (
                    request_id,
                    kind,
                    subject,
                    origin,
                    summary,
                    canonical_json(stored_payload),
                    deadline,
                    now,
                ),
            )
            _event(
                conn,
                self.ctx,
                "inbox.opened",
                {"id": request_id, "kind": kind, "subject": subject, "origin": origin},
            )
            fresh = True
        item = self.get(request_id)
        if fresh:
            _notify(self.ctx, item, notify)
        return item

    def get(self, request_id: str) -> HumanRequest:
        rows = self.ctx.db.query("SELECT * FROM human_requests WHERE id = ?", (request_id,))
        if not rows:
            raise FactoryError("INBOX_NOT_FOUND", f"找不到事项 {request_id}", id=request_id)
        return _request(rows[0])

    def list(self, *, status: str | None = None, kind: str | None = None) -> list[HumanRequest]:
        rows = self.ctx.db.query("SELECT * FROM human_requests ORDER BY created_at, id")
        found = [_request(row) for row in rows]
        if status is not None:
            found = [item for item in found if item.status == status]
        if kind is not None:
            found = [item for item in found if item.kind == kind]
        return found

    def find(self, kind: str, subject: str) -> HumanRequest | None:
        rows = self.ctx.db.query(
            "SELECT * FROM human_requests WHERE kind = ? AND subject = ? AND status = 'open'",
            (kind, subject),
        )
        return _request(rows[0]) if rows else None

    def close(
        self,
        request_id: str,
        *,
        status: Literal["answered", "cancelled", "expired"],
        answer: Mapping[str, Any] | None,
        by: str,
    ) -> HumanRequest:
        current = self.get(request_id)
        if current.status != RequestStatus.OPEN.value:
            raise FactoryError(
                "INBOX_STATE_INVALID",
                "事项不在可关闭状态",
                id=request_id,
                status=current.status,
            )
        stored = None if answer is None else _secrets(self.ctx).redact_obj(dict(answer))
        now = utc_iso(self.ctx.clock.now())
        with self.ctx.db.tx() as conn:
            conn.execute(
                """UPDATE human_requests
                   SET status = ?, answer = ?, answered_by = ?, answered_at = ?
                   WHERE id = ?""",
                (
                    status,
                    None if stored is None else canonical_json(stored),
                    by,
                    now,
                    request_id,
                ),
            )
            _event(conn, self.ctx, "inbox.closed", {"id": request_id, "status": status, "by": by})
        if current.origin.startswith("foreman:") and status in ("answered", "cancelled"):
            from mao.foreman import WakeQueue

            WakeQueue(self.ctx).file(
                "inbox",
                request_id,
                {"id": request_id, "kind": current.kind, "status": status},
            )
        return self.get(request_id)

    def cancel(self, request_id: str, *, by: str, note: str = "") -> HumanRequest:
        current = self.get(request_id)
        if current.kind in (RequestKind.APPROVAL.value, RequestKind.SIGNOFF.value):
            raise FactoryError(
                "INBOX_STATE_INVALID",
                "审批和签核不能取消",
                id=request_id,
                status=current.status,
            )
        return self.close(request_id, status="cancelled", answer={"note": note}, by=by)


def answer(ctx: Any, request_id: str, reply: Mapping[str, Any], *, by: str) -> HumanRequest:
    inbox = Inbox(ctx)
    current = inbox.get(request_id)
    if current.status != RequestStatus.OPEN.value:
        raise FactoryError(
            "INBOX_STATE_INVALID",
            "事项不在可回答状态",
            id=request_id,
            status=current.status,
        )
    keys = set(reply)
    if current.kind == RequestKind.QUESTION.value:
        if keys not in _QUESTION_REPLIES:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答与事项种类不符", reason="kind")
        _check_question(current, reply)
        return inbox.close(request_id, status="answered", answer=dict(reply), by=by)
    if current.kind == RequestKind.APPROVAL.value:
        decision = reply.get("decision")
        note = str(reply.get("note") or "")
        if decision not in ("approve", "deny") or keys - {"decision", "note"}:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答与事项种类不符", reason="kind")
        _decide_approval(ctx, current.subject, str(decision), by=by, note=note)
        return inbox.get(request_id)
    if current.kind == RequestKind.SIGNOFF.value:
        decision = reply.get("decision")
        note = str(reply.get("note") or "")
        if decision not in ("approve", "deny") or keys - {"decision", "note"}:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答与事项种类不符", reason="kind")
        _signoff(ctx, current.subject, str(decision), by=by, note=note)
        return inbox.get(request_id)
    if current.kind == RequestKind.SECRET.value:
        if keys != {"secret"}:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答与事项种类不符", reason="kind")
        secret = reply.get("secret")
        raw = secret if isinstance(secret, bytes) else str(secret or "").encode("utf-8")
        from mao.secrets import fulfil_secret

        fulfil_secret(ctx, current, raw, by=by)
        return inbox.get(request_id)
    if current.kind == RequestKind.HANDOFF.value:
        from mao.handoff import check_answer

        stored = check_answer(current, dict(reply))
        return inbox.close(request_id, status="answered", answer=stored, by=by)
    raise FactoryError("INBOX_ANSWER_INVALID", "回答与事项种类不符", reason="kind")


def _signoff(ctx: Any, cs_id: str, decision: str, *, by: str, note: str) -> None:
    from mao.governance.changesets import ChangePipeline
    from mao.governance.epochs import EpochStore

    ChangePipeline(ctx, EpochStore(ctx)).signoff(cs_id, decision, by=by, note=note)


def _decide_approval(ctx: Any, approval_id: str, decision: str, *, by: str, note: str) -> None:
    from mao.governance.epochs import EpochStore
    from mao.runner import open_runner

    snapshot = EpochStore(ctx).materialize(EpochStore(ctx).current())
    open_runner(ctx, snapshot).decide(approval_id, decision, by=by, note=note)


def console_user(root: Any) -> str:
    body = _factory(root)
    console = body.get("console") if isinstance(body, dict) else None
    if not isinstance(console, dict) or "user" not in console:
        return "console"
    user = console.get("user")
    if not isinstance(user, str) or _USER.fullmatch(user) is None:
        raise FactoryError("CONSOLE_CONFIG_INVALID", "console.user 不合格", field="console.user")
    return user


def _check_open(kind: str, summary: str, payload: Mapping[str, Any]) -> None:
    if kind not in _KINDS:
        raise FactoryError("INBOX_REQUEST_INVALID", "事项种类不合法", reason="kind")
    if not isinstance(summary, str) or summary == "" or len(summary) > 2000:
        raise FactoryError("INBOX_REQUEST_INVALID", "摘要不合法", reason="summary")
    if not isinstance(payload, Mapping) or isinstance(payload, (str, bytes)):
        raise FactoryError("INBOX_REQUEST_INVALID", "载荷不是对象", reason="payload")


def _check_question(current: HumanRequest, reply: Mapping[str, Any]) -> None:
    if "choice" in reply:
        choice = reply["choice"]
        options = current.payload.get("options") or []
        if not isinstance(choice, str) or (options and choice not in options):
            raise FactoryError("INBOX_ANSWER_INVALID", "选项不在列表中", reason="choice")
        return
    text = reply.get("text")
    if not isinstance(text, str) or len(text) > 4000:
        raise FactoryError("INBOX_ANSWER_INVALID", "文本回答不合法", reason="text")


def _factory(root: Any) -> dict[str, Any]:
    path = root / "factory.json"
    if not path.is_file():
        return {}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactoryError("INBOX_CONFIG_INVALID", "factory.json 无法解析", field="inbox") from exc
    if not isinstance(body, dict):
        raise FactoryError("INBOX_CONFIG_INVALID", "factory.json 不是对象", field="inbox")
    return body


def _notify_config(root: Any) -> dict[str, Any] | None:
    body = _factory(root)
    inbox = body.get("inbox")
    if inbox is None:
        return None
    if not isinstance(inbox, dict):
        raise FactoryError("INBOX_CONFIG_INVALID", "inbox 段不合法", field="inbox")
    if "notify" not in inbox:
        return None
    notify = inbox.get("notify")
    if not isinstance(notify, dict) or set(notify) - {"argv", "timeout_s"}:
        raise FactoryError("INBOX_CONFIG_INVALID", "通知钩子不合法", field="inbox.notify")
    argv = notify.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or item == "" for item in argv)
    ):
        raise FactoryError("INBOX_CONFIG_INVALID", "通知钩子 argv 不合法", field="inbox.notify.argv")
    timeout = notify.get("timeout_s")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise FactoryError("INBOX_CONFIG_INVALID", "通知钩子超时不合法", field="inbox.notify.timeout_s")
    port = 8090
    console = body.get("console")
    if (
        isinstance(console, dict)
        and isinstance(console.get("port"), int)
        and not isinstance(console.get("port"), bool)
    ):
        port = int(console["port"])
    return {"argv": [str(item) for item in argv], "timeout_s": float(timeout), "port": port}


def _notify(ctx: Any, item: HumanRequest, config: dict[str, Any] | None) -> None:
    if config is None:
        return
    body = {
        "id": item.id,
        "kind": item.kind,
        "subject": item.subject,
        "origin": item.origin,
        "summary": item.summary,
        "deadline": item.deadline,
        "url": f"http://127.0.0.1:{config['port']}/inbox/{item.id}",
    }
    code = ""
    try:
        proc = subprocess.run(
            list(config["argv"]),
            input=canonical_json(body).encode("utf-8"),
            capture_output=True,
            timeout=config["timeout_s"],
            check=False,
        )
    except subprocess.TimeoutExpired:
        code = "NOTIFY_TIMEOUT"
    except OSError:
        code = "NOTIFY_ERROR"
    else:
        if proc.returncode != 0:
            code = "NOTIFY_EXIT"
    kind = "inbox.notified" if code == "" else "inbox.notify_failed"
    data = {"id": item.id} if code == "" else {"id": item.id, "code": code}
    with ctx.db.tx() as conn:
        _event(conn, ctx, kind, data)


def _secrets(ctx: Any) -> Any:
    from mao.egress import SecretStore

    return SecretStore(ctx.root)


def _event(conn: Any, ctx: Any, kind: str, data: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, NULL, ?)",
        (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, canonical_json(dict(data))),
    )


def _request(row: Any) -> HumanRequest:
    answer_raw = row["answer"]
    return HumanRequest(
        id=row["id"],
        kind=row["kind"],
        subject=row["subject"],
        origin=row["origin"],
        summary=row["summary"],
        payload=dict(json.loads(row["payload"])),
        status=row["status"],
        answer=None if answer_raw is None else dict(json.loads(answer_raw)),
        answered_by=row["answered_by"],
        deadline=row["deadline"],
        created_at=row["created_at"],
        answered_at=row["answered_at"],
    )
