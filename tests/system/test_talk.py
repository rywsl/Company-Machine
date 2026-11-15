"""mao.talk 与 factory talk 的真实入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from mao.commands.talk import register
from mao.talk import list_messages, post_human, show, talk_reply
from tests.acceptance.m15.talk_world import hold_serve, open_world


def test_registrar_and_cli_send(tmp_path: Path) -> None:
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command", required=True))
    names = {action.dest for action in parser._actions}
    assert "talk_cmd" in names or any(getattr(action, "choices", None) for action in parser._actions)

    world = open_world(tmp_path)
    hold_serve(world.ctx)
    sent = post_human(world.ctx, "直接写入", by="ada")
    code, body, err = world.cli("talk", "send", "--by", "ada", "走命令")
    assert code == 0, err
    assert body["role"] == "human" and body["by"] == "ada"
    listed = list_messages(world.ctx)
    assert [item.id for item in listed] == [sent.id, body["id"]]
    assert show(world.ctx, body["id"]).to_json()["body"] == body["body"]
    code, shown, err = world.cli("talk", "show", body["id"])
    assert code == 0, err
    assert shown["id"] == body["id"]


def test_reply_outside_turn_is_rejected(tmp_path: Path) -> None:
    world = open_world(tmp_path)
    hold_serve(world.ctx)
    try:
        talk_reply(world.ctx, "在", by="fake-agent@1", turn_id="")
    except Exception as exc:
        assert exc.code == "TALK_NOT_IN_TURN"
    else:
        raise AssertionError("轮次外的 talk_reply 应当失败")
