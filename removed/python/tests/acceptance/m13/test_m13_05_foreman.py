"""M13-05：厂长常驻模式（spec/system/agents.md 5.8）。"""

from __future__ import annotations

from pathlib import Path

from tests.acceptance.m3.gov_world import changeset_doc, commit_change, toolbox_v2_image, upsert
from tests.acceptance.m5.auto_world import REVIEW_ACTION
from tests.acceptance.m13.agent_world import World


def _conversion_wake(ctx):
    from mao.foreman import ConversionStore, WakeQueue

    request = ConversionStore(ctx).open_or_merge(
        from_type="note.raw@1",
        guarantees=[],
        to_type="note.text@1",
        requires=[],
        found_at={"line": "acceptance"},
    )
    return WakeQueue(ctx).file("conversion", request.id, {"id": request.id})


def _verified(w: World, pipeline):
    source = commit_change(w.repo, toolbox_v2_image())
    cs = pipeline.propose(
        changeset_doc(w.base, [upsert("Machine", "toolbox")]), source, by="fake-agent@1", timeout_s=60
    )
    cs = pipeline.advance(cs.id)
    assert cs.state == "verified" and cs.reviews_required == 1
    return cs


def test_foreman_resident_takes_wakes(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    w = World(tmp_path, foreman={"mode": "resident"})
    w.script({"default": []})
    wakes = WakeQueue(w.ctx)
    other = wakes.file("other", "check-feeds", {"id": "check-feeds"})
    host = w.host()
    host.collect()
    [message] = w.messages("foreman")
    assert (message.kind, message.source) == ("wake", "wake:" + other.id)
    assert message.body["wake"]["id"] == other.id
    assert wakes.get(other.id).status == "taken"
    sup = w.supervisor()
    sup.start()
    try:
        assert sup.run_once() is None
    finally:
        sup.shutdown()
    assert w.rows("SELECT id FROM foreman_sessions") == []

    turn = host.run_once("foreman")
    assert turn.state == "ok", turn.error
    assert wakes.get(other.id).status == "done"
    assert (w.workspace("foreman") / "registry" / ".git").exists()

    conversion = _conversion_wake(w.ctx)
    host.collect()
    turn = host.run_once("foreman")
    assert turn.state == "ok", turn.error
    after = wakes.get(conversion.id)
    assert (after.status, after.attempts) == ("open", 1)
    code, body, err = w.cli("agent", "show", "foreman")
    assert code == 0, err
    assert body["name"] == "foreman" and body["session"] == turn.session


def test_reviewer_still_session(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    w = World(tmp_path, foreman={"mode": "resident"})
    w.script({"default": [REVIEW_ACTION]})
    pipeline = w.pipeline()
    cs = _verified(w, pipeline)
    wake = WakeQueue(w.ctx).file("review", cs.id, {"id": cs.id, "need": 1})
    host = w.host(pipeline)
    host.collect()
    assert w.messages("foreman") == []
    assert WakeQueue(w.ctx).get(wake.id).status == "open"
    sup = w.supervisor()
    sup.start()
    try:
        record = sup.run_once()
    finally:
        sup.shutdown()
    assert record.state == "ok", record.error
    assert record.role == "reviewer"
    assert WakeQueue(w.ctx).get(wake.id).status == "done"


def test_session_mode_unchanged(tmp_path: Path) -> None:
    from mao.foreman import WakeQueue

    w = World(tmp_path)
    w.script({"default": []})
    wake = WakeQueue(w.ctx).file("other", "plain", {"id": "plain"})
    host = w.host()
    host.collect()
    assert w.messages("foreman") == []
    assert WakeQueue(w.ctx).get(wake.id).status == "open"
    sup = w.supervisor()
    sup.start()
    try:
        record = sup.run_once()
    finally:
        sup.shutdown()
    assert (record.state, record.role) == ("ok", "foreman")
    assert WakeQueue(w.ctx).get(wake.id).status == "done"
    code, body, _ = w.cli("agent", "show", "foreman")
    assert code == 1 and body["error"]["code"] == "AGENT_NOT_FOUND"
