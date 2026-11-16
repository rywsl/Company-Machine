"""交接提示、陈旧接管和已解析工作区挂载。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from mao.agents import Mailbox, workspace_path
from mao.assets import capture_handoff
from mao.connectors import ReceiptStore
from mao.learning import LearningStore
from tests.acceptance.m13 import agent_world
from tests.acceptance.m16.asset_world import World


def test_assets_handoff_prompt_asks_for_next_json(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.script(
        {
            "by_turn": {
                "1": [{"do": "echo_task", "path": "/workspace/work-task.txt"}],
                "2": [{"do": "echo_task", "path": "/workspace/handoff-task.txt"}],
            }
        }
    )
    assert world.turn("建立会话") is not None
    world.host().rotate("scout")
    world.post("scout", "触发交接")
    assert world.host().run_once("scout") is None
    task = world.read("handoff-task.txt")
    assert "HANDOFF.next.json" in task
    assert "HANDOFF.md" not in task
    assert not (world.workspace() / "notebook" / "HANDOFF.md").exists()


def test_pending_takeover_keeps_old_workspace_when_business_moves(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.job(world.note("one\ntwo\n"))
    station = world.snapshot.key("Station", "upper@1")
    claimed = ReceiptStore(world.ctx).claim(
        station_key=station,
        key="handoff-progress",
        job_id=job.id,
        run_id=None,
        inputs={},
        verify_method="manual",
    )
    state = world.rows("SELECT state FROM jobs WHERE id = ?", (job.id,))[0]["state"]
    document = world.handoff_document()
    document["data"]["tasks"] = [{"kind": "job", "id": job.id, "state": state, "next": "继续"}]
    document["data"]["external"] = [
        {"resource": "publisher", "receipt": claimed.receipt.id, "next": "核对回执"}
    ]
    handoff = capture_handoff(world.ctx, "scout", document, expected=None, by="ada")
    trial = world.trial(handoff, world.takeover_contract(), "takeover")
    world.script({"default": [{"do": "sleep", "seconds": 2}]})
    world.post("scout", "占用当前轮")
    host = world.host()
    worker = threading.Thread(target=host.run_once, args=("scout",))
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        running = world.rows("SELECT id FROM agent_turns WHERE agent = 'scout' AND state = 'running'")
        if running:
            break
        time.sleep(0.05)
    else:
        worker.join(timeout=3)
        raise AssertionError("工作轮没有进入 running")
    prepared = LearningStore(world.ctx, world.epochs).prepare_takeover(trial["id"], by="ada")
    assert prepared["phase"] == "pending"
    with world.ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET state = 'DEAD' WHERE id = ?", (job.id,))
    ReceiptStore(world.ctx).publish(claimed.receipt.id, {"target": "publisher", "locator": {}, "items": []})
    worker.join(timeout=8)
    assert not worker.is_alive()
    row = world.rows("SELECT current_path, phase FROM agent_workspaces WHERE agent = 'scout'")[0]
    assert row["current_path"] == "agents/scout/workspace"
    assert row["phase"] != "testing"
    assert not (world.root / "agents" / "scout" / "takeovers").exists()
    notices = Mailbox(world.ctx).list("foreman")
    assert any(trial["id"] in str(item.body) for item in notices)


def test_takeover_materializes_declared_bean_into_new_workspace(tmp_path: Path) -> None:
    world = World(tmp_path)
    text = "继续工作的笔记\n"
    bean = world.note(text)
    old = world.workspace()
    old.mkdir(parents=True, exist_ok=True)
    (old / "private-history.txt").write_text("旧私有文件", encoding="utf-8")
    document = world.handoff_document(
        workspace=[{"path": "notebook/kept.txt", "bean": bean, "payload_path": "note.txt"}]
    )
    handoff = capture_handoff(world.ctx, "scout", document, expected=None, by="ada")
    trial = world.trial(handoff, world.takeover_contract(), "takeover")
    world.host().pause("scout")
    prepared = world.learning.prepare_takeover(trial["id"], by="ada")
    assert prepared["phase"] == "testing"
    current = (
        world.root
        / world.rows("SELECT current_path FROM agent_workspaces WHERE agent = 'scout'")[0]["current_path"]
    )
    assert (current / "notebook" / "kept.txt").read_text(encoding="utf-8") == text
    assert (current / ".home").is_dir()
    assert not (current / "private-history.txt").exists()
    assert (world.workspace() / "private-history.txt").is_file()


def test_takeover_workspace_receives_codex_config_and_registry_clone(tmp_path: Path) -> None:
    previous = agent_world.patch_registry

    def patch(repo: Path) -> None:
        previous(repo)
        path = repo / "runtimes" / "fake-agent.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "mcp: {format: claude-json, flag: --mcp-config}",
                "mcp: {format: codex-toml}",
                1,
            ),
            encoding="utf-8",
        )

    agent_world.patch_registry = patch
    try:
        world = World(tmp_path)
    finally:
        agent_world.patch_registry = previous
    handoff = world.handoff(agent="foreman")
    contract = world.contract(
        subject="agent:foreman",
        key="contract.foreman.mount",
        cases=[
            {
                "name": "cover",
                "kind": "turn",
                "subject": "agent:foreman",
                "tags": [
                    "normal",
                    "exception",
                    "recovery",
                    "uncertainty",
                    "planning",
                    "budget",
                    "change",
                    "handoff",
                ],
                "uses_target": True,
                "assert": 'out.state == "ok"',
            }
        ],
    )
    trial = world.trial(handoff, contract, "takeover")
    prepared = world.learning.prepare_takeover(trial["id"], by="ada")
    assert prepared["phase"] == "testing"
    host = world.host()
    snapshot = world.epochs.materialize(world.epochs.current())
    cfg = host._config_for(snapshot, "foreman")
    _argv, extra = host._prepare_ctl(
        snapshot,
        "fake-agent@1",
        world.root / "agents" / "foreman",
        "at-mount",
        cfg,
        "foreman",
        ["fake-agent"],
    )
    mounted = workspace_path(world.ctx, "foreman")
    assert extra["CODEX_HOME"] == "/workspace/.codex"
    assert (mounted / ".codex" / "config.toml").is_file()
    assert not (world.root / "agents" / "foreman" / "workspace" / ".codex").exists()
    clone = host._prepare_foreman_clone(world.epochs.current())
    assert clone == mounted / "registry"
    assert (clone / ".git").is_dir()
    assert not (world.root / "agents" / "foreman" / "workspace" / "registry").exists()
