"""M17 governance diagnostics: expected contract on the frozen c538768 baseline.

These are diagnostic regressions, not an existing milestone gate. They assert the
specified behaviour and deliberately fail when the baseline violates that contract.
All Git repositories, databases and fault injections live under pytest's temp dir.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mao.foreman import ConversionStore, WakeQueue
from tests.acceptance.m3.gov_world import (
    changeset_doc,
    commit_change,
    git,
    insert_run,
    open_world,
    toolbox_v2_image,
    upper_v2_limits,
    upsert,
)
from tests.acceptance.m3.test_m3_03_rollout import (
    T1_ROLLOUT,
    _now,
    _pipe,
    _t2_canary,
)


def _t2_shadow(w, pipe):
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipe.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]),
        source,
        by="fake-agent@1",
        timeout_s=60,
    )
    assert pipe.advance(cs.id).state == "verified"
    pipe.add_review(cs.id, runtime="script@1", verdict="approve", note="", by="script")
    shadow = pipe.advance(cs.id)
    assert (shadow.state, shadow.risk_tier) == ("shadow", "T2")
    return shadow


def _wake_audit(w):
    at = _now(w)
    with w.ctx.db.tx() as conn:
        conn.execute(
            "INSERT INTO audit_findings (id, invariant, subject, state, detail, at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("finding-m17", "I9", "machine", "wake", "{}", at, at),
        )


def test_safe_mode_stops_existing_t2_shadow(tmp_path: Path) -> None:
    """governance.md 5.13: an existing T2 shadow cannot enter canary while safe mode is on."""
    w = open_world(tmp_path)
    pipe = _pipe(w)
    cs = _t2_shadow(w, pipe)
    _wake_audit(w)
    pipe.tick()
    assert pipe.safe_mode()["on"] is True
    assert pipe.get(cs.id).state == "shadow"
    assert w.epochs.current() == w.base


def test_safe_mode_stops_existing_t2_canary_promotion(tmp_path: Path) -> None:
    """governance.md 5.13: an existing T2 canary cannot become active while safe mode is on."""
    w = open_world(tmp_path)
    pipe = _pipe(w)
    cs = _t2_canary(w, pipe)
    assert (cs.state, cs.risk_tier) == ("canary", "T2")
    insert_run(w.ctx, "r-m17-1", epoch=cs.commit, state="SUCCEEDED", created_at=_now(w))
    insert_run(w.ctx, "r-m17-2", epoch=cs.commit, state="SUCCEEDED", created_at=_now(w))
    _wake_audit(w)
    pipe.tick()
    assert pipe.safe_mode()["on"] is True
    assert pipe.get(cs.id).state == "canary"
    assert w.epochs.current() == w.base


def test_failed_promotion_keeps_database_and_git_pointers_atomic(tmp_path: Path) -> None:
    """governance.md 5.10: an aborted changeset transition must not activate the candidate."""
    w = open_world(tmp_path)
    pipe = _pipe(w)
    source = commit_change(w.repo, upper_v2_limits())
    cs = pipe.propose(
        changeset_doc(w.base, [upsert("Station", "upper")], rollout=T1_ROLLOUT),
        source,
        by="human:a",
        timeout_s=60,
    )
    assert pipe.advance(cs.id).state == "verified"
    assert pipe.advance(cs.id).state == "shadow"
    before = {
        "pointers": w.epochs.pointers(),
        "refs": git(w.repo, "for-each-ref", "--format=%(refname)=%(objectname)", "refs/factory/"),
    }
    with w.ctx.db.tx() as conn:
        conn.execute(
            "CREATE TRIGGER m17_abort_activation BEFORE UPDATE OF state ON changesets "
            "WHEN NEW.state = 'active' BEGIN SELECT RAISE(ABORT, 'M17 activation failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="M17 activation failure"):
        pipe.advance(cs.id)
    assert pipe.get(cs.id).state == "shadow"
    assert {
        "pointers": w.epochs.pointers(),
        "refs": git(w.repo, "for-each-ref", "--format=%(refname)=%(objectname)", "refs/factory/"),
    } == before
    assert w.epochs.current() == w.base


def test_rebuild_failure_preserves_promotion_and_wakes_foreman(tmp_path: Path) -> None:
    """governance.md 5.12: failed rebuilding wakes the foreman without failing promotion."""
    w = open_world(tmp_path)
    insert_run(w.ctx, "r-m17-blocked", epoch=w.base, state="FAILED", created_at=_now(w))
    request = ConversionStore(w.ctx).open_or_merge(
        from_type="note.raw@1",
        guarantees=[],
        to_type="note.text@1",
        requires=[],
        found_at={"run": "r-m17-blocked"},
        run_id="r-m17-blocked",
    )

    def fail_rebuild(run_id, snapshot):
        raise RuntimeError("M17 rebuild failed")

    pipe = _pipe(w, rebuild=fail_rebuild)
    source = commit_change(w.repo, upper_v2_limits())
    cs = pipe.propose(
        changeset_doc(w.base, [upsert("Station", "upper")], rollout=T1_ROLLOUT),
        source,
        by="human:a",
        timeout_s=60,
    )
    assert pipe.advance(cs.id).state == "verified"
    assert pipe.advance(cs.id).state == "shadow"
    try:
        result = pipe.advance(cs.id)
    except RuntimeError as exc:
        pytest.fail(
            f"advance raised {exc}; state={pipe.get(cs.id).state}; "
            f"conversion={[item.status for item in ConversionStore(w.ctx).list()]}; "
            f"wakes={len(WakeQueue(w.ctx).list(status='open'))}"
        )
    assert result.state == "active"
    assert w.epochs.current() == cs.commit
    assert [item.status for item in ConversionStore(w.ctx).list() if item.id == request.id] == ["resolved"]
    assert WakeQueue(w.ctx).list(status="open")


def test_existing_shadow_blocks_second_changeset(tmp_path: Path) -> None:
    """governance.md 5.9: the single rollout slot includes an ongoing shadow."""
    w = open_world(tmp_path)
    pipe = _pipe(w)
    first_source = commit_change(w.repo, upper_v2_limits())
    second_source = commit_change(w.repo, toolbox_v2_image())
    first = pipe.propose(
        changeset_doc(w.base, [upsert("Station", "upper")], rollout=T1_ROLLOUT),
        first_source,
        by="human:a",
        timeout_s=60,
    )
    second = pipe.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]),
        second_source,
        by="fake-agent@1",
        timeout_s=60,
    )
    assert pipe.advance(first.id).state == "verified"
    assert pipe.advance(second.id).state == "verified"
    pipe.add_review(second.id, runtime="script@1", verdict="approve", note="", by="script")
    assert pipe.advance(first.id).state == "shadow"
    assert w.epochs.pointers()["candidate"] == first.commit
    waiting = pipe.advance(second.id)
    assert waiting.state == "verified"
    assert waiting.report["reason"] == "queued"
    assert w.epochs.pointers()["candidate"] == first.commit


def test_undeclared_new_agent_cannot_hide_in_changeset(tmp_path: Path) -> None:
    """governance.md 5.7: a ChangeSet cannot activate an undeclared structural Agent."""
    from tests.acceptance.m13.agent_world import HELPER, World

    w = World(tmp_path)
    pipe = _pipe(w)
    agent = HELPER.replace("name: helper", "name: extra-worker").replace(
        "  budget:",
        "  lines: [note-trigger@1]\n  subscribe:\n    schedule:\n      - {every: 1h, note: periodic work}\n  budget:",
    )
    files = {**upper_v2_limits(), "agents/extra-worker.yaml": agent}
    source = commit_change(w.repo, files, parent=w.base)
    cs = pipe.propose(
        changeset_doc(w.base, [upsert("Station", "upper")], rollout=T1_ROLLOUT),
        source,
        by="human:a",
        timeout_s=60,
    )
    candidate = w.epochs.materialize(cs.commit)
    assert "Agent:extra-worker@1" in candidate.keys("Agent")
    checked = pipe.advance(cs.id)
    assert checked.state == "rejected", {
        "state": checked.state,
        "risk_tier": checked.risk_tier,
        "undeclared_agent": "Agent:extra-worker@1",
        "declared_changes": checked.document["spec"]["changes"],
    }


def test_verifying_resumes_after_transient_epoch_failure(tmp_path: Path, monkeypatch) -> None:
    """governance.md 5.7: an interrupted verifying ChangeSet restarts verification."""
    w = open_world(tmp_path)
    pipe = _pipe(w)
    source = commit_change(w.repo, upper_v2_limits())
    cs = pipe.propose(
        changeset_doc(w.base, [upsert("Station", "upper")], rollout=T1_ROLLOUT),
        source,
        by="human:a",
        timeout_s=60,
    )
    original = w.epochs.materialize
    failed = False

    def fail_first_candidate(commit):
        nonlocal failed
        if commit == cs.commit and not failed:
            failed = True
            raise OSError("M17 transient epoch read failure")
        return original(commit)

    monkeypatch.setattr(w.epochs, "materialize", fail_first_candidate)
    with pytest.raises(OSError, match="M17 transient epoch read failure"):
        pipe.advance(cs.id)
    assert pipe.get(cs.id).state == "verifying"
    recovered = pipe.advance(cs.id)
    assert recovered.state == "verified"
