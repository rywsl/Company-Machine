"""M13-04：上下文轮换与 agent 角色的 MCP 工具（spec/system/agents.md 3.4、5.6）。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.acceptance.m13.agent_world import World, tool_result

HANDOFF = [{"do": "write", "path": "/workspace/notebook/HANDOFF.md", "text": "进行中：整理笔记 #42\n"}]


def test_rotation_by_turns(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.script(
        {
            "by_turn": {"4": HANDOFF + [{"do": "echo_task", "path": "/workspace/handoff-task.txt"}]},
            "default": [
                {"do": "echo_task", "path": "/workspace/last.txt"},
                {"do": "history", "path": "/workspace/history.json"},
            ],
        }
    )
    host = w.host()
    sessions = []
    for n in range(3):
        w.post("scout", f"round {n}")
        sessions.append(host.run_once("scout").session)
    assert len(set(sessions)) == 1
    assert w.rows("SELECT rotate_pending FROM agents WHERE name = 'scout'")[0]["rotate_pending"] == 1

    w.post("scout", "after rotation")
    turn = host.run_once("scout")
    turns = host.turns("scout")
    assert [(t.kind, t.conversation, t.number) for t in turns] == [
        ("work", 1, 1),
        ("work", 1, 2),
        ("work", 1, 3),
        ("handoff", 1, 4),
        ("work", 2, 1),
    ]
    handoff = turns[3]
    assert handoff.state == "ok" and handoff.session == sessions[0] and handoff.messages == ()
    assert "HANDOFF.md" in w.read("handoff-task.txt")
    assert w.read("notebook/HANDOFF.md") == "进行中：整理笔记 #42\n"
    assert turn.id == turns[4].id and turn.session != sessions[0]
    prompt = w.read("last.txt")
    assert "你是侦察员 scout" in prompt and "HANDOFF.md" in prompt and "after rotation" in prompt
    assert json.loads(w.read("history.json")) == []
    assert w.events("agent.rotated") == [{"agent": "scout", "conversation": 2, "ok": True}]


def test_rotation_by_tokens(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.script({"by_turn": {"1": [{"do": "usage", "input_tokens": 1500}]}, "default": []})
    host = w.host()
    w.post("scout", "big context")
    first = host.run_once("scout")
    assert first.input_tokens == 1500
    w.script({"default": []})
    w.post("scout", "next")
    host.run_once("scout")
    assert [(t.kind, t.conversation) for t in host.turns("scout")] == [
        ("work", 1),
        ("handoff", 1),
        ("work", 2),
    ]
    assert w.rows("SELECT rotate_pending FROM agents WHERE name = 'scout'")[0]["rotate_pending"] == 0

    code, _body, err = w.cli("agent", "rotate", "scout")
    assert code == 0, err
    w.post("scout", "manual rotation")
    host.run_once("scout")
    assert [(t.kind, t.conversation) for t in host.turns("scout")][-2:] == [("handoff", 2), ("work", 3)]
    assert [e["conversation"] for e in w.events("agent.rotated")] == [2, 3]


def test_agent_mcp_tools(tmp_path: Path) -> None:
    w = World(tmp_path)
    bean = w.note("note for the line\n")
    started, error = tool_result(w.mcp("run_start", {"line": "note-trigger@1", "inputs": {"note": bean}}))
    assert error is None and started["id"]
    [run] = w.rows("SELECT trigger FROM runs WHERE id = ?", (started["id"],))
    assert json.loads(run["trigger"]) == {"on": "agent", "agent": "scout"}
    _, error = tool_result(w.mcp("run_start", {"line": "note-pipeline@1", "inputs": {"task": bean}}))
    assert error["code"] == "AGENT_LINE_FORBIDDEN"
    reply = w.mcp("changeset_propose", {"commit": "0" * 40})
    assert reply.get("error", {}).get("message") == "unknown tool"

    w.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "save": "/workspace/mcp.json",
                    "calls": [
                        {"name": "agent_send", "arguments": {"to": "helper", "text": "帮我看看 #42"}},
                        {"name": "remind", "arguments": {"after": "2h", "note": "回头检查 #42"}},
                        {
                            "name": "ask_human",
                            "arguments": {"summary": "要不要发布 #42？", "options": ["要", "不要"]},
                        },
                    ],
                }
            ]
        }
    )
    host = w.host()
    w.post("scout", "please coordinate")
    turn = host.run_once("scout")
    assert turn.state == "ok", turn.error
    [sent] = w.messages("helper")
    assert (sent.kind, sent.source, sent.body) == ("message", "agent:scout", {"text": "帮我看看 #42"})
    [remind] = [m for m in w.messages("scout") if m.kind == "remind"]
    assert (
        remind.state == "pending"
        and remind.not_before is not None
        and remind.body == {"note": "回头检查 #42"}
    )
    asked = w.rows("SELECT origin, summary FROM human_requests WHERE kind = 'question'")
    assert [(r["origin"], r["summary"]) for r in asked] == [("agent:scout", "要不要发布 #42？")]
    assert host.run_once("scout") is None
    w.clock.advance(2 * 3600)
    w.script({"default": []})
    assert host.run_once("scout").messages == (remind.id,)
