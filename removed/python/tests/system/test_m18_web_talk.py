from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from mao.durable import LeaseStore
from mao.talk import list_messages
from mao.webapp.server import WebServer
from tests.acceptance.m15.talk_world import hold_serve, open_world


def test_web_talk_queues_into_existing_mailbox(tmp_path: Path) -> None:
    world = open_world(tmp_path, mode="resident")
    hold_serve(world.ctx, owner="m18-web")
    server = WebServer(world.ctx, snapshot=world.snapshot)
    port = server.start(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/web/talk",
            data=json.dumps({"text": "建立一个视频处理厂区"}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["state"] == "queued"
        assert payload["message"]["role"] == "human"
        assert payload["message"]["body"] == "建立一个视频处理厂区"
        messages = list_messages(world.ctx)
        assert messages[-1].body == "建立一个视频处理厂区"
        assert messages[-1].mailbox_id
        mailbox = world.ctx.db.query(
            "SELECT agent, kind, source, body FROM agent_messages WHERE id = ?", (messages[-1].mailbox_id,)
        )
        assert mailbox[0]["agent"] == "foreman"
        assert mailbox[0]["kind"] == "talk"
        assert mailbox[0]["source"] == "talk:" + messages[-1].id
        assert messages[-1].id in mailbox[0]["body"]
    finally:
        server.close()
        LeaseStore(world.ctx).release("serve", owner="m18-web")
        world.ctx.close()


def test_web_talk_exposes_serve_down_without_claiming_success(tmp_path: Path) -> None:
    world = open_world(tmp_path, mode="resident")
    server = WebServer(world.ctx, snapshot=world.snapshot)
    port = server.start(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/web/talk",
            data=json.dumps({"text": "继续处理"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 409
            payload = json.loads(error.read().decode("utf-8"))
            assert payload["code"] == "TALK_SERVE_DOWN"
            assert payload["message"]
        else:
            raise AssertionError("serve down must remain visible")
    finally:
        server.close()
        world.ctx.close()
