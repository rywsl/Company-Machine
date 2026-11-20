"""M3-02 验收：ChangeSet 受理、验证、风险与复核（spec/system/governance.md 7.2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.governance")

from mao.errors import FactoryError  # noqa: E402
from mao.governance import ChangePipeline, score_risk  # noqa: E402
from tests.acceptance.m3.gov_world import (  # noqa: E402
    changeset_doc,
    commit_change,
    constitution_edit,
    git,
    note_text_v1_1_incompatible,
    open_world,
    publish_local_v3,
    toolbox_v2_image,
    upper_v1_in_place,
    upper_v2_limits,
    upsert,
    write_factory_json,
)


class FakeResult:
    def __init__(self, n: int, verdict: str) -> None:
        self.id = f"ct-{n:04d}"
        self.kind = "fake"
        self.subject = "fake"
        self.verdict = verdict
        self.detail: dict = {}
        self.at = "2026-11-10T00:00:00.000Z"


class FakeContracts:
    """ContractRunner 替身：记录调用，按构造参数返回统一的 verdict。"""

    def __init__(self, verdict: str = "pass") -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, tuple]] = []

    def _result(self, name: str, args: tuple) -> FakeResult:
        self.calls.append((name, args))
        return FakeResult(len(self.calls), self.verdict)

    def run_validator(self, *args, **kwargs):
        return self._result("run_validator", args)

    def run_golden(self, *args, **kwargs):
        return self._result("run_golden", args)

    def run_equivalence(self, *args, **kwargs):
        return self._result("run_equivalence", args)

    def run_diff(self, *args, **kwargs):
        return self._result("run_diff", args)

    def run_sink_dry_run(self, *args, **kwargs):
        return self._result("run_sink_dry_run", args)

    def check_fixture_set(self, *args, **kwargs):
        return self._result("check_fixture_set", args)


def _pipeline(w, contracts: FakeContracts | None = None) -> ChangePipeline:
    fake = contracts or FakeContracts()
    return ChangePipeline(w.ctx, w.epochs, contracts=lambda snapshot: fake)


def _verified(w, files, changes, *, by="human:acceptance", contracts=None, **spec_extra):
    pipe = _pipeline(w, contracts)
    source = commit_change(w.repo, files)
    cs = pipe.propose(changeset_doc(w.base, changes, **spec_extra), source, by=by, timeout_s=60)
    return pipe, pipe.advance(cs.id)


def test_constitution_change_forbidden(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipeline(w)
    source = commit_change(w.repo, constitution_edit())
    with pytest.raises(FactoryError) as ei:
        pipe.propose(
            changeset_doc(w.base, [upsert("Station", "upper")]), source, by="fake-agent@1", timeout_s=60
        )
    assert ei.value.code == "CONSTITUTION_FORBIDDEN"
    assert ei.value.detail["path"] == "constitution/invariants.mrl"
    assert w.ctx.db.query("SELECT id FROM changesets") == []


def test_stale_base_rejected(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipeline(w)
    other = commit_change(w.repo, {"README.md": "other\n"})
    source = commit_change(w.repo, upper_v2_limits(), parent=other)
    with pytest.raises(FactoryError) as ei:
        pipe.propose(
            changeset_doc(other, [upsert("Station", "upper")]), source, by="human:acceptance", timeout_s=60
        )
    assert ei.value.code == "CHANGESET_STALE"
    assert w.ctx.db.query("SELECT id FROM changesets") == []


def test_propose_creates_control_plane_commit(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipeline(w)
    source = commit_change(w.repo, upper_v2_limits())
    cs = pipe.propose(
        changeset_doc(w.base[:12], [upsert("Station", "upper")]), source, by="human:a", timeout_s=60
    )
    assert cs.id == "cs-0001"
    assert cs.state == "draft"
    assert cs.base_epoch == w.base
    assert cs.source_commit == source
    assert cs.commit != source
    assert git(w.repo, "rev-parse", f"{cs.commit}^{{tree}}") == git(w.repo, "rev-parse", f"{source}^{{tree}}")
    assert git(w.repo, "rev-parse", f"{cs.commit}^") == w.base
    assert git(w.repo, "log", "-1", "--format=%B", cs.commit).startswith("changeset: cs-0001")
    assert git(w.repo, "rev-parse", "refs/changesets/cs-0001") == cs.commit
    assert git(w.repo, "rev-parse", "refs/heads/main") == w.base
    assert cs.risk_tier is None


def test_limits_change_is_t1(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    contracts = FakeContracts()
    _pipe, cs = _verified(w, upper_v2_limits(), [upsert("Station", "upper")], contracts=contracts)
    assert cs.state == "verified", cs.report
    assert cs.risk_tier == "T1"
    assert cs.reviews_required == 0
    assert ("run_diff", ("Station:upper@2",)) in [(n, a[:1]) for n, a in contracts.calls]
    assert cs.report["code"] is None
    assert "Station:upper@2" in cs.report["impact"]["stations"]


def test_risk_ignores_proposer_claim(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    _pipe, cs = _verified(w, toolbox_v2_image(), [upsert("Machine", "toolbox")], risk_tier="T0")
    assert cs.state == "verified", cs.report
    assert cs.risk_tier == "T2"
    assert cs.reviews_required == 1
    assert "risk_tier" not in cs.document["spec"]


def test_sink_change_is_t3(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    _pipe, cs = _verified(w, publish_local_v3(), [upsert("Station", "publish-local")])
    assert cs.state == "verified", cs.report
    assert cs.risk_tier == "T3"
    assert cs.reviews_required == 2


def test_unscored_is_not_low() -> None:
    with pytest.raises(FactoryError) as ei:
        score_risk({"change": [["c0", "Station", "modify"]], "field_changed": [["c0", "bogus"]]})
    assert ei.value.code == "RISK_UNSCORED"
    assert ei.value.detail["changes"]
    with pytest.raises(FactoryError) as ei:
        score_risk({"change": [["c0", "Plan", "add"]]})
    assert ei.value.code == "RISK_UNSCORED"
    assert (
        score_risk({"change": [["c0", "Machine", "modify"]], "field_changed": [["c0", "runtime.image"]]}).tier
        == "T2"
    )


def test_declared_changes_must_match(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    files = {**upper_v2_limits(), **toolbox_v2_image()}
    _pipe, cs = _verified(w, files, [upsert("Station", "upper")])
    assert cs.state == "rejected"
    assert cs.report["code"] == "CHANGESET_MISMATCH"
    assert ["Machine", "toolbox", "upsert"] in [list(x) for x in cs.report["detail"]["actual_only"]]


def test_in_place_edit_is_immutable(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    _pipe, cs = _verified(w, upper_v1_in_place(), [upsert("Station", "upper")])
    assert cs.state == "rejected"
    assert cs.report["code"] == "V3_IMMUTABLE"
    assert cs.report["detail"]["key"] == "Station:upper@1"


def test_minor_must_be_compatible(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    _pipe, cs = _verified(w, note_text_v1_1_incompatible(), [upsert("Type", "note.text")])
    assert cs.state == "rejected"
    assert cs.report["code"] == "V3_INCOMPATIBLE"


def test_v4_failure_rejects(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    _pipe, cs = _verified(w, upper_v2_limits(), [upsert("Station", "upper")], contracts=FakeContracts("fail"))
    assert cs.state == "rejected"
    assert cs.report["code"] == "V4_FAILED"
    assert cs.report["detail"]["results"]


def test_freeze_and_rate_limit(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    source = commit_change(w.repo, upper_v2_limits())
    doc = changeset_doc(w.base, [upsert("Station", "upper")])
    write_factory_json(
        w.ctx.root, w.repo, freeze=[{"start": "2026-11-01T00:00:00Z", "until": "2026-11-20T00:00:00Z"}]
    )
    with pytest.raises(FactoryError) as ei:
        _pipeline(w).propose(doc, source, by="human:a", timeout_s=60)
    assert ei.value.code == "FREEZE_WINDOW"
    assert ei.value.detail["until"] == "2026-11-20T00:00:00Z"

    write_factory_json(w.ctx.root, w.repo)
    now = "2026-11-09T12:00:00.000Z"
    for i in range(10):
        w.ctx.db.query(
            'INSERT INTO changesets (id, base_epoch, source_commit, "commit", state, proposer, document, '
            "activated_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', 'x', '{}', ?, ?, ?)",
            (f"cs-old{i}", w.base, w.base, f"{i:040d}", now, now, now),
        )
    with pytest.raises(FactoryError) as ei:
        _pipeline(w).propose(doc, source, by="human:a", timeout_s=60)
    assert ei.value.code == "RATE_LIMITED"
    assert ei.value.detail["count"] == 10


def test_review_rules(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipeline(w)
    source = commit_change(w.repo, toolbox_v2_image())
    draft = pipe.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    with pytest.raises(FactoryError) as ei:
        pipe.add_review(draft.id, runtime="script@1", verdict="approve", note="", by="script")
    assert ei.value.code == "CHANGESET_STATE_INVALID"

    cs = pipe.advance(draft.id)
    assert cs.state == "verified", cs.report
    assert cs.proposer_runtime == "OperatorRuntime:fake-agent@1"
    with pytest.raises(FactoryError) as ei:
        pipe.add_review(cs.id, runtime="fake-agent@1", verdict="approve", note="", by="fake")
    assert ei.value.code == "REVIEW_SAME_RUNTIME"
    with pytest.raises(FactoryError) as ei:
        pipe.add_review(cs.id, runtime="nope@1", verdict="approve", note="", by="x")
    assert ei.value.code == "REVIEW_INVALID"

    first = pipe.add_review(cs.id, runtime="script@1", verdict="deny", note="请按 T1 发布", by="script")
    assert pipe.approvals(cs.id) == []
    again = pipe.add_review(cs.id, runtime="script@1", verdict="approve", note="", by="script")
    assert again == first
    assert pipe.approvals(cs.id) == ["OperatorRuntime:script@1"]
    assert pipe.get(cs.id).risk_tier == "T2"


def test_withdraw(tmp_path: Path) -> None:
    w = open_world(tmp_path)
    pipe = _pipeline(w)
    source = commit_change(w.repo, upper_v2_limits())
    cs = pipe.propose(changeset_doc(w.base, [upsert("Station", "upper")]), source, by="human:a", timeout_s=60)
    gone = pipe.withdraw(cs.id, by="human:a")
    assert gone.state == "withdrawn"
    with pytest.raises(FactoryError) as ei:
        pipe.advance(cs.id)
    assert ei.value.code == "CHANGESET_STATE_INVALID"
    with pytest.raises(FactoryError) as ei:
        pipe.get("cs-9999")
    assert ei.value.code == "CHANGESET_NOT_FOUND"
