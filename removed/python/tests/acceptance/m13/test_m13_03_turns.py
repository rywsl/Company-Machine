"""M13-03：轮次、续接、预算、失败与恢复、命令行（spec/system/agents.md 5.3–5.5）。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.acceptance.m13.agent_world import World


def test_turns_resume_one_conversation(tmp_path: Path) -> None:
    w = World(tmp_path, record=True)
    w.script(
        {
            "by_turn": {
                "1": [{"do": "echo_task", "path": "/workspace/t1.txt"}],
                "2": [
                    {"do": "history", "path": "/workspace/h2.json"},
                    {"do": "echo_task", "path": "/workspace/t2.txt"},
                ],
            }
        }
    )
    host = w.host()
    w.post("scout", "first message")
    one = host.run_once("scout")
    assert (one.state, one.kind, one.conversation, one.number) == ("ok", "work", 1, 1)
    assert one.session == f"fake-{one.id}-1"
    first_prompt = w.read("t1.txt")
    assert "你是侦察员 scout" in first_prompt and "first message" in first_prompt

    w.post("scout", "second message")
    two = host.run_once("scout")
    assert (two.state, two.conversation, two.number, two.session) == ("ok", 1, 2, one.session)
    assert json.loads(w.read("h2.json")) == [first_prompt]
    second_prompt = w.read("t2.txt")
    assert "second message" in second_prompt and "你是侦察员 scout" not in second_prompt
    agent_specs = [s for s in w.opened if s.name == "mao-agent-scout"]
    assert len(agent_specs) == 1
    assert [e["turn"] for e in w.events("agent.turn.finished")] == [one.id, two.id]
    assert [t.id for t in host.turns("scout")] == [one.id, two.id]


def test_batch_and_messages_file(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.script(
        {
            "default": [
                {"do": "copy", "from": "/factory/in/messages.json", "to": "/workspace/messages.json"},
                {"do": "echo_task", "path": "/workspace/prompt.txt"},
            ]
        }
    )
    host = w.host()
    posted = [w.post("scout", f"note {n}").id for n in range(5)]
    turn = host.run_once("scout")
    assert turn.messages == tuple(posted[:3])
    seen = json.loads(w.read("messages.json"))
    assert [m["id"] for m in seen] == posted[:3]
    assert [m["body"]["text"] for m in seen] == ["note 0", "note 1", "note 2"]
    prompt = w.read("prompt.txt")
    assert "[1] message" in prompt and posted[2] in prompt and posted[3] not in prompt
    states = {m.id: m.state for m in w.messages("scout")}
    assert [states[i] for i in posted] == ["done", "done", "done", "pending", "pending"]
    turn = host.run_once("scout")
    assert turn.messages == tuple(posted[3:])
    assert host.run_once("scout") is None


def test_daily_budget(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.script({"cost_usd": 0.6, "default": []})
    host = w.host()
    for n in range(2):
        w.post("scout", f"work {n}")
        assert host.run_once("scout").state == "ok"
    waiting = w.post("scout", "over budget")
    assert host.run_once("scout") is None
    assert host.run_once("scout") is None
    assert [m.state for m in w.messages("scout") if m.id == waiting.id] == ["pending"]
    [event] = w.events("agent.budget_exhausted")
    assert (event["agent"], event["daily_usd"]) == ("scout", 1.0) and event["spent_usd"] >= 1.0
    w.clock.advance(24 * 3600 + 1)
    assert host.run_once("scout").state == "ok"
    assert len(w.events("agent.budget_exhausted")) == 1


def test_failures_retry_then_drop(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.script({"default": [{"do": "exit", "code": 3}]})
    host = w.host()
    message = w.post("scout", "will fail")
    states = []
    for _ in range(3):
        turn = host.run_once("scout")
        states.append(turn.state)
        assert turn.error["code"] == "SESSION_FAILED"
    assert states == ["failed", "failed", "failed"]
    [after] = w.messages("scout")
    assert (after.id, after.state, after.attempts) == (message.id, "dropped", 3)
    [dropped] = w.events("agent.dropped")
    assert dropped["messages"] == [message.id]
    item = w.rows("SELECT id, kind, origin, summary FROM human_requests WHERE id = ?", (dropped["request"],))[
        0
    ]
    assert (item["kind"], item["origin"]) == ("question", "agent:scout")
    assert "连续 3 轮失败" in item["summary"]
    assert host.run_once("scout") is None


def test_recover_interrupted(tmp_path: Path) -> None:
    w = World(tmp_path)
    host = w.host()
    message = w.post("scout", "in flight")
    with w.ctx.db.tx() as conn:
        conn.execute(
            """INSERT INTO agent_turns (id, agent, conversation, number, kind, session, state, messages,
               started_at) VALUES ('at-crash', 'scout', 1, 1, 'work', NULL, 'running', ?, '2026-01-01T00:00:00Z')""",
            (json.dumps([message.id]),),
        )
        conn.execute(
            "UPDATE agent_messages SET state = 'delivered', turn = 'at-crash' WHERE id = ?", (message.id,)
        )
    assert host.recover() == ["at-crash"]
    [turn] = host.turns("scout")
    assert (turn.state, turn.error["code"]) == ("interrupted", "AGENT_INTERRUPTED")
    [after] = w.messages("scout")
    assert (after.state, after.attempts, after.turn) == ("pending", 0, None)
    w.script({"default": []})
    assert host.run_once("scout").messages == (message.id,)


def test_agent_cli(tmp_path: Path) -> None:
    w = World(tmp_path)
    code, body, err = w.cli("agent", "list")
    assert code == 0, err
    assert [(a["name"], a["state"]) for a in body] == [("helper", "active"), ("scout", "active")]
    code, body, err = w.cli("agent", "send", "scout", "--text", "hi")
    assert code == 0, err
    code, body, err = w.cli("agent", "pause", "scout")
    assert code == 0, err
    host = w.host()
    assert host.run_once("scout") is None
    code, body, err = w.cli("agent", "show", "scout")
    assert code == 0, err
    assert (body["name"], body["state"], body["pending"], body["session"]) == ("scout", "paused", 1, None)
    code, body, err = w.cli("agent", "resume", "scout")
    assert code == 0, err
    w.script({"default": []})
    turn = host.run_once("scout")
    assert turn.state == "ok"
    code, body, err = w.cli("agent", "turns", "scout")
    assert code == 0, err
    assert [(t["id"], t["state"], t["kind"]) for t in body] == [(turn.id, "ok", "work")]
    code, body, err = w.cli("agent", "show", "scout")
    assert (body["state"], body["pending"], body["session"]) == ("active", 0, turn.session)


def test_run_serve_hosts_agents(tmp_path: Path) -> None:
    import threading
    import time

    from mao.agents import list_turns
    from mao.daemon import InlineJobExecutor, run_serve
    from mao.durable import LeaseStore
    from mao.jobs import load_job
    from mao.runner import Runner
    from tests.acceptance.m12.instance_world import SCHEDULER, serve_config

    w = World(tmp_path)
    w.script({"default": []})
    w.post("helper", "hosted")
    (w.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    executor = InlineJobExecutor(
        lambda job_id: Runner(w.ctx, w.epochs.materialize(load_job(w.ctx.db, job_id).epoch))
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=run_serve,
        args=(w.ctx, serve_config(tick_s=0.05)),
        kwargs={"executor": executor, "stop": stop, "epochs": w.epochs, "pipeline": w.pipeline()},
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and [m.state for m in w.messages("helper")] != ["done"]:
            time.sleep(0.1)
        assert [m.state for m in w.messages("helper")] == ["done"]
        assert LeaseStore(w.ctx).valid("agents") is not None
        assert "mao-agents" in [t.name for t in threading.enumerate()]
    finally:
        stop.set()
        thread.join(180)
    assert not thread.is_alive()
    assert LeaseStore(w.ctx).valid("agents") is None
    assert [t.state for t in list_turns(w.ctx, "helper")] == ["ok"]
