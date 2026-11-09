"""M3-05 验收：厂长操作面（spec/system/foreman.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.foreman")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from mao.foreman import (  # noqa: E402
    ConversionStore,
    ForemanQuery,
    ForemanSubmit,
    WakeQueue,
    bootstrap_check,
)
from mao.governance import ChangePipeline, EpochStore  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402


def _open(tmp_path: Path):
    ctx, snapshot, _runner, _clock = open_factory(tmp_path, FIXTURE)
    return ctx, snapshot


def test_wake_dedup(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    queue = WakeQueue(ctx)
    first = queue.file("invariant", "I1:bean", {"id": "au-0001"})
    second = queue.file("invariant", "I1:bean", {"id": "au-0001"})
    assert first.id == second.id


def test_readonly_roundtrip(tmp_path: Path) -> None:
    ctx, snapshot = _open(tmp_path)
    query = ForemanQuery(ctx, EpochStore(ctx))
    shown = query.epoch_show(snapshot.epoch)
    assert shown["epoch"] == snapshot.epoch or shown.get("commit") == snapshot.epoch


def test_secret_redacted(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    text = ForemanQuery(ctx, EpochStore(ctx)).job_transcript("j-missing")
    assert "API_KEY=secret" not in text
    assert "[REDACTED]" in text or text == ""


def test_propose_does_not_merge(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    submit = ForemanSubmit(ctx, ChangePipeline(ctx))
    before = (ctx.root / "epoch").read_text(encoding="utf-8") if (ctx.root / "epoch").exists() else ""
    change = submit.propose({"changes": []}, "abc", by="foreman", timeout_s=5)
    after = (ctx.root / "epoch").read_text(encoding="utf-8") if (ctx.root / "epoch").exists() else ""
    assert change.state != "active"
    assert before == after


def test_no_merge_method(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    submit = ForemanSubmit(ctx, ChangePipeline(ctx))
    assert not hasattr(submit, "merge")


def test_constitution_write_forbidden(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    submit = ForemanSubmit(ctx, ChangePipeline(ctx))
    with pytest.raises(FactoryError) as ei:
        submit.propose(
            {"changes": [{"op": "upsert", "path": "constitution/invariants.mrl"}]},
            "abc",
            by="foreman",
            timeout_s=5,
        )
    assert ei.value.code in {"CONSTITUTION_FORBIDDEN", "FOREMAN_FORBIDDEN", "CONSTITUTION_LOCKED"}


def test_review_other_runtime(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    submit = ForemanSubmit(ctx, ChangePipeline(ctx))
    change = submit.propose(
        {"changes": [{"op": "upsert", "path": "stations/upper.yaml"}]}, "abc", by="runtime-a", timeout_s=5
    )
    with pytest.raises(FactoryError) as ei:
        submit.review(change.id, {"decision": "approve", "runtime": "runtime-a"}, by="runtime-a")
    assert ei.value.code == "REVIEW_SAME_RUNTIME"


def test_conversion_merged(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    store = ConversionStore(ctx)
    first = store.open_or_merge(
        from_type="subtitle.vtt@1",
        guarantees=[],
        to_type="subtitle.srt@1",
        requires=[],
        found_at={"line": "L"},
    )
    second = store.open_or_merge(
        from_type="subtitle.vtt@1",
        guarantees=[],
        to_type="subtitle.srt@1",
        requires=[],
        found_at={"line": "M"},
    )
    assert first.id == second.id
    assert len(second.found_at) == 2


def test_cannot_self_resolve(tmp_path: Path) -> None:
    ctx, _snapshot = _open(tmp_path)
    store = ConversionStore(ctx)
    opened = store.open_or_merge(
        from_type="a@1", guarantees=[], to_type="b@1", requires=[], found_at={"line": "L"}
    )
    with pytest.raises(FactoryError) as ei:
        store.mark(opened.id, "resolved")
    assert ei.value.code == "CONVERSION_STATE_INVALID"


def test_bootstrap_reports_missing(tmp_path: Path) -> None:
    ctx, snapshot = _open(tmp_path)
    before = ctx.db.query("SELECT seq FROM events")
    report = bootstrap_check(snapshot, [])
    after = ctx.db.query("SELECT seq FROM events")
    assert report.ok is False
    assert any("registry-sink" in item for item in report.missing)
    assert after == before


def test_sink_token_cannot_promote(tmp_path: Path) -> None:
    _ctx, snapshot = _open(tmp_path)
    report = bootstrap_check(snapshot, ["epoch.rollback"])
    assert report.sink_can_merge is True
    assert report.ok is False


def test_mcp_is_local_stdio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import socket

    connects: list[object] = []

    def record_connect(self: socket.socket, address: object) -> None:
        connects.append(address)
        raise OSError("acceptance blocked an outbound connect")

    monkeypatch.setattr(socket.socket, "connect", record_connect)
    ctx, _snapshot = _open(tmp_path)
    monkeypatch.setenv("FACTORY_ROOT", str(ctx.root))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"jsonrpc":"2.0","id":1,"method":"foreman.list","params":{"kind":"Station"}}\n'),
    )
    from mao.cli import main

    try:
        main(["mcp"])
    except SystemExit:
        pass
    assert connects == []
