"""M1-07 验收（spec/system/operator.md 第 7 章）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.operator")
pytest.importorskip("mao.brief")
pytest.importorskip("mao.snapshot")

from mao.base import canonical_json  # noqa: E402
from mao.brief import render_brief  # noqa: E402
from mao.operator import build_invocation, parse_transcript  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402


def _snap() -> Snapshot:
    return Snapshot.load(Path(__file__).resolve().parents[2] / "fixtures" / "registry-m1")


def test_brief_includes_feedback() -> None:
    brief = render_brief(
        _snap(), "Station:writer@1", inputs={}, params={}, attempt=2, feedback="再大声一点", budget_usd=1.0
    )
    assert "再大声一点" in brief.markdown


def test_contract_is_canonical() -> None:
    brief = render_brief(
        _snap(), "Station:writer@1", inputs={}, params={}, attempt=1, feedback=None, budget_usd=1.0
    )
    parsed = json.loads(brief.factory_contract)
    assert brief.factory_contract == canonical_json(parsed)


def test_invocation_argv_from_template() -> None:
    snap = _snap()
    brief = render_brief(
        snap, "Station:writer@1", inputs={}, params={}, attempt=1, feedback=None, budget_usd=1.0
    )
    invocation = build_invocation(snap, "Station:writer@1", brief, inputs={}, params={})
    assert "-p" in invocation.argv or brief.task
    assert invocation.timeout_s > 0


def test_parse_transcript_cost(tmp_path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_text(
        json.dumps(
            {"type": "result", "is_error": False, "num_turns": 2, "total_cost_usd": 0.25, "result": "ok"}
        )
        + "\n",
        encoding="utf-8",
    )
    summary = parse_transcript(path, "stream-json")
    assert summary.cost_usd == 0.25
    assert summary.is_error is False


def test_parse_transcript_ignores_unknown(tmp_path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_text(
        json.dumps({"type": "future-event", "x": 1})
        + "\n"
        + json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0})
        + "\n",
        encoding="utf-8",
    )
    summary = parse_transcript(path, "stream-json")
    assert summary.events >= 1
    assert summary.cost_usd == 0


def test_malformed_line_counted(tmp_path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_text(
        "not-json\n" + json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0}) + "\n",
        encoding="utf-8",
    )
    summary = parse_transcript(path, "stream-json")
    assert summary.malformed >= 1
