"""M11-03 验收：ChangeSet 签核方式、bypass 仍过影子、信号泵与新产线的金丝雀路由（spec/system/delegation.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.delegation")

from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    ROLLOUT,
    changeset_doc,
    commit_change,
    publish_local_v3,
    toolbox_v2_image,
    upsert,
)
from tests.acceptance.m11.deleg_world import World  # noqa: E402

NEW_LINE = """apiVersion: mao/v1
kind: Line
metadata: {name: desk-line, version: 1}
spec:
  interface:
    inputs:
      note: {type: note.text@1, requires: [non_empty]}
  budget: {cap_usd: 1.0}
  steps:
    upper:
      station: upper@1
      in: {note: $inputs.note}
  keep: [upper.note]
"""
SCHEDULER = {
    "pools": {"cpu": None, "memory_mb": None, "gpu": None},
    "compartments": [],
    "breakers": [],
    "wip": [],
    "rates": [],
    "run_cap": None,
    "toxic_failures": None,
    "tick_s": None,
}


class FailShadow:
    def run(self, cs, *, candidate, active, rollout, golden_only):
        from mao.governance import ShadowReport

        return ShadowReport(ok=False, runs=1, spent_usd=0.0, regressions=[{"node": "upper", "kind": "state"}])


def _verified(w: World, pipeline, files: dict, changes: list, *, message: str = "change", rollout=None):
    source = commit_change(w.repo, files, message=message)
    cs = pipeline.propose(
        changeset_doc(w.base, changes, rollout=rollout), source, by="fake-agent@1", timeout_s=60
    )
    cs = pipeline.advance(cs.id)
    assert cs.state == "verified", cs.report
    return cs


def _t2(w: World, pipeline, message: str = "change"):
    cs = _verified(w, pipeline, toolbox_v2_image(), [upsert("Machine", "toolbox")], message=message)
    assert (cs.risk_tier, cs.reviews_required) == ("T2", 1)
    return cs


def test_auto_signoff_skips_reviews(tmp_path: Path) -> None:
    w = World(tmp_path, autonomy={"changesets": {"T2": "auto"}})
    pipeline = w.pipeline()
    cs = _t2(w, pipeline)
    assert pipeline.approvals(cs.id) == []
    cs = pipeline.advance(cs.id)
    assert cs.state == "shadow", cs.report
    assert w.cli("inbox", "ls")[1] == []


def test_human_signoff_flow(tmp_path: Path) -> None:
    w = World(tmp_path, autonomy={"changesets": {"T2": "human"}})
    pipeline = w.pipeline()
    cs = _t2(w, pipeline)
    cs = pipeline.advance(cs.id)
    assert cs.state == "verified"
    assert (cs.report["reason"], cs.report["signoff"]) == ("signoff", "human")
    pipeline.advance(cs.id)
    [item] = w.cli("inbox", "ls")[1]
    assert (item["kind"], item["subject"], item["origin"]) == ("signoff", cs.id, "governance")
    assert item["payload"]["tier"] == "T2" and item["payload"]["changeset"] == cs.id

    code, body, _ = w.cli("inbox", "answer", item["id"], "--approve", "--note", "看过了", actor="human:alice")
    assert code == 0, body
    assert body["status"] == "answered"
    assert pipeline.signoff_of(cs.id)["decision"] == "approve"
    assert pipeline.signoff_of(cs.id)["by"] == "human:alice"
    assert w.events("changeset.signoff") == [{"id": cs.id, "decision": "approve", "by": "human:alice"}]
    assert pipeline.advance(cs.id).state == "shadow"

    other = _t2(w, pipeline, message="second")
    code, body, _ = w.cli("changeset", "signoff", other.id, "deny", "--by", "human:bob", "--note", "不要")
    assert code == 0, body
    other = pipeline.advance(other.id)
    assert other.state == "rejected"
    assert other.report["code"] == "SIGNOFF_DENIED" and other.report["detail"] == {"by": "human:bob"}


def test_bypass_keeps_shadow(tmp_path: Path) -> None:
    w = World(tmp_path, autonomy={"mode": "bypass"})
    pipeline = w.pipeline(shadow=FailShadow())
    cs = _verified(w, pipeline, publish_local_v3(), [upsert("Station", "publish-local")])
    assert (cs.risk_tier, cs.reviews_required) == ("T3", 2)
    cs = pipeline.advance(cs.id)
    assert cs.state == "rejected" and cs.report["code"] == "SHADOW_FAILED"
    assert pipeline.approvals(cs.id) == []


def test_signoff_errors(tmp_path: Path) -> None:
    w = World(tmp_path)
    pipeline = w.pipeline()
    cs = _t2(w, pipeline)
    for kwargs, reason in (
        ({"decision": "approve", "by": "alice"}, "by"),
        ({"decision": "maybe", "by": "human:alice"}, "decision"),
        ({"decision": "approve", "by": "human:alice", "note": "x" * 2001}, "note"),
    ):
        with pytest.raises(FactoryError) as ei:
            pipeline.signoff(cs.id, **kwargs)
        assert ei.value.code == "SIGNOFF_INVALID" and ei.value.detail["reason"] == reason
    pipeline.signoff(cs.id, "approve", by="human:alice")
    with pytest.raises(FactoryError) as ei:
        pipeline.signoff(cs.id, "deny", by="human:bob")
    assert ei.value.code == "CHANGESET_STATE_INVALID"

    source = commit_change(w.repo, toolbox_v2_image(), message="draft")
    draft = pipeline.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    with pytest.raises(FactoryError) as ei:
        pipeline.signoff(draft.id, "approve", by="human:alice")
    assert ei.value.code == "CHANGESET_STATE_INVALID"
    assert w.cli("changeset", "signoff", cs.id, "approve")[0] == 2
    code, body, err = w.cli("changeset", "signoff", "cs-9999", "approve", "--by", "human:alice")
    assert code == 1 and w.error_code(body, err) == "CHANGESET_NOT_FOUND"


def test_pump_files_reviews_only_in_review_mode(tmp_path: Path) -> None:
    from mao.daemon import Daemon, InlineJobExecutor, ServeConfig

    w = World(tmp_path)
    pipeline = w.pipeline()
    first = _t2(w, pipeline)
    (w.root / "scheduler.json").write_text(json.dumps(SCHEDULER), encoding="utf-8")
    config = ServeConfig(
        tick_s=0.0,
        max_parallel_jobs=1,
        audit_every_s=None,
        canary_every_s=None,
        docker_timeout_s=60.0,
        supervise=False,
    )
    daemon = Daemon(
        w.ctx, config, executor=InlineJobExecutor(lambda job_id: w.runner), epochs=w.epochs, pipeline=pipeline
    )
    daemon.start()
    try:
        daemon.tick()
        assert [wake["subject"] for wake in w.wakes("review")] == [first.id]
        w.set_config("autonomy", {"changesets": {"T2": "human"}})
        second = _t2(w, pipeline, message="second")
        daemon.tick()
        assert second.id not in [wake["subject"] for wake in w.wakes("review")]
    finally:
        daemon.shutdown(timeout_s=5)


def test_new_line_routes_to_candidate(tmp_path: Path) -> None:
    w = World(tmp_path, autonomy={"changesets": {"T2": "auto"}})
    pipeline = w.pipeline()
    rollout = dict(ROLLOUT, canary={"percent": 1, "min_runs": 50})
    cs = _verified(
        w, pipeline, {"lines/desk-line.yaml": NEW_LINE}, [upsert("Line", "desk-line")], rollout=rollout
    )
    assert cs.risk_tier == "T2"
    assert pipeline.advance(cs.id).state == "shadow"
    cs = pipeline.advance(cs.id)
    assert cs.state == "canary", cs.report
    note = w.note()
    code, run, err = w.cli("line", "run", "desk-line@1", "--in", f"note={note}")
    assert code == 0, (run, err)
    assert run["epoch"] == cs.commit and run["state"] == "SUCCEEDED", run
