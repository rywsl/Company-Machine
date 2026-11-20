"""交接失败与接管中止的真实服务路径。"""

from __future__ import annotations

from pathlib import Path

from mao.assets import capture_handoff
from mao.errors import FactoryError
from mao.learning import LearningStore
from tests.acceptance.m16.asset_world import World


def test_secret_handoff_and_bad_task_do_not_move_the_head(tmp_path: Path) -> None:
    world = World(tmp_path)
    previous = world.handoff()
    secret = world.root.joinpath("secrets", "values", "SCOUT_TOKEN").read_text(encoding="utf-8").strip()
    leaked = world.handoff_document()
    leaked["summary"] = "含秘密 " + secret
    try:
        capture_handoff(world.ctx, "scout", leaked, expected=previous["id"], by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_SECRET"
    else:
        raise AssertionError("含秘密的交接不应固化")
    job = world.job(world.note("one\ntwo\n"))
    stale = world.handoff_document()
    stale["data"]["tasks"] = [{"kind": "job", "id": job.id, "state": "RUNNING", "next": "等待"}]
    try:
        capture_handoff(world.ctx, "scout", stale, expected=previous["id"], by="ada")
    except FactoryError as exc:
        assert exc.code == "ASSET_HANDOFF_INVALID"
    else:
        raise AssertionError("错误任务状态不应覆盖交接")
    head = world.rows("SELECT asset_id FROM asset_heads WHERE slot = 'handoff:scout'")[0]["asset_id"]
    assert head == previous["id"]


def test_takeover_abort_restores_workspace_and_keeps_pause(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.turn("旧进度")
    world.host().pause("scout")
    handoff = world.handoff()
    store = LearningStore(world.ctx, world.epochs)
    trial = store.open_trial(handoff["id"], world.takeover_contract()["id"], purpose="takeover", by="ada")
    prepared = store.prepare_takeover(trial["id"], by="ada")
    assert prepared["phase"] == "testing"
    aborted = store.abort_takeover(trial["id"], reason="按实际进度重新交接", by="ada")
    assert aborted["phase"] == "stable"
    row = world.rows("SELECT state, session FROM agents WHERE name = 'scout'")[0]
    assert row["state"] == "paused" and row["session"] is None
    current = world.rows("SELECT current_path, phase FROM agent_workspaces WHERE agent = 'scout'")[0]
    assert current["phase"] == "stable"
    assert str(current["current_path"]).endswith("agents/scout/workspace") or current[
        "current_path"
    ].endswith("agents\\scout\\workspace")
