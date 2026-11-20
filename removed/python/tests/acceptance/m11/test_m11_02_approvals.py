"""M11-02 验收：授权模式、厂长审批、改道给人与 factoryctl ask（spec/system/delegation.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.delegation")

from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m11.deleg_world import World  # noqa: E402

TIERS = ("T0", "T1", "T2", "T3")
DECIDE = {
    "do": "mcp",
    "calls": [
        {
            "name": "approval_decide",
            "arguments": {
                "approval_id": "@json:/factory/in/wake.json#subject",
                "decision": "approve",
                "note": "金额合理",
            },
        }
    ],
}


def _decide(w: World, approval_id: str, decision: str, note: str = "") -> dict:
    got, err = w.mcp("approval_decide", {"approval_id": approval_id, "decision": decision, "note": note})
    assert err is None, err
    return got


def test_autonomy_config(tmp_path: Path) -> None:
    from mao.delegation import AutonomyConfig

    w = World(tmp_path)
    config = AutonomyConfig.load(w.root)
    assert config.mode == "standard" and dict(config.changesets) == {tier: "review" for tier in TIERS}
    assert config.route("human") == "human" and config.route("review") == "review"

    w.set_config("autonomy", {"mode": "bypass", "changesets": {"T3": "human"}})
    code, body, _ = w.cli("autonomy", "show")
    assert code == 0, body
    assert body == {
        "mode": "bypass",
        "changesets": {tier: "auto" for tier in TIERS},
        "configured": {"T0": "review", "T1": "review", "T2": "review", "T3": "human"},
    }
    bypass = AutonomyConfig.load(w.root)
    routes = [bypass.route(r) for r in ("auto", "foreman", "review", "human")]
    assert routes == ["auto", "foreman", "foreman", "foreman"]
    assert bypass.signoff("T3") == "auto"

    for section, field in (
        ({"mode": "yolo"}, "mode"),
        ({"changesets": {"T9": "auto"}}, "changesets.T9"),
        ({"changesets": {"T2": "maybe"}}, "changesets.T2"),
        ({"changesets": ["T2"]}, "changesets"),
        ({"mode": "standard", "extra": 1}, "extra"),
    ):
        w.set_config("autonomy", section)
        with pytest.raises(FactoryError) as ei:
            AutonomyConfig.load(w.root)
        assert ei.value.code == "AUTONOMY_CONFIG_INVALID" and ei.value.detail["field"] == field
    code, body, err = w.cli("autonomy", "show")
    assert code == 1 and w.error_code(body, err) == "AUTONOMY_CONFIG_INVALID"


def test_foreman_route_wakes_and_decides(tmp_path: Path) -> None:
    w = World(tmp_path)
    bg = w.start("desk-foreman@1")
    approval = w.pending(bg.job_id)
    assert approval["route"] == "foreman"
    w.awaiting(bg.job_id)
    [wake] = w.wakes("approval")
    assert wake["subject"] == approval["id"] and wake["status"] == "open"
    assert json.loads(wake["payload"]) == {"id": approval["id"], "job": bg.job_id, "kind": "cost"}

    got, err = w.mcp("approval_get", {"approval_id": approval["id"]})
    assert err is None, err
    assert (got["kind"], got["route"], got["status"], got["detail"]) == ("cost", "foreman", "pending", "")
    assert got["job"] == {"id": bg.job_id, "station": w.key("desk-foreman@1"), "state": "AWAITING_APPROVAL"}

    decided = _decide(w, approval["id"], "approve", "金额合理")
    assert (decided["status"], decided["decided_by"]) == ("approved", "foreman:fake-agent@1")
    job = bg.join()
    assert job.state.value == "SEALED", job.error
    assert w.output(job)["response"]["decision"] == "approve"
    assert w.cli("inbox", "ls")[1] == []


def test_supervisor_decides_approval(tmp_path: Path) -> None:
    w = World(tmp_path, script={"roles": {"foreman": [DECIDE]}})
    sup = w.supervisor()
    sup.start()
    try:
        bg = w.start("desk-foreman@1")
        approval = w.pending(bg.job_id)
        record = sup.run_once()
        assert record is not None and (record.role, record.state) == ("foreman", "ok"), record
        assert bg.join().state.value == "SEALED"
        row = w.ctx.db.query("SELECT status, decided_by FROM approvals WHERE id = ?", (approval["id"],))[0]
        assert (row["status"], row["decided_by"]) == ("approved", "foreman:fake-agent@1")
        assert [wake["status"] for wake in w.wakes("approval")] == ["done"]

        w.script({"roles": {"foreman": []}})
        bg2 = w.start("desk-foreman@1")
        second = w.pending(bg2.job_id)
        failed = sup.run_once()
        assert failed is not None and failed.state == "failed"
        assert failed.error["code"] == "APPROVAL_UNDECIDED"
        code, _, _ = w.cli("approval", "decide", second["id"], "deny", "--by", "human:alice")
        assert code == 0
        assert bg2.join().state.value == "SEALED"
        w.clock.advance(3600)
        assert sup.run_once() is None
        assert [wake["status"] for wake in w.wakes("approval")] == ["done", "done"]
        assert len(sup.sessions()) == 2
    finally:
        sup.shutdown()


def test_human_route_uses_inbox(tmp_path: Path) -> None:
    w = World(tmp_path)
    bg = w.start("desk-human@1")
    approval = w.pending(bg.job_id)
    assert approval["route"] == "human" and w.wakes("approval") == []
    w.awaiting(bg.job_id)
    [item] = w.cli("inbox", "ls")[1]
    assert (item["kind"], item["subject"], item["origin"]) == ("approval", approval["id"], f"job:{bg.job_id}")
    assert item["payload"]["kind"] == "cost" and item["payload"]["station"] == w.key("desk-human@1")
    assert item["payload"]["cost_usd"] == 1.0

    _, err = w.mcp("approval_decide", {"approval_id": approval["id"], "decision": "approve", "note": ""})
    assert err["code"] == "FOREMAN_FORBIDDEN"

    code, body, _ = w.cli("inbox", "answer", item["id"], "--deny", "--note", "太贵", actor="human:alice")
    assert code == 0, body
    assert (body["status"], body["answer"]) == ("answered", {"decision": "deny", "note": "太贵"})
    job = bg.join()
    assert w.output(job)["response"]["decision"] == "deny"
    row = w.ctx.db.query("SELECT status, decided_by FROM approvals WHERE id = ?", (approval["id"],))[0]
    assert (row["status"], row["decided_by"]) == ("denied", "human:alice")


def test_bypass_delegates_to_foreman(tmp_path: Path) -> None:
    w = World(tmp_path, autonomy={"mode": "bypass"})
    bg = w.start("desk-human@1")
    approval = w.pending(bg.job_id)
    assert approval["route"] == "foreman"
    assert w.events("approval.delegated") == [{"id": approval["id"], "from": "human", "to": "foreman"}]
    assert [wake["subject"] for wake in w.wakes("approval")] == [approval["id"]]
    assert w.cli("inbox", "ls")[1] == []
    _decide(w, approval["id"], "approve")
    assert bg.join().state.value == "SEALED"

    bg2 = w.start("desk-review@1")
    second = w.pending(bg2.job_id)
    assert second["route"] == "foreman"
    assert w.events("approval.delegated")[-1] == {"id": second["id"], "from": "review", "to": "foreman"}
    _decide(w, second["id"], "deny", "不需要")
    job = bg2.join()
    assert w.output(job)["response"]["decision"] == "deny"
    assert job.error is None


def test_escalate_moves_to_inbox(tmp_path: Path) -> None:
    w = World(tmp_path)
    bg = w.start("desk-foreman@1")
    approval = w.pending(bg.job_id)
    got, err = w.mcp("approval_escalate", {"approval_id": approval["id"], "note": "超出我的判断"})
    assert err is None, err
    assert (got["route"], got["status"]) == ("human", "pending")
    assert w.events("approval.escalated") == [{"id": approval["id"], "by": "foreman:fake-agent@1"}]
    [item] = w.cli("inbox", "ls")[1]
    assert item["kind"] == "approval" and item["payload"]["note"] == "超出我的判断"
    _, err = w.mcp("approval_escalate", {"approval_id": approval["id"], "note": "再来一次"})
    assert err["code"] == "APPROVAL_STATE_INVALID"

    code, body, _ = w.cli("inbox", "answer", item["id"], "--approve")
    assert code == 0, body
    job = bg.join()
    assert job.state.value == "SEALED"
    assert w.output(job)["response"]["decision"] == "approve"
    row = w.ctx.db.query("SELECT decided_by FROM approvals WHERE id = ?", (approval["id"],))[0]
    assert row["decided_by"] == "human:cli"


def test_factoryctl_ask(tmp_path: Path) -> None:
    w = World(tmp_path)
    bg = w.start("desk-ask@1")
    approval = w.pending(bg.job_id)
    assert approval["route"] == "foreman"
    got, err = w.mcp("approval_get", {"approval_id": approval["id"]})
    assert err is None, err
    assert (got["kind"], got["detail"]) == ("ask:access", "please allow api.example.test")
    assert [wake["subject"] for wake in w.wakes("approval")] == [approval["id"]]

    _decide(w, approval["id"], "deny", "先走 ChangeSet")
    job = bg.join()
    out = w.output(job)
    assert out["code"] == 0
    assert out["response"] == {"decision": "deny", "note": "先走 ChangeSet", "code": "APPROVAL_DENIED"}
    assert (out["bad"], out["missing"], out["requests"]) == (2, 2, 1)
