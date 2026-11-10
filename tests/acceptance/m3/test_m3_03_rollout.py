"""M3-03 验收：影子、金丝雀、推广与回滚、安全模式（spec/system/governance.md 7.3）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("mao.governance")
pytest.importorskip("mao.foreman")

from mao.base import utc_iso  # noqa: E402
from mao.foreman import ConversionStore  # noqa: E402
from mao.governance import ChangePipeline, EpochRouter, ShadowReport, run_metrics  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    ROLLOUT,
    changeset_doc,
    commit_change,
    git,
    insert_job,
    insert_run,
    open_world,
    toolbox_v2_image,
    upper_v2_limits,
    upsert,
    write_factory_json,
)
from tests.acceptance.m3.test_m3_02_changesets import FakeContracts  # noqa: E402

T1_ROLLOUT = {"shadow": {"budget_usd": 1.0}, "observe": "1h", "slo": ROLLOUT["slo"]}


class FakeShadow:
    def __init__(self, *, ok: bool = True, regressions: list | None = None, spent: float = 0.0) -> None:
        self.ok = ok
        self.regressions = regressions or []
        self.spent = spent
        self.calls: list[dict] = []

    def run(self, cs, *, candidate, active, rollout, golden_only):
        self.calls.append(
            {"cs": cs.id, "candidate": candidate.epoch, "active": active.epoch, "golden_only": golden_only}
        )
        return ShadowReport(ok=self.ok, runs=1, spent_usd=self.spent, regressions=list(self.regressions))


class Rebuilds:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, run_id, snapshot):
        self.calls.append((run_id, snapshot.epoch))
        return "r-rebuilt-" + run_id


def _pipe(w, shadow=None, rebuild=None) -> ChangePipeline:
    fake = FakeContracts()
    return ChangePipeline(
        w.ctx,
        w.epochs,
        contracts=lambda snapshot: fake,
        shadow=shadow or FakeShadow(),
        rebuild=rebuild or Rebuilds(),
    )


def _now(w) -> str:
    return utc_iso(w.clock.now())


def _t1_active(w, pipe):
    base = w.epochs.current()
    source = commit_change(w.repo, upper_v2_limits(), parent=base)
    doc = changeset_doc(base, [upsert("Station", "upper")], rollout=T1_ROLLOUT)
    cs = pipe.propose(doc, source, by="human:a", timeout_s=60)
    assert pipe.advance(cs.id).state == "verified"
    assert pipe.advance(cs.id).state == "shadow"
    return pipe.advance(cs.id)


def _t2_canary(w, pipe):
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipe.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    assert pipe.advance(cs.id).state == "verified"
    pipe.add_review(cs.id, runtime="script@1", verdict="approve", note="", by="script")
    assert pipe.advance(cs.id).state == "shadow"
    return pipe.advance(cs.id)


def test_t1_shadow_then_active(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    shadow = FakeShadow()
    cs = _t1_active(w, _pipe(w, shadow=shadow))
    assert cs.state == "active", cs.report
    assert cs.activated_at == _now(w)
    assert w.epochs.pointers() == {"active": cs.commit, "candidate": None, "previous": w.base}
    assert git(w.repo, "rev-parse", "refs/factory/active") == cs.commit
    assert git(w.repo, "rev-parse", "refs/heads/main") == w.base
    assert shadow.calls == [{"cs": cs.id, "candidate": cs.commit, "active": w.base, "golden_only": True}]


def test_shadow_failure_rejects(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w, shadow=FakeShadow(regressions=[{"kind": "verdict", "subject": "upper", "detail": {}}]))
    source = commit_change(w.repo, upper_v2_limits())
    cs = pipe.propose(changeset_doc(w.base, [upsert("Station", "upper")]), source, by="human:a", timeout_s=60)
    assert pipe.advance(cs.id).state == "verified"
    cs = pipe.advance(cs.id)
    assert cs.state == "rejected"
    assert cs.report["code"] == "SHADOW_FAILED"
    assert w.epochs.pointers()["candidate"] is None
    assert w.epochs.current() == w.base


def test_t2_waits_for_review_then_canary(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w)
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipe.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    assert pipe.advance(cs.id).state == "verified"
    waiting = pipe.advance(cs.id)
    assert waiting.state == "verified"
    assert waiting.report["reason"] == "reviews"
    pipe.add_review(cs.id, runtime="script@1", verdict="approve", note="", by="script")
    shadowed = pipe.advance(cs.id)
    assert shadowed.state == "shadow"
    assert w.epochs.pointers()["candidate"] == cs.commit
    canary = pipe.advance(cs.id)
    assert canary.state == "canary"
    assert canary.stage_started_at == _now(w)
    assert w.epochs.current() == w.base


def test_canary_promotes_on_slo(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w)
    cs = _t2_canary(w, pipe)
    insert_run(w.ctx, "r-0001", epoch=cs.commit, state="SUCCEEDED", created_at=_now(w), spent_usd=1.0)
    waiting = pipe.advance(cs.id)
    assert waiting.state == "canary"
    assert waiting.report["reason"] == "samples"
    insert_run(w.ctx, "r-0002", epoch=cs.commit, state="SUCCEEDED", created_at=_now(w), spent_usd=1.0)
    promoted = pipe.advance(cs.id)
    assert promoted.state == "active", promoted.report
    assert w.epochs.pointers() == {"active": cs.commit, "candidate": None, "previous": w.base}


def test_canary_slo_violation_rolls_back(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w)
    cs = _t2_canary(w, pipe)
    insert_run(w.ctx, "r-0001", epoch=cs.commit, state="FAILED", created_at=_now(w))
    insert_run(w.ctx, "r-0002", epoch=cs.commit, state="SUCCEEDED", created_at=_now(w))
    rolled = pipe.advance(cs.id)
    assert rolled.state == "rolled-back"
    assert rolled.report["code"] == "SLO_ROLLBACK"
    assert rolled.report["detail"]["metric"] == "success_rate"
    assert w.epochs.current() == w.base
    assert w.epochs.pointers()["candidate"] is None


def test_observe_window_rolls_back(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w)
    cs = _t1_active(w, pipe)
    assert cs.state == "active"
    insert_run(w.ctx, "r-0001", epoch=cs.commit, state="FAILED", created_at=_now(w))
    w.clock.advance(7200)
    rolled = pipe.advance(cs.id)
    assert rolled.state == "rolled-back"
    assert rolled.report["code"] == "SLO_ROLLBACK"
    assert w.epochs.pointers() == {"active": w.base, "candidate": None, "previous": cs.commit}


def test_observe_window_passes(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w)
    cs = _t1_active(w, pipe)
    insert_run(w.ctx, "r-0001", epoch=cs.commit, state="SUCCEEDED", created_at=_now(w))
    w.clock.advance(7200)
    done = pipe.advance(cs.id)
    assert done.state == "active"
    assert done.report["stage"] == "observe"
    assert done.report["ok"] is True
    assert pipe.advance(cs.id).updated_at == done.updated_at
    assert w.epochs.current() == cs.commit


def test_router_is_deterministic(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    router = EpochRouter(w.ctx, w.epochs)
    assert {router.choose(f"Line:x@1:{i}") for i in range(20)} == {w.base}
    cs = _t2_canary(w, _pipe(w))
    picks = {f"k{i}": router.choose(f"k{i}") for i in range(200)}
    assert set(picks.values()) == {w.base, cs.commit}
    for key, commit in picks.items():
        assert router.choose(key) == commit
        bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
        assert commit == (cs.commit if bucket < 50 else w.base)


def test_run_metrics_definitions(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    since = _now(w)
    epoch = "e" * 40
    empty = run_metrics(w.ctx, epoch=epoch, since=since)
    assert empty == {
        "runs": 0,
        "jobs": 0,
        "success_rate": None,
        "gate_reject_rate": None,
        "cost_per_run_usd": None,
    }
    insert_run(w.ctx, "r-0001", epoch=epoch, state="SUCCEEDED", created_at=since, spent_usd=1.0)
    insert_run(w.ctx, "r-0002", epoch=epoch, state="FAILED", created_at=since, spent_usd=2.0)
    insert_run(w.ctx, "r-0003", epoch=epoch, state="RUNNING", created_at=since, spent_usd=9.0)
    insert_run(w.ctx, "r-0004", epoch=epoch, state="SUCCEEDED", created_at="2026-01-01T00:00:00.000Z")
    insert_job(w.ctx, "j-0001", run_id="r-0001", rejected=True, at=since)
    insert_job(w.ctx, "j-0002", run_id="r-0001", rejected=False, at=since)
    insert_job(w.ctx, "j-0003", run_id="r-0002", rejected=False, at=since)
    insert_job(w.ctx, "j-0004", run_id="r-0002", rejected=False, at=since)
    got = run_metrics(w.ctx, epoch=epoch, since=since)
    assert got == {
        "runs": 2,
        "jobs": 4,
        "success_rate": 0.5,
        "gate_reject_rate": 0.25,
        "cost_per_run_usd": 1.5,
    }


def test_safe_mode_pauses_t2(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w)
    for _ in range(2):
        cs = _t1_active(w, pipe)
        insert_run(w.ctx, f"r-{cs.id}", epoch=cs.commit, state="FAILED", created_at=_now(w))
        w.clock.advance(7200)
        assert pipe.advance(cs.id).state == "rolled-back"
    pipe.tick()
    mode = pipe.safe_mode()
    assert mode["on"] is True
    assert mode["level"] == 1

    source = commit_change(w.repo, toolbox_v2_image())
    t2 = pipe.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    assert pipe.advance(t2.id).state == "verified"
    pipe.add_review(t2.id, runtime="script@1", verdict="approve", note="", by="script")
    paused = pipe.advance(t2.id)
    assert paused.state == "verified"
    assert paused.report["reason"] == "safe-mode"

    w.clock.advance(86400 + 3600 + 1)
    pipe.tick()
    assert pipe.safe_mode()["on"] is False
    assert pipe.get(t2.id).state in {"shadow", "canary"}


def test_freeze_blocks_promotion(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipe(w)
    source = commit_change(w.repo, upper_v2_limits())
    doc = changeset_doc(w.base, [upsert("Station", "upper")], rollout=T1_ROLLOUT)
    cs = pipe.propose(doc, source, by="human:a", timeout_s=60)
    assert pipe.advance(cs.id).state == "verified"
    assert pipe.advance(cs.id).state == "shadow"
    write_factory_json(
        w.ctx.root, w.repo, freeze=[{"start": "2026-11-01T00:00:00Z", "until": "2026-11-20T00:00:00Z"}]
    )
    held = pipe.advance(cs.id)
    assert held.state == "shadow"
    assert held.report["reason"] == "freeze"
    assert w.epochs.current() == w.base


def test_blocked_runs_rebuilt_after_activation(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    insert_run(w.ctx, "r-blocked", epoch=w.base, state="FAILED", created_at=_now(w))
    store = ConversionStore(w.ctx)
    store.open_or_merge(
        from_type="note.raw@1",
        guarantees=[],
        to_type="note.text@1",
        requires=[],
        found_at={"run": "r-blocked"},
        run_id="r-blocked",
    )
    rebuilds = Rebuilds()
    cs = _t1_active(w, _pipe(w, rebuild=rebuilds))
    assert cs.state == "active"
    assert rebuilds.calls == [("r-blocked", cs.commit)]
    assert [r.status for r in store.list()] == ["resolved"]
