"""M2-11 验收：Source（spec/system/connectors.md 第 7 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.connectors")
pytest.importorskip("mao.runner")
pytest.importorskip("mao.errors")

from mao.errors import FactoryError  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory  # noqa: E402


def _runner(tmp_path: Path):
    ctx, snapshot, runner, _clock = open_factory(tmp_path, FIXTURE)
    return ctx, snapshot, runner


def test_source_ingests_and_seals(tmp_path: Path) -> None:
    ctx, snapshot, runner = _runner(tmp_path)
    folder = tmp_path / "incoming-notes"
    folder.mkdir()
    (folder / "a.txt").write_text("hello source\n", encoding="utf-8", newline="\n")
    job = runner.submit(snapshot.key("Station", "pull-notes@1"), {}, {"from_dir": str(folder)})
    done = runner.run(job.id)
    assert done.state.value == "SEALED"
    assert done.outputs
    kinds = [row["kind"] for row in ctx.db.query("SELECT kind FROM events WHERE job_id = ?", (job.id,))]
    assert "source.ingested" in kinds


def test_source_rejects_inputs(tmp_path: Path) -> None:
    _ctx, snapshot, runner = _runner(tmp_path)
    with pytest.raises(FactoryError) as ei:
        runner.submit(snapshot.key("Station", "pull-notes@1"), {"note": "sha256:" + "a" * 64}, {})
    assert ei.value.code == "INTAKE_REJECTED"


def test_untrusted_input_gated(tmp_path: Path) -> None:
    ctx, snapshot, runner = _runner(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "bad.txt").write_bytes(b"")
    job = runner.submit(snapshot.key("Station", "pull-notes@1"), {}, {"from_dir": str(empty)})
    done = runner.run(job.id)
    assert done.state.value in {"REJECTED", "DEAD"}
    assert done.outputs == {}
    assert ctx.warehouse.list(sealed_only=True) == []


def test_same_content_same_bean(tmp_path: Path) -> None:
    _ctx, snapshot, runner = _runner(tmp_path)
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("same", encoding="utf-8")
    first = runner.run(
        runner.submit(snapshot.key("Station", "pull-notes@1"), {}, {"from_dir": str(folder)}).id
    )
    second = runner.run(
        runner.submit(snapshot.key("Station", "pull-notes@1"), {}, {"from_dir": str(folder)}).id
    )
    assert first.outputs == second.outputs


def test_service_form_unsupported(tmp_path: Path) -> None:
    _ctx, snapshot, runner = _runner(tmp_path)
    with pytest.raises(FactoryError) as ei:
        runner.run(runner.submit(snapshot.key("Station", "service-source@1"), {}, {}).id)
    assert ei.value.code == "FORM_UNSUPPORTED"


def test_policy_violation_defense(tmp_path: Path) -> None:
    _ctx, snapshot, runner = _runner(tmp_path)
    with pytest.raises(FactoryError) as ei:
        runner.submit(
            snapshot.key("Station", "pull-notes@1"), {}, {"from_dir": str(tmp_path)}, run={"id": "x"}
        )
    assert ei.value.code == "POLICY_VIOLATION"
