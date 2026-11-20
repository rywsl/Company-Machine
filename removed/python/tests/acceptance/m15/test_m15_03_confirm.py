"""M15-03 验收：运行卡片与人确认后的 start_run（spec/system/talk.md 第 5.6 节）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.talk")

from mao.talk import answer  # noqa: E402
from tests.acceptance.m15.talk_world import cards, hold_serve, open_world  # noqa: E402


def _foreman_raw(world, name: str, arguments: dict) -> dict:
    """厂长会话的原始 JSON-RPC 回复。未知工具是协议错误，没有 result。"""
    from mao.mcp import McpServer, Session

    server = McpServer(
        world.ctx,
        world.epochs,
        Session(role="foreman", actor="fake-agent@1"),
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
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


def _propose(world, bean: str) -> str:
    world.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "calls": [
                        {
                            "name": "talk_propose_run",
                            "arguments": {
                                "line": "note-trigger@1",
                                "inputs": {"note": bean},
                                "sink_mode": "dry-run",
                            },
                        },
                        {"name": "talk_reply", "arguments": {"text": "等你确认"}},
                    ],
                }
            ]
        }
    )
    from mao.talk import post_human

    post_human(world.ctx, "跑一下", by="ada")
    turn = world.host().run_once("foreman")
    assert turn.state == "ok", turn.error
    [card] = cards(world, "run")
    assert card.card["status"] == "proposed" and card.card["run_id"] is None
    return card.id


def test_propose_run_records_card_only(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    bean = world.sealed_note("hello factory\n")
    card_id = _propose(world, bean)
    assert world.rows("SELECT id FROM runs") == []
    reply = _foreman_raw(world, "run_start", {"line": "note-trigger@1", "inputs": {"note": bean}})
    assert reply.get("error", {}).get("message") == "unknown tool"
    assert cards(world, "run")[0].id == card_id
    from mao.inbox import Inbox

    [notice] = [item for item in Inbox(world.ctx).list(status="open") if item.origin == "talk:" + card_id]
    assert notice.kind == "question" and notice.payload["message"] == card_id


def test_confirm_starts_run(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    bean = world.sealed_note("hello factory\n")
    card_id = _propose(world, bean)
    code, body, err = world.cli("talk", "answer", card_id, "--by", "ada", "--confirm")
    assert code == 0, err
    assert body["card"]["run_id"] and body["card"]["status"] == "PENDING"
    [row] = world.rows("SELECT id, trigger, sink_mode FROM runs")
    assert row["id"] == body["card"]["run_id"]
    assert row["sink_mode"] == "dry-run"
    assert json.loads(row["trigger"]) == {"on": "talk", "by": "ada", "message": card_id}
    from mao.inbox import Inbox

    [notice] = [item for item in Inbox(world.ctx).list() if item.origin == "talk:" + card_id]
    assert notice.status == "cancelled"


def test_answer_without_confirm_does_not_start(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    bean = world.sealed_note("hello factory\n")
    card_id = _propose(world, bean)
    code, body, err = world.cli("talk", "answer", card_id, "--by", "ada")
    assert code == 1, err
    assert body["error"]["code"] == "TALK_CONFIRM_REQUIRED"
    assert world.rows("SELECT id FROM runs") == []
    with pytest.raises(Exception) as refused:
        answer(world.ctx, card_id, by="ada", confirm=False)
    assert refused.value.code == "TALK_CONFIRM_REQUIRED"


def test_confirm_rejects_non_run_and_repeat(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    from mao.talk import post_human

    human = post_human(world.ctx, "这不是卡片", by="ada")
    code, body, err = world.cli("talk", "answer", human.id, "--by", "ada", "--confirm")
    assert code == 1, err
    assert body["error"]["code"] == "TALK_NOT_CONFIRMABLE"
    bean = world.sealed_note("hello factory\n")
    card_id = _propose(world, bean)
    assert world.cli("talk", "answer", card_id, "--by", "ada", "--confirm")[0] == 0
    code, body, err = world.cli("talk", "answer", card_id, "--by", "ada", "--confirm")
    assert code == 1, err
    assert body["error"]["code"] == "TALK_ALREADY_ANSWERED"
    assert len(world.rows("SELECT id FROM runs")) == 1


def test_run_state_writes_back(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    bean = world.sealed_note("hello factory\n")
    card_id = _propose(world, bean)
    confirmed = answer(world.ctx, card_id, by="ada", confirm=True)
    run_id = confirmed.card["run_id"]
    from mao.runs import open_line_runner

    open_line_runner(world.ctx, world.snapshot).cancel(run_id, by="ada")
    statuses = [item.card.get("status") for item in cards(world, "run") if item.card.get("run_id") == run_id]
    assert "CANCELLING" in statuses
    written = [
        item
        for item in cards(world, "run")
        if item.card.get("status") == "CANCELLING" and item.card.get("run_id") == run_id
    ]
    assert written and written[0].ref_id == run_id


def test_confirm_follows_epoch_router(tmp_path: Path) -> None:
    from mao.base import canonical_json
    from mao.governance.rollout import EpochRouter
    from mao.talk import answer
    from tests.acceptance.m3.gov_world import (
        ROLLOUT,
        changeset_doc,
        commit_change,
        toolbox_v2_image,
        upsert,
    )

    world = open_world(tmp_path)
    hold_serve(world.ctx)
    pipe = world.pipeline()
    source = commit_change(world.repo, toolbox_v2_image())
    rollout = dict(ROLLOUT, canary={"percent": 100, "min_runs": 50})
    cs = pipe.propose(
        changeset_doc(world.base, [upsert("Machine", "toolbox")], rollout=rollout),
        source,
        by="fake-agent@1",
        timeout_s=60,
    )
    assert pipe.advance(cs.id).state == "verified"
    pipe.add_review(cs.id, runtime="script@1", verdict="approve", note="", by="script")
    assert pipe.advance(cs.id).state == "shadow"
    assert pipe.advance(cs.id).state == "canary"
    bean = world.sealed_note("hello factory\n")
    card_id = _propose(world, bean)
    confirmed = answer(world.ctx, card_id, by="ada", confirm=True)
    key = "note-trigger@1:" + canonical_json({"note": bean})
    chosen = EpochRouter(world.ctx, world.epochs).choose(key, line="note-trigger@1")
    [row] = world.rows("SELECT epoch FROM runs WHERE id = ?", (confirmed.card["run_id"],))
    assert row["epoch"] == chosen == cs.commit
    assert chosen != world.base
