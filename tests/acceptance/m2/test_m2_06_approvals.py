"""M2-06 验收：审批（spec/system/approvals.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.approvals")
pytest.importorskip("mao.errors")

from mao.approvals import ApprovalPhase, Approvals  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402

CANARY = "CANARY-SECRET-VALUE-0123456789"


def _approvals(tmp_path: Path):
    ctx, snapshot, _runner, clock = open_factory(tmp_path, FIXTURE)
    return ctx, snapshot, Approvals(ctx, snapshot), clock


def test_conformance_approval_cases() -> None:
    import json
    import subprocess

    proc = subprocess.run(
        ["uv", "run", "mrl", "conformance", "--json"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0
    failed = {item["case"] for item in json.loads(proc.stdout)["failures"]}
    assert "factory/phases-propose-without-rule" not in failed
    assert "factory/approval-propose-without-phases" not in failed


def test_auto_if_approves_without_parking(tmp_path: Path) -> None:
    ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0001",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 0.2, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    assert opened.decided_by == "auto"
    assert opened.status.value == "approved"
    job = ctx.db.query("SELECT state FROM jobs WHERE id = ?", ("j-0001",))
    assert job
    assert job[0]["state"] != "AWAITING_APPROVAL"


def test_auto_if_false_parks_for_foreman(tmp_path: Path) -> None:
    ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0002",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    assert opened.route == "foreman"
    assert opened.status.value == "pending"
    decided = approvals.preset(
        station_key=snapshot.key("Station", "quote@1"),
        phase=ApprovalPhase.PROPOSE,
        decision="approve",
        by="foreman",
    )
    assert decided is None
    again = approvals.open_proposal(
        job_id="j-0003",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    assert again.status.value == "approved"


def test_deny_propose_does_not_execute(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0004",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    denied = approvals.apply_decision(opened.id, "deny", by="foreman")
    assert denied.decision == "deny"
    assert denied.status.value == "denied"


def test_in_run_deny_returns_to_running(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_request(
        job_id="j-0005",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"kind": "tool", "summary": "run", "cost_usd": 1.0},
        inputs={},
        params={},
    )
    denied = approvals.apply_decision(opened.id, "deny", by="foreman")
    assert denied.phase == ApprovalPhase.IN_RUN
    assert denied.decision == "deny"


def test_in_run_missing_rule_rejects(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    rule = approvals.rule(snapshot.key("Station", "upper@1"), ApprovalPhase.IN_RUN)
    assert rule == {"mode": "missing"}
    with pytest.raises(FactoryError) as ei:
        approvals.open_request(
            job_id="j-0006",
            attempt=1,
            station_key=snapshot.key("Station", "upper@1"),
            payload={"kind": "tool", "summary": "x", "cost_usd": 0.0},
            inputs={},
            params={},
        )
    assert ei.value.code == "APPROVAL_RULE_MISSING"


def test_timeout_uses_spec_not_default(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0007",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    assert opened.deadline is not None
    clock.advance(2)
    expired = approvals.expire_due()
    assert opened.id in expired


def test_review_quorum_unset(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    with pytest.raises(FactoryError) as ei:
        approvals.open_proposal(
            job_id="j-0008",
            attempt=1,
            station_key=snapshot.key("Station", "quote@1"),
            payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
            inputs={},
            params={},
        )
    assert ei.value.code == "REVIEW_QUORUM_UNSET"


def test_review_counts_distinct_runtimes(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0009",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    first = approvals.apply_decision(opened.id, "approve", by="runtime-a")
    assert first.status.value == "pending"
    with pytest.raises(FactoryError) as ei:
        approvals.apply_decision(opened.id, "approve", by="runtime-a")
    assert ei.value.code == "APPROVAL_STATE_INVALID"
    second = approvals.apply_decision(opened.id, "approve", by="runtime-b")
    assert second.status.value == "approved"
    assert {vote.runtime for vote in approvals.votes(opened.id)} == {"runtime-a", "runtime-b"}


def test_eval_error_is_not_true(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0010",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote"},
        inputs={},
        params={},
    )
    assert opened.decided_by != "auto"
    assert opened.route != "auto"


def test_sink_propose_does_not_claim(tmp_path: Path) -> None:
    ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0011",
        attempt=1,
        station_key=snapshot.key("Station", "publish-local@1"),
        payload={"summary": "publish", "cost_estimate_usd": 0.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    rows = ctx.db.query("SELECT id FROM receipts WHERE job_id = ?", (opened.job_id,))
    assert rows == []


def test_proposal_missing(tmp_path: Path) -> None:
    _ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    with pytest.raises(FactoryError) as ei:
        approvals.open_proposal(
            job_id="j-0012",
            attempt=1,
            station_key=snapshot.key("Station", "quote@1"),
            payload={},
            inputs={},
            params={},
        )
    assert ei.value.code == "PROPOSAL_MISSING"


def test_secret_not_in_approval_records(tmp_path: Path) -> None:
    ctx, snapshot, approvals, _clock = _approvals(tmp_path)
    opened = approvals.open_proposal(
        job_id="j-0013",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": CANARY, "cost_estimate_usd": 0.1, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    blob = str(opened.note) + str(ctx.db.query("SELECT data FROM events"))
    listed = approvals.for_provenance(opened.job_id)
    assert CANARY not in blob
    assert CANARY not in str(listed)
