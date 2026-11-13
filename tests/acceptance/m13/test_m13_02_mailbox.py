"""M13-02：邮箱与订阅（spec/system/agents.md 3.2、5.1、5.2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.m13.agent_world import SCOUT_TOKEN, World


def test_schedule_messages(tmp_path: Path) -> None:
    w = World(tmp_path)
    host = w.host()
    first = host.collect()
    [message] = w.messages("scout")
    assert first == [message.id]
    assert (message.kind, message.source, message.body, message.state) == (
        "schedule",
        "schedule:0",
        {"note": "整理笔记", "every": "1h"},
        "pending",
    )
    assert host.collect() == []
    w.clock.advance(3599)
    assert host.collect() == []
    w.clock.advance(1)
    assert len(host.collect()) == 1
    assert [m.kind for m in w.messages("scout")] == ["schedule", "schedule"]
    assert w.messages("helper") == []
    assert [e["kind"] for e in w.events("agent.message")] == ["schedule", "schedule"]


def test_bean_subscription(tmp_path: Path) -> None:
    w = World(tmp_path)
    host = w.host()
    host.collect()
    wanted = w.sealed_note("for the scout\n")
    w.ctx.warehouse.label(wanted, "src", "scout", by="human:acceptance")
    other = w.sealed_note("not labelled\n")
    host.collect()
    beans = [m for m in w.messages("scout") if m.kind == "bean"]
    assert [m.body["bean"] for m in beans] == [wanted]
    assert beans[0].source == "bean:0"
    assert beans[0].body["labels"] == {"src": "scout"}
    assert other not in [m.body.get("bean") for m in w.messages("scout")]
    host.collect()
    assert len([m for m in w.messages("scout") if m.kind == "bean"]) == 1


def test_ingress_to_agent(tmp_path: Path) -> None:
    from mao.ingress import IngressConfig, IngressServer, routes

    w = World(tmp_path)
    assert routes(w.snapshot)["scout-in"].deliver == "agent"
    server = IngressServer(w.ctx, w.epochs, IngressConfig(port=0))
    body = "私信：明天发布吗？\n".encode()
    status, reply = server.handle(
        "POST",
        "/in/scout-in",
        {
            "Content-Length": str(len(body)),
            "Authorization": "Bearer " + SCOUT_TOKEN,
            "Content-Type": "text/plain",
        },
        body,
    )
    assert status == 202, reply
    [message] = w.messages("scout")
    assert (message.kind, message.source) == ("ingress", "ingress:scout-in")
    assert message.body == {
        "id": reply["id"],
        "route": "scout-in",
        "content_type": "text/plain",
        "text": "私信：明天发布吗？\n",
    }
    assert w.events("ingress.received")[-1]["target"] == "scout"


def test_send_remind_and_redaction(tmp_path: Path) -> None:
    from mao.agents import Mailbox
    from mao.errors import FactoryError

    w = World(tmp_path)
    code, body, err = w.cli("agent", "send", "scout", "--text", "token is " + SCOUT_TOKEN)
    assert code == 0, err
    [message] = w.messages("scout")
    assert message.kind == "message" and message.source.startswith("human:")
    assert SCOUT_TOKEN not in message.body["text"] and message.body["text"].startswith("token is ")
    assert body["id"] == message.id

    later = Mailbox(w.ctx).post(
        "scout", "remind", "agent:scout", {"note": "check"}, not_before="2999-01-01T00:00:00Z"
    )
    assert (later.kind, later.state, later.not_before) == ("remind", "pending", "2999-01-01T00:00:00Z")
    assert later.to_json()["not_before"] == "2999-01-01T00:00:00Z"

    with pytest.raises(FactoryError) as ei:
        Mailbox(w.ctx).post("scout", "message", "human:acceptance", {"text": "x" * 70000})
    assert ei.value.code == "AGENT_MESSAGE_TOO_LARGE"
    code, body, _ = w.cli("agent", "send", "nobody", "--text", "hi")
    assert code == 1 and body["error"]["code"] == "AGENT_NOT_FOUND"


def test_inbox_answer_returns(tmp_path: Path) -> None:
    from mao.inbox import Inbox, answer

    w = World(tmp_path)
    item = Inbox(w.ctx).open(
        "question",
        subject="q-scout-1",
        origin="agent:scout",
        summary="明天发布吗？",
        payload={"detail": "", "options": ["发布", "再等等"]},
    )
    answer(w.ctx, item.id, {"choice": "再等等", "note": "下周"}, by="human:alice")
    [message] = w.messages("scout")
    assert (message.kind, message.source) == ("inbox", "inbox:" + item.id)
    assert message.body == {
        "id": item.id,
        "kind": "question",
        "status": "answered",
        "reply": {"choice": "再等等"},
    }
    assert w.rows("SELECT id FROM wakes WHERE signal = 'inbox'") == []
