"""M16-03：可靠轮换和单岗位替换（assets.md 4.5、5.5–5.6）。"""

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.learning")

from tests.acceptance.m16.asset_world import World, error_code  # noqa: E402


def _row(w: World):
    return w.rows("SELECT * FROM agents WHERE name = 'scout'")[0]


def test_failed_handoff_preserves_session_and_last_good_version(tmp_path: Path) -> None:
    """交接 CLI 失败不清旧 session/交接，暂停且只开一个通知。"""
    w = World(tmp_path)
    previous = w.handoff()
    w.script({"default": []})
    w.turn()
    before = dict(_row(w))
    w.host().rotate("scout")
    w.script({"default": [{"do": "exit", "code": 7}]})
    w.post("scout", "待完成工作")
    assert w.host().run_once("scout") is None
    after = _row(w)
    assert after["session"] == before["session"] and after["conversation"] == before["conversation"]
    assert after["state"] == "paused" and after["rotate_pending"] == 1
    assert (
        w.rows("SELECT asset_id FROM asset_heads WHERE slot = 'handoff:scout'")[0]["asset_id"]
        == previous["id"]
    )
    w.host().run_once("scout")
    assert len(w.rows("SELECT id FROM human_requests WHERE origin = 'asset-handoff:scout'")) == 1
    assert w.events("agent.rotated") == []


def test_successful_handoff_is_immutable_before_rotation(tmp_path: Path) -> None:
    """合法结构化交接先固定，下一轮新 session，工作区编辑不改资产。"""
    w = World(tmp_path)
    w.turn()
    w.host().rotate("scout")
    document = w.handoff_document()
    w.script(
        {
            "by_turn": {
                "2": [
                    {
                        "do": "write",
                        "path": "/workspace/notebook/HANDOFF.next.json",
                        "text": json.dumps(document),
                    }
                ]
            },
            "default": [],
        }
    )
    w.post("scout", "继续")
    after = w.host().run_once("scout")
    assert after is not None and after.conversation == 2
    head = w.rows("SELECT asset_id FROM asset_heads WHERE slot = 'handoff:scout'")[0]["asset_id"]
    fixed = w.assets.get(head)
    (w.workspace() / "notebook" / "HANDOFF.next.json").write_text("damaged", encoding="utf-8")
    assert w.assets.get(head) == fixed
    assert w.events("agent.rotated")[-1]["ok"] is True


def test_malformed_or_secret_handoff_never_rotates(tmp_path: Path) -> None:
    """CLI 成功不等于交接成功，损坏或秘密材料不能促成轮换。"""
    from tests.acceptance.m13.agent_world import SCOUT_TOKEN

    for index, content in enumerate(["{broken", json.dumps({"secret": SCOUT_TOKEN})]):
        w = World(tmp_path / str(index))
        w.turn()
        before = dict(_row(w))
        w.host().rotate("scout")
        w.script(
            {"default": [{"do": "write", "path": "/workspace/notebook/HANDOFF.next.json", "text": content}]}
        )
        w.post("scout", "next")
        assert w.host().run_once("scout") is None
        assert _row(w)["session"] == before["session"] and _row(w)["state"] == "paused"


def test_takeover_starts_clean_and_consumes_one_real_queue(tmp_path: Path) -> None:
    """新工作区看不到旧私有历史；代表任务只消费一次并形成有效能力证明。"""
    w = World(tmp_path)
    w.turn("旧私有对话")
    old_workspace = w.workspace()
    (old_workspace / "private-history.txt").write_text("不该出现在新工作区", encoding="utf-8")
    w.host().pause("scout")
    handoff = w.handoff()
    trial = w.trial(handoff, w.takeover_contract(), "takeover")
    prepared = w.learning.prepare_takeover(trial["id"], by="ada")
    assert prepared["phase"] == "testing"
    state = w.rows("SELECT * FROM agent_workspaces WHERE agent = 'scout'")[0]
    current = w.root / state["current_path"]
    assert current != old_workspace and not (current / "private-history.txt").exists()
    assert (old_workspace / "private-history.txt").is_file()
    w.host().resume("scout")
    evidence = w.takeover_work()
    result = w.learning.evaluate(trial["id"], evidence, by="ada")
    assert result["state"] == "passed"
    w.learning.finish_takeover(trial["id"], by="ada")
    assert w.rows("SELECT phase FROM agent_workspaces WHERE agent = 'scout'")[0]["phase"] == "stable"
    assert (
        w.rows("SELECT proof FROM asset_heads WHERE slot = 'capability:agent:scout'")[0]["proof"]
        == trial["id"]
    )
    for value in evidence.values():
        use = w.rows(
            "SELECT handoff FROM asset_uses WHERE kind = 'turn' AND execution_id = ?", (value["id"],)
        )[0]
        assert use["handoff"] == handoff["id"]
    assert len(w.messages("scout", state="done")) == 5  # 一条旧工作，四条新工作


def test_takeover_failure_does_not_redeliver_completed_messages(tmp_path: Path) -> None:
    """失败暂停且保留新工作成果；abort 不把已做事项重新派给旧 worker。"""
    w = World(tmp_path)
    w.turn("旧进度")
    w.host().pause("scout")
    handoff = w.handoff()
    trial = w.trial(handoff, w.takeover_contract(), "takeover")
    w.learning.prepare_takeover(trial["id"], by="ada")
    w.host().resume("scout")
    evidence = w.takeover_work()
    wrong = {**evidence, "uncertainty": evidence["normal"]}
    assert w.learning.evaluate(trial["id"], wrong, by="ada")["state"] == "failed"
    assert _row(w)["state"] == "paused"
    before = [(m.id, m.state) for m in w.messages("scout")]
    with error_code("ASSET_PROOF_REQUIRED"):
        w.learning.finish_takeover(trial["id"], by="ada")
    w.learning.abort_takeover(trial["id"], reason="按实际进度重新交接", by="ada")
    assert [(m.id, m.state) for m in w.messages("scout")] == before
    assert _row(w)["state"] == "paused" and _row(w)["session"] is None


def test_stale_business_progress_blocks_handoff_capture(tmp_path: Path) -> None:
    """交接记录写错任务状态时拒绝，不用旧记忆覆盖真实执行进度。"""
    from mao.assets import capture_handoff

    w = World(tmp_path)
    job = w.job(w.note("one\ntwo\n"))
    doc = w.handoff_document()
    doc["data"]["tasks"] = [{"kind": "job", "id": job.id, "state": "RUNNING", "next": "等待"}]
    with error_code("ASSET_HANDOFF_INVALID"):
        capture_handoff(w.ctx, "scout", doc, expected=None, by="ada")
    assert w.rows("SELECT * FROM asset_heads WHERE slot = 'handoff:scout'") == []


def test_workspace_escape_is_rejected(tmp_path: Path) -> None:
    """交接不能借路径穿越或 CLI 私有目录访问旧数据。"""
    from mao.assets import capture_handoff

    w = World(tmp_path)
    bean = w.note()
    for path in ["../escape", "/absolute", ".codex/auth.json", ".home/history", "C:\\private"]:
        doc = w.handoff_document(workspace=[{"path": path, "bean": bean, "payload_path": "note.txt"}])
        with error_code("ASSET_HANDOFF_INVALID"):
            capture_handoff(w.ctx, "scout", doc, expected=None, by="ada")


def test_ordinary_rotation_does_not_create_takeover_or_duplicate_work(tmp_path: Path) -> None:
    """普通上下文轮换只增加已有 handoff 轮，不建立验证副本或新试验。"""
    w = World(tmp_path)
    w.turn()
    w.host().rotate("scout")
    doc = w.handoff_document()
    w.script(
        {
            "by_turn": {
                "2": [
                    {"do": "write", "path": "/workspace/notebook/HANDOFF.next.json", "text": json.dumps(doc)}
                ]
            },
            "default": [],
        }
    )
    w.turn("第二项真实工作")
    assert [t.kind for t in w.host().turns("scout")] == ["work", "handoff", "work"]
    assert w.rows("SELECT id FROM asset_trials") == []
    assert len(w.messages("scout", state="done")) == 2
