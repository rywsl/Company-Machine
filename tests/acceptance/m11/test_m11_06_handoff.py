"""M11-06 验收：人工协助（验证码等）、截图与控制台点图回答（spec/system/inbox.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.handoff")

from tests.acceptance.m11.deleg_world import Console, World  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"


def _open_handoff(w: World) -> str:
    rows = w.wait("SELECT id FROM human_requests WHERE kind = 'handoff' AND status = 'open'")
    return str(rows[0]["id"])


def _ctl_error(w: World, job) -> dict:
    responses = sorted((w.root / "jobs" / job.id / "1" / "ctl" / "responses").glob("*.json"))
    bodies = [json.loads(path.read_text(encoding="utf-8")) for path in responses]
    [failed] = [body for body in bodies if not body["ok"]]
    return failed["error"]


def test_handoff_point_answer(tmp_path: Path) -> None:
    w = World(tmp_path)
    bg = w.start("desk-handoff@1")
    request_id = _open_handoff(w)
    shown = w.cli("inbox", "show", request_id)[1]
    assert shown["origin"] == f"job:{bg.job_id}" and shown["deadline"] is not None
    assert shown["payload"] == {
        "job": bg.job_id,
        "station": w.key("desk-handoff@1"),
        "kind": "captcha",
        "answer_type": "point",
        "hint": "click the cat",
        "view": {"kind": "screenshot", "width": 40, "height": 30},
    }
    assert (w.root / "inbox" / request_id / "screenshot.png").read_bytes()[:8] == PNG
    w.awaiting(bg.job_id)
    assert w.events("handoff.requested") == [{"id": request_id, "job": bg.job_id, "kind": "captcha"}]

    code, body, err = w.cli("inbox", "answer", request_id, "--point", "40,10")
    assert code == 1 and w.error_code(body, err) == "INBOX_ANSWER_INVALID"
    code, body, _ = w.cli("inbox", "answer", request_id, "--point", "12,7")
    assert code == 0 and body["answer"] == {"point": [12, 7]}, body
    job = bg.join()
    assert job.state.value == "SEALED", job.error
    out = w.output(job)
    assert out["code"] == 0
    assert out["response"] == {"decision": "answered", "answer": {"point": [12, 7]}, "note": "", "code": ""}
    assert out["missing_answer"] == 2


def test_handoff_not_allowed(tmp_path: Path) -> None:
    w = World(tmp_path)
    denied = {"decision": "deny", "answer": None, "note": "", "code": "HANDOFF_NOT_ALLOWED"}
    job = w.run("desk-handoff-none@1")
    assert w.output(job)["response"] == denied
    other = w.run("desk-handoff@1", {"kind": "sms"})
    assert w.output(other)["response"] == denied
    assert w.cli("inbox", "ls", "--status", "open")[1] == []
    assert w.ctx.db.query("SELECT id FROM human_requests") == []


def test_handoff_timeout_and_cancel(tmp_path: Path) -> None:
    w = World(tmp_path)
    bg = w.start("desk-handoff@1", {"answer": "text", "shot": "none"})
    request_id = _open_handoff(w)
    w.clock.advance(301)
    job = bg.join()
    expected = {"decision": "timeout", "answer": None, "note": "", "code": "HANDOFF_TIMEOUT"}
    assert w.output(job)["response"] == expected
    assert w.cli("inbox", "show", request_id)[1]["status"] == "expired"

    bg2 = w.start("desk-handoff@1", {"answer": "text", "shot": "none"})
    second = _open_handoff(w)
    code, body, _ = w.cli("inbox", "cancel", second, "--note", "不处理")
    assert code == 0 and body["status"] == "cancelled"
    expected = {"decision": "cancelled", "answer": None, "note": "不处理", "code": "HANDOFF_CANCELLED"}
    assert w.output(bg2.join())["response"] == expected


def test_handoff_screenshot_rules(tmp_path: Path) -> None:
    w = World(tmp_path)
    job = w.run("desk-handoff@1", {"shot": "txt"})
    assert w.output(job)["code"] == 1
    error = _ctl_error(w, job)
    assert error["code"] == "HANDOFF_SCREENSHOT_INVALID" and "format" in error["message"]
    missing = w.run("desk-handoff@1", {"answer": "point", "shot": "none"})
    error = _ctl_error(w, missing)
    assert error["code"] == "HANDOFF_SCREENSHOT_INVALID" and "required" in error["message"]
    assert w.ctx.db.query("SELECT id FROM human_requests") == []
    assert w.output(missing)["missing_answer"] == 2


def test_console_screenshot_click(tmp_path: Path) -> None:
    w = World(tmp_path, console={"user": "alice"})
    bg = w.start("desk-handoff@1")
    request_id = _open_handoff(w)
    console = Console(w)
    try:
        status, headers, page = console.request("GET", f"/inbox/{request_id}")
        assert status == 200 and b'<input type="image" name="point"' in page
        assert "img-src 'self'" in headers["content-security-policy"]
        status, headers, image = console.request("GET", f"/inbox/{request_id}/screenshot")
        assert status == 200 and headers["content-type"] == "image/png" and image[:8] == PNG
        form = {"point.x": "5", "point.y": "6", "csrf": Console.csrf(request_id)}
        status, _, _ = console.request("POST", f"/inbox/{request_id}", form)
        assert status == 303
        job = bg.join()
        assert w.output(job)["response"]["answer"] == {"point": [5, 6]}
        assert w.cli("inbox", "show", request_id)[1]["answered_by"] == "human:alice"
    finally:
        console.close()
