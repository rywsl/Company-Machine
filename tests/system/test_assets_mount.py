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


def _wait_running(world: World, agent: str, worker: threading.Thread) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        running = world.rows("SELECT id FROM agent_turns WHERE agent = ? AND state = 'running'", (agent,))
        if running:
            return
        time.sleep(0.05)
    worker.join(timeout=3)
    raise AssertionError("工作轮没有进入 running")


def test_failed_turn_cancels_pending_and_later_success_keeps_old_mount(tmp_path: Path) -> None:
    world = World(tmp_path)
    handoff = world.handoff()
    trial = world.trial(handoff, world.takeover_contract(), "takeover")
    world.script({"default": [{"do": "sleep", "seconds": 2}, {"do": "exit", "code": 1}]})
    world.post("scout", "这轮会失败")
    host = world.host()
    worker = threading.Thread(target=host.run_once, args=("scout",))
    worker.start()
    _wait_running(world, "scout", worker)
    prepared = world.learning.prepare_takeover(trial["id"], by="ada")
    assert prepared["phase"] == "pending"
    worker.join(timeout=8)
    assert not worker.is_alive()
    failed = world.rows("SELECT state FROM agent_turns WHERE agent = 'scout' ORDER BY started_at")[-1]
    assert failed["state"] != "ok"
    row = world.rows("SELECT current_path, phase, trial_id FROM agent_workspaces WHERE agent = 'scout'")[0]
    assert row["current_path"] == "agents/scout/workspace"
    assert row["phase"] != "pending" and row["trial_id"] is None
    world.script({"default": [{"do": "echo_task", "path": "/workspace/later.txt"}]})
    later = host.run_once("scout")
    assert later is not None and later.state == "ok"
    again = world.rows("SELECT current_path, phase FROM agent_workspaces WHERE agent = 'scout'")[0]
    assert again["current_path"] == "agents/scout/workspace"
    assert again["phase"] != "testing"
    assert not (world.root / "agents" / "scout" / "takeovers").exists()


def test_recover_cancels_pending_before_a_later_success(tmp_path: Path) -> None:
    world = World(tmp_path)
    handoff = world.handoff()
    trial = world.trial(handoff, world.takeover_contract(), "takeover")
    world.script({"default": [{"do": "sleep", "seconds": 3}, {"do": "exit", "code": 1}]})
    world.post("scout", "这轮会被中断")
    host = world.host()
    worker = threading.Thread(target=host.run_once, args=("scout",))
    worker.start()
    _wait_running(world, "scout", worker)
    assert world.learning.prepare_takeover(trial["id"], by="ada")["phase"] == "pending"
    interrupted = host.recover()
    assert interrupted
    row = world.rows("SELECT current_path, phase, trial_id FROM agent_workspaces WHERE agent = 'scout'")[0]
    assert row["current_path"] == "agents/scout/workspace"
    assert row["phase"] != "pending" and row["trial_id"] is None
    worker.join(timeout=8)
    world.script({"default": [{"do": "echo_task", "path": "/workspace/after-recover.txt"}]})
    later = host.run_once("scout")
    assert later is not None and later.state == "ok"
    again = world.rows("SELECT current_path, phase FROM agent_workspaces WHERE agent = 'scout'")[0]
    assert again["current_path"] == "agents/scout/workspace" and again["phase"] != "testing"


def test_pending_second_takeover_keeps_installed_workspace(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.host().pause("scout")
    first = world.handoff()
    trial = world.trial(first, world.takeover_contract(), "takeover")
    prepared = world.learning.prepare_takeover(trial["id"], by="ada")
    assert prepared["phase"] == "testing"
    mounted = world.rows("SELECT current_path, previous_path FROM agent_workspaces WHERE agent = 'scout'")[0]
    first_path = str(mounted["current_path"])
    world.host().resume("scout")
    evidence = world.takeover_work()
    assert world.learning.evaluate(trial["id"], evidence, by="ada")["state"] == "passed"
    world.learning.finish_takeover(trial["id"], by="ada")
    second = capture_handoff(world.ctx, "scout", world.handoff_document(), expected=first["id"], by="ada")
    trial2 = world.trial(second, world.takeover_contract(), "takeover")
    world.script({"default": [{"do": "sleep", "seconds": 2}]})
    world.post("scout", "占用已安装的工作区")
    host = world.host()
    worker = threading.Thread(target=host.run_once, args=("scout",))
    worker.start()
    _wait_running(world, "scout", worker)
    prepared2 = world.learning.prepare_takeover(trial2["id"], by="ada")
    assert prepared2["phase"] == "pending"
    row = world.rows("SELECT current_path, previous_path, phase FROM agent_workspaces WHERE agent = 'scout'")[
        0
    ]
    assert row["current_path"] == first_path
    assert row["previous_path"] == mounted["previous_path"]
    assert row["phase"] == "pending"
    assert first_path != "agents/scout/workspace"
    worker.join(timeout=8)


def test_work_prompt_reads_handoff_from_mounted_workspace(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.host().pause("scout")
    handoff = world.handoff()
    trial = world.trial(handoff, world.takeover_contract(), "takeover")
    world.learning.prepare_takeover(trial["id"], by="ada")
    mounted = workspace_path(world.ctx, "scout")
    (mounted / "notebook").mkdir(parents=True, exist_ok=True)
    (mounted / "notebook" / "HANDOFF.md").write_text("新工作区的交接\n", encoding="utf-8")
    old = world.workspace()
    old.mkdir(parents=True, exist_ok=True)
    assert not (old / "notebook" / "HANDOFF.md").exists()
    world.script({"default": [{"do": "echo_task", "path": "/workspace/prompt.txt"}]})
    world.host().resume("scout")
    world.post("scout", "读取挂载的交接")
    turn = world.host().run_once("scout")
    assert turn is not None and turn.state == "ok"
    prompt = (mounted / "prompt.txt").read_text(encoding="utf-8")
    assert mounted != old
    assert "上一段对话的交接在 /workspace/notebook/HANDOFF.md" in prompt


def test_checkpoint_copies_business_from_mounted_takeover_workspace(tmp_path: Path) -> None:
    from mao.packages import create_checkpoint, restore_checkpoint

    world = World(tmp_path)
    world.host().pause("scout")
    handoff = world.handoff()
    trial = world.trial(handoff, world.takeover_contract(), "takeover")
    world.learning.prepare_takeover(trial["id"], by="ada")
    mounted = workspace_path(world.ctx, "scout")
    text = "只在接管工作区\n"
    (mounted / "notebook" / "only-here.txt").write_text(text, encoding="utf-8")
    assert not (world.workspace() / "notebook" / "only-here.txt").exists()
    root = world.factory_asset()
    dest = tmp_path / "checkpoint"
    create_checkpoint(world.ctx, dest, factory_asset=root["id"])
    copied = dest / "business" / "scout" / "notebook" / "only-here.txt"
    assert copied.is_file() and copied.read_text(encoding="utf-8") == text
    restored = tmp_path / "restored"
    restore_checkpoint(dest, restored)
    current = world.rows("SELECT current_path FROM agent_workspaces WHERE agent = 'scout'")[0]["current_path"]
    assert (restored / current / "notebook" / "only-here.txt").read_text(encoding="utf-8") == text
