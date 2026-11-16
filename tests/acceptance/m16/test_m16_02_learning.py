"""M16-02：固定契约、真实证据、正式知识（assets.md 4.2–4.4、5.2–5.4）。"""

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.learning")

from tests.acceptance.m16.asset_world import World, error_code  # noqa: E402


def _setup(tmp_path: Path):
    w = World(tmp_path)
    bean = w.note("one\ntwo\n")
    candidate = w.asset(beans=[bean])
    contract = w.contract()
    return w, bean, candidate, contract


def test_contract_requires_exact_human_authorization(tmp_path: Path) -> None:
    """厂长不能靠候选中的自评或未授权契约晋升。"""
    w, _bean, candidate, _contract = _setup(tmp_path)
    untrusted = w.contract(authorize=False, key="contract.new")
    with error_code("ASSET_NOT_AUTHORIZED"):
        w.trial(candidate, untrusted)
    with error_code("ASSET_PROOF_REQUIRED"):
        w.learning.promote(candidate["id"], trial_id=None, expected=None, by="ada")
    assert w.learning.heads("station:upper@1") == []


def test_normal_work_is_evidence_without_extra_execution(tmp_path: Path) -> None:
    """新真实 job 的既有 Gate facts 直接验收，不额外生产或调用模型。"""
    w, bean, candidate, contract = _setup(tmp_path)
    trial = w.trial(candidate, contract)
    job = w.job(bean)
    before = len(w.rows("SELECT id FROM jobs"))
    result = w.learning.evaluate(trial["id"], {"normal": {"kind": "job", "id": job.id}}, by="ada")
    assert result["state"] == "passed" and not result["stale"]
    assert len(w.rows("SELECT id FROM jobs")) == before == 1
    promoted = w.learning.promote(candidate["id"], trial_id=trial["id"], expected=None, by="ada")
    assert promoted["asset_id"] == candidate["id"]


def test_old_or_wrong_input_job_cannot_be_new_evidence(tmp_path: Path) -> None:
    """旧 job/cache 产出和没有直接使用候选材料的新 job 都拒绝。"""
    w, bean, candidate, contract = _setup(tmp_path)
    old = w.job(bean)
    trial = w.trial(candidate, contract)
    for job in [old, w.job(w.note("other\nmaterial\n"))]:
        with error_code("ASSET_EVIDENCE_INVALID"):
            w.learning.evaluate(trial["id"], {"normal": {"kind": "job", "id": job.id}}, by="ada")
    assert w.learning.get_trial(trial["id"])["state"] == "open"


def test_failed_assertion_preserves_old_head(tmp_path: Path) -> None:
    """实际产物不达既定标准时失败，不覆盖原正式版本。"""
    w, bean, candidate, contract = _setup(tmp_path)
    t1 = w.trial(candidate, contract)
    j1 = w.job(bean)
    w.learning.evaluate(t1["id"], {"normal": {"kind": "job", "id": j1.id}}, by="ada")
    w.learning.promote(candidate["id"], trial_id=t1["id"], expected=None, by="ada")
    bad = w.asset(beans=[w.note("one line\n")], summary="过度概括的候选")
    t2 = w.trial(bad, contract)
    j2 = w.job(bad["document"]["beans"][0])
    assert (
        w.learning.evaluate(t2["id"], {"normal": {"kind": "job", "id": j2.id}}, by="ada")["state"] == "failed"
    )
    with error_code("ASSET_PROOF_REQUIRED"):
        w.learning.promote(bad["id"], trial_id=t2["id"], expected=candidate["id"], by="ada")
    assert w.learning.heads("station:upper@1")[0]["asset_id"] == candidate["id"]


def test_promotion_is_cas_and_terminal_proof_is_immutable(tmp_path: Path) -> None:
    """过时 expected 不能覆盖并发晋升；不能换证据改写终态。"""
    w, bean, candidate, contract = _setup(tmp_path)
    trial = w.trial(candidate, contract)
    job = w.job(bean)
    ev = {"normal": {"kind": "job", "id": job.id}}
    first = w.learning.evaluate(trial["id"], ev, by="ada")
    assert w.learning.evaluate(trial["id"], ev, by="bea") == first
    w.learning.promote(candidate["id"], trial_id=trial["id"], expected=None, by="ada")
    with error_code("ASSET_HEAD_CONFLICT"):
        w.learning.promote(candidate["id"], trial_id=trial["id"], expected=None, by="bea")
    newer = w.job(bean)
    with error_code("ASSET_TRIAL_CONFLICT"):
        w.learning.evaluate(trial["id"], {"normal": {"kind": "job", "id": newer.id}}, by="bea")


def test_environment_change_stales_proof_without_rerun(tmp_path: Path) -> None:
    """环境变化使证明过期；查询状态不自动安排重测。"""
    w, bean, candidate, contract = _setup(tmp_path)
    trial = w.trial(candidate, contract)
    job = w.job(bean)
    w.learning.evaluate(trial["id"], {"normal": {"kind": "job", "id": job.id}}, by="ada")
    w.config["assets"]["environment"]["toolchain"] = "v2"
    w.save_config()
    assert w.learning.get_trial(trial["id"])["stale"]
    with error_code("ASSET_CONTEXT_STALE"):
        w.learning.promote(candidate["id"], trial_id=trial["id"], expected=None, by="ada")
    assert len(w.rows("SELECT id FROM jobs")) == 1


def test_agent_uses_version_pinned_context(tmp_path: Path) -> None:
    """下一次正常工作实际挂载候选，只有本岗位资产且不展开历史。"""
    w = World(tmp_path)
    target = w.asset(subject="agent:scout")
    w.asset(key="other.secret-notes", subject="agent:helper", summary="不属于 scout 的上下文")
    contract = w.contract(
        subject="agent:scout",
        cases=[
            {
                "name": "normal",
                "kind": "turn",
                "subject": "agent:scout",
                "tags": ["normal"],
                "uses_target": True,
                "assert": 'out.state == "ok"',
            }
        ],
    )
    trial = w.trial(target, contract)
    w.script(
        {
            "default": [
                {"do": "echo_task", "path": "/workspace/prompt.txt"},
                {"do": "copy", "from": "/factory/in/asset-context.json", "to": "/workspace/context.json"},
            ]
        }
    )
    turn = w.turn()
    context = json.loads(w.read("context.json"))
    assert target["id"] in [a["id"] for a in context["assets"]]
    assert "不属于 scout" not in w.read("context.json")
    assert "/factory/in/asset-context.json" in w.read("prompt.txt")
    assert (
        w.learning.evaluate(trial["id"], {"normal": {"kind": "turn", "id": turn.id}}, by="ada")["state"]
        == "passed"
    )


def test_directive_needs_authority_not_experiment(tmp_path: Path) -> None:
    """用户偏好需授权确切版本；保留 claim 分类与可追溯指针。"""
    w = World(tmp_path)
    target = w.asset(
        subject="agent:scout",
        data={
            "claim": {
                "kind": "directive",
                "statement": "用户指定中文交接",
                "limits": "本岗位",
                "revalidate": ["用户改意图"],
            }
        },
    )
    with error_code("ASSET_NOT_AUTHORIZED"):
        w.learning.promote(target["id"], trial_id=None, expected=None, by="ada")
    w.learning.authorize(target["id"], by="ada")
    w.learning.promote(target["id"], trial_id=None, expected=None, by="ada")
    assert w.assets.get(target["id"])["document"]["data"]["claim"]["kind"] == "directive"


def test_case_coverage_and_mrl_errors_do_not_pass(tmp_path: Path) -> None:
    """无效 MRL 和不完整接管覆盖不得被授权/用于替换。"""
    w = World(tmp_path)
    malformed = w.contract(
        authorize=False,
        cases=[
            {
                "name": "bad",
                "kind": "turn",
                "subject": "agent:scout",
                "tags": ["normal"],
                "uses_target": True,
                "assert": "undefined_symbol +",
            }
        ],
        subject="agent:scout",
    )
    with error_code("ASSET_CONTRACT_INVALID"):
        w.learning.authorize(malformed["id"], by="ada")
    handoff = w.handoff()
    weak = w.contract(
        subject="agent:scout",
        cases=[
            {
                "name": "normal",
                "kind": "turn",
                "subject": "agent:scout",
                "tags": ["normal"],
                "uses_target": True,
                "assert": 'out.state == "ok"',
            }
        ],
        key="weak",
    )
    with error_code("ASSET_CONTRACT_INVALID"):
        w.trial(handoff, weak, "takeover")
