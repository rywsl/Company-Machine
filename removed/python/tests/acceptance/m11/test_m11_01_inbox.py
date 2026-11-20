"""M11-01 验收：人工收件箱、厂长上报、通知钩子与控制台回答（spec/system/inbox.md 第 7 章）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mao.inbox")

from tests.acceptance.m11.deleg_world import Console, World, tool_path  # noqa: E402

QUESTION = {"summary": "要开通 B 站投稿吗？", "detail": "需要人确认账号归属", "options": ["yes", "no"]}


def test_escalate_and_answer(tmp_path: Path) -> None:
    w = World(tmp_path)
    req, err = w.mcp("escalate", QUESTION)
    assert err is None, err
    assert (req["kind"], req["status"], req["origin"]) == ("question", "open", "foreman:fake-agent@1")
    assert req["subject"] == req["id"] and req["id"].startswith("hr-")
    assert req["payload"] == {"detail": "需要人确认账号归属", "options": ["yes", "no"]}

    code, listed, _ = w.cli("inbox", "ls")
    assert code == 0 and [item["id"] for item in listed] == [req["id"]]
    code, body, err_text = w.cli("inbox", "answer", req["id"], "--choice", "maybe")
    assert code == 1 and w.error_code(body, err_text) == "INBOX_ANSWER_INVALID"
    assert w.wakes("inbox") == []

    code, body, _ = w.cli("inbox", "answer", req["id"], "--choice", "yes", actor="human:alice")
    assert code == 0, body
    assert (body["status"], body["answer"], body["answered_by"]) == (
        "answered",
        {"choice": "yes"},
        "human:alice",
    )
    [wake] = w.wakes("inbox")
    assert (wake["subject"], wake["status"]) == (req["id"], "open")
    seen, err = w.mcp("inbox_get", {"request_id": req["id"]})
    assert err is None and seen["answer"] == {"choice": "yes"}

    assert w.cli("inbox", "ls")[1] == []
    assert [item["id"] for item in w.cli("inbox", "ls", "--status", "answered")[1]] == [req["id"]]
    assert [e["kind"] for e in w.events("inbox.opened")] == ["question"]
    assert w.events("inbox.closed") == [{"id": req["id"], "status": "answered", "by": "human:alice"}]


def test_notify_hook(tmp_path: Path) -> None:
    from mao.inbox import Inbox

    received = tmp_path / "notified.jsonl"
    hook = {"argv": [sys.executable, tool_path(tmp_path, "notify-ok"), str(received)], "timeout_s": 30}
    w = World(tmp_path, inbox={"notify": hook}, console={"port": 8123, "user": "alice"})
    req, err = w.mcp("escalate", {"summary": "需要人看一下"})
    assert err is None, err
    [body] = [json.loads(line) for line in received.read_text(encoding="utf-8").splitlines() if line]
    assert body == {
        "id": req["id"],
        "kind": "question",
        "subject": req["id"],
        "origin": "foreman:fake-agent@1",
        "summary": "需要人看一下",
        "deadline": None,
        "url": f"http://127.0.0.1:8123/inbox/{req['id']}",
    }
    assert w.events("inbox.notified") == [{"id": req["id"]}]

    inbox = Inbox(w.ctx)
    first = inbox.open("signoff", subject="cs-0042", origin="governance", summary="T2 cs-0042", payload={})
    again = inbox.open("signoff", subject="cs-0042", origin="governance", summary="T2 cs-0042", payload={})
    assert again.id == first.id
    lines = [line for line in received.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2 and len(w.events("inbox.opened")) == 2

    w.set_config(
        "inbox", {"notify": {"argv": [sys.executable, tool_path(tmp_path, "notify-fail")], "timeout_s": 30}}
    )
    failed, err = w.mcp("escalate", {"summary": "钩子会失败"})
    assert err is None, err
    assert failed["status"] == "open"
    assert w.events("inbox.notify_failed") == [{"id": failed["id"], "code": "NOTIFY_EXIT"}]


def test_inbox_cli_errors(tmp_path: Path) -> None:
    w = World(tmp_path)
    code, body, err = w.cli("inbox", "show", "hr-9999")
    assert code == 1 and w.error_code(body, err) == "INBOX_NOT_FOUND"
    req, _ = w.mcp("escalate", {"summary": "问一下", "options": ["a", "b"]})
    assert w.cli("inbox", "answer", req["id"])[0] == 2
    assert w.cli("inbox", "answer", req["id"], "--text", "x", "--choice", "a")[0] == 2
    code, body, _ = w.cli("inbox", "cancel", req["id"], "--note", "不需要了")
    assert code == 0 and body["status"] == "cancelled"
    code, body, err = w.cli("inbox", "answer", req["id"], "--choice", "a")
    assert code == 1 and w.error_code(body, err) == "INBOX_STATE_INVALID"
    assert [wake["subject"] for wake in w.wakes("inbox")] == [req["id"]]

    w.set_config("inbox", {"notify": {"argv": [], "timeout_s": 5}})
    before = len(w.cli("inbox", "ls", "--status", "cancelled")[1])
    _, err = w.mcp("escalate", {"summary": "配置坏了"})
    assert err["code"] == "INBOX_CONFIG_INVALID" and err["detail"]["field"] == "inbox.notify.argv"
    assert w.cli("inbox", "ls")[1] == [] and before == 1


def test_console_answers_with_csrf(tmp_path: Path) -> None:
    w = World(tmp_path, console={"user": "alice"})
    req, _ = w.mcp("escalate", QUESTION)
    console = Console(w)
    try:
        status, _, page = console.request("GET", f"/inbox/{req['id']}")
        assert status == 200
        assert f'name="csrf" value="{Console.csrf(req["id"])}"'.encode() in page

        status, _, _ = console.request("POST", f"/inbox/{req['id']}", {"choice": "yes"})
        assert status == 403
        status, _, _ = console.request("POST", f"/inbox/{req['id']}", {"choice": "yes", "csrf": "0" * 64})
        assert status == 403
        assert w.cli("inbox", "show", req["id"])[1]["status"] == "open"

        form = {"choice": "yes", "csrf": Console.csrf(req["id"])}
        status, headers, _ = console.request("POST", f"/inbox/{req['id']}", form)
        assert status == 303 and headers["location"] == f"/inbox/{req['id']}"
        shown = w.cli("inbox", "show", req["id"])[1]
        assert (shown["status"], shown["answered_by"]) == ("answered", "human:alice")

        listed = json.loads(console.request("GET", "/api/inbox")[2])
        assert listed == []
        assert console.request("POST", "/runs", {"x": "1"})[0] == 405
    finally:
        console.close()
