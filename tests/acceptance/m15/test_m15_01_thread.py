"""M15-01 验收：线程、回复与未回复（spec/system/talk.md 第 3.1、5.1–5.4 节）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.talk")

from mao.talk import list_messages, post_human, show, talk_reply  # noqa: E402
from tests.acceptance.m15.talk_world import (  # noqa: E402
    SECRET,
    drop_agents,
    foreman_call,
    hold_serve,
    open_world,
    thread,
)

ROOT = Path(__file__).resolve().parents[3]
OBLIGATION = "本轮有 human talk 消息时必须 talk_reply。"


def _err(world, *args: str) -> dict:
    code, body, err = world.cli(*args)
    assert code == 1, err
    return body["error"]


def test_post_human_writes_thread_and_mailbox(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.put_secret("DESK_TOKEN", "other", SECRET)
    code, body, err = world.cli("talk", "send", "--by", "ada", f"请看 {SECRET}")
    assert code == 0, err
    assert body["role"] == "human" and body["kind"] == "text"
    assert body["by"] == "ada" and body["reply_state"] == "open"
    assert SECRET not in body["body"] and "***" in body["body"]
    assert body["id"].startswith("tm-")

    from mao.agents import Mailbox

    [mail] = Mailbox(world.ctx).list("foreman")
    assert mail.kind == "talk" and mail.source == "talk:" + body["id"]
    assert mail.body == {"message": body["id"], "by": "ada", "text": body["body"]}
    assert SECRET not in json.dumps(mail.body)


def test_send_checks_resident_agents_and_serve(tmp_path: Path) -> None:
    for name in ("absent", "agents", "serve"):
        (tmp_path / name).mkdir()
    absent = open_world(tmp_path / "absent", mode="session")
    hold_serve(absent.ctx)
    assert _err(absent, "talk", "send", "--by", "ada", "你好")["code"] == "TALK_FOREMAN_ABSENT"
    assert thread(absent) == []

    missing = open_world(tmp_path / "agents")
    hold_serve(missing.ctx)
    drop_agents(missing)
    assert _err(missing, "talk", "send", "--by", "ada", "你好")["code"] == "AGENTS_CONFIG_MISSING"

    down = open_world(tmp_path / "serve")
    assert _err(down, "talk", "send", "--by", "ada", "你好")["code"] == "TALK_SERVE_DOWN"
    assert thread(down) == []

    hold_serve(down.ctx)
    with pytest.raises(Exception) as bad_by:
        post_human(down.ctx, "你好", by="bad name")
    assert bad_by.value.code == "TALK_ACTOR_INVALID"
    with pytest.raises(Exception) as empty:
        post_human(down.ctx, "  ", by="ada")
    assert empty.value.code == "TALK_EMPTY"
    assert thread(down) == []


def test_ls_show_and_talk_reply(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    first = post_human(world.ctx, "第一条", by="ada")
    second = post_human(world.ctx, "第二条", by="ada")
    code, listed, err = world.cli("talk", "ls")
    assert code == 0, err
    assert [item["id"] for item in listed] == [first.id, second.id]
    code, shown, err = world.cli("talk", "show", first.id)
    assert code == 0, err
    assert shown == first.to_json()
    assert _err(world, "talk", "show", "tm-missing")["code"] == "TALK_NOT_FOUND"
    _value, error = foreman_call(world, "talk_reply", {"text": "在"})
    assert error["code"] == "TALK_NOT_IN_TURN"
    with pytest.raises(Exception) as missing_turn:
        talk_reply(world.ctx, "在", by="fake-agent@1", turn_id="")
    assert missing_turn.value.code == "TALK_NOT_IN_TURN"


def test_other_roles_cannot_talk_reply(tmp_path: Path) -> None:
    from mao.mcp import McpServer, Session

    world = open_world(tmp_path)

    def names(role: str, agent: str | None = None) -> list[str]:
        server = McpServer(
            world.ctx,
            world.epochs,
            Session(role=role, actor="fake-agent@1", agent=agent),
            pipeline=world.pipeline(),
        )
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        return [item["name"] for item in listed["result"]["tools"]]

    reviewer = names("reviewer")
    agent = names("agent", "scout")
    assert "talk_reply" not in reviewer and "talk_propose_run" not in reviewer
    assert "talk_reply" not in agent and "talk_propose_run" not in agent
    reply = world.mcp("talk_reply", {"text": "不该出现"})
    assert reply.get("error", {}).get("message") == "unknown tool"


def test_ok_without_reply_opens_inbox(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.script({"default": [{"do": "echo_task", "path": "/workspace/task.txt"}]})
    post_human(world.ctx, "有人吗", by="ada")
    turn = world.host().run_once("foreman")
    assert turn.state == "ok", turn.error
    [human] = [item for item in thread(world) if item.role == "human"]
    assert human.reply_state == "unanswered"
    assert OBLIGATION in world.read("task.txt", "foreman")
    from mao.inbox import Inbox

    opened = [item for item in Inbox(world.ctx).list(status="open") if item.origin == "talk:" + human.id]
    assert len(opened) == 1 and opened[0].kind == "question"
    assert opened[0].payload["message"] == human.id


def test_reply_covers_the_batch(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.script(
        {"default": [{"do": "mcp", "calls": [{"name": "talk_reply", "arguments": {"text": "收到"}}]}]}
    )
    post_human(world.ctx, "第一句", by="ada")
    post_human(world.ctx, "第二句", by="ada")
    turn = world.host().run_once("foreman")
    assert turn.state == "ok", turn.error
    humans = [item for item in thread(world) if item.role == "human"]
    assert [item.reply_state for item in humans] == ["answered", "answered"]
    assert any(item.role == "foreman" and item.body == "收到" for item in thread(world))
    from mao.inbox import Inbox

    assert [item for item in Inbox(world.ctx).list() if item.origin.startswith("talk:")] == []


def test_failed_turn_does_not_open_inbox(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.script({"default": [{"do": "exit", "code": 1}]})
    sent = post_human(world.ctx, "还在吗", by="ada")
    turn = world.host().run_once("foreman")
    assert turn.state == "failed"
    assert show(world.ctx, sent.id).reply_state == "open"
    from mao.agents import Mailbox
    from mao.inbox import Inbox

    [mail] = [item for item in Mailbox(world.ctx).list("foreman") if item.kind == "talk"]
    assert mail.state == "pending"
    assert [item for item in Inbox(world.ctx).list() if item.origin.startswith("talk:")] == []


def test_later_reply_clears_unanswered(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.script({"default": []})
    sent = post_human(world.ctx, "第一问", by="ada")
    host = world.host()
    assert host.run_once("foreman").state == "ok"
    assert show(world.ctx, sent.id).reply_state == "unanswered"
    world.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "calls": [
                        {
                            "name": "talk_reply",
                            "arguments": {"text": "补上", "in_reply_to": sent.id},
                        }
                    ],
                }
            ]
        }
    )
    post_human(world.ctx, "第二问", by="ada")
    assert host.run_once("foreman").state == "ok"
    assert show(world.ctx, sent.id).reply_state == "answered"
    from mao.inbox import Inbox

    [item] = [row for row in Inbox(world.ctx).list() if row.origin == "talk:" + sent.id]
    assert item.status == "cancelled"


def test_dropped_talk_marks_unanswered(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.script({"default": [{"do": "exit", "code": 1}]})
    sent = post_human(world.ctx, "还在吗", by="ada")
    host = world.host()
    for _ in range(2):
        assert host.run_once("foreman").state == "failed"
        assert show(world.ctx, sent.id).reply_state == "open"
    assert host.run_once("foreman").state == "failed"
    assert show(world.ctx, sent.id).reply_state == "unanswered"
    from mao.agents import Mailbox
    from mao.inbox import Inbox

    [mail] = [item for item in Mailbox(world.ctx).list("foreman") if item.kind == "talk"]
    assert mail.state == "dropped"
    opened = [item for item in Inbox(world.ctx).list() if item.origin == "talk:" + sent.id]
    assert len(opened) == 1 and opened[0].kind == "question"
    assert opened[0].payload["message"] == sent.id


def test_no_console_talk_route() -> None:
    text = (ROOT / "src" / "mao" / "console.py").read_text(encoding="utf-8")
    assert "/talk" not in text
    assert list_messages.__name__ == "list_messages"
