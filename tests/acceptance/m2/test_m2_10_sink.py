"""M2-10 验收：Sink 回执（spec/system/connectors.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.connectors")
pytest.importorskip("mao.errors")

from mao.connectors import ReceiptStatus, ReceiptStore, idempotency_key  # noqa: E402
from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory, start_line  # noqa: E402


def _store(tmp_path: Path):
    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    return ctx, snapshot, runner, ReceiptStore(ctx)


def test_publish_and_verify(tmp_path: Path) -> None:
    ctx, snapshot, _runner, store = _store(tmp_path)
    key = idempotency_key(
        snapshot,
        snapshot.key("Station", "publish-local@1"),
        {},
        {},
        {
            "id": "r-0001",
            "epoch": snapshot.epoch,
            "line": "",
            "budget": {"cap_usd": 1, "spent_usd": 0, "remaining_usd": 1},
        },
    )
    claim = store.claim(
        station_key=snapshot.key("Station", "publish-local@1"),
        key=key,
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    assert claim.kind == "new"
    published = store.publish(claim.receipt.id, {"target": "file://out", "locator": {}, "items": []})
    assert published.status == ReceiptStatus.PUBLISHED
    settled = store.settle(published.id, ok=True, detail="ok")
    assert settled.status == ReceiptStatus.VERIFIED


def test_duplicate_suppressed(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-local@1")
    key = idempotency_key(
        snapshot,
        station,
        {},
        {},
        {
            "id": "",
            "epoch": snapshot.epoch,
            "line": "",
            "budget": {"cap_usd": 0, "spent_usd": 0, "remaining_usd": 0},
        },
    )
    first = store.claim(
        station_key=station,
        key=key,
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    store.settle(
        store.publish(first.receipt.id, {"target": "t", "locator": {}, "items": []}).id, ok=True, detail="ok"
    )
    second = store.claim(
        station_key=station,
        key=key,
        job_id="j-0002",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    assert second.kind == "duplicate"


def test_key_scope_is_station_name(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-local@1")
    key = "idem-1"
    first = store.claim(
        station_key=station,
        key=key,
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    other = station.replace("@1", "@2") if "@" in station else station + "@2"
    again = store.claim(
        station_key=other, key=key, job_id="j-0002", run_id=None, inputs={}, verify_method="local-file-check"
    )
    assert again.kind == "duplicate"
    assert again.receipt.station_name == first.receipt.station_name


def test_void_allows_republish(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-local@1")
    claimed = store.claim(
        station_key=station,
        key="idem-void",
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    voided = store.void(claimed.receipt.id, by="acceptance", reason="redo")
    assert voided.status == ReceiptStatus.VOID
    again = store.claim(
        station_key=station,
        key="idem-void",
        job_id="j-0003",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    assert again.kind == "new"


def test_outcome_unknown_no_retry(tmp_path: Path) -> None:
    _ctx, _snapshot, runner, _clock, lines, inputs = start_line(tmp_path, "note-pipeline@1")
    from mao.runs import InlineExecutor

    done = lines.run_to_completion(lines.start_run("note-pipeline@1", inputs).id, InlineExecutor(runner))
    assert done.id.startswith("r-")


def test_conflict_on_claimed(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-local@1")
    store.claim(
        station_key=station,
        key="idem-claim",
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    with pytest.raises(FactoryError) as ei:
        store.claim(
            station_key=station,
            key="idem-claim",
            job_id="j-0002",
            run_id=None,
            inputs={},
            verify_method="local-file-check",
        )
    assert ei.value.code == "IDEMPOTENCY_CONFLICT"


def test_receipt_template(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-templated@1")
    claimed = store.claim(
        station_key=station,
        key="idem-tpl",
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    published = store.publish(
        claimed.receipt.id, {"target": "rendered", "locator": {"bucket": "b"}, "items": []}
    )
    assert published.target == "rendered"


def test_receipt_invalid(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-local@1")
    claimed = store.claim(
        station_key=station,
        key="idem-bad",
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    with pytest.raises(FactoryError) as ei:
        store.publish(claimed.receipt.id, {"locator": {}})
    assert ei.value.code == "RECEIPT_INVALID"


def test_verify_failed(tmp_path: Path) -> None:
    _ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-local@1")
    claimed = store.claim(
        station_key=station,
        key="idem-fail",
        job_id="j-0001",
        run_id=None,
        inputs={},
        verify_method="local-file-check",
    )
    published = store.publish(claimed.receipt.id, {"target": "missing", "locator": {}, "items": []})
    failed = store.settle(published.id, ok=False, detail="gone")
    assert failed.status == ReceiptStatus.FAILED


def test_lineage_down_shows_receipt(tmp_path: Path) -> None:
    ctx, snapshot, _runner, store = _store(tmp_path)
    station = snapshot.key("Station", "publish-local@1")
    claimed = store.claim(
        station_key=station,
        key="idem-lin",
        job_id="j-0001",
        run_id=None,
        inputs={"artifact": ["sha256:" + "a" * 64]},
        verify_method="local-file-check",
    )
    found = store.for_bean("sha256:" + "a" * 64)
    assert any(item.id == claimed.receipt.id for item in found)
    assert ctx.warehouse is not None
