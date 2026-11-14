"""人工协助：截图、点击与收件箱回答。"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from mao.base import canonical_json, parse_duration, utc_iso
from mao.errors import FactoryError

_PNG = b"\x89PNG\r\n\x1a\n"
_ANSWERS = ("text", "point", "points", "offset", "done")
_MAX_BYTES = 5242880


def open_handoff(
    ctx: Any,
    snapshot: Any,
    *,
    job_id: str,
    attempt: int,
    station_key: str,
    request_id: str,
    args: dict[str, Any],
    screenshot: Path | None,
) -> Any:
    del attempt
    from mao.inbox import Inbox

    station = snapshot.spec(station_key)
    policy_ref = station.get("policy")
    policy = snapshot.spec(snapshot.key("Policy", policy_ref)) if policy_ref else {}
    handoff = policy.get("handoff") if isinstance(policy, dict) else None
    kind = str(args.get("kind") or "")
    summary = args.get("summary")
    answer = str(args.get("answer") or "")
    hint = str(args.get("hint") or "")
    if not isinstance(handoff, dict) or kind not in set(handoff.get("kinds") or []):
        return _result(request_id, "deny", None, "", "HANDOFF_NOT_ALLOWED")
    if (
        not isinstance(summary, str)
        or summary == ""
        or len(summary) > 2000
        or answer not in _ANSWERS
        or len(hint) > 500
    ):
        return _error(request_id, "CTL_REQUEST_INVALID", "协助参数不合法")
    shot = str(args.get("screenshot") or "")
    if answer in ("point", "points") and shot == "":
        return _error(request_id, "HANDOFF_SCREENSHOT_INVALID", "required")
    view = None
    host = screenshot
    if shot:
        if host is None or not host.is_file():
            return _error(request_id, "HANDOFF_SCREENSHOT_INVALID", "path")
        try:
            data = host.read_bytes()
        except OSError:
            return _error(request_id, "HANDOFF_SCREENSHOT_INVALID", "path")
        if len(data) > _MAX_BYTES:
            return _error(request_id, "HANDOFF_SCREENSHOT_INVALID", "too-large")
        size = _png_size(data)
        if size is None:
            return _error(request_id, "HANDOFF_SCREENSHOT_INVALID", "format")
        view = {"kind": "screenshot", "width": size[0], "height": size[1]}
    deadline = utc_iso(ctx.clock.now() + timedelta(seconds=parse_duration(str(handoff["timeout"]))))
    item = Inbox(ctx).open(
        "handoff",
        subject=f"{job_id}:{request_id}",
        origin="job:" + job_id,
        summary=summary,
        payload={
            "job": job_id,
            "station": station_key,
            "kind": kind,
            "answer_type": answer,
            "hint": hint,
            "view": view,
        },
        deadline=deadline,
    )
    if host is not None and host.is_file():
        dest = Path(ctx.root) / "inbox" / item.id / "screenshot.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(host, dest)
    _event(ctx, "handoff.requested", {"id": item.id, "job": job_id, "kind": kind})
    return item


def poll_handoff(ctx: Any, request: Any) -> dict[str, Any] | None:
    from mao.inbox import Inbox

    current = request
    if current.status == "open" and _due(ctx, current.deadline):
        current = Inbox(ctx).close(current.id, status="expired", answer=None, by="timeout")
    if current.status == "open":
        return None
    if current.status == "answered":
        stored = dict(current.answer or {})
        note = str(stored.pop("note", "") or "")
        return _body("answered", stored, note, "")
    if current.status == "cancelled":
        note = ""
        if isinstance(current.answer, dict):
            note = str(current.answer.get("note") or "")
        return _body("cancelled", None, note, "HANDOFF_CANCELLED")
    if current.status == "expired":
        return _body("timeout", None, "", "HANDOFF_TIMEOUT")
    return None


def check_answer(request: Any, reply: dict[str, Any]) -> dict[str, Any]:
    kind = str((request.payload or {}).get("answer_type") or "")
    note = reply.get("note", "")
    if "note" in reply and (not isinstance(note, str) or len(note) > 2000):
        raise FactoryError("INBOX_ANSWER_INVALID", "备注不合法", reason="answer")
    extra = set(reply) - {"note", kind}
    if kind == "text":
        text = reply.get("text")
        if extra or not isinstance(text, str) or text == "" or len(text) > 1000:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答不合法", reason="answer")
        body: dict[str, Any] = {"text": text}
    elif kind == "point":
        point = reply.get("point")
        if extra or not _point_ok(point, request):
            raise FactoryError("INBOX_ANSWER_INVALID", "回答不合法", reason="answer")
        body = {"point": [int(point[0]), int(point[1])]}
    elif kind == "points":
        points = reply.get("points")
        if extra or not isinstance(points, list) or not 1 <= len(points) <= 20:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答不合法", reason="answer")
        if any(not _point_ok(item, request) for item in points):
            raise FactoryError("INBOX_ANSWER_INVALID", "回答不合法", reason="answer")
        body = {"points": [[int(item[0]), int(item[1])] for item in points]}
    elif kind == "offset":
        offset = reply.get("offset")
        if extra or isinstance(offset, bool) or not isinstance(offset, int) or abs(offset) > 100000:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答不合法", reason="answer")
        body = {"offset": offset}
    elif kind == "done":
        if extra or reply.get("done") is not True:
            raise FactoryError("INBOX_ANSWER_INVALID", "回答不合法", reason="answer")
        body = {"done": True}
    else:
        raise FactoryError("INBOX_ANSWER_INVALID", "回答不合法", reason="answer")
    if isinstance(note, str) and note:
        body["note"] = note
    return body


def latest(ctx: Any, subject: str) -> Any | None:
    from mao.inbox import Inbox

    found = [item for item in Inbox(ctx).list() if item.kind == "handoff" and item.subject == subject]
    if not found:
        return None
    return sorted(found, key=lambda item: (item.created_at, item.id))[-1]


def _point_ok(point: Any, request: Any) -> bool:
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        return False
    x, y = point
    if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
        return False
    view = (request.payload or {}).get("view") or {}
    width = int(view.get("width") or 0)
    height = int(view.get("height") or 0)
    return 0 <= x < width and 0 <= y < height


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(_PNG) or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _due(ctx: Any, deadline: str | None) -> bool:
    if not deadline:
        return False
    moment = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    return ctx.clock.now() >= moment


def _result(request_id: str, decision: str, answer: Any, note: str, code: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "ok": True,
        "result": _body(decision, answer, note, code),
        "error": None,
    }


def _body(decision: str, answer: Any, note: str, code: str) -> dict[str, Any]:
    return {"decision": decision, "answer": answer, "note": note, "code": code}


def _error(request_id: str, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "ok": False, "result": None, "error": {"code": code, "message": message}}


def _event(ctx: Any, kind: str, data: dict[str, Any]) -> None:
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO events (id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
            (ctx.ids.new("ev"), utc_iso(ctx.clock.now()), kind, data.get("job"), canonical_json(data)),
        )
