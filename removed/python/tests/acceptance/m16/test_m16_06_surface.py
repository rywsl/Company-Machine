"""M16-06：操作面权限和不增算力的闭环（assets.md 3.4、5.2–5.8）。"""

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.packages")
pytest.importorskip("mao.learning")

from mao.mcp import McpServer, Session  # noqa: E402
from tests.acceptance.m13.agent_world import tool_result  # noqa: E402
from tests.acceptance.m15.talk_world import foreman_call  # noqa: E402
from tests.acceptance.m16.asset_world import World  # noqa: E402


def test_cli_catalog_roundtrip_and_authorize(tmp_path: Path) -> None:
    """CLI 读写使用同一目录；人的 authorize 绑定确切契约。"""
    w = World(tmp_path)
    sample = w.asset()
    path = tmp_path / "asset.json"
    path.write_text(json.dumps({**sample["document"], "key": "cli.notes"}), encoding="utf-8")
    code, body, err = w.cli("asset", "put", str(path), "--by", "ada")
    assert code == 0, err
    code, shown, err = w.cli("asset", "show", body["id"])
    assert code == 0 and shown["document"]["key"] == "cli.notes", err
    contract = w.contract(authorize=False)
    code, _body, err = w.cli("knowledge", "authorize", contract["id"], "--by", "ada")
    assert code == 0, err
    assert w.rows("SELECT asset_id FROM asset_authorizations WHERE asset_id = ?", (contract["id"],))


def test_mcp_roles_keep_authorization_and_disk_outside_agents(tmp_path: Path) -> None:
    """厂长可组织和验收；普通 Agent 只贡献自己的候选，不能改标准/磁盘。"""
    w = World(tmp_path)
    maps = {}
    for role in ["foreman", "agent", "reviewer"]:
        session = Session(role=role, actor="fake-agent@1", agent="scout" if role == "agent" else None)
        maps[role] = {t["name"] for t in McpServer(w.ctx, w.epochs, session).tools()}
        assert not ({"knowledge_authorize", "package_export", "asset_release"} & maps[role])
    assert "knowledge_promote" in maps["foreman"]
    assert "asset_put" in maps["agent"] and "knowledge_promote" not in maps["agent"]
    assert "asset_get" in maps["reviewer"] and "asset_put" not in maps["reviewer"]


def test_agent_can_contribute_own_asset_without_actor_spoofing(tmp_path: Path) -> None:
    """MCP 的作者由 Session 决定，Agent 不能登记别的岗位资产。"""
    w = World(tmp_path)
    doc = {
        "key": "scout.notes",
        "kind": "knowledge",
        "summary": "本岗位候选",
        "scope": {"subject": "agent:scout"},
        "beans": [],
        "requires": [],
        "evidence": [],
        "data": {},
    }
    result, error = tool_result(w.mcp("asset_put", {"document": doc}))
    assert error is None and result["by"] == "fake-agent@1"
    reply = w.mcp("asset_put", {"document": {**doc, "scope": {"subject": "agent:helper"}}})
    _result, error = tool_result(reply)
    assert error is not None
    assert w.assets.list(subject="agent:helper") == []


def test_foreman_normal_work_to_knowledge_loop(tmp_path: Path) -> None:
    """厂长用 MCP 选候选、收正常轮次证据、晋升，不增加生产轮次。"""
    w = World(tmp_path)
    target = w.asset(subject="agent:scout")
    contract = w.contract(
        subject="agent:scout",
        cases=[
            {
                "name": "normal",
                "kind": "turn",
                "subject": "agent:scout",
                "tags": ["normal"],
                "uses_target": True,
                "assert": 'out.state == "ok" && out.actions[0].tool == "ask_human"',
            }
        ],
    )
    trial, error = foreman_call(
        w,
        "knowledge_trial_open",
        {"target": target["id"], "contract": contract["id"], "purpose": "knowledge"},
    )
    assert error is None
    w.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "calls": [
                        {
                            "name": "ask_human",
                            "arguments": {"summary": "现有证据不够，需要补原件", "options": ["补充"]},
                        }
                    ],
                }
            ]
        }
    )
    turn = w.turn()
    evaluated, error = foreman_call(
        w,
        "knowledge_evaluate",
        {"trial_id": trial["id"], "evidence": {"normal": {"kind": "turn", "id": turn.id}}},
    )
    assert error is None and evaluated["state"] == "passed"
    promoted, error = foreman_call(
        w, "knowledge_promote", {"target": target["id"], "trial_id": trial["id"], "expected": None}
    )
    assert error is None and promoted["asset_id"] == target["id"]
    assert len(w.host().turns("scout")) == 1 and w.rows("SELECT id FROM jobs") == []


def test_package_verify_cli_reports_no_business_acceptance(tmp_path: Path) -> None:
    """CLI 的静态校验只报告字节/引用完整性，不宣布业务验收通过。"""
    from mao.packages import create_checkpoint

    w = World(tmp_path)
    root = w.factory_asset()
    dest = tmp_path / "checkpoint"
    create_checkpoint(w.ctx, dest, factory_asset=root["id"])
    code, body, err = w.cli("package", "verify", str(dest))
    assert code == 0 and body["ok"], err
    assert body["acceptance"] == "not_run"


def test_asset_operations_leave_governance_and_jobs_unchanged(tmp_path: Path) -> None:
    """资产组织不绕过治理：不修改 Registry/宪法，不生成 job 或启动发布。"""
    w = World(tmp_path)
    epoch = w.epochs.current()
    config = (w.root / "factory.json").read_bytes()
    document = {
        "key": "new.tool",
        "kind": "tool",
        "summary": "等待正常治理的工具候选",
        "scope": {"subject": "factory"},
        "beans": [],
        "requires": [],
        "evidence": [],
        "data": {},
    }
    result, error = foreman_call(w, "asset_put", {"document": document})
    assert error is None and result["document"]["kind"] == "tool"
    assert w.epochs.current() == epoch and (w.root / "factory.json").read_bytes() == config
    for table in ["jobs", "runs", "receipts", "changesets"]:
        assert not w.rows("SELECT * FROM " + table)


def test_repeated_status_reads_do_not_schedule_validation(tmp_path: Path) -> None:
    """读目录、heads、试验状态不消耗模型、派消息或制造验证任务。"""
    w = World(tmp_path)
    target = w.asset(beans=[w.note("one\ntwo\n")])
    trial = w.trial(target, w.contract())
    for _ in range(3):
        assert w.learning.get_trial(trial["id"])["state"] == "open"
        assert w.learning.heads("station:upper@1") == []
        w.assets.get(target["id"])
    assert w.rows("SELECT id FROM jobs") == [] and w.rows("SELECT id FROM agent_turns") == []
    assert w.messages("scout") == []


def test_foreman_can_prepare_its_own_successor_after_current_turn(tmp_path: Path) -> None:
    """厂长自身的接管请求延迟到本轮成功后安装，不中途换挂载。"""
    w = World(tmp_path)
    handoff = w.handoff(agent="foreman")
    contract = w.contract(
        subject="agent:foreman",
        key="contract.foreman.prepare",
        cases=[
            {
                "name": "preparation",
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
                "assert": 'out.state == "ok" && size(out.actions) > 0',
            }
        ],
    )
    trial = w.trial(handoff, contract, "takeover")
    w.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "save": "/workspace/prepare-result.json",
                    "calls": [{"name": "agent_takeover_prepare", "arguments": {"trial_id": trial["id"]}}],
                }
            ]
        }
    )
    turn = w.turn("准备自己的下一班", agent="foreman")
    assert turn is not None and turn.state == "ok", turn
    result = w.rows("SELECT * FROM agent_workspaces WHERE agent = 'foreman'")[0]
    assert result["phase"] == "testing" and result["trial_id"] == trial["id"]
    agent = w.rows("SELECT state, session FROM agents WHERE name = 'foreman'")[0]
    assert agent["state"] == "active" and agent["session"] is None
    assert w.rows("SELECT * FROM asset_heads WHERE slot = 'capability:agent:foreman'") == []


def test_resident_foreman_orchestrates_existing_workers_and_reminders(tmp_path: Path) -> None:
    """常驻厂长复用邮箱委派和提醒，worker 仍只运行已授权产线。"""
    w = World(tmp_path)
    bean = w.note("one\ntwo\n")
    sent, error = foreman_call(w, "agent_send", {"to": "scout", "text": "运行已有资料整理任务"})
    assert error is None and sent["source"] == "agent:foreman"
    w.script(
        {
            "default": [
                {
                    "do": "mcp",
                    "calls": [
                        {
                            "name": "run_start",
                            "arguments": {"line": "note-trigger@1", "inputs": {"note": bean}},
                        }
                    ],
                }
            ]
        }
    )
    turn = w.host().run_once("scout")
    assert turn is not None and turn.state == "ok", turn
    assert len(w.rows("SELECT id FROM runs")) == 1
    notice, error = foreman_call(w, "remind", {"after": "2h", "note": "检查这次正常工作的证据"})
    assert error is None and notice["agent"] == "foreman" and notice["not_before"] is not None
    assert w.rows("SELECT id FROM jobs") == []
    assert "run_start" not in {
        t["name"] for t in McpServer(w.ctx, w.epochs, Session(role="foreman", actor="fake-agent@1")).tools()
    }
