"""动态展开的纯决策：上限只来自调用方传入的 max_items，不另造数字。"""

from __future__ import annotations

from mao.dynamic import map_fanout


def test_missing_limit_expands_when_budget_is_zero() -> None:
    assert map_fanout(5, None, None, 0.0) == ("expand", 5)


def test_missing_limit_is_invalid_when_budget_is_positive() -> None:
    assert map_fanout(5, None, None, 0.1) == ("invalid", 0)


def test_within_declared_limit_keeps_every_item() -> None:
    assert map_fanout(2, 4, None, 1.0) == ("expand", 2)


def test_over_declared_limit_rejects_unless_truncate() -> None:
    assert map_fanout(6, 4, None, 0.0) == ("reject", 0)
    assert map_fanout(6, 4, "reject", 0.0) == ("reject", 0)


def test_truncate_uses_only_the_declared_limit() -> None:
    assert map_fanout(6, 4, "truncate", 0.0) == ("expand", 4)
