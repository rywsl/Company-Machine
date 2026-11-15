"""M15-02 验收：talk 轮次的卡片与后来的状态（spec/system/talk.md 第 5.5 节）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.talk")

from tests.acceptance.m3.gov_world import toolbox_v2_image, upsert  # noqa: E402
from tests.acceptance.m5.auto_world import commit_actions, propose_action  # noqa: E402
from tests.acceptance.m15.talk_world import (  # noqa: E402
    SECRET,
    cards,
    dumped,
    hold_serve,
    open_world,
    thread,
)


def _hash_tree(root: Path) -> dict[str, str]:
    import hashlib

    found = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def test_escalate_card_on_foreman_turn(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "calls": [
                        {
                            "name": "escalate",
                            "arguments": {"summary": "要不要停", "options": ["停", "继续"]},
                        },
                        {"name": "talk_reply", "arguments": {"text": "已上报"}},
                    ],
                }
            ]
        }
    )
    from mao.agents import Mailbox

    Mailbox(world.ctx).post("foreman", "message", "human:acceptance", {"text": "不是 talk"})
    host = world.host()
    assert host.run_once("foreman").state == "ok"
    [early] = cards(world, "escalate")
    assert early.card["status"] == "open" and early.ref_id == early.card["request_id"]

    from mao.talk import post_human

    post_human(world.ctx, "请上报", by="ada")
    assert host.run_once("foreman").state == "ok"
    found = cards(world, "escalate")
    assert len(found) == 2 and found[0].id == early.id
    card = found[1]
    assert card.card["status"] == "open" and card.card["type"] == "escalate"
    assert card.ref_kind == "inbox" and card.ref_id == card.card["request_id"]
    assert "value" not in card.card


def test_changeset_card_and_status(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    world.script(
        {
            "default": [
                *commit_actions(toolbox_v2_image(), "talk change"),
                propose_action([upsert("Machine", "toolbox")]),
                {"do": "mcp", "calls": [{"name": "talk_reply", "arguments": {"text": "已提议"}}]},
            ]
        }
    )
    from mao.talk import post_human

    post_human(world.ctx, "改一下镜像", by="ada")
    assert world.host().run_once("foreman").state == "ok"
    [card] = cards(world, "changeset")
    assert card.card["state"] == "draft" and card.card["id"]
    [row] = world.rows("SELECT id, state FROM changesets")
    assert row["id"] == card.card["id"] and row["state"] == "draft"

    world.pipeline().withdraw(card.card["id"], by="ada")
    states = [item.card.get("state") for item in cards(world, "changeset")]
    assert states == ["draft", "withdrawn"]
    assert cards(world, "changeset")[-1].ref_id == card.card["id"]


def test_secret_card_omits_value(tmp_path: Path) -> None:
    world = open_world(tmp_path, secrets=True)
    hold_serve(world.ctx)
    world.put_secret("DESK_TOKEN", "other", SECRET)
    world.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "calls": [
                        {
                            "name": "secret_request",
                            "arguments": {
                                "name": "DESK_TOKEN",
                                "secret_class": "other",
                                "station": "talk-secret@1",
                                "reason": f"需要 {SECRET}",
                            },
                        },
                        {"name": "talk_reply", "arguments": {"text": "已申请"}},
                    ],
                }
            ]
        }
    )
    from mao.talk import post_human

    post_human(world.ctx, "申请令牌", by="ada")
    turn = world.host().run_once("foreman")
    assert turn.state == "ok", turn.error
    [card] = cards(world, "secret")
    assert set(card.card) == {"type", "request_id", "status", "name", "station"}
    assert card.card["name"] == "DESK_TOKEN" and card.card["status"] == "open"
    assert SECRET not in dumped(world)

    from mao.inbox import Inbox

    Inbox(world.ctx).cancel(card.ref_id, by="ada", note=SECRET)
    assert SECRET not in dumped(world)
    assert cards(world, "secret")[-1].card["status"] == "cancelled"


def test_bean_sealed_stays_out(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    from mao.talk import post_human

    post_human(world.ctx, "先说一句", by="ada")
    before = len(thread(world))
    world.sealed_note("sealed outside talk\n")
    assert len(thread(world)) == before
    assert cards(world) == []


def test_talk_does_not_advance_or_write_constitution(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    factory_before = (world.root / "factory.json").read_bytes()
    constitution_before = _hash_tree(world.repo / "constitution")
    world.script(
        {
            "default": [
                *commit_actions(toolbox_v2_image(), "talk change"),
                propose_action([upsert("Machine", "toolbox")]),
                {"do": "mcp", "calls": [{"name": "talk_reply", "arguments": {"text": "只提议"}}]},
            ]
        }
    )
    from mao.talk import post_human

    post_human(world.ctx, "不要合入", by="ada")
    assert world.host().run_once("foreman").state == "ok"
    [card] = cards(world, "changeset")
    assert card.card["state"] == "draft"
    assert (world.root / "factory.json").read_bytes() == factory_before
    assert _hash_tree(world.repo / "constitution") == constitution_before
    assert world.rows("SELECT id FROM runs") == []
