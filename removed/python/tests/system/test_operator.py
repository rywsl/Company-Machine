"""M1-07 单元：mao.operator。模块缺失时整文件跳过。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.operator")
pytest.importorskip("mao.brief")
pytest.importorskip("mao.snapshot")

from types import SimpleNamespace  # noqa: E402

from mao.brief import render_brief  # noqa: E402
from mao.operator import build_invocation, parse_transcript  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "registry-m1"


def _task():
    return SimpleNamespace(
        id="sha256:" + "11" * 32,
        type="agent.task@1.0",
        facts={"steps": 1},
        meta={},
        payload=[],
    )


def _brief(feedback: str | None):
    return render_brief(
        Snapshot.load(FIXTURE),
        "Station:writer@1",
        inputs={"task": _task()},
        params={},
        attempt=1,
        feedback=feedback,
        budget_usd=1.0,
    )


def test_invocation_has_positive_timeout() -> None:
    snap = Snapshot.load(FIXTURE)
    brief = _brief(None)
    invocation = build_invocation(snap, "Station:writer@1", brief, inputs={}, params={})
    assert invocation.timeout_s > 0
    assert invocation.argv


def test_transcript_cost(tmp_path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_text(
        json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.25, "result": "ok"}) + "\n",
        encoding="utf-8",
    )
    summary = parse_transcript(path, "stream-json")
    assert summary.cost_usd == 0.25
    assert summary.is_error is False


def test_transcript_error_flag(tmp_path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_text(
        json.dumps({"type": "result", "is_error": True, "total_cost_usd": 0.1}) + "\n",
        encoding="utf-8",
    )
    assert parse_transcript(path, "stream-json").is_error is True


def test_unknown_event_does_not_zero_cost(tmp_path) -> None:
    path = tmp_path / "out.jsonl"
    path.write_text(
        json.dumps({"type": "future-event"})
        + "\n"
        + json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.4})
        + "\n",
        encoding="utf-8",
    )
    assert parse_transcript(path, "stream-json").cost_usd == 0.4


def test_feedback_reaches_invocation_brief() -> None:
    snap = Snapshot.load(FIXTURE)
    brief = _brief("再大声一点")
    invocation = build_invocation(snap, "Station:writer@1", brief, inputs={}, params={})
    assert "再大声一点" in brief.markdown
    assert invocation.timeout_s > 0
