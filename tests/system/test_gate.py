"""M1-06 单元：mao.gate 的报告形状（spec/system/gate.md 第 3 节）。模块缺失时整文件跳过。"""

from __future__ import annotations

import pytest

pytest.importorskip("mao.gate")

from mao.gate import Check, GateReport  # noqa: E402


def _report(**kwargs) -> GateReport:
    base = dict(
        job_id="j-0001",
        station="Station:upper@1",
        attempt=1,
        verdict="pass",
        outputs={"note": ["bean-1"]},
        facts={},
        checks=(Check(port="note", index=0, stage="structure", name="non_empty", ok=True, detail=""),),
        feedback="",
    )
    base.update(kwargs)
    return GateReport(**base)


def test_to_json_keeps_verdict() -> None:
    body = _report().to_json()
    assert body["verdict"] == "pass"
    assert body["job_id"] == "j-0001"
    assert body["outputs"]["note"] == ["bean-1"]


def test_digest_changes_with_feedback() -> None:
    first = _report(feedback="").digest()
    second = _report(feedback="again").digest()
    assert first.startswith("sha256:")
    assert first != second


def test_reject_is_not_pass() -> None:
    assert _report(verdict="reject").to_json()["verdict"] == "reject"


def test_check_stage_is_recorded() -> None:
    body = _report().to_json()
    stages = {item["stage"] for item in body["checks"]}
    assert stages == {"structure"}


def test_error_verdict_still_has_digest() -> None:
    report = _report(verdict="error", outputs={})
    assert report.digest().startswith("sha256:")
    assert report.to_json()["outputs"] == {}
