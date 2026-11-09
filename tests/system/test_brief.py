"""M1-07 单元：mao.brief。模块缺失时整文件跳过。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mao.brief")
pytest.importorskip("mao.base")
pytest.importorskip("mao.snapshot")

from mao.base import canonical_json  # noqa: E402
from mao.brief import render_brief  # noqa: E402
from mao.snapshot import Snapshot  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "registry-m1"
STATION = "Station:writer@1"


def _render(feedback: str | None, attempt: int = 1):
    return render_brief(
        Snapshot.load(FIXTURE),
        STATION,
        inputs={},
        params={},
        attempt=attempt,
        feedback=feedback,
        budget_usd=1.0,
    )


def test_feedback_is_in_markdown() -> None:
    assert "再大声一点" in _render("再大声一点").markdown


def test_other_feedback_is_not_copied() -> None:
    brief = _render("beta-token")
    assert "beta-token" in brief.markdown
    assert "alpha-token" not in brief.markdown


def test_contract_is_canonical_json() -> None:
    brief = _render(None)
    parsed = json.loads(brief.factory_contract)
    assert brief.factory_contract == canonical_json(parsed)


def test_attempt_changes_contract() -> None:
    first = json.loads(_render(None, attempt=1).factory_contract)
    second = json.loads(_render(None, attempt=2).factory_contract)
    assert first != second


def test_missing_feedback_omits_that_sentence() -> None:
    assert "再大声一点" not in _render(None).markdown
