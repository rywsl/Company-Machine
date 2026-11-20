"""M17-02：真实 serve 装配、启动失败可重试、失租后的 MCP 不能改状态。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from mao.durable import LeaseStore
from mao.mcp import McpServer, Session
from tests.acceptance.m13.agent_world import World


def test_lost_lease_blocks_late_mcp_side_effect(tmp_path: Path) -> None:
    world = World(tmp_path)
    store = LeaseStore(world.ctx)
    token = store.acquire_fenced("agents", owner=world.ctx.owner)
    assert token is not None
    server = McpServer(
        world.ctx,
        world.epochs,
        Session(
            "agent",
            "fake-agent@1",
            agent="helper",
            turn_id="at-late",
            lease_owner=token.owner,
            lease_generation=token.generation,
        ),
    )
    server.initialized = True
    world.clock.advance(120)
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "agent_send", "arguments": {"to": "scout", "text": "late"}},
        }
    )
    body = json.loads(reply["result"]["content"][0]["text"])
    assert reply["result"]["isError"] is True
    assert body["code"] == "LEASE_LOST"
    assert world.messages("scout") == []
    world.ctx.close()


def test_host_process_stops_when_serve_stop_is_set(tmp_path: Path) -> None:
    world = World(tmp_path)
    host = world.host()
    host.pause("scout")
    host.pause("helper")
    stop = threading.Event()
    worker = threading.Thread(target=host.serve, args=(stop,), daemon=True)
    worker.start()
    try:
        deadline = __import__("time").monotonic() + 3
        while __import__("time").monotonic() < deadline and LeaseStore(world.ctx).valid("agents") is None:
            __import__("time").sleep(0.02)
        assert LeaseStore(world.ctx).valid("agents") is not None
        stop.set()
        worker.join(timeout=2)
    finally:
        stop.set()
        worker.join(timeout=5)
        world.ctx.close()
    assert not worker.is_alive()
