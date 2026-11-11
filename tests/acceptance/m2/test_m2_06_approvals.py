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


def _review_approvals(tmp_path: Path, *, quorum: bool):
    """第 7 章的补丁：quote@1 的 propose 改成 else: review；quorum 为真时，宪法副本加上法定人数 2。

    补丁只落在测试自己的副本上，不改受保护的夹具原件。规范本身不规定 2 这个数。
    """
    import shutil

    registry = tmp_path / "registry"
    shutil.copytree(FIXTURE, registry)
    station = registry / "stations" / "quote.yaml"
    old = 'propose: {auto_if: "proposal.cost_estimate_usd <= 1.0", else: foreman}'
    text = station.read_text(encoding="utf-8")
    assert old in text
    station.write_text(
        text.replace(old, old.replace("else: foreman", "else: review")), encoding="utf-8", newline="\n"
    )
    if quorum:
        constitution = registry / "constitution" / "invariants.mrl"
        body = constitution.read_text(encoding="utf-8") + 'limit("approval_review_quorum", 2.0).\n'
        constitution.write_text(body, encoding="utf-8", newline="\n")
    ctx, snapshot, _runner, clock = open_factory(tmp_path / "run", registry)
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


def _insert_job(ctx, snapshot, job_id: str) -> None:
    station = snapshot.key("Station", "quote@1")
    with ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO jobs (id, station, epoch, state, attempt, inputs, params, outputs, created_at, updated_at) "
            "VALUES (?, ?, ?, 'RUNNING', 1, '{}', '{}', '{}', ?, ?)",
            (job_id, station, snapshot.epoch, "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
        )


def test_timeout_uses_spec_not_default(tmp_path: Path) -> None:
    import shutil

    ctx, snapshot, approvals, clock = _approvals(tmp_path)
    _insert_job(ctx, snapshot, "j-0007")
    opened = approvals.open_proposal(
        job_id="j-0007",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    assert opened.deadline == "2026-01-01T00:00:01.000Z"
    clock.advance(2)
    expired = approvals.expire_due()
    assert opened.id in expired
    job = ctx.db.query("SELECT state, error FROM jobs WHERE id = ?", ("j-0007",))
    assert job[0]["state"] == "FAILED"
    assert "APPROVAL_TIMEOUT" in (job[0]["error"] or "")

    registry = tmp_path / "registry-no-timeout"
    shutil.copytree(FIXTURE, registry)
    quote = registry / "stations" / "quote.yaml"
    quote.write_text(quote.read_text(encoding="utf-8").replace("    timeout: 1s\n", ""), encoding="utf-8")
    ctx2, snapshot2, _runner, clock2 = open_factory(tmp_path / "no-timeout", registry)
    approvals2 = Approvals(ctx2, snapshot2)
    _insert_job(ctx2, snapshot2, "j-0008")
    parked = approvals2.open_proposal(
        job_id="j-0008",
        attempt=1,
        station_key=snapshot2.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    assert parked.deadline is None
    assert parked.status.value == "pending"
    clock2.advance(7200)
    assert parked.id not in approvals2.expire_due()
    still = ctx2.db.query("SELECT state FROM jobs WHERE id = ?", ("j-0008",))
    assert still[0]["state"] != "FAILED"


def test_review_quorum_unset(tmp_path: Path) -> None:
    ctx, snapshot, approvals, _clock = _review_approvals(tmp_path, quorum=False)
    _insert_job(ctx, snapshot, "j-0008")
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
    # 不停车：没有 pending 行，job 失败
    assert ctx.db.query("SELECT id FROM approvals WHERE job_id = ? AND status = 'pending'", ("j-0008",)) == []
    assert ctx.db.query("SELECT state FROM jobs WHERE id = ?", ("j-0008",))[0]["state"] == "FAILED"


def test_review_counts_distinct_runtimes(tmp_path: Path) -> None:
    ctx, snapshot, approvals, _clock = _review_approvals(tmp_path, quorum=True)
    _insert_job(ctx, snapshot, "j-0009")
    _insert_job(ctx, snapshot, "j-0014")
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
    split = approvals.open_proposal(
        job_id="j-0014",
        attempt=1,
        station_key=snapshot.key("Station", "quote@1"),
        payload={"summary": "quote", "cost_estimate_usd": 9.0, "time_estimate_s": 1},
        inputs={},
        params={},
    )
    assert split.route == "review"
    approvals.apply_decision(split.id, "approve", by="runtime-a")
    denied = approvals.apply_decision(split.id, "deny", by="runtime-b")
    assert denied.status.value == "denied"
    job = ctx.db.query("SELECT state, error FROM jobs WHERE id = ?", ("j-0014",))[0]
    assert job["state"] == "FAILED"
    assert __import__("json").loads(job["error"])["code"] == "APPROVAL_REVIEW_SPLIT"


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
