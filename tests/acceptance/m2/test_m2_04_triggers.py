"""M2-04 验收：bean 触发器（spec/system/lines.md 第 7.2 节）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mao.runs")

from mao.runs import TriggerEngine  # noqa: E402
from tests.acceptance.m2.world import FIXTURE, open_factory, seal_input  # noqa: E402


def _engine(tmp_path: Path):
    ctx, snapshot, runner, clock = open_factory(tmp_path, FIXTURE)
    from mao.runs import LineRunner

    lines = LineRunner(ctx, snapshot, runner, cache=None)
    return ctx, snapshot, TriggerEngine(ctx, snapshot, lines), clock


def test_bean_trigger_starts_run_once(tmp_path: Path) -> None:
    ctx, snapshot, engine, _clock = _engine(tmp_path)
    bean = seal_input(ctx, snapshot, "brief.video@1")
    ctx.warehouse.label(bean, "line", "explainer-multilang", by="acceptance")
    first = engine.process()
    second = engine.process()
    assert len(first) == 1
    assert second == []


def test_label_filter(tmp_path: Path) -> None:
    ctx, snapshot, engine, _clock = _engine(tmp_path)
    bean = seal_input(ctx, snapshot, "brief.video@1")
    ctx.warehouse.label(bean, "line", "other", by="acceptance")
    assert engine.process() == []
    ctx.warehouse.label(bean, "line", "explainer-multilang", by="factoryctl")
    assert engine.process()


def test_trigger_invalid_interface(tmp_path: Path) -> None:
    ctx, snapshot, engine, _clock = _engine(tmp_path)
    line_name = None
    for path in (FIXTURE / "lines").glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        if text.count("type:") >= 2 and "inputs:" in text:
            line_name = path.stem
    assert line_name
    bean = seal_input(ctx, snapshot, "note.text@1")
    ctx.warehouse.label(bean, "line", line_name, by="acceptance")
    assert engine.process() == []
    rows = ctx.db.query("SELECT kind, data FROM events")
    blob = "\n".join(str(row["kind"]) + str(row["data"]) for row in rows)
    assert "TRIGGER_INVALID" in blob


def test_cursor_survives_restart(tmp_path: Path) -> None:
    from mao.runner import Runner
    from mao.runs import LineRunner, TriggerEngine

    ctx, snapshot, engine, _clock = _engine(tmp_path)
    bean = seal_input(ctx, snapshot, "brief.video@1")
    ctx.warehouse.label(bean, "line", "explainer-multilang", by="acceptance")
    first = engine.process()
    assert first
    again = TriggerEngine(ctx, snapshot, LineRunner(ctx, snapshot, Runner(ctx, snapshot)))
    assert again.process() == []
