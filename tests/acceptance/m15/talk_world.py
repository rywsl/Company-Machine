"""M15 验收夹具。导入本模块不会加载 mao。

在 M13 的 World 上：缺省 `foreman.mode = resident`。需要秘密卡片时，给 toolbox 加上
`DESK_TOKEN` 的核验配方，并添加工位 `talk-secret@1`。这些改动只发生在测试副本上。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

POLICY = """apiVersion: mao/v1
kind: Policy
metadata: {name: talk-secret, version: 1}
spec:
  applies_to: [transform]
  egress: {none: true}
  secrets: [DESK_TOKEN]
"""

STATION = """apiVersion: mao/v1
kind: Station
metadata: {name: talk-secret, version: 1}
spec:
  role: transform
  machine: toolbox@1
  recipe: upper
  operator: {runtime: script@1}
  inputs:  {note: {type: note.text@1, requires: [non_empty]}}
  outputs: {note: {type: note.text@1, guarantees: [non_empty, shouting]}}
  limits:  {timeout: 60s, budget_usd: 0, retries: {rejected: 0, failed: 1, mode: fresh}}
  policy: talk-secret@1
"""

SECRET = "super-secret-value"


def _add_secret_station(repo: Path) -> None:
    machine = repo / "machines" / "toolbox" / "machine.yaml"
    text = machine.read_text(encoding="utf-8")
    old = "  needs: {egress: none}\n"
    new = (
        "  needs:\n"
        "    egress: none\n"
        "    secrets: [DESK_TOKEN]\n"
        "    secret_checks:\n"
        "      DESK_TOKEN: {run: [desk-check, DESK_TOKEN], timeout_s: 30}\n"
    )
    if text.count(old) != 1:
        raise AssertionError("toolbox needs layout changed")
    machine.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    (repo / "policies" / "talk-secret.yaml").write_text(POLICY, encoding="utf-8", newline="\n")
    (repo / "stations" / "talk-secret.yaml").write_text(STATION, encoding="utf-8", newline="\n")


def open_world(tmp: Path, *, mode: str = "resident", secrets: bool = False) -> Any:
    import tests.acceptance.m13.agent_world as agent_world

    original = agent_world.patch_registry

    def patch(repo: Path) -> None:
        original(repo)
        if secrets:
            _add_secret_station(repo)

    agent_world.patch_registry = patch
    try:
        return agent_world.World(tmp, foreman={"mode": mode})
    finally:
        agent_world.patch_registry = original


def hold_serve(ctx: Any, owner: str = "m15") -> None:
    from mao.durable import LeaseStore

    assert LeaseStore(ctx).acquire("serve", owner=owner, ttl_s=3600)


def drop_agents(world: Any) -> None:
    path = world.root / "factory.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body.pop("agents", None)
    path.write_text(json.dumps(body), encoding="utf-8")


def thread(world: Any) -> list[Any]:
    from mao.talk import list_messages

    return list_messages(world.ctx)


def cards(world: Any, card_type: str | None = None) -> list[Any]:
    found = [item for item in thread(world) if item.kind == "card"]
    if card_type is not None:
        found = [item for item in found if item.card_type == card_type]
    return found


def dumped(world: Any) -> str:
    chunks = []
    for item in thread(world):
        chunks.append(item.body)
        chunks.append(json.dumps(item.card, ensure_ascii=False))
    return "\n".join(chunks)


def foreman_call(
    world: Any, name: str, arguments: dict[str, Any], *, turn_id: str | None = None
) -> tuple[Any, Any]:
    from mao.mcp import McpServer, Session
    from tests.acceptance.m13.agent_world import tool_result

    server = McpServer(
        world.ctx,
        world.epochs,
        Session(role="foreman", actor="fake-agent@1", wake_id=turn_id),
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
    reply = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    return tool_result(reply)
