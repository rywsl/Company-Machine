"""Frozen design inputs: Agent risk is computed from trusted diff facts."""

from __future__ import annotations

import pytest

from mao.errors import FactoryError
from mao.governance.risk import score_risk


@pytest.mark.parametrize("op,tier", [("add", "T2"), ("deprecate", "T2"), ("retire", "T3")])
def test_agent_lifecycle_has_explicit_risk(op: str, tier: str) -> None:
    assert score_risk({"change": [["c0", "Agent", op]]}).tier == tier


def test_agent_charter_change_is_t2() -> None:
    assert (
        score_risk({"change": [["c0", "Agent", "modify"]], "field_changed": [["c0", "charter"]]}).tier == "T2"
    )


def test_proven_agent_scope_reduction_is_t1() -> None:
    assert (
        score_risk({"change": [["c0", "Agent", "modify"]], "field_changed": [["c0", "scope.reduced"]]}).tier
        == "T1"
    )


def test_agent_scope_expansion_is_t3_even_with_other_changes() -> None:
    assert (
        score_risk(
            {
                "change": [["c0", "Agent", "modify"]],
                "field_changed": [["c0", "scope.expanded"], ["c0", "charter"]],
            }
        ).tier
        == "T3"
    )


def test_unknown_agent_field_fails_closed() -> None:
    with pytest.raises(FactoryError) as exc:
        score_risk({"change": [["c0", "Agent", "modify"]], "field_changed": [["c0", "unrecognized"]]})
    assert exc.value.code == "RISK_UNSCORED"
