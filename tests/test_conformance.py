"""一致性用例：spec/mrl/conformance/ 下的每个用例都是一个 pytest 用例。"""

from __future__ import annotations

import pytest

from mrl.conformance import load_cases, run_case
from mrl.factory.paths import spec_root

CASES = list(load_cases([str(spec_root() / "mrl" / "conformance")]))


def test_suites_found() -> None:
    kinds = {c.kind for c in CASES}
    assert {"expr", "template", "rules", "factory"} <= kinds
    assert "invalid" not in kinds, [c.data for c in CASES if c.kind == "invalid"]


@pytest.mark.parametrize("case", CASES, ids=[f"{c.suite}/{c.id}" for c in CASES])
def test_case(case) -> None:
    result = run_case(case)
    assert result.ok, result.detail
